"""Dual-loop and MEA (multi-expert agent) execution routes.

Extracted verbatim from ``control_plane/api.py`` (backlog item A1: split the
1975-line monolith into per-domain routers). Handler bodies are unchanged; the
shared dependencies they used to close over are now bound from
:class:`~..deps.ControlPlaneDeps` as local aliases.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status

from ...platform_contracts.enums import RunType
from ...platform_contracts.enums import WorkflowStatus
from ...platform_contracts.events import utc_now
from ...platform_contracts.objects import WorkflowRun
from ..schemas import DualLoopRequest
from ..service import ConflictError
from ..service import NotFoundError
from ..progress_bus import progress_bus as _progress_bus
from ..deps import ControlPlaneDeps
from ..deps import _map_summary_status
from ..deps import _spawn_bg_thread
from ..deps import acquire_run_slot
from ..deps import release_run_slot
from ..deps import translate_exc as _translate


def build_loops_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the dual-loop & MEA router bound to *deps*."""

    router = APIRouter()

    # --- local aliases: keep handler bodies byte-identical to the old closures ---
    svc = deps.svc
    orchestrator = deps.orchestrator
    _run_cancel_events = deps.run_cancel_events
    _shutdown_event = deps.shutdown_event

    @router.post(
        "/workflow-runs/{run_id}/dual-loop",
        summary="Run the dual loop (inner research -> outer audit -> recursive improvement)",
    )
    def run_dual_loop_endpoint(run_id: str, req: DualLoopRequest) -> dict:
        # R1 fix: register the cancel event *synchronously*, before the worker thread is
        # spawned. Previously ``_run_cancel_events.get(run_id)`` was read inside the
        # thread and returned ``None`` (nothing ever pre-registered the key), so
        # ``POST /workflow-runs/{id}/cancel`` was a silent no-op for every loop endpoint.
        cancel_ev = acquire_run_slot(_run_cancel_events, run_id)
        if cancel_ev is None:
            # R8 fix: single-flight. A second POST for the same run used to spawn a
            # second driver thread racing the first one on the same StageRuns/metrics.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run {run_id} 已有正在执行的循环；请先取消（POST /workflow-runs/{run_id}/cancel）",
            )

        # 缺陷2 fix: background the loop; wire cancel_event; finalize status + capture record.
        def _drive() -> None:
            if _shutdown_event.is_set():
                release_run_slot(_run_cancel_events, run_id)
                return
            try:
                # Ensure the run is running so stage runs can be created.
                try:
                    svc.start_workflow_run(run_id)
                except (ConflictError, ValueError):
                    pass  # already running / terminal — proceed
                inner_params = dict(req.inner_params) or {
                    "preset": "titanic",
                    "model": "gbm",
                    "data_dir": os.path.join(
                        os.path.dirname(__file__), "..", "data", "kaggle", "titanic"
                    ),
                    "cv_folds": 5,
                    "threshold": 0.82,
                }
                summary = orchestrator.run_dual_loop(
                    run_id,
                    inner_capability=req.inner_capability,
                    inner_params=inner_params,
                    audit_params=req.audit_params or {"threshold": 0.8},
                    max_outer_iters=req.max_outer_iters,
                    agent_inner=req.agent_inner,
                    cancel_event=cancel_ev,
                    progress_callback=_progress_bus.emit,
                )
                svc.set_run_status(run_id, _map_summary_status(summary.get("status", "exited_budget")))
                # 自动入库"最优自主研究记录"。
                try:
                    svc.capture_run_record(run_id)
                except Exception:
                    logging.exception("capture_run_record failed for run=%s", run_id)
            except Exception:
                logging.exception("dual loop failed for run=%s", run_id)
                try:
                    svc.set_run_status(run_id, "failed")
                except Exception:
                    logging.exception("set_run_status('failed') failed for run=%s", run_id)
            finally:
                # R3 fix: this cleanup used to be nested inside the ``except`` block, so
                # it never ran on the (most common) success path — pending meta-loop
                # strategies leaked into the next run and the cancel event was never
                # released. It now runs unconditionally.
                try:
                    orchestrator.expire_pending_strategies(run_id)
                except Exception:
                    logging.exception("expire_pending_strategies failed for run=%s", run_id)
                finally:
                    release_run_slot(_run_cancel_events, run_id)  # 缺陷10: always release the cancel event once the run ends

        _spawn_bg_thread(_drive, name=f"dual-loop-{run_id}")
        return {"run_id": run_id, "status": "running", "accepted": True}

    # ----- Evolutionary search (Phase 3): parallel population -> audit champion -----
    @router.post(
        "/workflow-runs/{run_id}/mea",
        summary="Run the Manage-Execute-Audit loop with per-step optimal agents",
    )
    def run_mea_endpoint(
        run_id: str,
        role_spec: str | None = None,
        objective: str | None = None,
        max_rounds: int = 25,
    ) -> dict:
        # R1 fix: pre-register the cancel event before spawning the worker (see the
        # dual-loop endpoint above for the full rationale).
        cancel_ev = acquire_run_slot(_run_cancel_events, run_id)
        if cancel_ev is None:
            # R8 fix: single-flight. A second POST for the same run used to spawn a
            # second driver thread racing the first one on the same StageRuns/metrics.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run {run_id} 已有正在执行的循环；请先取消（POST /workflow-runs/{run_id}/cancel）",
            )

        # 缺陷2 fix: background the loop; wire cancel_event; finalize status.
        def _drive() -> None:
            if _shutdown_event.is_set():
                release_run_slot(_run_cancel_events, run_id)
                return
            try:
                # Create-or-start: honor the client run_id so the TaskState / MEA keys
                # match. start_workflow_run raises NotFound for a missing run, so we
                # materialize it first (with the requested id) instead of crashing.
                try:
                    svc.get_workflow_run(run_id)
                except NotFoundError:
                    svc._repo.put_workflow_run(
                        WorkflowRun(
                            run_id=run_id,
                            program_id="mea",
                            run_type=RunType.STANDARD_RESEARCH,
                            entry_stage="00_agent_orchestration",
                            target_id=run_id,
                            objective_snapshot={"goal": objective or run_id},
                            status=WorkflowStatus.REQUESTED,
                            started_at=utc_now(),
                        )
                    )
                try:
                    svc.start_workflow_run(run_id)
                except (ConflictError, ValueError):
                    pass  # already running / terminal — proceed

                # R26 fix: this used to be named ``status``, shadowing the imported
                # ``fastapi.status`` module for the WHOLE closure — any later use of
                # ``status.HTTP_*`` inside _drive would raise UnboundLocalError.
                loop_status = orchestrator.run_mea_loop(
                    run_id,
                    role_spec=role_spec,
                    max_rounds=max_rounds,
                    objective=objective,
                    cancel_event=cancel_ev,
                )
                svc.set_run_status(run_id, _map_summary_status(loop_status))
            except Exception:
                logging.exception("mea loop failed for run=%s", run_id)
                try:
                    svc.set_run_status(run_id, "failed")
                except Exception:
                    logging.exception("set_run_status('failed') failed for run=%s", run_id)
            finally:
                # R3 fix: unconditional cleanup (used to be nested in ``except``).
                try:
                    orchestrator.expire_pending_strategies(run_id)
                except Exception:
                    logging.exception("expire_pending_strategies failed for run=%s", run_id)
                finally:
                    release_run_slot(_run_cancel_events, run_id)  # 缺陷10: always release the cancel event once the run ends

        _spawn_bg_thread(_drive, name=f"mea-{run_id}")
        return {"run_id": run_id, "status": "running", "accepted": True}

    @router.get(
        "/workflow-runs/{run_id}/mea/state",
        summary="Trusted TaskState for a MEA run (verified facts only)",
    )
    def mea_state(run_id: str) -> dict:
        from ...control_plane.task_state import TaskState

        try:
            svc.get_workflow_run(run_id)
            state = TaskState.load(run_id)
            if state is None:
                return {"run_id": run_id, "exists": False}
            return {"run_id": run_id, "exists": True, **state.model_dump()}
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual-loop observability: HypothesisTree snapshot -----

    return router
