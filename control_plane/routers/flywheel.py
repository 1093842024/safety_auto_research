"""B Flywheel routes — one-iteration badcase retrain + regression gate + scheduler.

Phase 1.5 of ``doc/auto_research_task_taxonomy.md``. A flywheel run (``RunType.FLYWHEEL``)
iterates the data-flywheel loop: collect badcase (or reuse a provided CSV) → replay retrain
(frozen architecture) → regression gate (the frozen original eval set must not degrade).

Endpoints
---------
* ``POST /workflow-runs/{run_id}/flywheel`` — run ONE iteration synchronously (sub-second
  on tabular data). Returns the before/after metrics + the accept/reject verdict.
* ``GET  /workflow-runs/{run_id}/flywheel`` — replay the persisted ``eval_completed``
  events as the iteration history (badcase-coverage curve data).
* ``POST   /workflow-runs/{run_id}/flywheel/schedule`` — start a background scheduler
  that auto-triggers an iteration when the badcase CSV row count crosses a threshold.
  Event-driven in the sense that the trigger fires off accumulated badcase data, not on
  a wall-clock; the ``poll_interval_sec`` is the polling granularity.
* ``GET    /workflow-runs/{run_id}/flywheel/schedule`` — scheduler status.
* ``DELETE /workflow-runs/{run_id}/flywheel/schedule`` — stop the scheduler.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from fastapi import APIRouter

from ..deps import ControlPlaneDeps
from ..deps import translate_exc as _translate
from ..schemas import FlywheelRequest
from ..schemas import FlywheelScheduleRequest
from ..service import ConflictError
from ..service import NotFoundError

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_DATA_DIR = os.path.join(_PKG_ROOT, "data", "kaggle", "titanic")

_scheduler_lock = threading.Lock()
_schedulers: dict[str, "_SchedulerState"] = {}


@dataclass
class _SchedulerState:
    run_id: str
    thread: threading.Thread
    stop_event: threading.Event
    started_at: float
    config: dict[str, Any]
    last_check_at: float = 0.0
    last_badcase_count: int = 0
    last_trigger_at: float = 0.0
    trigger_count: int = 0
    last_verdict: str = ""
    last_detail: str = ""
    error: str = ""


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

    # ====================================================================== #
    # Event-driven scheduler: poll the badcase CSV, auto-run when threshold  #
    # is reached. One scheduler per run (id is the run_id).                  #
    # ====================================================================== #

    def _count_badcase_rows(path: str) -> int:
        import pandas as pd

        if not os.path.exists(path):
            return 0
        try:
            return int(len(pd.read_csv(path)))
        except Exception as exc:  # partial / malformed CSV -> treat as zero
            logging.warning("scheduler: badcase_path %r unreadable: %s", path, exc)
            return 0

    def _clear_badcase_csv(path: str) -> None:
        import pandas as pd

        if os.path.exists(path):
            try:
                pd.DataFrame().to_csv(path, index=False)
            except Exception as exc:
                logging.warning("scheduler: failed to clear %r: %s", path, exc)

    def _scheduler_loop(state: _SchedulerState) -> None:
        log = logging.getLogger(f"flywheel.scheduler.{state.run_id}")
        log.info("started threshold=%d interval=%.1fs", state.config["threshold"], state.config["poll_interval_sec"])
        while not state.stop_event.is_set():
            try:
                n = _count_badcase_rows(state.config["badcase_path"])
                state.last_check_at = time.time()
                state.last_badcase_count = n
                if n >= state.config["threshold"]:
                    log.info("triggering: badcase=%d >= threshold=%d", n, state.config["threshold"])
                    # Run the iteration through the platform's capability runner (preserves
                    # the full audit/event contract).
                    cfg = state.config
                    params = {
                        "preset": cfg["preset"], "target": cfg["target"], "model": cfg["model"],
                        "fe": cfg["fe"], "drop_cols": cfg["drop_cols"],
                        "data_dir": cfg["data_dir"], "badcase_path": cfg["badcase_path"],
                        "badcase_ratio": cfg["badcase_ratio"], "regression_tol": cfg["regression_tol"],
                        "eval_metric": cfg["eval_metric"], "heldout_frac": cfg["heldout_frac"],
                        "heldout_seed": cfg["heldout_seed"],
                    }
                    try:
                        # Ensure the run is started so a StageRun can be created
                        # (mirrors the one-shot ``run_flywheel_iteration`` flow).
                        try:
                            svc.start_workflow_run(state.run_id)
                        except (ConflictError, ValueError):
                            pass  # already running / terminal — proceed
                        _, result = orchestrator.run_capability(state.run_id, "badcase_retrain", params)
                        state.last_trigger_at = time.time()
                        state.trigger_count += 1
                        state.last_verdict = "ACCEPT" if result.gate_result.value == "passed" else "REJECT"
                        state.last_detail = result.detail
                        if cfg["auto_clear_after_trigger"]:
                            _clear_badcase_csv(cfg["badcase_path"])
                    except Exception as exc:  # an iteration crash must NOT kill the scheduler
                        log.exception("iteration failed")
                        state.error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # outer guard: never let the loop die
                log.exception("scheduler loop error")
                state.error = f"{type(exc).__name__}: {exc}"
            # Interruptible sleep so DELETE /schedule returns immediately.
            if state.stop_event.wait(state.config["poll_interval_sec"]):
                break
        log.info("stopped")

    @router.post(
        "/workflow-runs/{run_id}/flywheel/schedule",
        summary="Start a background scheduler that auto-runs a flywheel iteration when the badcase CSV crosses a threshold",
    )
    def start_scheduler(run_id: str, req: FlywheelScheduleRequest) -> dict:
        try:
            svc.get_workflow_run(run_id)
        except NotFoundError as exc:
            raise _translate(exc)
        with _scheduler_lock:
            existing = _schedulers.get(run_id)
            if existing is not None and existing.thread.is_alive():
                raise _translate(ConflictError(
                    f"run {run_id} 已有正在运行的飞轮调度（已触发 {existing.trigger_count} 次）"
                ))
            stop_event = threading.Event()
            config = req.model_dump()
            state = _SchedulerState(
                run_id=run_id,
                thread=None,  # late-bind to break the chicken-and-egg below
                stop_event=stop_event,
                started_at=time.time(),
                config=config,
            )
            thread = threading.Thread(
                target=_scheduler_loop,
                args=(state,),
                name=f"flywheel-sched-{run_id}",
                daemon=True,
            )
            state.thread = thread  # late-bind so the loop can read its own back-pointer
            _schedulers[run_id] = state
            thread.start()
        return {
            "run_id": run_id,
            "scheduled": True,
            "threshold": config["threshold"],
            "poll_interval_sec": config["poll_interval_sec"],
            "started_at": state.started_at,
        }

    @router.get(
        "/workflow-runs/{run_id}/flywheel/schedule",
        summary="Status of the event-driven flywheel scheduler for a run",
    )
    def get_scheduler(run_id: str) -> dict:
        with _scheduler_lock:
            state = _schedulers.get(run_id)
            if state is None:
                return {"run_id": run_id, "scheduled": False}
            return {
                "run_id": run_id,
                "scheduled": state.thread.is_alive(),
                "started_at": state.started_at,
                "last_check_at": state.last_check_at,
                "last_badcase_count": state.last_badcase_count,
                "last_trigger_at": state.last_trigger_at,
                "trigger_count": state.trigger_count,
                "last_verdict": state.last_verdict,
                "last_detail": state.last_detail,
                "error": state.error,
                "config": state.config,
            }

    @router.delete(
        "/workflow-runs/{run_id}/flywheel/schedule",
        summary="Stop the event-driven flywheel scheduler for a run",
    )
    def stop_scheduler(run_id: str) -> dict:
        with _scheduler_lock:
            state = _schedulers.get(run_id)
            if state is None:
                return {"run_id": run_id, "stopped": False, "reason": "no active scheduler"}
            state.stop_event.set()
        state.thread.join(timeout=10.0)
        with _scheduler_lock:
            _schedulers.pop(run_id, None)
        return {
            "run_id": run_id,
            "stopped": True,
            "trigger_count": state.trigger_count,
        }

    return router
