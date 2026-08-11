from __future__ import annotations

from .enums import DecisionType
from .enums import StageStatus
from .enums import WorkflowStatus
from .objects import DecisionRecord


WORKFLOW_STATUS_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.REQUESTED: frozenset({
        WorkflowStatus.RUNNING,
        WorkflowStatus.WAITING_APPROVAL,
        WorkflowStatus.CANCELLED,
        # R5 fix: a run can fail during *setup*, before it ever reaches RUNNING (bad
        # task config, missing data dir, agent wiring rejected, ...). Without this edge
        # the background driver's ``set_run_status(run_id, "failed")`` raised
        # ValueError inside its own except-handler and the run was pinned at
        # REQUESTED forever — the UI showed "已请求" for a run that is dead.
        WorkflowStatus.FAILED,
    }),
    WorkflowStatus.RUNNING: frozenset({
        WorkflowStatus.WAITING_APPROVAL,
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.EXITED_BUDGET,
        WorkflowStatus.EXITED_CONVERGED,
        WorkflowStatus.CANCELLED,
    }),
    WorkflowStatus.WAITING_APPROVAL: frozenset({
        WorkflowStatus.RUNNING,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    }),
    WorkflowStatus.SUCCEEDED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.EXITED_BUDGET: frozenset(),
    WorkflowStatus.EXITED_CONVERGED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}


STAGE_STATUS_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.QUEUED: frozenset({
        StageStatus.RUNNING,
        StageStatus.WAITING_APPROVAL,
        StageStatus.CANCELLED,
    }),
    StageStatus.RUNNING: frozenset({
        StageStatus.WAITING_APPROVAL,
        StageStatus.SUCCEEDED,
        StageStatus.FAILED,
        StageStatus.CANCELLED,
    }),
    StageStatus.WAITING_APPROVAL: frozenset({
        StageStatus.RUNNING,
        StageStatus.FAILED,
        StageStatus.CANCELLED,
    }),
    StageStatus.SUCCEEDED: frozenset(),
    StageStatus.FAILED: frozenset(),
    StageStatus.CANCELLED: frozenset(),
}


def validate_workflow_status_transition(
    from_status: WorkflowStatus,
    to_status: WorkflowStatus,
) -> bool:
    if to_status not in WORKFLOW_STATUS_TRANSITIONS[from_status]:
        raise ValueError(f"invalid workflow status transition: {from_status} -> {to_status}")
    return True


def validate_stage_status_transition(
    from_status: StageStatus,
    to_status: StageStatus,
) -> bool:
    if to_status not in STAGE_STATUS_TRANSITIONS[from_status]:
        raise ValueError(f"invalid stage status transition: {from_status} -> {to_status}")
    return True


def validate_decision_record_contract(decision: DecisionRecord) -> bool:
    if decision.decision_type == DecisionType.REVISIT and not decision.target_stage:
        raise ValueError("revisit decision requires target_stage")

    if decision.decision_type in {
        DecisionType.CONTINUE,
        DecisionType.EXIT_SUCCESS,
        DecisionType.EXIT_BUDGET,
        DecisionType.EXIT_CONVERGED,
    } and decision.target_stage is not None:
        raise ValueError(f"{decision.decision_type} decision must not set target_stage")

    return True