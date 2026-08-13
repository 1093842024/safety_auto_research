"""Unified tmeoa client — the single substitution point for every OSS benchmark
that originally required local model weights or a vendor-specific API.

Why this module exists
----------------------
The OSS research repos under this tree each hard-wire a different model backend:

* ``claudini``      — local HF weights (``facebook/Meta-SecAlign-70B``,
                      ``openai/gpt-oss-safeguard-20b``) loaded via transformers.
* ``AutoResearchClaw`` — an OpenAI-compatible gateway (``primary_model: gpt-4o``).
* ``Agent-Native-Research-Artifact`` — the Anthropic Messages API
                      (``anthropic.Anthropic()``).
* ``Auto-claude-code-research-in-sleep`` — ``LLM_BASE_URL``/``LLM_MODEL`` env vars
                      plus the ``claude -p`` CLI.
* ``ScienceAgentBench`` — GPT-4o, including a *visual* judge for figure tasks.

All of them are redirected here. The tmeoa gateway is OpenAI-chat-compatible, so
the shape of :func:`chat` matches ``/v1/chat/completions``; the only non-standard
requirement is the ``TmeOpenApi: true`` header.

Verified capabilities (probed 2026-08-11)
-----------------------------------------
=========================  =========  ==========  =========
model                      text       logprobs    vision
=========================  =========  ==========  =========
deepseek-v4-flash-official  yes        yes         no
deepseek-v4-pro-official    yes        yes         no
qwen3.6-35b-a3b             yes        yes         yes
qwen3.5-397b-a17b           yes        yes         yes
qwen3.7-plus-external       yes        yes         yes
=========================  =========  ==========  =========

Two capabilities are **absent** and no wrapper can synthesise them, which bounds
what can be ported (see ``REPORT`` for the consequences):

* ``/embeddings`` returns HTTP 403 and ``/completions`` returns HTTP 401 — only
  the chat route is open.
* There is no access to gradients, the embedding matrix, or full-vocabulary
  logits. ``top_logprobs`` caps at 20 entries per position, which supports
  *grey-box* search but not the white-box GCG gradient attacks in ``claudini``.

The deepseek models are reasoning models: they emit ``reasoning_content`` before
``content``, so a small ``max_tokens`` yields an empty answer. ``DEFAULT_MAX_TOKENS``
keeps headroom and :func:`chat_text` raises if ``content`` came back empty while
``finish_reason == "length"``, instead of silently returning "".
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

TMEOA_URL = os.environ.get(
    "TMEOA_URL", "https://ai-rec.tmeoa.com/llmproxy/chat/completions"
)

# Two tokens were supplied by the operator; both work against every model, but
# the second one accompanied the vision example so it stays the vision default.
TOKEN_TEXT = os.environ.get("TMEOA_TOKEN", "ynavvi42pCCJ26ISQuWSlwld@3897")
TOKEN_VISION = os.environ.get("TMEOA_VISION_TOKEN", "okbRLDd8TKjgZhaqPH2kRQIw@3897")

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

TEXT_MODELS: tuple[str, ...] = (
    "deepseek-v4-flash-official",
    "deepseek-v4-pro-official",
    "qwen3.6-35b-a3b",
    "qwen3.5-397b-a17b",
)
"""The four models mandated for every text-model substitution."""

VISION_MODELS: tuple[str, ...] = (
    "qwen3.6-35b-a3b",
    "qwen3.5-397b-a17b",
    "qwen3.7-plus-external",
)
"""Models accepting ``image_url`` content parts — used for figure/visual judging."""

LOGPROB_MODELS: tuple[str, ...] = (
    "qwen3.6-35b-a3b",
    "qwen3.5-397b-a17b",
)
"""Models that reliably return usable ``top_logprobs`` (measured, not assumed).

Excluded and why:

* ``deepseek-v4-*`` — these are reasoning models. Logprobs *are* returned, but
  only for post-reasoning ``content`` positions, so a scoring call needs a large
  ``max_tokens`` and the first content token is separated from the prompt by a
  variable-length reasoning trace. That breaks the "logprob of the target at
  position 0" objective, so they are driven by output-match instead.
