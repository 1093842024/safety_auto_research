from __future__ import annotations

import logging
import os
import shutil
import threading

from fastapi import FastAPI
from fastapi import File
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi import status

from ..platform_contracts.objects import DecisionRecord
from ..platform_contracts.objects import StageRun
from ..platform_contracts.objects import WorkflowRun
from .schemas import CapabilityRunRequest
from .schemas import CreateStageRunRequest
from .schemas import CreateWorkflowRunRequest
from .schemas import DebugRequest
from .schemas import DispatchRequest
from .schemas import DualLoopRequest
from .schemas import EvaluateRunRequest
from .schemas import InnerLoopConfig
from .schemas import LaunchBenchmarkRequest
from .schemas import MessageResponse
from .schemas import RecordDecisionRequest
from .schemas import RegisterTaskRequest
from .schemas import ReportMetricRequest
from .schemas import RequestApprovalRequest
from .schemas import ResolveApprovalRequest
from .schemas import ResolveCollaborationRequest
from .schemas import UpdateStageStatusRequest
from .schemas import ValidateTaskRequest
from .service import ConflictError
from .service import ControlPlaneService
from .service import NotFoundError
from .store_tree import ResearchStateStore
from ..benchmark_tasks import get_catalog
from ..benchmark_tasks import get_task
from ..benchmark_tasks import _default_eval_method as _get_eval_method_desc
from ..benchmark_tasks import to_dict

_shutdown_event = threading.Event()


