"""B Flywheel routes — one-iteration badcase retrain + regression gate.

Phase 1.5 of ``doc/auto_research_task_taxonomy.md``. A flywheel run (``RunType.FLYWHEEL``)
iterates the data-flywheel loop: collect badcase (or reuse a provided CSV) → replay retrain
(frozen architecture) → regression gate (the frozen original eval set must not degrade).

The ``POST`` runs exactly ONE iteration synchronously (two sklearn fits on tabular data —
sub-second) and returns the before/after metrics + the accept/reject verdict, so the
frontend can render the flywheel visualization directly. The ``GET`` replays the persisted
``eval_completed`` events as the iteration history (badcase-coverage curve).
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter

from ..deps import ControlPlaneDeps
from ..deps import translate_exc as _translate
from ..schemas import FlywheelRequest
from ..service import ConflictError
from ..service import NotFoundError

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_DATA_DIR = os.path.join(_PKG_ROOT, "data", "kaggle", "titanic")


def build_flywheel_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the B-flywheel router bound to *deps*."""

    router = APIRouter()

    svc = deps.svc
    orchestrator = deps.orchestrator

    @router.post(
        "/workflow-runs/{run_id}/flywheel",
        summary="Run one B-flywheel iteration (collect badcase -> replay retrain -> regression gate)",
    )
    def run_flywheel_iteration(run_id: str, req: FlywheelRequest) -> dict:
        # The run must already exist (created via POST /workflow-runs with run_type=flywheel).
        try:
            svc.get_workflow_run(run_id)
        except NotFoundError as exc:
            raise _translate(exc)
        # Start it so a StageRun can be created for the badcase_retrain capability.
        try:
            svc.start_workflow_run(run_id)
        except (ConflictError, ValueError):
            pass  # already running / terminal — proceed

        data_dir = req.data_dir or _DEFAULT_DATA_DIR

        # ---- flywheel step 1: collect badcase (auto-derive from baseline mispredictions) ----
        badcase_path = req.badcase_path
        collected = badcase_path is None
        if collected:
            # Lazy import: the executor transitively pulls the SDK (control_plane.*), so
            # import at request time, not module load, to avoid a package import cycle.
            from ...execution_plane.capabilities.badcase_retrain_executor import (
                BadcaseRetrainExecutor,
            )

            _tmp = tempfile.mkdtemp(prefix="flywheel-badcase-")
            badcase_path, _n = BadcaseRetrainExecutor.collect_badcase(
                data_dir=data_dir,
                preset=req.preset,
                target=req.target,
                model_name=req.model,
                drop_cols=req.drop_cols,
                fe=req.fe,
                heldout_frac=req.heldout_frac,
                heldout_seed=req.heldout_seed,
                out_path=os.path.join(_tmp, "badcase.csv"),
            )

        params = {
            "preset": req.preset,
            "target": req.target,
            "model": req.model,
            "fe": req.fe,
            "drop_cols": req.drop_cols,
            "data_dir": data_dir,
            "badcase_path": badcase_path,
            "badcase_ratio": req.badcase_ratio,
            "regression_tol": req.regression_tol,
            "eval_metric": req.eval_metric,
            "heldout_frac": req.heldout_frac,
            "heldout_seed": req.heldout_seed,
        }

        stage, result = orchestrator.run_capability(run_id, "badcase_retrain", params)

        metrics = result.event.metrics if result.event is not None else {}
        return {
            "run_id": run_id,
            "stage_run_id": stage.stage_run_id,
            "gate_result": result.gate_result.value,
            "badcase_collected": collected,
            "badcase_path": badcase_path if req.badcase_path else None,
            "metrics": metrics,
            "regression_passed": bool(metrics.get("regression_passed", 0.0) == 1.0),
            "badcase_improved": bool(metrics.get("badcase_improved", 0.0) == 1.0),
            "verdict": "ACCEPT" if result.gate_result.value == "passed" else "REJECT",
            "detail": result.detail,
        }

    @router.get(
        "/workflow-runs/{run_id}/flywheel",
        summary="Replay the flywheel iteration history for a run",
    )
    def list_flywheel_iterations(run_id: str) -> list[dict]:
        try:
            svc.get_workflow_run(run_id)
        except NotFoundError as exc:
            raise _translate(exc)
        iterations: list[dict] = []
        for e in svc.list_events(run_id):
            if e.get("event_type") != "eval_completed":
                continue
            suite = str(e.get("eval_suite_id") or "")
            if not suite.startswith("flywheel-"):
                continue
            iterations.append(
                {
                    "event_id": e.get("event_id"),
                    "stage_run_id": e.get("stage_run_id"),
                    "eval_suite_id": suite,
                    "passed": e.get("passed"),
                    "gate_passed": e.get("gate_passed"),
                    "metrics": e.get("metrics") or {},
                    "occurred_at": e.get("occurred_at"),
                }
            )
        return iterations

    return router