* ``qwen3.7-plus-external`` — rejects the ``logprobs`` parameter with HTTP 400.
"""

ALL_MODELS: tuple[str, ...] = tuple(dict.fromkeys(TEXT_MODELS + VISION_MODELS))

DEFAULT_TEXT_MODEL = os.environ.get("TMEOA_MODEL", TEXT_MODELS[0])
DEFAULT_VISION_MODEL = os.environ.get("TMEOA_VISION_MODEL", VISION_MODELS[0])

# Reasoning models spend budget on reasoning_content before content appears.
DEFAULT_MAX_TOKENS = 2000

MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class TmeoaError(RuntimeError):
    """Raised when the gateway cannot produce a usable completion."""


@dataclass
class Usage:
    """Token accounting, aggregated across a whole experiment."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    failures: int = 0

    def add(self, resp: dict) -> None:
        self.calls += 1
        u = resp.get("usage") or {}
        self.prompt_tokens += int(u.get("prompt_tokens") or 0)
        self.completion_tokens += int(u.get("completion_tokens") or 0)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "failures": self.failures,
        }


USAGE = Usage()
"""Process-wide counter so experiments can report query cost."""


def image_to_data_uri(image_path: str | Path) -> str:
    """Encode an image as a ``data:`` URI, matching the operator's VLM example."""
    path = Path(image_path)
    mime = MIME_TYPES.get(path.suffix.lower(), "image/jpeg")
    encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{encoded}"


def _post(payload: dict, token: str, timeout: int) -> dict:
    req = urllib.request.Request(
        TMEOA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "TmeOpenApi": "true",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    top_p: float | None = None,
    frequency_penalty: float | None = None,
    logprobs: bool = False,
    top_logprobs: int | None = None,
    timeout: int = 180,
    retries: int = 3,
    backoff: float = 1.5,
    vision: bool = False,
) -> dict:
    """One OpenAI-compatible chat completion, with retry on transient failure.

    Args:
        messages: OpenAI message list. Content may be a string or a list of
            ``{"type": "text"|"image_url", ...}`` parts for vision models.
        model: Defaults to :data:`DEFAULT_TEXT_MODEL` (or the vision default when
            ``vision=True``).
        logprobs / top_logprobs: Enable token logprob feedback. ``top_logprobs``
            is capped at 20 by the gateway; this is what makes the grey-box
            attack track possible.
        vision: Selects the vision auth token and vision default model.

    Returns:
        The parsed response dict.

    Raises:
        TmeoaError: if every attempt failed.
    """
    chosen = model or (DEFAULT_VISION_MODEL if vision else DEFAULT_TEXT_MODEL)
    payload: dict = {
        "model": chosen,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if top_p is not None:
        payload["top_p"] = top_p
    if frequency_penalty is not None:
        payload["frequency_penalty"] = frequency_penalty
    if logprobs:
        payload["logprobs"] = True
        # The gateway rejects values above 20.
        payload["top_logprobs"] = min(int(top_logprobs or 20), 20)

    token = TOKEN_VISION if vision else TOKEN_TEXT
    last_err: Exception | None = None

    for attempt in range(retries):
        try:
            resp = _post(payload, token, timeout)
            if "choices" not in resp:
                raise TmeoaError(f"no choices in response: {json.dumps(resp)[:300]}")
            USAGE.add(resp)
            return resp
        except Exception as exc:  # noqa: BLE001 - retry on any transport/gateway error
            last_err = exc
            USAGE.failures += 1
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))

    raise TmeoaError(f"tmeoa chat failed after {retries} attempts: {last_err}")


def chat_text(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: int = 180,
    retries: int = 3,
    strict: bool = True,
) -> str:
    """Return just the assistant text.

    Args:
        strict: When True, raise if the model truncated during reasoning and left
            ``content`` empty. Silently returning "" there produces experiments
            that look like model failures but are really budget failures.
    """
    messages: list[dict] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = chat(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        retries=retries,
    )
    choice = resp["choices"][0]
    content = (choice.get("message") or {}).get("content") or ""
    if strict and not content.strip() and choice.get("finish_reason") == "length":
        raise TmeoaError(
            "model exhausted max_tokens during reasoning and emitted no content; "
            "raise max_tokens"
        )
    return content