def create_app(service: ControlPlaneService | None = None) -> FastAPI:
    """Build the FastAPI application for the unified control plane."""

    app = FastAPI(
        title="Safety R&D Unified Control Plane",
        version="0.1.0",
        description=(
            "Minimal control-plane API wiring WorkflowRun / StageRun / DecisionRecord "
            "into a validated state machine. See platform_contracts for the canonical schemas."
        ),
    )
    svc = service or ControlPlaneService()

    @app.on_event("shutdown")
    def _on_shutdown():
        _shutdown_event.set()

    from ..execution_plane.orchestrator import ClosedLoopOrchestrator

    # Shared cumulative-state store (dual-loop): the orchestrator threads it into the
    # SDK so hypothesis-tree / experience-bank / strategy-archive persist per app.
    state_store = ResearchStateStore()
    orchestrator = ClosedLoopOrchestrator(svc, state_store=state_store)

    # When the app builds its own service (real launch, not test injection) we enable
    # on-disk persistence so research records survive restarts: the Repository is backed
    # by a JSON file and the dual-loop ResearchStateStore by a SQLite db. Both reload on
    # the next startup. Paths are overridable via env vars and default under data/.
    if service is None:
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = os.path.join(pkg_root, "data")
        os.makedirs(data_dir, exist_ok=True)
        repo_path = os.environ.get(
            "CONTROL_PLANE_STORE", os.path.join(data_dir, "control_plane_store.json")
        )
        state_path = os.environ.get(
            "RESEARCH_STATE_DB", os.path.join(data_dir, "research_state.db")
        )
        svc = ControlPlaneService(store_path=repo_path)
        state_store = ResearchStateStore(db_path=state_path)
        orchestrator = ClosedLoopOrchestrator(svc, state_store=state_store)

    # Remote-agent wiring (Tasks 1 & 4): if AGENT_COMMAND is set, plug a real external
    # agent so agent-mode inner loops can actually execute. Without it, agent mode fails
    # loudly instead of silently falling back to the scripted executor.
    agent_command = os.environ.get("AGENT_COMMAND")
    if agent_command:
        from ..execution_plane.agent.harness import RemoteAgentHarness

        orchestrator = ClosedLoopOrchestrator(
            svc, state_store=state_store, mode="agent", harness=RemoteAgentHarness(agent_command=agent_command)
        )

    def _make_agent_orchestrator(agent_cli: str) -> "ClosedLoopOrchestrator":
        """Build a per-run orchestrator wired to the chosen agent CLI.

        * ``"codex"``      -> ``RemoteAgentHarness(CodexTransport(...))``
        * ``"claude_code"``-> ``RemoteAgentHarness(ClaudeCodeTransport(...))``
        * ``"auto"`` / None-> the global ``orchestrator`` only if it already has a real
          remote agent (i.e. ``AGENT_COMMAND`` was set); otherwise raise so the caller can
          fail the run loudly instead of silently running scripted.

        The transport's ``capability_runner`` is bound to the global orchestrator's
        ``run_capability`` (capabilities are stateless w.r.t. which orchestrator instance,
        so this is safe) and every agent step is streamed to the run's trace sink by the
        harness.
        """

        if agent_cli == "codex":
            from ..execution_plane.agent.harness import RemoteAgentHarness
            from ..execution_plane.agent.transport import CodexTransport

            transport = CodexTransport(capability_runner=orchestrator.run_capability)
            return ClosedLoopOrchestrator(
                svc, state_store=state_store, mode="agent", harness=RemoteAgentHarness(transport=transport)
            )
        if agent_cli == "claude_code":
            from ..execution_plane.agent.harness import RemoteAgentHarness
            from ..execution_plane.agent.transport import ClaudeCodeTransport

            transport = ClaudeCodeTransport(capability_runner=orchestrator.run_capability)
            return ClosedLoopOrchestrator(
                svc, state_store=state_store, mode="agent", harness=RemoteAgentHarness(transport=transport)
            )
        # auto / None: reuse a globally-wired real agent if present.
        if orchestrator.has_real_agent():
            return orchestrator
        raise RuntimeError("no real remote agent configured (set AGENT_COMMAND)")

    def _translate(exc: Exception) -> HTTPException:
        if isinstance(exc, NotFoundError):
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        if isinstance(exc, ConflictError):
            return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        raise exc

    # ----- WorkflowRun -----
    @app.post(
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

    @app.post(
        "/workflow-runs/{run_id}/start",
        response_model=WorkflowRun,
        summary="Start a workflow run (requested -> running)",
    )
    def start_workflow_run(run_id: str) -> WorkflowRun:
        try:
            return svc.start_workflow_run(run_id)
        except Exception as exc:
            raise _translate(exc)

    @app.post(
        "/workflow-runs/{run_id}/cancel",
        response_model=WorkflowRun,
        summary="Cancel a workflow run (-> cancelled)",
    )
    def cancel_workflow_run(run_id: str) -> WorkflowRun:
        try:
            return svc.cancel_workflow_run(run_id)
        except Exception as exc:
            raise _translate(exc)

    @app.get(
        "/workflow-runs",
        response_model=list[WorkflowRun],
        summary="List workflow runs",
    )
    def list_workflow_runs() -> list[WorkflowRun]:
        return svc.list_workflow_runs()

    @app.get(
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
    @app.post(
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

    @app.post(
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
    @app.post(
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

    @app.post(
        "/stages/{stage_run_id}/status",
        response_model=StageRun,
        summary="Update a stage run status (validated transition)",
    )
    def update_stage_status(stage_run_id: str, req: UpdateStageStatusRequest) -> StageRun:
        try:
            return svc.update_stage_status(stage_run_id, req)
        except Exception as exc:
            raise _translate(exc)

    @app.get(
        "/stages/{stage_run_id}",
        response_model=StageRun,
        summary="Get a stage run",
    )
    def get_stage_run(stage_run_id: str) -> StageRun:
        try:
            return svc.get_stage_run(stage_run_id)
        except Exception as exc:
            raise _translate(exc)

    @app.get(
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
    @app.post(
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

    @app.get(
        "/workflow-runs/{run_id}/decisions",
        response_model=list[DecisionRecord],
        summary="List decisions for a workflow",
    )
    def list_decisions(run_id: str) -> list[DecisionRecord]:
        try:
            return svc.list_decisions(run_id)
        except Exception as exc:
            raise _translate(exc)

    @app.get(
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
    @app.get(
        "/events",
        summary="List platform events (optionally filtered by run_id)",
    )
    def list_events(run_id: str | None = None) -> list[dict]:
        return svc.list_events(run_id)

    # ----- Agent contract: the wire protocol an external agent speaks -----
    @app.get(
        "/agent/protocol",
        summary="Agent<->platform wire-protocol schemas (for Codex / WorkBuddy integration)",
    )
    def agent_protocol() -> dict:
        # Local import keeps the control_plane -> execution_plane coupling lazy and
        # avoids a module-load cycle (execution_plane already imports control_plane).
        from ..execution_plane.agent.harness import AGENT_TOOL_NAMES
        from ..execution_plane.agent.protocol import all_schemas
        from ..execution_plane.capabilities.registry import default_capability_registry

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
    @app.post(
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
    @app.post(
        "/workflow-runs/{run_id}/capabilities/{capability_id}/run",
        response_model=MessageResponse,
        summary="Run an infrastructure-layer capability (the agent-facing run_capability tool)",
    )
    def run_capability(run_id: str, capability_id: str, req: CapabilityRunRequest) -> MessageResponse:
        try:
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
    @app.post(
        "/workflow-runs/{run_id}/closed-loop",
        summary="Run the 评测→决策 / 红队→决策 / 经验→回注 closed loop",
    )
    def run_closed_loop(run_id: str, max_rounds: int = 4, auto_loop: bool = True) -> dict:
        try:
            return orchestrator.run_closed_loop(run_id, max_rounds=max_rounds, auto_loop=auto_loop)
        except Exception as exc:
            raise _translate(exc)

    # ----- Lessons promoted for a run (reinjection objects) -----
    @app.get(
        "/workflow-runs/{run_id}/lessons",
        summary="List promoted LessonCards (reinjection objects) for a run",
    )
    def list_lessons(run_id: str) -> list[dict]:
        try:
            return [l.model_dump(mode="json") for l in svc._repo.list_lessons(run_id)]
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual loop: inner research -> outer audit -> recursive improvement -----
    @app.post(
        "/workflow-runs/{run_id}/dual-loop",
        summary="Run the dual loop (inner research -> outer audit -> recursive improvement)",
    )
    def run_dual_loop_endpoint(run_id: str, req: DualLoopRequest) -> dict:
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
            return orchestrator.run_dual_loop(
                run_id,
                inner_capability=req.inner_capability,
                inner_params=inner_params,
                audit_params=req.audit_params or {"threshold": 0.8},
                max_outer_iters=req.max_outer_iters,
                agent_inner=req.agent_inner,
            )
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual-loop observability: HypothesisTree snapshot -----
    @app.get(
        "/workflow-runs/{run_id}/hypo-tree",
        summary="Cumulative HypothesisTree snapshot (Arbor-style cross-round state)",
    )
    def hypo_tree(run_id: str) -> dict:
        try:
            svc.get_workflow_run(run_id)
            return state_store.hypo_tree.snapshot(run_id=run_id)
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual-loop observability: external audit events -----
    @app.get(
        "/workflow-runs/{run_id}/audit",
        summary="External-audit events (AuditCompletedEvent) for a run",
    )
    def list_audit(run_id: str) -> list[dict]:
        try:
            svc.get_workflow_run(run_id)
            return [e for e in svc.list_events(run_id) if e.get("event_type") == "audit_completed"]
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual-loop observability: recursive-improvement events -----
    @app.get(
        "/workflow-runs/{run_id}/improvements",
        summary="Recursive-improvement events (ImprovementAppliedEvent) for a run",
    )
    def list_improvements(run_id: str) -> list[dict]:
        try:
            svc.get_workflow_run(run_id)
            return [e for e in svc.list_events(run_id) if e.get("event_type") == "improvement_applied"]
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual-loop observability: cross-run ExperienceBank -----
    @app.get(
        "/experiences",
        summary="Cross-run ExperienceBank entries (training-free reinjection)",
    )
    def list_experiences() -> list[dict]:
        return [e.model_dump(mode="json") for e in state_store.experience_bank.list_all()]

    # ----- Benchmark task catalog (mined from the OSS projects) -----
    @app.get(
        "/benchmark-tasks",
        summary="List curated benchmark / baseline research tasks (selectable in the frontend)",
    )
    def list_benchmark_tasks() -> list[dict]:
        return [to_dict(t) for t in get_catalog()]

    # ----- Custom task registration (schema-driven) -----
    @app.get(
        "/benchmark-tasks/task-types",
        summary="Task-type specs for the custom-task registration form (schema-driven)",
    )
    def list_task_types() -> list[dict]:
        from ..benchmark_tasks import registry

        return registry.get_task_type_specs()

    @app.post(
        "/benchmark-tasks/register",
        status_code=status.HTTP_201_CREATED,
        summary="Register a custom research task (validated against its task-type spec)",
    )
    def register_benchmark_task(req: RegisterTaskRequest) -> dict:
        from ..benchmark_tasks import registry

        try:
            record = registry.register_custom_task(req.task_type, req.values)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        return registry.custom_task_to_benchmark_dict(record)

    @app.delete(
        "/benchmark-tasks/{task_id}",
        summary="Delete a custom-registered task (curated catalog entries cannot be deleted)",
    )
    def delete_benchmark_task(task_id: str) -> dict:
        from ..benchmark_tasks import registry

        if not task_id.startswith("custom."):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="仅可删除自定义注册任务（task_id 以 custom. 开头）",
            )
        if not registry.delete_custom_task(task_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"custom task {task_id} not found"
            )
        return {"deleted": task_id}

    @app.post(
        "/benchmark-tasks/upload-dataset",
        summary="Upload a dataset file for custom-task registration (csv/jsonl/zip; zip auto-extracts)",
    )
    async def upload_dataset(file: UploadFile = File(...)) -> dict:
        import re as _re
        import time as _time
        import zipfile

        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        upload_root = os.environ.get(
            "CUSTOM_UPLOAD_DIR", os.path.join(pkg_root, "data", "custom_uploads")
        )
        safe = _re.sub(r"[^0-9A-Za-z._-]+", "_", file.filename or "dataset")
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        dest_dir = os.path.join(upload_root, f"{stamp}_{os.path.splitext(safe)[0]}")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, safe)
        content = await file.read()
        if len(content) > 512 * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="上传文件超过 512MB 限制，请改为填写本地数据路径",
            )
        with open(dest, "wb") as fh:
            fh.write(content)
        extracted = False
        if safe.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(dest) as zf:
                    for member in zf.namelist():  # zip-slip guard
                        target = os.path.realpath(os.path.join(dest_dir, member))
                        if not target.startswith(os.path.realpath(dest_dir) + os.sep):
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"zip 内含非法路径: {member}",
                            )
                    zf.extractall(dest_dir)
                extracted = True
                os.remove(dest)
            except zipfile.BadZipFile:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="zip 文件损坏，无法解压"
                )
        return {
            "path": dest_dir if extracted else dest,
            "dir": dest_dir,
            "filename": safe,
            "extracted": extracted,
            "size_bytes": len(content),
        }

    @app.post(
        "/benchmark-tasks/{task_id}/launch",
        summary="Launch a benchmark task as a workflow run (dual loop for platform-native tasks)",
    )
    def launch_benchmark_task(
        task_id: str,
        auto_run: bool = True,
        body: LaunchBenchmarkRequest | None = None,
    ) -> dict:
        task = get_task(task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"benchmark task {task_id} not found")
        from ..platform_contracts.enums import RunType

        cfg = body or LaunchBenchmarkRequest()
        # Fold the legacy top-level model/fe into the richer inner-loop config when the
        # caller did not send one explicitly.
        if cfg.inner_loop is not None:
            inner = cfg.inner_loop
        else:
            inner = InnerLoopConfig(model=cfg.model, fe="rich" if cfg.fe else "basic")

        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Custom-registered platform-executable tasks (tabular_classification) carry
        # their own data_dir / target / threshold in type_config.
        type_cfg = dict(task.type_config or {})
        is_custom_exec = (
            task.task_id.startswith("custom.")
            and task.harness == "kaggle_eval"
            and task.supported_by_platform
        )

        # Resolve the competition preset: explicit inner.preset wins, else derive from the
        # task id (platform.titanic / platform.spaceship), else fall back to titanic.
        if is_custom_exec:
            preset = "custom"
        elif inner.preset:
            preset = inner.preset
        elif task.task_id == "platform.spaceship":
            preset = "spaceship"
        elif task.task_id == "platform.titanic":
            preset = "titanic"
        else:
            preset = "titanic"

        # kaggle_eval expects fe as "basic"/"rich" (string), not a bool.
        fe_value = (inner.fe or "basic").lower()
        if fe_value not in ("basic", "rich"):
            fe_value = "basic"

        if is_custom_exec:
            data_dir = inner.data_dir or str(type_cfg.get("data_dir") or "")
        else:
            data_dir = inner.data_dir or os.path.join(pkg_root, "data", "kaggle", preset)
        # Threshold gate: explicit inner.threshold wins, else the task's own registered
        # threshold (custom tasks), else the competition default.
        if inner.threshold is not None:
            threshold = float(inner.threshold)
        elif is_custom_exec and type_cfg.get("threshold") not in (None, ""):
            threshold = float(type_cfg["threshold"])
        elif preset == "spaceship":
            threshold = 0.80
        else:
            threshold = 0.82

        # ---- Inner-loop execution mode ----
        # Task 4: harness-dependent tasks (anything the platform cannot run natively,
        # e.g. docker / Arbor / Harbor tasks) are executed via the agent mode — the
        # external agent becomes the execution environment and the docker / Arbor
        # harness deps are stripped. Only the task's core info (goal / definition / data
        # / eval method / metrics) is retained and forwarded to the agent (task_spec).
        is_harness_task = not task.supported_by_platform
        agent_mode = (inner.mode == "agent") or is_harness_task

        # Core task spec for agent execution (Task 4): keep only what an agent needs —
        # goal / definition / data / eval method / metrics — and drop docker/Arbor deps.
        eval_method = task.eval_method or _get_eval_method_desc(task)
        goal_text = (
            f"任务：{task.name}（{task.source_project}）。"
            f"目标：优化指标 {task.eval_metric}（{('越高越好' if task.direction == 'higher' else '越低越好')}），"
            f"baseline={task.baseline}，reference={task.reference}。"
            f"评估方式：{eval_method}。"
            f"任务定义与数据：{task.dataset_desc}"
        )
        task_spec = {
            "task_id": task.task_id,
            "name": task.name,
            "source_project": task.source_project,
            "modality": task.modality,
            "goal": goal_text,
            "definition": task.dataset_desc,
            "data": task.dataset_desc,
            "eval_method": eval_method,
            "metrics": {
                "eval_metric": task.eval_metric,
                "direction": task.direction,
                "baseline": task.baseline,
                "reference": task.reference,
                "gates": task.gates,
            },
            "tags": list(task.tags),
            # Original harness info is kept ONLY as a reference note; it is no longer
            # used to execute the task (the agent is the execution environment now).
            "original_harness": task.harness,
            "original_run_command": task.run_command,
        }

        inner_agent_config = (
            {**inner.model_dump(), "task_spec": task_spec, "goal": goal_text}
            if agent_mode
            else None
        )

        req = CreateWorkflowRunRequest(
            program_id="benchmark",
            run_type=RunType.STANDARD_RESEARCH,
            entry_stage="inner_research",
            target_id=task.task_id,
            objective_snapshot={
                "benchmark_task_id": task.task_id,
                "name": task.name,
                "goal": goal_text,
                "source_project": task.source_project,
                "category": task.category,
                "eval_metric": task.eval_metric,
                "direction": task.direction,
                "baseline": task.baseline,
                "reference": task.reference,
                "gates": task.gates,
                "dataset_desc": task.dataset_desc,
                "harness": task.harness,
                "execution_mode": "agent" if agent_mode else "platform",
                "supported_by_platform": task.supported_by_platform,
                "agent_cli": inner.agent_cli or cfg.agent_cli,
                "task_spec": task_spec,
                "config": {
                    "audit_threshold": cfg.audit_threshold,
                    "max_outer_iters": cfg.max_outer_iters,
                    "model": inner.model,
                    "fe": fe_value,
                    "agent_cli": inner.agent_cli or cfg.agent_cli,
                    "inner_loop": inner.model_dump(),
                },
            },
            requested_outcomes=[f"Improve {task.eval_metric} vs baseline ({task.baseline})"],
        )
        run = svc.create_workflow_run(req)

        # ---- Resolve agent CLI + per-run orchestrator (Task 1 & Task 2) ----
        # agent_mode is True for (a) an *explicit* agent-mode request (inner.mode=="agent")
        # or (b) a harness-dependent task (the platform can't run it natively). For case
        # (b) launched *normally* (no real agent wired) the run is recorded as `requested`
        # (tracked-only) and is NOT auto-failed; only an *explicit* agent request with no
        # satisfiable agent fails loudly.
        explicit_agent = (inner.mode == "agent")
        agent_cli = inner.agent_cli or cfg.agent_cli
        run_orchestrator = None
        agent_block_reason: str | None = None

        if agent_mode:
            if agent_cli in ("codex", "claude_code"):
                cli_bin = "codex" if agent_cli == "codex" else os.environ.get("CLAUDE_CMD", "claude")
                if shutil.which(cli_bin) is None:
                    agent_block_reason = (
                        f"agent 模式选择 {agent_cli}，但在 PATH 中找不到 {cli_bin} 可执行文件，已终止。"
                        "请先在环境中安装该 CLI，或改用 auto 模式并设置环境变量 AGENT_COMMAND 接入远程 Agent。"
                    )
                else:
                    run_orchestrator = _make_agent_orchestrator(agent_cli)
            else:  # auto / None
                if orchestrator.has_real_agent():
                    run_orchestrator = orchestrator
                elif explicit_agent:
                    agent_block_reason = (
                        "内循环 agent 模式需要接入远程 Agent（RemoteAgentHarness），当前环境未配置，已终止。"
                        "请设置环境变量 AGENT_COMMAND 接入远程 Agent（Codex / Claude Code CLI），"
                        "或将内循环改为「脚本化」模式（仅平台原生任务可用）。"
                    )
                # else: harness task launched normally without a real agent -> tracked-only.

        if agent_block_reason is not None:
            MSG = agent_block_reason
            svc.start_workflow_run(run.run_id)  # REQUESTED -> RUNNING (valid pre-state)
            svc.set_run_status(run.run_id, "failed", detail=MSG)
            return {
                "run_id": run.run_id,
                "task_id": task.task_id,
                "supported_by_platform": task.supported_by_platform,
                "status": "failed",
                "message": MSG,
            }

        # Platform-native tasks drive the dual loop in scripted mode; agent-driven runs
        # (explicit agent mode, or a harness task when a real agent is wired in) auto-start
        # the per-run orchestrator. Tracked-only (harness) tasks with no satisfiable agent
        # stay `requested` for the user to run externally.
        can_run_platform = task.supported_by_platform and not agent_mode
        can_run_agent = agent_mode and run_orchestrator is not None
        if auto_run and (can_run_platform or can_run_agent):
            svc.start_workflow_run(run.run_id)
            active_orchestrator = run_orchestrator or orchestrator
            inner_params = {
                "preset": preset,
                "model": inner.model,
                "fe": fe_value,
                "data_dir": data_dir,
                "cv_folds": inner.cv_folds,
                "threshold": threshold,
            }
            if inner.drop_cols:
                inner_params["drop_cols"] = list(inner.drop_cols)
            if is_custom_exec:
                # Custom tabular tasks: forward the registered target column and fold
                # id_col / drop_cols from the registration form into drop_cols.
                if type_cfg.get("target_col"):
                    inner_params["target"] = str(type_cfg["target_col"])
                extra_drops = list(type_cfg.get("drop_cols") or [])
                if type_cfg.get("id_col"):
                    extra_drops.append(str(type_cfg["id_col"]))
                merged = list(inner_params.get("drop_cols", [])) + extra_drops
                if merged:
                    inner_params["drop_cols"] = sorted(set(merged))
                # Registered baseline model / cv_folds win unless the launcher overrode them.
                if type_cfg.get("model") and inner.model == "gbm":
                    inner_params["model"] = str(type_cfg["model"])
                if type_cfg.get("cv_folds") and inner.cv_folds == 5:
                    inner_params["cv_folds"] = int(type_cfg["cv_folds"])

            def _drive() -> None:
                if _shutdown_event.is_set():
                    return
                try:
                    collab_mode = getattr(inner, "collaboration_mode", None) or "autonomous"
                    summary = active_orchestrator.run_dual_loop(
                        run.run_id,
                        inner_capability="kaggle_eval",
                        inner_params=inner_params,
                        audit_params={"threshold": cfg.audit_threshold},
                        max_outer_iters=cfg.max_outer_iters,
                        agent_inner=agent_mode,
                        inner_agent_config=inner_agent_config,
                        collaboration_mode=collab_mode,
                    )
                    # 双循环结束后把终态写回 run（否则 status 永远停在 running）
                    svc.set_run_status(run.run_id, summary.get("status", "exited_budget"))
                    # 自动入库"最优自主研究记录"（平台可实跑任务会自动算出指标）
                    try:
                        svc.capture_run_record(run.run_id)
                    except Exception:
                        logging.exception("capture_run_record failed for run=%s", run.run_id)
                except Exception:
                    logging.exception("dual loop failed for run=%s", run.run_id)
                    try:
                        svc.set_run_status(run.run_id, "failed")
                    except Exception:
                        logging.exception("set_run_status('failed') failed for run=%s", run.run_id)

            threading.Thread(target=_drive, daemon=True).start()

        return {
            "run_id": run.run_id,
            "task_id": task.task_id,
            "supported_by_platform": task.supported_by_platform,
            "status": run.status,
        }

    # ------------------------------------------------------------------ #
    # Task-registration live validation (no persistence)                  #
    # ------------------------------------------------------------------ #
    @app.post(
        "/benchmark-tasks/validate",
        summary="Validate a task-registration form without saving it",
    )
    def validate_benchmark_task(body: ValidateTaskRequest) -> dict:
        from ..benchmark_tasks import registry

        errors = registry.validate_registration(body.task_type, body.values)
        return {"valid": not errors, "errors": errors}

    # ------------------------------------------------------------------ #
    # Research records: leaderboard / per-task history / reproduce        #
    # ------------------------------------------------------------------ #
    @app.get(
        "/research-records",
        summary="List autonomous research records (optionally filtered by task_id)",
    )
    def list_research_records(task_id: str | None = None) -> list[dict]:
        return svc.list_research_records(task_id)

    @app.get(
        "/research-records/leaderboard",
        summary="Cross-task global leaderboard (best record per task)",
    )
    def get_leaderboard() -> list[dict]:
        return svc.leaderboard()

    @app.post(
        "/research-records/{record_id}/reproduce",
        summary="Reproduce a research record by re-launching its config",
    )
    def reproduce_research_record(record_id: str) -> dict:
        try:
            return svc.reproduce_record(record_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    # ------------------------------------------------------------------ #
    # Tracked-only run evaluation / metric reporting                     #
    # ------------------------------------------------------------------ #
    @app.post(
        "/workflow-runs/{run_id}/evaluate",
        summary="Evaluate an external run's outputs (LLM-judge / retrieval recall) and record it",
    )
    def evaluate_workflow_run(run_id: str, body: EvaluateRunRequest) -> dict:
        try:
            return svc.evaluate_run(
                run_id,
                eval_dataset_path=body.eval_dataset_path,
                predictions_path=body.predictions_path,
                ranked_lists_path=body.ranked_lists_path,
                judge_url=body.judge_url,
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.post(
        "/workflow-runs/{run_id}/report-metric",
        summary="Manually report a metric for a run to enter the leaderboard",
    )
    def report_run_metric(run_id: str, body: ReportMetricRequest) -> dict:
        try:
            return svc.report_run_metric(
                run_id,
                metric_name=body.metric_name,
                direction=body.direction,
                score=body.score,
                config=body.config,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    @app.get(
        "/workflow-runs/{run_id}/agent-trace",
        summary="Agent execution trace (inner-loop tool calls / final answers) for a run",
    )
    def get_agent_trace(run_id: str) -> list[dict]:
        """Return the recorded ``agent_step`` events for a run, ordered by sequence.

        Each entry is ``{seq, kind, tool, args_summary, result_summary, detail}`` — the
        inner-loop agent's execution flow ("Agent 执行流水"), persisted via ``AgentStepEvent``
        as the agent ran. Empty list when the run never ran in agent mode or produced no steps.
        """

        try:
            events = svc.list_events(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        trace = []
        for ev in events:
            if ev.get("event_type") != "agent_step":
                continue
            trace.append({
                "seq": int(ev.get("seq", 0) or 0),
                "kind": ev.get("kind", "tool_call"),
                "tool": ev.get("tool"),
                "args_summary": ev.get("args_summary", ""),
                "result_summary": ev.get("result_summary", ""),
                "detail": ev.get("detail"),
            })
        trace.sort(key=lambda e: e["seq"])
        return trace

    # ------------------------------------------------------------------ #
    # Run-context reconstruction (debug / re-run)                        #
    # ------------------------------------------------------------------ #

    def _build_run_ctx(run) -> dict[str, Any]:
        """Reconstruct the full execution context (orchestrator, params, config)
        from a run's ``objective_snapshot`` so debug / run-experiment can replay.
        """
        obj = run.objective_snapshot or {}
        cfg0 = obj.get("config", {}) or {}
        inner_dump = cfg0.get("inner_loop") or {}
        inner = InnerLoopConfig(**inner_dump) if inner_dump else InnerLoopConfig()
        task_id = obj.get("benchmark_task_id", "")
        task = get_task(task_id)
        if task is None:
            raise ValueError(f"cannot rebuild context: task {task_id} not found")
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        type_cfg = dict(task.type_config or {})
        is_custom_exec = (
            task_id.startswith("custom.")
            and task.harness == "kaggle_eval"
            and task.supported_by_platform
        )
        # Resolve preset
        if is_custom_exec:
            preset = "custom"
        elif inner.preset:
            preset = inner.preset
        elif task_id == "platform.spaceship":
            preset = "spaceship"
        elif task_id == "platform.titanic":
            preset = "titanic"
        else:
            preset = "titanic"
        fe_value = (inner.fe or "basic").lower()
        if fe_value not in ("basic", "rich"):
            fe_value = "basic"
        if is_custom_exec:
            data_dir = inner.data_dir or str(type_cfg.get("data_dir") or "")
        else:
            data_dir = inner.data_dir or os.path.join(pkg_root, "data", "kaggle", preset)
        if inner.threshold is not None:
            threshold = float(inner.threshold)
        elif is_custom_exec and type_cfg.get("threshold") not in (None, ""):
            threshold = float(type_cfg["threshold"])
        elif preset == "spaceship":
            threshold = 0.80
        else:
            threshold = 0.82
        agent_mode = obj.get("execution_mode") == "agent"
        agent_cli = cfg0.get("agent_cli") or obj.get("agent_cli")
        inner_params = {
            "preset": preset,
            "model": inner.model,
            "fe": fe_value,
            "data_dir": data_dir,
            "cv_folds": inner.cv_folds,
            "threshold": threshold,
        }
        if inner.drop_cols:
            inner_params["drop_cols"] = list(inner.drop_cols)
        if is_custom_exec:
            if type_cfg.get("target_col"):
                inner_params["target"] = str(type_cfg["target_col"])
            extra_drops = list(type_cfg.get("drop_cols") or [])
            if type_cfg.get("id_col"):
                extra_drops.append(str(type_cfg["id_col"]))
            merged = list(inner_params.get("drop_cols", [])) + extra_drops
            if merged:
                inner_params["drop_cols"] = sorted(set(merged))
            if type_cfg.get("model") and inner.model == "gbm":
                inner_params["model"] = str(type_cfg["model"])
            if type_cfg.get("cv_folds") and inner.cv_folds == 5:
                inner_params["cv_folds"] = int(type_cfg["cv_folds"])
        inner_agent_config = (
            {**inner.model_dump(), "task_spec": obj.get("task_spec", {}), "goal": obj.get("goal", "")}
            if agent_mode
            else None
        )
        per_run_orch = None
        if agent_mode and agent_cli in ("codex", "claude_code"):
            cli_bin = "codex" if agent_cli == "codex" else os.environ.get("CLAUDE_CMD", "claude")
            if shutil.which(cli_bin):
                per_run_orch = _make_agent_orchestrator(agent_cli)
        elif agent_mode and orchestrator.has_real_agent():
            per_run_orch = orchestrator
        active_orchestrator = per_run_orch or orchestrator
        collab_mode = getattr(inner, "collaboration_mode", None) or "autonomous"
        return {
            "active_orchestrator": active_orchestrator,
            "inner_params": inner_params,
            "inner_agent_config": inner_agent_config,
            "agent_mode": agent_mode,
            "audit_threshold": cfg0.get("audit_threshold", 0.8),
            "max_outer_iters": cfg0.get("max_outer_iters", 3),
            "collaboration_mode": collab_mode,
            "execution_mode": obj.get("execution_mode", "platform"),
        }

    def _spawn_full_experiment(run, ctx: dict[str, Any]) -> None:
        """Start a full autonomous experiment (dual loop) in a background daemon thread."""
        active_orchestrator = ctx["active_orchestrator"]
        try:
            svc.start_workflow_run(run.run_id)
        except (ConflictError, ValueError):
            pass  # already running / terminal

        def _drive() -> None:
            try:
                summary = active_orchestrator.run_dual_loop(
                    run.run_id,
                    inner_capability="kaggle_eval",
                    inner_params=ctx["inner_params"],
                    audit_params={"threshold": ctx["audit_threshold"]},
                    max_outer_iters=ctx["max_outer_iters"],
                    agent_inner=ctx["agent_mode"],
                    inner_agent_config=ctx.get("inner_agent_config"),
                    collaboration_mode=ctx.get("collaboration_mode", "autonomous"),
                )
                svc.set_run_status(run.run_id, summary.get("status", "exited_budget"))
                try:
                    svc.capture_run_record(run.run_id)
                except Exception:
                    pass
            except Exception:
                logging.exception("experiment failed for run=%s", run.run_id)
                try:
                    svc.set_run_status(run.run_id, "failed")
                except Exception:
                    pass

        threading.Thread(target=_drive, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Debug: isolate one stage (inner / outer) of the dual loop          #
    # ------------------------------------------------------------------ #
    @app.post(
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
            from ..execution_plane.sdk import PlatformSDK
            from ..platform_contracts.events import DebugEvent
            sdk = PlatformSDK(svc, state_store=state_store)
            # StageRun creation requires the workflow to be RUNNING.
            try:
                svc.start_workflow_run(run_id)
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

        threading.Thread(target=_debug_thread, daemon=True).start()
        return {"run_id": run_id, "stage": stage, "status": "debugging"}

    # ------------------------------------------------------------------ #
    # Run full experiment (for existing requested runs)                  #
    # ------------------------------------------------------------------ #
    @app.post(
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
                    svc.set_run_status(run_id, "failed", detail=msg)
                    return {"run_id": run_id, "status": "failed", "message": msg}
            elif not orchestrator.has_real_agent():
                msg = "内循环 agent 模式需要接入远程 Agent，当前环境未配置，已终止。请设置环境变量 AGENT_COMMAND 或选择 codex/claude_code。"
                svc.set_run_status(run_id, "failed", detail=msg)
                return {"run_id": run_id, "status": "failed", "message": msg}

        _spawn_full_experiment(run, ctx)
        return {"run_id": run_id, "status": "running", "collaboration_mode": ctx.get("collaboration_mode", "autonomous")}

    # ------------------------------------------------------------------ #
    # Human-in-the-loop collaboration: resolve a paused step             #
    # ------------------------------------------------------------------ #
    @app.post(
        "/workflow-runs/{run_id}/resolve-collaboration",
        summary="Resolve a collaboration pause (approve+adjust / reject) to resume the loop",
    )
    def resolve_collaboration(run_id: str, body: ResolveCollaborationRequest) -> dict:
        from ..execution_plane.orchestrator import _collab_lock as _orch_lock
        from ..execution_plane.orchestrator import _collab_pauses as _orch_pauses

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

        with lock:
            if run_id in pauses:
                pauses[run_id].get("evt", threading.Event()).set()

        return {"status": "ok", "run_id": run_id, "resolution": body.resolution}

    return app
