"""Failure-mode mining — Self-Harness-style weakness mining over the event log.

Instead of letting failures evaporate as one-off log lines (the platform previously
kept them only inside pruned hypothesis nodes and unread ``rejected_candidates``),
this module clusters them into *recurring failure modes* with counts and evidence
refs. The mined modes feed two consumers:

  1. the playbook reflector (negative "avoid" bullets for the next inner loop), and
  2. the refine path (folded into the agent goal / inner params so a Refine verdict
     actually changes the next trajectory).

The miner is deterministic and run-scoped: it reads only the platform event log of
one run — it never touches the curated outer-audit input, preserving the dual-loop
context-separation invariants.
"""

from __future__ import annotations

import re
from typing import Any


def _norm_mode(text: str) -> str:
    """Cluster key: lowercase, strip volatile ids/numbers so similar failures group."""

    t = re.sub(r"[0-9a-f]{8,}", "<id>", text.lower())
    t = re.sub(r"\d+\.\d+", "<num>", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:120]


def mine_failure_modes(events: list[dict[str, Any]], *, top_n: int = 5) -> list[dict[str, Any]]:
    """Cluster failure signals from a run's event log.

    Signals considered (all already present in the platform event log):
      * ``eval_completed`` with ``gate_passed=False`` — the inner answer missed the gate;
      * ``audit_completed`` with unresolved claims — external-audit rejections;
      * ``improvement_applied`` with ``reverted=True`` — meta-loop proposal failures.

    Returns a list of ``{"mode", "count", "evidence_refs"}`` sorted by count desc.
    """

    clusters: dict[str, dict[str, Any]] = {}

    def _bump(mode: str, ref: str | None) -> None:
        key = _norm_mode(mode)
        slot = clusters.setdefault(key, {"mode": mode, "count": 0, "evidence_refs": []})
        slot["count"] += 1
        if ref and ref not in slot["evidence_refs"]:
            slot["evidence_refs"].append(ref)

    for e in events:
        etype = e.get("event_type")
        if etype == "eval_completed" and not e.get("gate_passed", True):
            metrics = e.get("metrics") or {}
            _bump(
                "eval_below_gate: 主指标未过门 "
                f"(primary={metrics.get('primary')}, acc={metrics.get('accuracy')})",
                e.get("report_ref") or e.get("stage_run_id"),
            )
        elif etype == "audit_completed":
            for claim in e.get("unresolved_claims") or []:
                _bump(f"audit_unresolved: {claim}", e.get("report_ref") or e.get("audit_id"))
        elif etype == "improvement_applied" and e.get("reverted"):
            _bump(
                "self_evolution_reverted: 元循环提案被冻结校验拒绝",
                e.get("rollback_id"),
            )

    out = sorted(clusters.values(), key=lambda c: c["count"], reverse=True)
    return out[:top_n]