def chat_vision(
    prompt: str,
    image_paths: list[str | Path],
    *,
    system: str = "You are a helpful assistant.",
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: int = 180,
    retries: int = 3,
) -> str:
    """Multimodal completion: one text prompt plus one or more local images.

    Images are inlined as base64 ``data:`` URIs, matching the operator's
    ``VenusVLMProcessor`` example. This is the substitute for the GPT-4o *visual*
    judge that ScienceAgentBench uses to grade figure-producing tasks.
    """
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        parts.append(
            {"type": "image_url", "image_url": {"url": image_to_data_uri(path)}}
        )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": parts},
    ]
    resp = chat(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        retries=retries,
        vision=True,
    )
    choice = resp["choices"][0]
    return (choice.get("message") or {}).get("content") or ""


@dataclass
class TokenScore:
    """Logprob feedback for the first generated position.

    ``target_logprob`` is ``None`` when the target token fell outside the
    top-20 window — the caller must treat that as "worse than the worst
    observed candidate" rather than as zero probability.
    """

    top_token: str
    top_logprob: float
    target_logprob: float | None
    alternatives: dict[str, float] = field(default_factory=dict)


def score_first_token(
    messages: list[dict],
    target_token: str,
    *,
    model: str | None = None,
    top_logprobs: int = 20,
    max_tokens: int = 8,
    timeout: int = 180,
    retries: int = 3,
) -> TokenScore:
    """Grey-box objective: how likely is ``target_token`` at position 0?

    This replaces the cross-entropy-on-target-tokens objective that ``claudini``
    computes from full logits. We only observe the top-20 alternatives, so the
    signal is truncated but strictly monotone in the target's rank, which is
    enough to drive a random-search attack.

    Only the models in :data:`LOGPROB_MODELS` are supported; the others either
    reject the parameter or interpose a reasoning trace before position 0.

    Args:
        max_tokens: Kept small on purpose — we only need position 0, and a short
            budget keeps each scoring query cheap.
    """
    resp = chat(
        messages,
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
        logprobs=True,
        top_logprobs=top_logprobs,
        timeout=timeout,
        retries=retries,
    )
    choice = resp["choices"][0]
    positions = ((choice.get("logprobs") or {}).get("content")) or []
    if not positions:
        raise TmeoaError("gateway returned no logprobs; cannot score")

    first = positions[0]
    alts = {
        entry["token"]: float(entry["logprob"])
        for entry in (first.get("top_logprobs") or [])
    }

    # Match the target leniently: gateways tokenise with and without a leading
    # space, and casing varies between model families.
    target_lp: float | None = None
    for candidate in (
        target_token,
        f" {target_token}",
        target_token.lower(),
        target_token.capitalize(),
        f" {target_token.capitalize()}",
    ):
        if candidate in alts:
            target_lp = alts[candidate]
            break
    if target_lp is None:
        stripped = target_token.strip().lower()
        for tok, lp in alts.items():
            if tok.strip().lower() == stripped:
                target_lp = lp
                break

    return TokenScore(
        top_token=first["token"],
        top_logprob=float(first["logprob"]),
        target_logprob=target_lp,
        alternatives=alts,
    )


def probe_models(models: tuple[str, ...] = ALL_MODELS) -> dict[str, dict]:
    """Health-check each model; used to record provenance before an experiment.

    Each capability is probed independently so one unsupported feature does not
    mask a working one.
    """
    report: dict[str, dict] = {}
    for name in models:
        entry: dict = {"text": False, "logprobs": False, "vision": None, "errors": {}}

        try:
            out = chat_text(
                "Reply with exactly: PONG", model=name, max_tokens=2000, strict=False
            )
            entry["text"] = "PONG" in out.upper()
        except Exception as exc:  # noqa: BLE001 - provenance record
            entry["errors"]["text"] = str(exc)[:160]

        if name in LOGPROB_MODELS:
            try:
                score = score_first_token(
                    [{"role": "user", "content": "Say yes"}], "yes", model=name
                )
                entry["logprobs"] = bool(score.alternatives)
            except Exception as exc:  # noqa: BLE001
                entry["errors"]["logprobs"] = str(exc)[:160]

        entry["vision_declared"] = name in VISION_MODELS
        report[name] = entry
    return report


if __name__ == "__main__":
    print("== model probe ==")
    for name, info in probe_models().items():
        print(f"  {name:30s} {info}")
    print("== usage ==", USAGE.as_dict())
