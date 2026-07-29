"""Contamination assessment — is the benchmark's test set already in pretraining?

Two layers. The declarative heuristic + canary scan are fully implemented and
deterministic. The LLM membership-inference and web-search probes are stubbed
behind the interface (return no signal) and are a follow-up. The whole thing is
non-blocking: any error or timeout degrades to the declarative result and never
raises into the run loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Literal

log = logging.getLogger(__name__)

Status = Literal["clean", "warn", "contaminated", "unknown"]

# Best-effort model knowledge-cutoff dates (ISO). Unknown models → no date signal.
_MODEL_CUTOFFS: dict[str, str] = {
    "claude-sonnet-4-20250514": "2025-03-01",
    "claude-opus-4-8": "2026-01-01",
    "gpt-4o": "2023-10-01",
}


@dataclass
class ContaminationReport:
    status: Status = "unknown"
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons), "signals": dict(self.signals)}


def scan_canaries(outputs: Iterable[str], canaries: list[str]) -> list[str]:
    """Return the canary strings that appear in any of *outputs*."""
    blob = "\n".join(o for o in outputs if o)
    return [c for c in canaries if c and c in blob]


def _cutoff_date(model: str | None) -> date | None:
    iso = _MODEL_CUTOFFS.get(model or "")
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def declarative_assess(contamination: dict[str, Any], model: str | None) -> ContaminationReport:
    """Heuristic assessment from the declared contamination block. Never raises."""
    reasons: list[str] = []
    signals: dict[str, Any] = {}
    status: Status = "unknown"

    if contamination.get("is_public"):
        status = "warn"
        reasons.append("test set / answers are declared public (is_public: true)")
        signals["is_public"] = True

    release = contamination.get("release_date")
    cutoff = _cutoff_date(model)
    if release and cutoff:
        try:
            rel = date.fromisoformat(str(release))
            signals["release_date"] = str(release)
            if rel <= cutoff:
                status = "contaminated"
                reasons.append(
                    f"test set released {rel} <= model knowledge cutoff {cutoff}"
                )
        except ValueError:
            pass

    return ContaminationReport(status=status, reasons=reasons, signals=signals)


class ContaminationProbe:
    """Active probe with a declarative fallback. Non-blocking by construction."""

    async def assess(
        self,
        *,
        dataset_info: Any,
        eval_contract: dict[str, Any],
        model: str | None = None,
        outputs: Iterable[str] = (),
        provider: Any = None,
        search_tool: Any = None,
        timeout: float = 60.0,
    ) -> ContaminationReport:
        contamination = (eval_contract or {}).get("contamination", {}) or {}
        outputs = list(outputs)

        # 1. Canary scan (deterministic, cheap, authoritative when it hits).
        canaries = contamination.get("canaries") or []
        hits = scan_canaries(outputs, canaries)
        if hits:
            return ContaminationReport(
                status="contaminated",
                reasons=[f"canary string(s) leaked into output: {', '.join(hits)}"],
                signals={"canaries": hits},
            )

        # 2. Active probe (stubbed) — guarded by timeout, degrades to declarative.
        if provider is not None:
            try:
                signal = await asyncio.wait_for(
                    self._llm_membership_probe(
                        dataset_info=dataset_info, model=model, provider=provider,
                    ),
                    timeout=timeout,
                )
                if signal is not None:
                    return signal
            except asyncio.TimeoutError as exc:
                log.warning("contamination: active probe timed out: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("contamination: active probe failed: %s", exc)

        # 3. Declarative fallback.
        return declarative_assess(contamination, model)

    async def _llm_membership_probe(
        self, *, dataset_info: Any, model: str | None, provider: Any
    ) -> ContaminationReport | None:
        """STUB — follow-up. Will ask the model to reproduce held-out rows /
        recognize the dataset and infer membership. Returns None (no signal)
        for now so :meth:`assess` falls through to the declarative heuristic."""
        return None
