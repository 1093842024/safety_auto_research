"""Auto-label rows with an LLM (Venus proxy by default).

Pipeline:
  1. ``label_rows`` formats each row through the user's prompt template (the template
     is a Jinja-lite Python ``str.format`` with ``{row}`` and ``{columns}`` placeholders
     so a researcher can drop in a "given a Titanic passenger, output Survived" prompt
     without learning Jinja).
  2. Calls :class:`LLMClient` (OpenAI-compatible, single chat per row by default; can
     batch up to ``batch_size`` rows per call for cost).
  3. Parses each response — strict JSON ``{"label": "..."}`` first, then a permissive
     fallback that takes the first non-empty line / token (so a slightly-chatty
     reasoning model still feeds the retrain step).

Used by:
  * :class:`execution_plane.capabilities.auto_label_executor.AutoLabelExecutor` — the
    ``auto_label`` capability (one CSV in, one labeled CSV out).
  * the ``/agent/llm/test-label`` endpoint — single-row debug surface for the frontend
    prompt-lab panel.

The function is intentionally pure: takes rows + prompt + config, returns labels. It
does **not** do I/O beyond the LLM call so it stays unit-test-friendly (mock the client
or the ``chat`` method).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .client import ChatMessage
from .client import LLMClient
from .client import LLMClientError
from .providers import ProviderConfig


# Default prompts. Researchers override them via the request body / frontend. Kept here
# so a fresh ``auto_label`` call has a working baseline: a strict one-line label.
DEFAULT_SYSTEM_PROMPT = (
    "你是一个精确的数据标注员。根据给定的样本特征输出真实标签。\n"
    "严格要求：仅输出 JSON 对象 {\"label\": <value>}，不要解释、不要 Markdown 代码块、"
    "不要多余文字。"
)

# Used when the user does not pass a custom row template. The default ``{row}`` is the
# JSON-encoded dict, ``{columns}`` is the comma-separated column list.
DEFAULT_USER_TEMPLATE = (
    "样本（{columns}）：\n{row}\n\n"
    "请根据以上特征输出真实标签。返回 JSON {{\"label\": <value>}}。"
)


@dataclass
class LabelRowResult:
    """One labeled row + the raw model output (handy for the prompt-debug panel)."""

    row: dict[str, Any]
    label: str
    raw_text: str
    error: str | None = None


def _format_user_prompt(template: str, row: dict[str, Any]) -> str:
    """Format the user prompt with the row. ``{row}`` = JSON; ``{columns}`` = csv list."""
    safe_row = {str(k): v for k, v in row.items()}
    return template.format(row=json.dumps(safe_row, ensure_ascii=False),
                           columns=", ".join(safe_row.keys()))


def _parse_label_response(text: str) -> str:
    """Best-effort parse of a label from the model's reply.

    Order:
      1. A strict JSON object ``{"label": ...}`` somewhere in the reply.
      2. A single ``label: <value>`` line (case-insensitive).
      3. The first non-empty stripped line / token (permissive fallback for reasoning
         models that ignore the system prompt).
    """
    if not text:
        return ""
    # 1) JSON object with "label" key.
    for match in re.finditer(r"\{[^{}]*?\"label\"[^{}]*?\}", text):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "label" in obj:
            return str(obj["label"]).strip()
    # 2) "label: X" line.
    for line in text.splitlines():
        m = re.match(r"^\s*label\s*[:=]\s*(.+?)\s*$", line, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # 3) Permissive fallback.
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s.split()[0] if s.split() else s
    return text.strip()


def label_rows(
    rows: list[dict[str, Any]],
    *,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    user_template: str = DEFAULT_USER_TEMPLATE,
    config: ProviderConfig | None = None,
    client: LLMClient | None = None,
    batch_size: int = 1,
) -> list[LabelRowResult]:
    """Label every row with the LLM; return one :class:`LabelRowResult` per row.

    ``batch_size=1`` makes one chat call per row (simple, easier to debug). Larger
    ``batch_size`` packs multiple rows into a single prompt — cheaper, but the model's
    output parsing gets harder (one ``{"label": ...}`` per row, in order).
    """
    cfg = config or ProviderConfig.from_env_or_request()
    cli = client or LLMClient(cfg)
    results: list[LabelRowResult] = []

    if batch_size <= 1:
        for row in rows:
            user = _format_user_prompt(user_template, row)
            try:
                resp = cli.chat(
                    [ChatMessage("system", system_prompt), ChatMessage("user", user)]
                )
                results.append(
                    LabelRowResult(
                        row=row,
                        label=_parse_label_response(resp.text),
                        raw_text=resp.text,
                    )
                )
            except LLMClientError as exc:
                results.append(LabelRowResult(row=row, label="", raw_text="", error=str(exc)))
        return results

    # Batched path: pack up to ``batch_size`` rows per call, expect one label per row.
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        lines = [f"{i + 1}. " + _format_user_prompt(user_template, r) for i, r in enumerate(chunk)]
        user = "请为以下样本依次输出标签，每行一个 JSON：\n\n" + "\n\n".join(lines)
        try:
            resp = cli.chat(
                [ChatMessage("system", system_prompt), ChatMessage("user", user)]
            )
        except LLMClientError as exc:
            for r in chunk:
                results.append(LabelRowResult(row=r, label="", raw_text="", error=str(exc)))
            continue
        labels = _parse_batch_labels(resp.text, expected=len(chunk))
        for r, lbl, raw in zip(chunk, labels, [resp.text] * len(chunk)):
            results.append(LabelRowResult(row=r, label=lbl, raw_text=raw))
    return results


def _parse_batch_labels(text: str, expected: int) -> list[str]:
    """Parse ``expected`` labels from a batched reply. Falls back to blanks on mismatch."""
    found: list[str] = []
    for match in re.finditer(r"\{[^{}]*?\"label\"[^{}]*?\}", text):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "label" in obj:
            found.append(str(obj["label"]).strip())
            if len(found) == expected:
                break
    if len(found) < expected:
        # Pad with blanks so downstream CSV alignment is preserved.
        found.extend([""] * (expected - len(found)))
    return found[:expected]


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_USER_TEMPLATE",
    "LabelRowResult",
    "label_rows",
]
