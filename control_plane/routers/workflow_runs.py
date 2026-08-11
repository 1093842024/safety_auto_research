"""WorkflowRun, StageRun, DecisionRecord, events, agent protocol and capability routes.

Extracted verbatim from ``control_plane/api.py`` (backlog item A1: split the
1975-line monolith into per-domain routers). Handler bodies are unchanged; the
shared dependencies they used to close over are now bound from
:class:`~..deps.ControlPlaneDeps` as local aliases.
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status
from fastapi.responses import StreamingResponse

from ...platform_contracts.objects import DecisionRecord
from ...platform_contracts.objects import StageRun
from ...platform_contracts.objects import WorkflowRun
from ..schemas import CapabilityRunRequest
from ..schemas import CreateStageRunRequest
from ..schemas import CreateWorkflowRunRequest
from ..schemas import DispatchRequest
from ..schemas import MessageResponse
from ..schemas import RecordDecisionRequest
from ..schemas import RequestApprovalRequest
from ..schemas import ResolveApprovalRequest
from ..schemas import UpdateStageStatusRequest
from ..progress_bus import progress_bus as _progress_bus
from ..service import ConflictError
from ..deps import ControlPlaneDeps
from ..deps import _map_summary_status
from ..deps import _spawn_bg_thread
from ..deps import acquire_run_slot
from ..deps import release_run_slot
from ..deps import translate_exc as _translate


def build_workflow_runs_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the workflow-run / stage / decision / event / capability router bound to *deps*."""

    router = APIRouter()

    # --- local aliases: keep handler bodies byte-identical to the old closures ---
    svc = deps.svc
    orchestrator = deps.orchestrator
    _run_cancel_events = deps.run_cancel_events
    _shutdown_event = deps.shutdown_event

    @router.post(
        "/workflow-runs",
        response_model=WorkflowRun,
        status_code=status.HTTP_201_CREATED,
        summary="Create a workflow run (status=requested)",
    )
    def create_workflow_run(req: CreateWorkflowRunRequest) -> WorkflowRun:
        try:
            return svc.create_workflow_run(req)
        except Exception as exc:  # pragma: no cover - defensive
            raise _translate(exc)

    @router.post(
        "/workflow-runs/{run_id}/start",
        response_model=WorkflowRun,
        summary="Start a workflow run (requested -> running)",
    )
    def start_workflow_run(run_id: str) -> WorkflowRun:
        try:
            return svc.start_workflow_run(run_id)
        except Exception as exc:
            raise _translate(exc)

    @router.post(
        "/workflow-runs/{run_id}/cancel",
        response_model=WorkflowRun,
        summary="Cancel a workflow run (-> cancelled)",
    )
    def cancel_workflow_run(run_id: str) -> WorkflowRun:
        try:
            # Signal any running background thread to abort at the next checkpoint.
            # R8 fix: use ``get`` — the old ``setdefault(...).set()`` INSERTED an entry
            # for runs with no live driver, and since the entry is only released by a
            # driver's finally, that orphan would permanently block the run from being
            # started again (409 forever) under the new single-flight guard. Drivers
            # now always pre-register their event synchronously (R1), so a cancel that
            # can actually do something always finds it.
            ev = _run_cancel_events.get(run_id)
            if ev is not None:
                ev.set()
            return svc.cancel_workflow_run(run_id)
        except Exception as exc:
            raise _translate(exc)

    # ------------------------------------------------------------------ #
    # Server-Sent Events: live progress stream for the frontend           #
    # ------------------------------------------------------------------ #

    @router.get(
        "/workflow-runs/{run_id}/stream",
        summary="SSE stream of dual-loop progress events for a run",
    )
    async def stream_progress(run_id: str):
        """Server-Sent Events endpoint. Connect with EventSource in the
        frontend to receive live dual-loop progress without polling."""
        import asyncio as _asyncio

        # F1 fix: capture the serving event loop so background _drive threads can
        # publish progress events (they cannot discover it from their own thread).
        _progress_bus.set_loop(_asyncio.get_running_loop())
        return StreamingResponse(
            _progress_bus.subscribe(run_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get(
        "/workflow-runs",
        response_model=list[WorkflowRun],
        summary="List workflow runs",
    )
    def list_workflow_runs() -> list[WorkflowRun]:
        return svc.list_workflow_runs()

    @router.get(
        "/workflow-runs/{run_id}",
        response_model=WorkflowRun,
        summary="Get a workflow run",
    )
    def get_workflow_run(run_id: str) -> WorkflowRun:
        try:
            return svc.get_workflow_run(run_id)
        except Exception as exc:
            raise _translate(exc)

    # ----- Approval (HITL) -----
    @router.post(
        "/workflow-runs/{run_id}/request-approval",
        response_model=MessageResponse,
        summary="Open a HITL approval gate (running -> waiting_approval)",
    )
    def request_approval(run_id: str, req: RequestApprovalRequest) -> MessageResponse:
        try:
            _run, approval_id = svc.request_approval(run_id, req)
            return MessageResponse(
                detail="approval required",
                run_id=run_id,
                approval_id=approval_id,
            )
        except Exception as exc:
            raise _translate(exc)

    @router.post(
        "/workflow-runs/{run_id}/resolve-approval",
        response_model=MessageResponse,
        summary="Resolve an open approval (approved -> running | rejected -> failed)",
    )
    def resolve_approval(run_id: str, req: ResolveApprovalRequest) -> MessageResponse:
        try:
            _run, approval_id = svc.resolve_approval(run_id, req)
            return MessageResponse(
                detail=f"approval {req.resolution}",
                run_id=run_id,
                approval_id=approval_id,
            )
        except Exception as exc:
            raise _translate(exc)

    # ----- StageRun -----
    @router.post(
        "/workflow-runs/{run_id}/stages",
        response_model=StageRun,
        status_code=status.HTTP_201_CREATED,
        summary="Create a stage run (status=queued)",
    )
    def create_stage_run(run_id: str, req: CreateStageRunRequest) -> StageRun:
        try:
            return svc.create_stage_run(run_id, req)
        except Exception as exc:
            raise _translate(exc)

    @router.post(
        "/stages/{stage_run_id}/status",
        response_model=StageRun,
        summary="Update a stage run status (validated transition)",
    )
    def update_stage_status(stage_run_id: str, req: UpdateStageStatusRequest) -> StageRun:
        try:
            return svc.update_stage_status(stage_run_id, req)
        except Exception as exc:
            raise _translate(exc)

    @router.get(
        "/stages/{stage_run_id}",
        response_model=StageRun,
        summary="Get a stage run",
    )
    def get_stage_run(stage_run_id: str) -> StageRun:
        try:
            return svc.get_stage_run(stage_run_id)
        except Exception as exc:
            raise _translate(exc)

    @router.get(
        "/workflow-runs/{run_id}/stages",
        response_model=list[StageRun],
        summary="List stage runs for a workflow",
    )
    def list_stage_runs(run_id: str) -> list[StageRun]:
        try:
            return svc.list_stage_runs(run_id)
        except Exception as exc:
            raise _translate(exc)

    # ----- DecisionRecord -----
    @router.post(
        "/workflow-runs/{run_id}/decisions",
        response_model=DecisionRecord,
        status_code=status.HTTP_201_CREATED,
        summary="Record a control-plane decision",
    )
    def record_decision(run_id: str, req: RecordDecisionRequest) -> DecisionRecord:
        try:
            return svc.record_decision(run_id, req)
        except Exception as exc:
            raise _translate(exc)

    @router.get(
        "/workflow-runs/{run_id}/decisions",
        response_model=list[DecisionRecord],
        summary="List decisions for a workflow",
    )
    def list_decisions(run_id: str) -> list[DecisionRecord]:
        try:
            return svc.list_decisions(run_id)
        except Exception as exc:
            raise _translate(exc)

    @router.get(
        "/decisions/{decision_id}",
        response_model=DecisionRecord,
        summary="Get a decision",
    )
    def get_decision(decision_id: str) -> DecisionRecord:
        try:
            return svc.get_decision(decision_id)
        except Exception as exc:
            raise _translate(exc)

    # ----- Event log -----
    @router.get(
        "/events",
        summary="List platform events (optionally filtered by run_id)",
    )
    def list_events(run_id: str | None = None) -> list[dict]:
        return svc.list_events(run_id)

    # ----- Agent contract: the wire protocol an external agent speaks -----
    @router.get(
        "/agent/protocol",
        summary="Agent<->platform wire-protocol schemas (for Codex / WorkBuddy integration)",
    )
    def agent_protocol() -> dict:
        # Local import keeps the control_plane -> execution_plane coupling lazy and
        # avoids a module-load cycle (execution_plane already imports control_plane).
        from ...execution_plane.agent.harness import AGENT_TOOL_NAMES
        from ...execution_plane.agent.protocol import all_schemas
        from ...execution_plane.capabilities.registry import default_capability_registry

        return {
            "message_types": sorted(all_schemas().keys()),
            "schemas": all_schemas(),
            "agent_tools": list(AGENT_TOOL_NAMES),
            "capabilities": default_capability_registry().list_all_capabilities(),
            "tool_reference": (
                "load_object(ref) | publish_artifact(payload, schema_version, metadata) | "
                "emit_event(event) | request_approval(run_id, payload) | "
                "record_metric(run_id, name, value, tags) | register_lesson(payload) | "
                "run_capability(run_id, capability_id, params)"
            ),
        }

    # ----- Execution plane: dispatch a single stage via its adapter -----
    @router.post(
        "/workflow-runs/{run_id}/dispatch",
        response_model=MessageResponse,
        summary="Dispatch one stage to its execution-plane adapter (emits event + auto-decision)",
    )
    def dispatch_stage(run_id: str, req: DispatchRequest) -> MessageResponse:
        try:
            stage, result = orchestrator.dispatch_stage(
                run_id, req.stage_code, req.params, req.executor_family
            )
            event_id = result.event.event_id if result.event else None
            decision_id = None
            if result.event is not None:
                _route, decision = orchestrator.decide_and_record(run_id, result.event)
                decision_id = decision.decision_id
            return MessageResponse(
                detail=f"dispatched {req.stage_code}: {result.detail}",
                run_id=run_id,
                stage_run_id=stage.stage_run_id,
                decision_id=decision_id,
                event_id=event_id,
            )
        except Exception as exc:
            raise _translate(exc)

    # ----- Execution plane: run one infrastructure-layer capability (agent tool) -----
    @router.post(
        "/workflow-runs/{run_id}/capabilities/{capability_id}/run",
        response_model=MessageResponse,
        summary="Run an infrastructure-layer capability (the agent-facing run_capability tool)",
    )
    def run_capability(run_id: str, capability_id: str, req: CapabilityRunRequest) -> MessageResponse:
        try:
            # 缺陷1 fix: enforce isolation invariant (2) at the EXTERNAL HTTP surface.
            # The control plane internally drives the reserved outer-loop capabilities
            # (layer_11_external_audit / layer_09_self_iterative_evolution) via
            # orchestrator.run_capability — that trusted path must stay open. But an
            # external client calling this tool endpoint must NEVER be able to run the
            # audit or self-evolution on its own result (destroys evaluation-bias /
            # self-confirmation guarantees). The agent tool path is already guarded by
            # RemoteAgentHarness._tool_handler; this closes the remaining HTTP gap.
            from ...execution_plane.agent.harness import assert_inner_capability_allowed

            assert_inner_capability_allowed(capability_id)
            stage, result = orchestrator.run_capability(run_id, capability_id, req.params)
            return MessageResponse(
                detail=result.detail,
                run_id=run_id,
                stage_run_id=stage.stage_run_id,
                event_id=result.event.event_id if result.event else None,
            )
        except Exception as exc:
            raise _translate(exc)

    # ----- Execution plane: run the full 评测→红队→经验 closed loop -----
    @router.post(
        "/workflow-runs/{run_id}/closed-loop",
        summary="Run the 评测→决策 / 红队→决策 / 经验→回注 closed loop",
    )
    def run_closed_loop(run_id: str, max_rounds: int = 4, auto_loop: bool = True) -> dict:
        # R1 fix: pre-register the cancel event synchronously so ``POST .../cancel``
        # reaches this loop (``.get(run_id)`` inside the thread always returned None).
        cancel_ev = acquire_run_slot(_run_cancel_events, run_id)
        if cancel_ev is None:
            # R8 fix: single-flight. A second POST for the same run used to spawn a
            # second driver thread racing the first one on the same StageRuns/metrics.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run {run_id} 已有正在执行的循环；请先取消（POST /workflow-runs/{run_id}/cancel）",
            )

        # 缺陷2 fix: run in a background thread so the HTTP worker is not blocked for the
        # (potentially hours-long) loop; wire cancel_event so POST .../cancel is honored
        # and finalize the run status on exit.
        def _drive() -> None:
            if _shutdown_event.is_set():
                release_run_slot(_run_cancel_events, run_id)
                return
            try:
                # R4 fix: this endpoint was the only loop driver that never transitioned
                # the run out of REQUESTED. ``create_stage_run`` rejects stages on a
                # non-running run, so a freshly created run failed on its first stage.
                try:
                    svc.start_workflow_run(run_id)
                except (ConflictError, ValueError):
                    pass  # already running / terminal — proceed
                summary = orchestrator.run_closed_loop(
                    run_id,
                    max_rounds=max_rounds,
                    auto_loop=auto_loop,
                    cancel_event=cancel_ev,
                )
                svc.set_run_status(run_id, _map_summary_status(summary.get("status", "exited_budget")))
            except Exception:
                logging.exception("closed loop failed for run=%s", run_id)
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

        _spawn_bg_thread(_drive, name=f"closed-loop-{run_id}")
        return {"run_id": run_id, "status": "running", "accepted": True}

    # ----- Lessons promoted for a run (reinjection objects) -----
    @router.get(
        "/workflow-runs/{run_id}/lessons",
        summary="List promoted LessonCards (reinjection objects) for a run",
    )
    def list_lessons(run_id: str) -> list[dict]:
        try:
            return [l.model_dump(mode="json") for l in svc._repo.list_lessons(run_id)]
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual loop: inner research -> outer audit -> recursive improvement -----

    return router
