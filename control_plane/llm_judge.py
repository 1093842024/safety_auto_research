"""Real LLM judge client (Phase 2: evaluator hardening).

The platform ships deterministic heuristic judges everywhere so the full chain runs
with zero external dependencies. This module is the single, shared bridge to a real
LLM judge when ``LLM_JUDGE_URL`` is set — used by:

  * ``eval_runner.llm_judge_eval`` (tracked-only LLM tasks: SFT/RL/OPD outputs), and
  * ``audit_executor`` (opt-in via ``LLM_AUDIT_JUDGE=1``) for claim-support scoring.

Protocol (POST JSON, response JSON) — deliberately minimal so any judge service
(OpenAI-compatible wrapper, internal eval service, etc.) can implement it::

    request:  {"prompt": str, "reference": str, "prediction": str,
               "require": ["score", "rationale", "evidence_refs"]}
    response: {"score": float in [0,1], "rationale": str,
               "evidence_refs": [str, ...]}     # Chain-of-Evidence style (ScientistOne)

Every caller MUST handle ``LLMJudgeError`` by falling back to its heuristic judge —
a judge outage must never crash a research run (evaluator availability is a platform
reliability concern, not a research outcome).
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


class LLMJudgeError(Exception):
    """Raised when the real LLM judge is unavailable or returns a bad payload."""


def judge_url(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("LLM_JUDGE_URL")


def call_llm_judge(
    *,
    prompt: str,
    reference: str,
    prediction: str,
    url: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Call the real LLM judge once. Returns {score, rationale, evidence_refs}.

    Raises ``LLMJudgeError`` on any transport/parse/validation failure so callers can
    fall back deterministically.
    """

    endpoint = judge_url(url)
    if not endpoint:
        raise LLMJudgeError("LLM_JUDGE_URL 未设置，无法调用真实 judge")
    _timeout = float(timeout or os.environ.get("LLM_JUDGE_TIMEOUT", "30"))
    body = json.dumps(
        {
            "prompt": prompt,
            "reference": reference,
            "prediction": prediction,
            "require": ["score", "rationale", "evidence_refs"],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # transport / decode errors -> deterministic fallback
        raise LLMJudgeError(f"LLM judge 调用失败: {exc}") from exc

    try:
        score = float(data["score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMJudgeError(f"LLM judge 响应缺少合法 score: {data!r}") from exc
    return {
        "score": max(0.0, min(1.0, score)),
        "rationale": str(data.get("rationale", "")),
        "evidence_refs": [str(x) for x in (data.get("evidence_refs") or [])],
    }


def make_claim_judge(fallback: Any) -> Any:
    """Build an ``(answer, claim, evidence) -> float`` judge backed by the real LLM.

    Used by the external audit when ``LLM_AUDIT_JUDGE=1``. Falls back to the given
    heuristic judge on any failure (the audit must always produce a verdict).
    """

    def _judge(answer: str, claim: str, evidence: str) -> float:
        try:
            out = call_llm_judge(prompt=answer, reference=evidence, prediction=claim)
            return out["score"]
        except LLMJudgeError:
            return float(fallback(answer, claim, evidence))

    return _judge
