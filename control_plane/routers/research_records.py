"""Research records, leaderboard, comparison, reproduction, evaluation and agent traces.

Extracted verbatim from ``control_plane/api.py`` (backlog item A1: split the
1975-line monolith into per-domain routers). Handler bodies are unchanged; the
shared dependencies they used to close over are now bound from
:class:`~..deps.ControlPlaneDeps` as local aliases.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status

from ..schemas import EvaluateRunRequest
from ..schemas import ReportMetricRequest
from ...benchmark_tasks import get_task
from ..deps import ControlPlaneDeps


def build_research_records_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the research-record router bound to *deps*."""

    router = APIRouter()

    # --- local aliases: keep handler bodies byte-identical to the old closures ---
    svc = deps.svc
    _build_run_ctx = deps.build_run_ctx
    _spawn_full_experiment = deps.spawn_full_experiment

    @router.get(
        "/research-records",
        summary="List autonomous research records (optionally filtered by task_id)",
    )
    def list_research_records(task_id: str | None = None) -> list[dict]:
        return svc.list_research_records(task_id)

    @router.get(
        "/research-records/leaderboard",
        summary="Cross-task global leaderboard (best record per task)",
    )
    def get_leaderboard() -> list[dict]:
        return svc.leaderboard()

    @router.get(
        "/research-records/compare",
        summary="Side-by-side comparison of multiple runs",
    )
    def compare_runs(ids: str = "") -> list[dict]:
        """Compare runs by comma-separated run_ids.
        Example:  /research-records/compare?ids=run-abc,run-def,run-xyz
        """
        run_ids = [rid.strip() for rid in ids.split(",") if rid.strip()]
        if not run_ids:
            return []
        return svc.compare_runs(run_ids)

    @router.post(
        "/research-records/{record_id}/reproduce",
        summary="Reproduce a research record by re-launching its config",
    )
    def reproduce_research_record(record_id: str, autostart: bool = False) -> dict:
        try:
            payload = svc.reproduce_record(record_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        if autostart:
            # F3 fix: close the reproduce loop — a platform-executable task can be
            # re-driven immediately (previously the REQUESTED run was a dead end).
            try:
                new_run = svc.get_workflow_run(payload["run_id"])
                task = get_task(payload["task_id"])
                if task is not None and getattr(task, "supported_by_platform", False):
                    ctx = _build_run_ctx(new_run)
                    _spawn_full_experiment(new_run, ctx)
                    payload["autostarted"] = True
                else:
                    payload["autostarted"] = False
                    payload["note"] = "tracked-only 任务：已建档为 REQUESTED，需外部执行后上报指标"
            except Exception as exc:  # never let autostart failure lose the new run
                logging.exception("autostart after reproduce failed for run=%s", payload["run_id"])
                payload["autostarted"] = False
                payload["note"] = f"自动启动失败（run 已建档，可手动启动）: {exc}"
        return payload

    # ------------------------------------------------------------------ #
    # Tracked-only run evaluation / metric reporting                     #
    # ------------------------------------------------------------------ #
    @router.post(
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

    @router.post(
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

    @router.get(
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

    return router
