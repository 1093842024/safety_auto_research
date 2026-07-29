from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Any

from ..platform_contracts.enums import DecisionType
from ..platform_contracts.enums import EventType
from ..platform_contracts.enums import RunType
from ..platform_contracts.enums import StageStatus
from ..platform_contracts.enums import WorkflowStatus
from ..platform_contracts.events import ApprovalRequiredEvent
from ..platform_contracts.events import ApprovalResolvedEvent
from ..platform_contracts.events import DecisionRecordedEvent
from ..platform_contracts.events import StageStatusChangedEvent
from ..platform_contracts.events import WorkflowStatusChangedEvent
from ..platform_contracts.events import utc_now
from ..platform_contracts.objects import DecisionRecord
from ..platform_contracts.objects import StageRun
from ..platform_contracts.objects import WorkflowRun
from ..platform_contracts.transitions import validate_decision_record_contract
from ..platform_contracts.transitions import validate_stage_status_transition
from ..platform_contracts.transitions import validate_workflow_status_transition
from .schemas import CreateStageRunRequest
from .schemas import CreateWorkflowRunRequest
from .schemas import RecordDecisionRequest
from .schemas import RequestApprovalRequest
from .schemas import ResolveApprovalRequest
from .schemas import UpdateStageStatusRequest
from .store import Repository
from . import eval_runner
from ..benchmark_tasks import get_task

# Map a target StageStatus to the event_type used in StageStatusChangedEvent.
_STAGE_EVENT_TYPE: dict[StageStatus, EventType] = {
    StageStatus.QUEUED: EventType.STAGE_QUEUED,
    StageStatus.RUNNING: EventType.STAGE_STARTED,
    StageStatus.WAITING_APPROVAL: EventType.APPROVAL_REQUIRED,
    StageStatus.SUCCEEDED: EventType.GATE_PASSED,
    StageStatus.FAILED: EventType.GATE_FAILED,
    StageStatus.CANCELLED: EventType.STAGE_CANCELLED,
}

# All workflow terminal statuses collapse to a single "run closed" signal;
# the specific outcome is captured in the event's `to_status` field.
_WORKFLOW_TERMINAL_TYPE = EventType.WORKFLOW_FINISHED


class NotFoundError(Exception):
    """Raised when a referenced control-plane object does not exist."""


class ConflictError(Exception):
    """Raised when a requested transition is not allowed by the state machine."""


