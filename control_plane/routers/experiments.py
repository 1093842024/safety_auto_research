"""Stage-isolated debug, full-experiment launch and human-collaboration resolution.

Extracted verbatim from ``control_plane/api.py`` (backlog item A1: split the
1975-line monolith into per-domain routers). Handler bodies are unchanged; the
shared dependencies they used to close over are now bound from
:class:`~..deps.ControlPlaneDeps` as local aliases.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status

from ...platform_contracts.objects import StageRun
from ..schemas import DebugRequest
from ..schemas import ResolveApprovalRequest
from ..schemas import ResolveCollaborationRequest
from ..service import ConflictError
from ..deps import ControlPlaneDeps
from ..deps import _spawn_bg_thread
from ..deps import translate_exc as _translate


def build_experiments_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the experiment router bound to *deps*."""

    router = APIRouter()

    # --- local aliases: keep handler bodies byte-identical to the old closures ---
    svc = deps.svc
    state_store = deps.state_store
    orchestrator = deps.orchestrator
    _shutdown_event = deps.shutdown_event
    _build_run_ctx = deps.build_run_ctx
    _spawn_full_experiment = deps.spawn_full_experiment

    def _fail_run(run_id: str, msg: str) -> None:
        """Mark a run failed without ever turning a *validation* answer into a 500.

        R14 fix: these pre-flight rejections (agent CLI missing, no remote agent)
        carry a human-readable Chinese reason that the user must see. Previously a
        rejected status transition inside ``set_run_status`` bubbled out as HTTP
        500 and the message was lost. The run status is best-effort; the message
        is not.
        """
        try:
            svc.set_run_status(run_id, "failed", detail=msg)
        except Exception:
            logging.exception("could not mark run=%s failed (%s)", run_id, msg)

    @router.post(
        "/workflow-runs/{run_id}/debug",
        summary="Debug one stage (inner/outer) of the dual loop in isolation",
    )
    def debug_run(run_id: str, body: DebugRequest) -> dict:
        try:
            run = svc.get_workflow_run(run_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")
        try:
            ctx = _build_run_ctx(run)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        active_orch = ctx["active_orchestrator"]
        stage = body.stage
        if stage not in ("inner", "outer"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="stage must be 'inner' or 'outer'")

        def _debug_thread() -> None:
            if _shutdown_event.is_set():
                return
            from ...execution_plane.sdk import PlatformSDK
            from ...platform_contracts.events import DebugEvent
            sdk = PlatformSDK(svc, state_store=state_store)
            # StageRun creation requires the workflow to be RUNNING.
            started_here = False
            try:
                svc.start_workflow_run(run_id)
                started_here = True
            except (ConflictError, ValueError):
                pass  # already running / terminal
            try:
                if stage == "inner":
                    if ctx["agent_mode"]:
                        goal = run.objective_snapshot.get("goal", "") if run.objective_snapshot else ""
                        _, result = active_orch.dispatch_open_goal(
                            run_id, goal,
                            agent_config=ctx.get("inner_agent_config"),
                        )
                    else:
                        _, result = active_orch.run_capability(
                            run_id, "kaggle_eval", ctx["inner_params"]
                        )
                    ev = result.event
                    metrics = dict(ev.metrics) if ev and getattr(ev, "metrics", None) else {}
                    ok = bool(ev and getattr(ev, "passed", True))
                    report = getattr(ev, "report_ref", None) if ev else None
                    detail = result.detail
                    summary = f"内循环调试完成 | accuracy={metrics.get('accuracy','?')} | ok={ok}"
                    sdk.emit_event(DebugEvent(
                        run_id=run_id, stage="inner", ok=ok, summary=summary,
                        metrics=metrics, report_ref=report, detail=detail,
                    ))
                else:  # outer
                    events = svc.list_events(run_id)
                    last_inner = None
                    for e in reversed(events):
                        if e.get("event_type") == "eval_completed":
                            last_inner = e
                            break
                    audit_input = body.audit_input_override
                    if audit_input is None:
                        if last_inner is None:
                            sdk.emit_event(DebugEvent(
                                run_id=run_id, stage="outer", ok=False,
                                summary="未找到内循环结果，请先调试内循环",
                                error="no inner result found",
                            ))
                            return
                        audit_input = {
                            "objective": run.objective_snapshot.get("goal", "") if run.objective_snapshot else "",
                            "result_metrics": last_inner.get("metrics", {}),
                            "result_report_ref": last_inner.get("report_ref"),
                            "result_gate_passed": last_inner.get("gate_passed", True),
                            "result_real_eval": "kaggle" in str(last_inner.get("report_ref", "") or ""),
                            "prior_audits": [],
                            "constraints": [],
                        }
                    _, result = active_orch.run_capability(
                        run_id, "layer_11_external_audit",
                        {"objective": audit_input.get("objective", ""), "audit_input": audit_input, "threshold": ctx["audit_threshold"]},
                    )
                    ev = result.event
                    ok = bool(ev and getattr(ev, "gate_passed", True))
                    verdict = {
                        "confidence": getattr(ev, "confidence", 0) if ev else 0,
                        "recoverable": getattr(ev, "recoverable", True) if ev else True,
                        "gate_passed": getattr(ev, "gate_passed", True) if ev else True,
                        "unresolved": list(getattr(ev, "unresolved_claims", []) if ev else []),
                        "rejected": list(getattr(ev, "rejected_candidates", []) if ev else []),
                    } if ev else None
                    summary = f"外审计调试完成 | confidence={verdict.get('confidence',0):.2f} | ok={ok}" if verdict else "外审计调试完成"
                    sdk.emit_event(DebugEvent(
                        run_id=run_id, stage="outer", ok=ok, summary=summary,
                        verdict=verdict, detail=result.detail,
                    ))
            except Exception as exc:
                logging.exception("debug failed for run=%s stage=%s", run_id, stage)
                sdk.emit_event(DebugEvent(
                    run_id=run_id, stage=stage, ok=False,
                    summary=f"调试失败: {exc}", error=str(exc),
                ))
            finally:
                # R13 fix: give back the RUNNING state we only borrowed to create
                # the debug StageRun; otherwise the run list keeps showing an idle
                # run as "运行中" forever.
                if started_here:
                    try:
                        svc.end_debug_session(run_id)
                    except Exception:
                        logging.exception("failed to release debug session run=%s", run_id)

        _spawn_bg_thread(_debug_thread, name=f"debug-{run_id}-{stage}")
        return {"run_id": run_id, "stage": stage, "status": "debugging"}

    # ------------------------------------------------------------------ #
    # Run full experiment (for existing requested runs)                  #
    # ------------------------------------------------------------------ #
    @router.post(
        "/workflow-runs/{run_id}/run-experiment",
        summary="Start the full autonomous experiment (dual loop) on an existing requested run",
    )
    def start_full_experiment(run_id: str) -> dict:
        try:
            run = svc.get_workflow_run(run_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")
        try:
            ctx = _build_run_ctx(run)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

        # Check agent availability for agent-mode runs.
        agent_mode = ctx["agent_mode"]
        if agent_mode:
            obj = run.objective_snapshot or {}
            cfg0 = obj.get("config", {}) or {}
            agent_cli = cfg0.get("agent_cli") or obj.get("agent_cli")
            if agent_cli in ("codex", "claude_code"):
                cli_bin = "codex" if agent_cli == "codex" else os.environ.get("CLAUDE_CMD", "claude")
                if shutil.which(cli_bin) is None:
                    msg = f"agent 模式选择 {agent_cli}，但在 PATH 中找不到 {cli_bin}，已终止。"
                    _fail_run(run_id, msg)
                    return {"run_id": run_id, "status": "failed", "message": msg}
            elif not orchestrator.has_real_agent():
                msg = "内循环 agent 模式需要接入远程 Agent，当前环境未配置，已终止。请设置环境变量 AGENT_COMMAND 或选择 codex/claude_code。"
                _fail_run(run_id, msg)
                return {"run_id": run_id, "status": "failed", "message": msg}

        _spawn_full_experiment(run, ctx)
        return {"run_id": run_id, "status": "running", "collaboration_mode": ctx.get("collaboration_mode", "autonomous")}

    # ------------------------------------------------------------------ #
    # Human-in-the-loop collaboration: resolve a paused step             #
    # ------------------------------------------------------------------ #
    @router.post(
        "/workflow-runs/{run_id}/resolve-collaboration",
        summary="Resolve a collaboration pause (approve+adjust / reject) to resume the loop",
    )
    def resolve_collaboration(run_id: str, body: ResolveCollaborationRequest) -> dict:
        from ...execution_plane.orchestrator import _collab_lock as _orch_lock
        from ...execution_plane.orchestrator import _collab_pauses as _orch_pauses

        pauses = _orch_pauses()
        lock = _orch_lock()
        # Carry adjustments into the shared state BEFORE resolving the approval, so the
        # waiting background thread sees them once the run transitions back to RUNNING.
        with lock:
            if run_id in pauses:
                ctx = pauses[run_id]
                ctx["adjustments"] = body.adjustments.model_dump() if body.adjustments else {}
                ctx["rejected"] = (body.resolution != "approved")

        try:
            svc.resolve_approval(
                run_id,
                ResolveApprovalRequest(
                    resolution=body.resolution,
                    resolved_by=body.reviewer,
                ),
            )
        except Exception as exc:
            raise _translate(exc)
        finally:
            # 缺陷5 fix: always release the paused background loop thread, even if
            # approval resolution failed — otherwise it deadlocks on evt.wait().
            with lock:
                if run_id in pauses:
                    pauses[run_id].get("evt", threading.Event()).set()

        return {"status": "ok", "run_id": run_id, "resolution": body.resolution}

    return router
