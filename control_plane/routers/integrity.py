"""Opt-in integrity gates over a *finished* run (spark-to-paper integration, Phase 2).

This router is the adapter between platform state and
:mod:`safety_auto_research.integrity_suite` — nothing more. Design rules, all of
them load-bearing for the "don't destabilise the platform" constraint:

* **Off by default.** Every endpoint checks the ``INTEGRITY_GATES`` environment
  switch at *request* time and returns ``503`` when it is unset. Nothing in the
  default control plane changes behaviour because this module exists.
* **Routes are registered unconditionally; only the behaviour is switched.** If
  registration itself depended on the env var, the OpenAPI surface would become
  environment-dependent and the frozen-surface test in
  ``tests/test_control_plane_structure.py`` could pass or fail depending on the
  shell that launched it. A stable route table is worth more than hiding two
  inert endpoints.
* **Read-only.** No research record is written, no run status is mutated, no
  event is emitted, no background thread is spawned, no run slot is acquired.
  There is therefore nothing to release — the R9 shutdown discipline
  (``deps.release_run_slot``) is satisfied vacuously rather than by imitation.
* **Terminal runs only.** Gating a still-running run would read a moving target
  and could race the loop that is writing it. Non-terminal runs get ``409``.
* **Curated input only.** ``claims`` are accepted from the caller; this router
  never scrapes inner-loop narrative into a claim set. That boundary is what
  keeps the outer audit independent of the inner loop
  (``tests/test_dual_loop.py::ContextSeparationTest``), and it is not weakened
  here for convenience.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field

from ..deps import ControlPlaneDeps
from ..deps import translate_exc
from ..service import _TERMINAL_STATUSES
from ...integrity_suite import gate_runner

logger = logging.getLogger(__name__)

# Values that turn the suite on. Anything else (including unset) leaves it off.
_TRUTHY = {"1", "true", "on", "yes"}

ENV_SWITCH = "INTEGRITY_GATES"


def gates_enabled() -> bool:
    """Read the switch at call time so a test/operator can flip it per request."""
    return os.environ.get(ENV_SWITCH, "").strip().lower() in _TRUTHY


class IntegrityCheckRequest(BaseModel):
    """Everything the gates need that the platform does not already know.

    ``scores`` is normally harvested from the run's own ``eval_completed`` events;
    supply it only to override that projection. ``claims`` must come from curated
    audit material (see the module docstring).
    """

    stage: str = Field(default="all", description="gate stage, or 'all' for the full set")
    claims: list[dict[str, Any]] | None = Field(
        default=None, description="curated {claim, answer, evidence} triples"
    )
    figures: list[str] | None = Field(
        default=None, description="SVG paths, absolute or relative to 'workdir'"
    )
    workdir: str | None = Field(default=None, description="base directory for relative figures")
    scores: dict[str, list[float]] | None = Field(
        default=None, description="override the harvested metric series"
    )
    min_claim_score: float | None = Field(
        default=None, description="fail claims below this score (default: status 'missing')"
    )
    min_kept_frac: float | None = Field(
        default=None, description="novelty gate: minimum surviving fraction"
    )
    svg_audit_args: list[str] | None = Field(
        default=None,
        description=(
            "extra svg_audit.py tuning flags, e.g. ['--min-font-px','8']; restricted "
            "to a closed whitelist (--min-font-px/--font-family/--max-marker/--pad/"
            "--pad-em/--min-cleanliness/--tol/--port-gap) — --json/--selftest and "
            "positionals are rejected"
        ),
    )


def harvest_scores(events: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Project a run's ``eval_completed`` events onto ``{metric: [values]}``.

    A *read-only* projection, deliberately not delegated to
    ``ControlPlaneService.capture_run_record`` — that method persists a research
    record and recomputes the task's top-3, which an integrity check has no
    business doing.

    Unlike ``compare_runs``, non-finite samples are **kept**: a NaN in the series
    is exactly the kind of defect the ``metric_direction`` gate exists to catch,
    so silently dropping it here would blind the gate.
    """
    scores: dict[str, list[float]] = {}
    for e in events or []:
        if not isinstance(e, dict) or e.get("event_type") != "eval_completed":
            continue
        metrics = e.get("metrics")
        if not isinstance(metrics, dict):
            continue
        for name, value in metrics.items():
            try:
                fv = float(value)
            except (TypeError, ValueError):
                continue  # non-numeric metadata, not a metric sample
            scores.setdefault(str(name), []).append(fv)
    return scores


