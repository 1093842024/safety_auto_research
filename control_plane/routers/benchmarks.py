"""Benchmark task catalog, suites, registration, dataset upload, validation and launch.

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
import time as _time

from fastapi import APIRouter
from fastapi import File
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi import status

from ...platform_contracts.enums import RunType
from ..schemas import CreateWorkflowRunRequest
from ..schemas import InnerLoopConfig
from ..schemas import LaunchBenchmarkRequest
from ..schemas import RegisterTaskRequest
from ..schemas import ValidateTaskRequest
from ..progress_bus import progress_bus as _progress_bus
from ...benchmark_tasks import get_catalog
from ...benchmark_tasks import get_task
from ...benchmark_tasks import _default_eval_method as _get_eval_method_desc
from ...benchmark_tasks import to_dict
from ..deps import ControlPlaneDeps
from ..deps import _map_summary_status
from ..deps import _spawn_bg_thread
from ..deps import acquire_run_slot
from ..deps import release_run_slot
from ..deps import resolve_eval_params as _resolve_eval_params


def build_benchmarks_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the benchmark-task router bound to *deps*."""

    router = APIRouter()

    # --- local aliases: keep handler bodies byte-identical to the old closures ---
    svc = deps.svc
    orchestrator = deps.orchestrator
    _run_cancel_events = deps.run_cancel_events
    _shutdown_event = deps.shutdown_event
    _make_agent_orchestrator = deps.make_agent_orchestrator

    @router.get(
        "/benchmark-tasks",
        summary="List curated benchmark / baseline research tasks (selectable in the frontend)",
    )
    def list_benchmark_tasks() -> list[dict]:
        return [to_dict(t) for t in get_catalog()]

    # ----- External benchmark suites (ScienceAgentBench / MLE-bench) -----
    @router.get(
        "/benchmark-suites",
        summary="List integrated external benchmark suites (Weng harness appendix)",
    )
    def list_benchmark_suites() -> list[dict]:
        from ...benchmark_tasks import suites

        return [s.to_dict() for s in suites.list_suites()]

    @router.get(
        "/benchmark-suites/{suite_id}",
        summary="Suite detail: metrics, data acquisition, resource-scaling/contamination analyses",
    )
    def get_benchmark_suite(suite_id: str) -> dict:
        from ...benchmark_tasks import suites

        suite = suites.get_suite(suite_id)
        if suite is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"suite {suite_id} not found")
        return suite.to_dict()

    @router.get(
        "/benchmark-suites/{suite_id}/tasks",
        summary="Suite sub-task manifest (default excludes known-issue/leakage competitions; "
                "set include_excluded=true to see all). Filters: domain/split/category substring match",
    )
    def list_suite_tasks(
        suite_id: str,
        domain: str | None = None,
        split: str | None = None,
        category: str | None = None,
        include_excluded: bool = False,
        # R22 fix: unvalidated paging — ``offset=-2`` produced a wrap-around slice
        # (silently returning the tail of the list) and ``limit`` was unbounded.
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict:
        from ...benchmark_tasks import suites

        suite = suites.get_suite(suite_id)
        if suite is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"suite {suite_id} not found")
        rows = suite.load_manifest(include_excluded=include_excluded)

        def _match(row: dict) -> bool:
            if domain and domain.lower() not in str(row.get("domain", "")).lower():
                return False
            if split and split.lower() != str(row.get("complexity_split", "")).lower():
                return False
            if category:
                hay = (str(row.get("subtask_categories", "")) + " " + str(row.get("category", ""))).lower()
                if category.lower() not in hay:
                    return False
            return True

        filtered = [r for r in rows if _match(r)]
        return {
            "suite_id": suite_id,
            "total": len(filtered),
            "offset": offset,
            "limit": limit,  # R22: the client cannot page without knowing the window
            "tasks": filtered[offset : offset + limit],
        }

    @router.get(
        "/benchmark-suites/{suite_id}/baselines",
        summary="Official baseline / leaderboard results for a suite",
    )
    def list_suite_baselines(suite_id: str) -> dict:
        from dataclasses import asdict

        from ...benchmark_tasks import suites

        suite = suites.get_suite(suite_id)
        if suite is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"suite {suite_id} not found")
        headline = suites.suite_headline_baseline(suite)
        return {
            "suite_id": suite_id,
            "headline_metric": suite.headline_metric,
            "direction": suite.direction,
            "headline": asdict(headline) if headline else None,
            "baselines": [asdict(b) for b in suite.baselines],
        }

    # ----- Custom task registration (schema-driven) -----
    @router.get(
        "/benchmark-tasks/task-types",
        summary="Task-type specs for the custom-task registration form (schema-driven)",
    )
    def list_task_types() -> list[dict]:
        from ...benchmark_tasks import registry

        return registry.get_task_type_specs()

    @router.post(
        "/benchmark-tasks/register",
        status_code=status.HTTP_201_CREATED,
        summary="Register a custom research task (validated against its task-type spec)",
    )
    def register_benchmark_task(req: RegisterTaskRequest) -> dict:
        from ...benchmark_tasks import registry

        try:
            record = registry.register_custom_task(req.task_type, req.values)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        out = registry.custom_task_to_benchmark_dict(record)
        # Rubric stage (registration-time): report how good the declared evaluation
        # standard is — and, when none was declared, what rubric will be generated.
        # Advisory only: a weak standard does not block registration (the run-time
        # layer_12 stage will synthesize a stronger rubric anyway), but the researcher
        # sees the defects immediately instead of discovering them after a wasted run.
        out["rubric_review"] = registry.review_registration_standard(
            req.task_type, {**req.values, "task_id": record["task_id"]}
        )
        return out

    @router.post(
        "/benchmark-tasks/review-standard",
        summary="Review a task's declared evaluation standard (accuracy / completeness / "
                "scientificity) and preview the executable rubric — no persistence",
    )
    def review_task_standard(body: ValidateTaskRequest) -> dict:
        """Rubric stage, dry-run: audit the standard + preview the induced rubric.

        Same engine the runtime ``layer_12_rubric_induction`` stage uses, so what the
        researcher previews here is exactly what the run will be graded against.
        """

        from ...benchmark_tasks import registry

        return registry.review_registration_standard(body.task_type, body.values)

    @router.get(
        "/benchmark-tasks/{task_id}/rubric",
        summary="The executable scoring rubric for a catalog task (induced on demand, "
                "identical to what a run of this task will be graded against)",
    )
    def get_task_rubric(task_id: str, use_llm: bool = False) -> dict:
        from ...rubric import RubricEngine
        from ...rubric import TaskSpec

        task = get_task(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"benchmark task {task_id} not found",
            )
        spec = TaskSpec.from_benchmark_dict(to_dict(task))
        rubric = RubricEngine(use_llm=use_llm).induce(spec)
        return rubric.model_dump(mode="json")

    @router.delete(
        "/benchmark-tasks/{task_id}",
        summary="Delete a custom-registered task (curated catalog entries cannot be deleted)",
    )
    def delete_benchmark_task(task_id: str) -> dict:
        from ...benchmark_tasks import registry

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

    @router.post(
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
                    infos = zf.infolist()
                    # ---- zip-bomb guard (M6): cap entry count + total uncompressed size ----
                    _MAX_ZIP_ENTRIES = 100_000
                    _MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
                    if len(infos) > _MAX_ZIP_ENTRIES:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"zip 内含文件过多（{len(infos)} > {_MAX_ZIP_ENTRIES}），拒绝解压",
                        )
                    total = 0
                    for info in infos:
                        if info.file_size < 0 or info.file_size > _MAX_UNCOMPRESSED_BYTES:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"zip 单文件过大（{info.file_size} 字节），拒绝解压",
                            )
                        total += info.file_size
                        if total > _MAX_UNCOMPRESSED_BYTES:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail="zip 解压后总体积超限（> 4 GiB），疑似 zip bomb，拒绝解压",
                            )
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

    @router.post(
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
        from ...platform_contracts.enums import RunType

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
        # A2: resolve the shared launch params (preset / fe / data_dir / threshold /
        # is_custom_exec) through the single helper used by both fresh launches and
        # debug replays — no more hand-synced duplication.
        preset, fe_value, data_dir, threshold, is_custom_exec = _resolve_eval_params(
            task, inner, type_cfg, pkg_root
        )

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
        # F3 (agent-mode sandbox wiring): hand the agent the container execution path
        # so the formerly "empty-run" tasks actually execute inside the Docker sandbox.
        #  * tabular / kaggle_eval tasks: run_capability("kaggle_eval", ...) auto-routes
        #    into the sandbox when AGENT_SANDBOX=1 (or the agent opts in); the inner
        #    loop's kaggle_eval is what gets confined.
        #  * non-tabular tracked-only tasks: there is no platform executor, but the
        #    task's run_command is a real (if not-yet-present) script. We surface it as
        #    `research_cmd` + the `run_research_sandbox` capability so the agent can run
        #    it in-container; if no in-repo script exists the run fails *honestly*
        #    (no silent empty-run).
        if agent_mode and inner_agent_config is not None:
            # F2: text_classification is the one non-kaggle modality that runs in the
            # sklearn-only sandbox image (TF-IDF + linear model, no torch/GPU). Route it
            # through the dedicated text_cls_sandbox capability so a real in-container run
            # happens instead of an empty-run. Other modalities fall back to the generic
            # research_cmd path (honest failure when no in-repo executor exists).
            task_type = task.task_type or (task.type_config or {}).get("task_type")
            if task.harness in ("kaggle_eval",) or task.supported_by_platform:
                sandbox_capability = "kaggle_eval"
            elif task_type == "text_classification":
                sandbox_capability = "text_cls_sandbox"
                tc = task.type_config or {}
                inner_agent_config["task_type"] = "text_classification"
                inner_agent_config["text_col"] = tc.get("text_col", "text")
                inner_agent_config["label_col"] = tc.get("label_col", "label")
                inner_agent_config["data_subdir"] = tc.get("data_subdir") or "text_cls_demo"
                if tc.get("target_value") is not None:
                    inner_agent_config["threshold"] = float(tc["target_value"])
                inner_agent_config["eval_metric"] = task.eval_metric
            elif task_type == "image_classification":
                sandbox_capability = "image_cls_sandbox"
                tc = task.type_config or {}
                inner_agent_config["task_type"] = "image_classification"
                inner_agent_config["data_subdir"] = tc.get("data_subdir") or "image_cls_demo"
                inner_agent_config["arch"] = tc.get("base_model", "tiny_cnn")
                inner_agent_config["epochs"] = tc.get("num_epochs", 6)
                if tc.get("target_value") is not None:
                    inner_agent_config["threshold"] = float(tc["target_value"])
                inner_agent_config["eval_metric"] = task.eval_metric
            elif task_type == "audio_classification":
                sandbox_capability = "audio_cls_sandbox"
                tc = task.type_config or {}
                inner_agent_config["task_type"] = "audio_classification"
                inner_agent_config["data_subdir"] = tc.get("data_subdir") or "audio_cls_demo"
                inner_agent_config["feature"] = tc.get("feature", "logmel")
                inner_agent_config["epochs"] = tc.get("num_epochs", 8)
                if tc.get("target_value") is not None:
                    inner_agent_config["threshold"] = float(tc["target_value"])
                inner_agent_config["eval_metric"] = task.eval_metric
            elif task_type == "embedding_contrastive":
                sandbox_capability = "embedding_sandbox"
                tc = task.type_config or {}
                inner_agent_config["task_type"] = "embedding_contrastive"
                inner_agent_config["data_subdir"] = tc.get("data_subdir") or "embedding_demo"
                # 768 matches the registry form default (registry.py embedding_dim);
                # a missing key should fall back to the same value the form pre-fills.
                inner_agent_config["dim"] = tc.get("embedding_dim", 768)
                inner_agent_config["epochs"] = tc.get("num_epochs", 30)
                if tc.get("target_value") is not None:
                    inner_agent_config["threshold"] = float(tc["target_value"])
                inner_agent_config["eval_metric"] = task.eval_metric
            else:
                sandbox_capability = "run_research_sandbox"
                if not task.supported_by_platform and task.run_command and task.run_command != "manual":
                    inner_agent_config["research_cmd"] = task.run_command
            inner_agent_config["sandbox_capability"] = sandbox_capability

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
                "sandbox_isolation": "none" if task.supported_by_platform else "host",
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

            # R1 fix: pre-register the cancel event synchronously so ``POST .../cancel``
            # is honored (``.get(run_id)`` inside the thread always returned None).
            cancel_ev = acquire_run_slot(_run_cancel_events, run.run_id)
            if cancel_ev is None:
                # R8 fix: single-flight (see loops.py for the full rationale).
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"run {run.run_id} 已有正在执行的循环；请先取消后再启动",
                )

            def _drive() -> None:
                if _shutdown_event.is_set():
                    release_run_slot(_run_cancel_events, run.run_id)
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
                        cancel_event=cancel_ev,
                        progress_callback=_progress_bus.emit,
                    )
                    svc.set_run_status(run.run_id, _map_summary_status(summary.get("status", "exited_budget")))
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
                finally:
                    # I3 fix: no pending meta-loop proposal survives an abnormal exit.
                    try:
                        active_orchestrator.expire_pending_strategies(run.run_id)
                    except Exception:
                        logging.exception("expire_pending_strategies failed for run=%s", run.run_id)
                    finally:
                        release_run_slot(_run_cancel_events, run.run_id)  # 缺陷10: always release the cancel event once the run ends

            _spawn_bg_thread(_drive, name=f"dual-loop-{run.run_id}")

        _sandbox = (
            "none" if (task.harness == "kaggle_eval" or task.supported_by_platform)
            else ("container-hard" if (os.environ.get("AGENT_SANDBOX") == "1" and shutil.which("docker"))
                  else "container-soft" if os.environ.get("AGENT_SANDBOX") == "1"
                  else "host")
        )
        return {
            "run_id": run.run_id,
            "task_id": task.task_id,
            "supported_by_platform": task.supported_by_platform,
            "sandbox_isolation": _sandbox,
            "status": run.status,
        }

    # ------------------------------------------------------------------ #
    # Task-registration live validation (no persistence)                  #
    # ------------------------------------------------------------------ #
    @router.post(
        "/benchmark-tasks/validate",
        summary="Validate a task-registration form without saving it",
    )
    def validate_benchmark_task(body: ValidateTaskRequest) -> dict:
        from ...benchmark_tasks import registry

        errors = registry.validate_registration(body.task_type, body.values)
        # Two independent verdicts, deliberately kept separate:
        #   * ``valid``/``errors``  — is the FORM well-formed? (blocks registration)
        #   * ``rubric_review``     — is the declared EVALUATION STANDARD accurate,
        #     complete and scientific? (advisory; drives the console's review card)
        # A form can be perfectly valid and still declare a scientifically weak standard,
        # which is exactly the gap this stage closes.
        return {
            "valid": not errors,
            "errors": errors,
            **registry.review_registration_standard(body.task_type, body.values),
        }

    # ------------------------------------------------------------------ #
    # Research records: leaderboard / per-task history / reproduce        #
    # ------------------------------------------------------------------ #

    return router