class ControlPlaneService:
    """Minimal control-plane: orchestrates WorkflowRun / StageRun / DecisionRecord.

    Every mutation is validated by the canonical transition contracts in
    ``platform_contracts.transitions`` and emits a platform event into the log.
    """

    def __init__(
        self,
        repository: Repository | None = None,
        store_path: str | None = None,
    ) -> None:
        # ``store_path`` lets the caller enable on-disk persistence: a Repository
        # backed by a JSON file reloads all prior research records on startup.
        if repository is None:
            repository = Repository(store_path=store_path)
        self._repo = repository
        self._open_approvals: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def _emit_workflow_status_change(
        self,
        run: WorkflowRun,
        to_status: WorkflowStatus,
        event_type: EventType,
    ) -> None:
        from_status = run.status
        try:
            validate_workflow_status_transition(from_status, to_status)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        run.status = to_status
        self._repo.append_event(
            WorkflowStatusChangedEvent(
                run_id=run.run_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
            )
        )

    def _emit_stage_status_change(
        self,
        stage: StageRun,
        to_status: StageStatus,
    ) -> None:
        from_status = stage.status
        try:
            validate_stage_status_transition(from_status, to_status)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        event_type = _STAGE_EVENT_TYPE.get(to_status)
        if event_type is None:
            raise ValueError(f"unsupported stage status for event mapping: {to_status}")
        stage.status = to_status
        self._repo.append_event(
            StageStatusChangedEvent(
                run_id=stage.run_id,
                event_type=event_type,
                stage_run_id=stage.stage_run_id,
                from_status=from_status,
                to_status=to_status,
            )
        )

    # ------------------------------------------------------------------ WorkflowRun
    def create_workflow_run(self, req: CreateWorkflowRunRequest) -> WorkflowRun:
        run = WorkflowRun(
            run_id=self._repo.next_id("run"),
            program_id=req.program_id,
            run_type=req.run_type,
            entry_stage=req.entry_stage,
            target_id=req.target_id,
            objective_snapshot=req.objective_snapshot,
            requested_outcomes=req.requested_outcomes,
            status=WorkflowStatus.REQUESTED,
            started_at=utc_now(),
            ended_at=None,
        )
        self._repo.put_workflow_run(run)
        self._repo.append_event(
            WorkflowStatusChangedEvent(
                run_id=run.run_id,
                event_type=EventType.WORKFLOW_REQUESTED,
                from_status=WorkflowStatus.REQUESTED,
                to_status=WorkflowStatus.REQUESTED,
            )
        )
        return run

    def start_workflow_run(self, run_id: str) -> WorkflowRun:
        run = self._require_workflow_run(run_id)
        self._emit_workflow_status_change(run, WorkflowStatus.RUNNING, EventType.WORKFLOW_STARTED)
        return run

    def cancel_workflow_run(self, run_id: str) -> WorkflowRun:
        run = self._require_workflow_run(run_id)
        self._emit_workflow_status_change(run, WorkflowStatus.CANCELLED, _WORKFLOW_TERMINAL_TYPE)
        return run

    def set_run_status(
        self,
        run_id: str,
        status: str,
        event_type: EventType = EventType.WORKFLOW_REQUESTED,
        detail: str | None = None,
    ) -> WorkflowRun:
        """Force/complete a run's workflow status (used by async dual-loop driver).

        ``detail`` lets the async driver attach a human-readable reason for a terminal
        state (e.g. why an agent-mode run was rejected because no remote agent is wired).
        """
        run = self._require_workflow_run(run_id)
        self._emit_workflow_status_change(run, WorkflowStatus(status), event_type)
        if detail is not None:
            run.status_detail = detail
            self._repo.put_workflow_run(run)
        return run

    def get_workflow_run(self, run_id: str) -> WorkflowRun:
        return self._require_workflow_run(run_id)

    def list_workflow_runs(self) -> list[WorkflowRun]:
        return self._repo.list_workflow_runs()

    # ------------------------------------------------------------------ Research records (leaderboard)
    def capture_run_record(self, run_id: str) -> dict[str, Any] | None:
        """Harvest the achieved metric from a finished run and persist a research record.

        Returns the created record dict, or None when no metric could be harvested
        (e.g. a tracked-only task whose run produced no platform-computed metric).
        """
        run = self._require_workflow_run(run_id)
        obj = run.objective_snapshot or {}
        task_id = obj.get("benchmark_task_id") or run.target_id
        metric_name = str(obj.get("eval_metric") or "accuracy").strip().lower()
        direction = str(obj.get("direction") or "higher").strip().lower()
        config = dict(obj.get("config") or {})

        # Gather all numeric scores from recorded metrics + event payloads.
        scores: dict[str, list[float]] = {}
        for m in self._repo.list_metrics(run_id):
            try:
                scores.setdefault(m["name"], []).append(float(m["value"]))
            except (TypeError, ValueError, KeyError):
                pass
        for e in self._repo.list_events(run_id):
            mm = e.get("metrics") if isinstance(e, dict) else None
            if isinstance(mm, dict):
                for k, v in mm.items():
                    try:
                        scores.setdefault(k, []).append(float(v))
                    except (TypeError, ValueError):
                        pass
        if not scores:
            return None

        def _best(vals: list[float]) -> float:
            return max(vals) if direction == "higher" else min(vals)

        # Prefer an exact (prefix-insensitive) match on the task's declared metric.
        target: float | None = None
        for nm, vals in scores.items():
            base = nm.lower().replace("eval.", "")
            if base == metric_name or nm.lower() == metric_name:
                target = _best(vals)
                break
        if target is None:  # fall back to accuracy-like, then any available metric
            for cand in ("accuracy", "eval.accuracy", "cv_accuracy", "score"):
                if cand in scores:
                    target = _best(scores[cand])
                    break
        if target is None:
            nm = next(iter(scores))
            target = _best(scores[nm])

        artifact_ids = [a.artifact_id for a in self._repo.list_artifacts(run_id)]
        record = {
            "record_id": self._repo.next_id("rec"),
            "task_id": task_id,
            "run_id": run_id,
            "metric_name": metric_name,
            "direction": direction,
            "score": round(float(target), 6),
            "config_snapshot": config,
            "artifact_ids": artifact_ids,
            "status": run.status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._repo.put_research_record(record)
        self._recompute_top3(task_id)
        return record

    def _recompute_top3(self, task_id: str) -> None:
        """Mark the top-3 records (by score, direction-aware) for a task as is_top3."""
        recs = self._repo.list_research_records(task_id)
        if not recs:
            return
        direction = recs[0].get("direction", "higher")
        ordered = sorted(recs, key=lambda r: r.get("score", 0.0), reverse=(direction != "lower"))
        for i, r in enumerate(ordered):
            self._repo.update_research_record(r["record_id"], is_top3=(i < 3))

    # ------------------------------------------------------------------ Research records: report / evaluate
    def _make_record(
        self,
        *,
        task_id: str,
        run_id: str,
        metric_name: str,
        direction: str,
        score: float,
        config: dict[str, Any] | None = None,
        status: str = "evaluated",
        artifact_ids: list[str] | None = None,
        source: str = "platform",
    ) -> dict[str, Any]:
        record = {
            "record_id": self._repo.next_id("rec"),
            "task_id": task_id,
            "run_id": run_id,
            "metric_name": str(metric_name).strip().lower(),
            "direction": str(direction).strip().lower(),
            "score": round(float(score), 6),
            "config_snapshot": dict(config or {}),
            "artifact_ids": list(artifact_ids or []),
            "status": status,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._repo.put_research_record(record)
        self._recompute_top3(task_id)
        return record

    def report_run_metric(
        self,
        run_id: str,
        metric_name: str,
        direction: str,
        score: float,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Manually report a metric for a (typically tracked-only) run; creates a record."""
        run = self._require_workflow_run(run_id)
        obj = run.objective_snapshot or {}
        task_id = obj.get("benchmark_task_id") or run.target_id
        return self._make_record(
            task_id=task_id,
            run_id=run_id,
            metric_name=metric_name,
            direction=direction,
            score=score,
            config=config if config is not None else dict(obj.get("config") or {}),
            status=run.status,
            source="reported",
        )

    def evaluate_run(
        self,
        run_id: str,
        eval_dataset_path: str | None = None,
        predictions_path: str | None = None,
        ranked_lists_path: str | None = None,
        judge_url: str | None = None,
    ) -> dict[str, Any]:
        """Run the task-type-appropriate evaluator and persist the resulting record."""
        run = self._require_workflow_run(run_id)
        obj = run.objective_snapshot or {}
        task_id = obj.get("benchmark_task_id") or run.target_id
        task_type = ""
        task = get_task(task_id) if task_id else None
        if task is not None:
            task_type = getattr(task, "task_type", "") or ""
        if not task_type:
            raise ValueError(f"无法确定 run {run_id} 的任务类型（task_id={task_id}），无法选择评估器。")

        result = eval_runner.evaluate_for_task(
            task_type,
            eval_dataset_path=eval_dataset_path,
            predictions_path=predictions_path,
            ranked_lists_path=ranked_lists_path,
            judge_url=judge_url,
        )
        return self._make_record(
            task_id=task_id,
            run_id=run_id,
            metric_name=result["metric_name"],
            direction=result["direction"],
            score=result["score"],
            config=dict(obj.get("config") or {}),
            status=run.status,
            source="evaluated",
        )

    # ------------------------------------------------------------------ Research records: query / leaderboard / reproduce
    def list_research_records(self, task_id: str | None = None) -> list[dict[str, Any]]:
        recs = self._repo.list_research_records(task_id)
        # best-first ordering (direction-aware)
        def _key(r: dict[str, Any]):
            rev = r.get("direction", "higher") != "lower"
            return (r.get("is_top3", False), -r.get("score", 0.0) if rev else r.get("score", 0.0))
        return sorted(recs, key=_key, reverse=False)  # top3 True < top3 False; score desc

    def leaderboard(self) -> list[dict[str, Any]]:
        """Cross-task global board: each task contributes its single best record."""
        by_task: dict[str, dict[str, Any]] = {}
        for r in self._repo.list_research_records():
            if r.get("score") is None:
                continue  # can't rank a record without a score
            cur = by_task.get(r["task_id"])
            if cur is None:
                by_task[r["task_id"]] = r
                continue
            dirr = r.get("direction", "higher")
            # better = larger score when higher-is-better, smaller when lower-is-better
            is_better = (r["score"] - cur["score"]) * (1 if dirr == "higher" else -1) > 0
            if is_better:
                by_task[r["task_id"]] = r
        board = list(by_task.values())
        board.sort(
            key=lambda r: r["score"] * (1 if r.get("direction", "higher") == "higher" else -1),
            reverse=True,
        )
        return board

    def reproduce_record(self, record_id: str) -> dict[str, Any]:
        """Re-launch a run using a recorded config snapshot; returns the new run payload."""
        rec = self._repo.get_research_record(record_id)
        if rec is None:
            raise KeyError(f"research record {record_id} not found")
        task_id = rec["task_id"]
        cfg = dict(rec.get("config_snapshot") or {})
        task = get_task(task_id)
        if task is None:
            raise KeyError(f"task {task_id} no longer exists; cannot reproduce")
        req = CreateWorkflowRunRequest(
            program_id="benchmark",
            run_type=RunType.STANDARD_RESEARCH,
            entry_stage="inner_research",
            target_id=task_id,
            objective_snapshot={
                "benchmark_task_id": task_id,
                "name": getattr(task, "name", task_id),
                "eval_metric": getattr(task, "eval_metric", rec.get("metric_name")),
                "direction": getattr(task, "direction", rec.get("direction", "higher")),
                "config": cfg,
            },
            requested_outcomes=[f"Reproduce {rec.get('metric_name')} = {rec.get('score')}"],
        )
        run = self.create_workflow_run(req)
        return {"run_id": run.run_id, "task_id": task_id, "from_record": record_id}

    # ------------------------------------------------------------------ Approval (HITL)
    def request_approval(self, run_id: str, req: RequestApprovalRequest) -> tuple[WorkflowRun, str]:
        run = self._require_workflow_run(run_id)
        if run.status == WorkflowStatus.WAITING_APPROVAL:
            raise ConflictError(f"run {run_id} is already waiting for approval")
        approval_id = self._repo.next_id("apr")
        self._open_approvals[run_id] = approval_id
        self._emit_workflow_status_change(
            run, WorkflowStatus.WAITING_APPROVAL, EventType.APPROVAL_REQUIRED
        )
        self._repo.append_event(
            ApprovalRequiredEvent(
                run_id=run.run_id,
                approval_id=approval_id,
                subject_type=req.subject_type,
                subject_ref=run.run_id,
                reason=req.reason,
                policy_ref=req.policy_ref,
                required_roles=req.required_roles,
                risk_tier=req.risk_tier,
            )
        )
        return run, approval_id

    def resolve_approval(self, run_id: str, req: ResolveApprovalRequest) -> tuple[WorkflowRun, str]:
        run = self._require_workflow_run(run_id)
        approval_id = self._open_approvals.get(run_id)
        if approval_id is None:
            if run.status == WorkflowStatus.WAITING_APPROVAL:
                logging.warning(
                    "run %s is WAITING_APPROVAL but has no open approval record "
                    "(likely after restart); generating recovery approval_id.",
                    run_id,
                )
                approval_id = self._repo.next_id("apr-recovery")
                self._open_approvals[run_id] = approval_id
            else:
                raise ConflictError(f"run {run_id} has no open approval to resolve")
        if run.status != WorkflowStatus.WAITING_APPROVAL:
            raise ConflictError(f"run {run_id} is not in waiting_approval state")

        if req.resolution == "approved":
            to_status = WorkflowStatus.RUNNING
            status_event = EventType.APPROVAL_RESOLVED
        elif req.resolution == "rejected":
            to_status = WorkflowStatus.FAILED
            status_event = _WORKFLOW_TERMINAL_TYPE
        else:
            raise ValueError("resolution must be 'approved' or 'rejected'")

        self._emit_workflow_status_change(run, to_status, status_event)
        self._repo.append_event(
            ApprovalResolvedEvent(
                run_id=run.run_id,
                approval_id=approval_id,
                resolution=req.resolution,
                resolved_by=req.resolved_by,
                note=req.note,
            )
        )
        self._open_approvals.pop(run_id, None)
        return run, approval_id

    # ------------------------------------------------------------------ StageRun
    def create_stage_run(self, run_id: str, req: CreateStageRunRequest) -> StageRun:
        run = self._require_workflow_run(run_id)
        if run.status not in (WorkflowStatus.RUNNING, WorkflowStatus.WAITING_APPROVAL):
            raise ConflictError("stages can only be created for a running workflow")
        stage = StageRun(
            stage_run_id=self._repo.next_id("stage"),
            run_id=run.run_id,
            stage_code=req.stage_code,
            input_refs=req.input_refs,
            output_refs=req.output_refs,
            executor_family=req.executor_family,
            reviewer_family=req.reviewer_family,
            gate_result=req.gate_result,
            status=StageStatus.QUEUED,
            retry_count=0,
        )
        self._repo.put_stage_run(stage)
        # Initial assignment bypasses the self-transition guard in the transition
        # contract (a freshly created stage has no prior status to transition from).
        self._repo.append_event(
            StageStatusChangedEvent(
                run_id=stage.run_id,
                event_type=EventType.STAGE_QUEUED,
                stage_run_id=stage.stage_run_id,
                from_status=StageStatus.QUEUED,
                to_status=StageStatus.QUEUED,
            )
        )
        return stage

    def update_stage_status(self, stage_run_id: str, req: UpdateStageStatusRequest) -> StageRun:
        stage = self._require_stage_run(stage_run_id)
        self._emit_stage_status_change(stage, req.to_status)
        return stage

    def get_stage_run(self, stage_run_id: str) -> StageRun:
        return self._require_stage_run(stage_run_id)

    def list_stage_runs(self, run_id: str) -> list[StageRun]:
        self._require_workflow_run(run_id)
        return self._repo.list_stage_runs(run_id)

    # ------------------------------------------------------------------ DecisionRecord
    def record_decision(self, run_id: str, req: RecordDecisionRequest) -> DecisionRecord:
        self._require_workflow_run(run_id)
        decision = DecisionRecord(
            decision_id=self._repo.next_id("decision"),
            run_id=run_id,
            decision_type=req.decision_type,
            target_stage=req.target_stage,
            reason_codes=req.reason_codes,
            evidence_refs=req.evidence_refs,
            policy_hits=req.policy_hits,
            approved_by=req.approved_by,
        )
        validate_decision_record_contract(decision)
        self._repo.put_decision(decision)
        self._repo.append_event(
            DecisionRecordedEvent(
                run_id=run_id,
                decision_id=decision.decision_id,
                decision_type=decision.decision_type,
            )
        )
        return decision

    def get_decision(self, decision_id: str) -> DecisionRecord:
        decision = self._repo.get_decision(decision_id)
        if decision is None:
            raise NotFoundError(f"decision {decision_id} not found")
        return decision

    def list_decisions(self, run_id: str) -> list[DecisionRecord]:
        self._require_workflow_run(run_id)
        return self._repo.list_decisions(run_id)

    # ------------------------------------------------------------------ Event log
    def list_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self._repo.list_events(run_id)

    def append_event(self, event: Any) -> None:
        """Public sink for adapter-emitted platform events (spec §9.4 emit_event)."""

        self._repo.append_event(event)

    # ------------------------------------------------------------------ require
    def _require_workflow_run(self, run_id: str) -> WorkflowRun:
        run = self._repo.get_workflow_run(run_id)
        if run is None:
            raise NotFoundError(f"workflow run {run_id} not found")
        return run

    def _require_stage_run(self, stage_run_id: str) -> StageRun:
        stage = self._repo.get_stage_run(stage_run_id)
        if stage is None:
            raise NotFoundError(f"stage run {stage_run_id} not found")
        return stage