def build_gate_context(
    run: Any, events: list[dict[str, Any]], req: IntegrityCheckRequest
) -> dict[str, Any]:
    """Assemble the gate context from run state + the caller's curated additions.

    The run supplies what it can prove (metric name, direction, measured series);
    the caller supplies what only it can know (which claims were curated, which
    figures ship). Candidates are intentionally absent: a finished run's
    population lives in the evolution archive, and re-filtering it here would
    re-litigate a decision the loop already made — so the novelty gate simply
    waives unless a caller passes candidates explicitly.
    """
    obj = run.objective_snapshot or {}
    ctx: dict[str, Any] = {
        "run_id": run.run_id,
        "metric": str(obj.get("eval_metric") or "accuracy").strip().lower(),
        "direction": str(obj.get("direction") or "higher").strip().lower(),
        "scores": req.scores if req.scores is not None else harvest_scores(events),
    }
    if req.claims is not None:
        ctx["claims"] = req.claims
    if req.figures is not None:
        ctx["figures"] = req.figures
    if req.workdir:
        ctx["workdir"] = req.workdir
    if req.min_claim_score is not None:
        ctx["min_claim_score"] = req.min_claim_score
    if req.min_kept_frac is not None:
        ctx["min_kept_frac"] = req.min_kept_frac
    if req.svg_audit_args is not None:
        ctx["svg_audit_args"] = req.svg_audit_args
    return ctx


def build_integrity_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the integrity-gate router bound to *deps*."""

    router = APIRouter()
    svc = deps.svc

    def _require_enabled() -> None:
        if not gates_enabled():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"integrity gates are disabled; set {ENV_SWITCH}=1 to enable "
                    f"(off by default so the default control plane is unaffected)"
                ),
            )

    @router.get(
        "/integrity/gates",
        summary="Describe the integrity-gate suite (and whether it is enabled)",
        tags=["integrity"],
    )
    def describe_gates() -> dict[str, Any]:
        """Capability descriptor. Readable even when the suite is disabled, so a
        client can discover the switch instead of guessing at a 503."""
        return {
            "enabled": gates_enabled(),
            "env_switch": ENV_SWITCH,
            "stages": {k: list(v) for k, v in gate_runner.STAGE_GATES.items()},
            "gates": sorted(gate_runner.GATES),
            "svg_audit_available": gate_runner.SVG_AUDIT.exists(),
            "exit_codes": {"ok": gate_runner.GATE_OK, "issues": gate_runner.GATE_ISSUES,
                           "usage": gate_runner.GATE_USAGE},
        }

    @router.post(
        "/workflow-runs/{run_id}/integrity-check",
        summary="Run fail-closed integrity gates over a finished run (opt-in, read-only)",
        tags=["integrity"],
    )
    def integrity_check(run_id: str, req: IntegrityCheckRequest | None = None) -> dict[str, Any]:
        """Gate a terminal run and return the verdict report.

        Returns ``200`` with ``ok: false`` when a gate goes red — the *request*
        succeeded; the verdict is data. Callers MUST branch on ``ok`` (or on
        ``code``, which carries the same 0/1/2 vocabulary as the CLI). A red gate
        is not an HTTP error, and treating it as one would hide it from clients
        that only check status codes.
        """
        _require_enabled()
        payload = req or IntegrityCheckRequest()
        try:
            run = svc.get_workflow_run(run_id)
        except Exception as exc:
            raise translate_exc(exc)

        if run.status not in _TERMINAL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"run {run_id} is {getattr(run.status, 'value', run.status)!r}; integrity "
                    f"gates only run on a terminal run (a live run is a moving target)"
                ),
            )

        if payload.stage not in gate_runner.STAGE_GATES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown stage {payload.stage!r}; known: {sorted(gate_runner.STAGE_GATES)}",
            )

        ctx = build_gate_context(run, svc.list_events(run_id), payload)
        report = gate_runner.run_gates(payload.stage, ctx)
        if not report["ok"]:
            logger.info("integrity gates red for run=%s: %s", run_id, report["summary"])
        return {
            "run_id": run_id,
            "status": getattr(run.status, "value", str(run.status)),
            **report,
        }

    return router
