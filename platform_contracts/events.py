from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from pydantic import Field
from pydantic import model_validator

from .base import ContractModel
from .enums import ArtifactType
from .enums import DecisionType
from .enums import EventType
from .enums import StageStatus
from .enums import WorkflowStatus


def new_event_id() -> str:
    """Allocate a unique event id (prefixed for readability in logs/traces)."""

    return f"evt-{uuid4().hex}"


def utc_now() -> datetime:
    """Current UTC timestamp used as the default event occurrence time."""

    return datetime.now(timezone.utc)


class BasePlatformEvent(ContractModel):
    event_id: str = Field(default_factory=new_event_id)
    event_type: EventType
    run_id: str
    occurred_at: datetime = Field(default_factory=utc_now)


class WorkflowStatusChangedEvent(BasePlatformEvent):
    from_status: WorkflowStatus
    to_status: WorkflowStatus

    @model_validator(mode="after")
    def _validate_event_type(self) -> "WorkflowStatusChangedEvent":
        allowed = {
            EventType.WORKFLOW_REQUESTED,
            EventType.WORKFLOW_STARTED,
            EventType.WORKFLOW_FINISHED,
            EventType.APPROVAL_REQUIRED,
            EventType.APPROVAL_RESOLVED,
        }
        if self.event_type not in allowed:
            raise ValueError("workflow status change event_type is invalid")
        return self


class StageStatusChangedEvent(BasePlatformEvent):
    stage_run_id: str
    from_status: StageStatus
    to_status: StageStatus

    @model_validator(mode="after")
    def _validate_event_type(self) -> "StageStatusChangedEvent":
        allowed = {
            EventType.STAGE_QUEUED,
            EventType.STAGE_STARTED,
            EventType.GATE_PASSED,
            EventType.GATE_FAILED,
            EventType.APPROVAL_REQUIRED,
            EventType.APPROVAL_RESOLVED,
            EventType.STAGE_CANCELLED,
        }
        if self.event_type not in allowed:
            raise ValueError("stage status change event_type is invalid")
        return self


class DecisionRecordedEvent(BasePlatformEvent):
    event_type: EventType = Field(default=EventType.DECISION_ISSUED)
    decision_id: str
    decision_type: DecisionType


class ArtifactPublishedEvent(BasePlatformEvent):
    event_type: EventType = Field(default=EventType.ARTIFACT_PUBLISHED)
    artifact_id: str
    artifact_type: ArtifactType


class ApprovalRequiredEvent(BasePlatformEvent):
    """A HITL gate was opened. Carries the policy/role context for the approval.

    Emitted in addition to the workflow/stage status-change event when a run
    enters ``waiting_approval`` (see ``WorkflowStatusChangedEvent`` /
    ``StageStatusChangedEvent``), so the control plane can route it to reviewers.
    """

    event_type: EventType = Field(default=EventType.APPROVAL_REQUIRED)
    approval_id: str
    subject_type: str
    subject_ref: str
    reason: str
    policy_ref: str
    required_roles: list[str] = Field(default_factory=list)
    risk_tier: str | None = None

    @model_validator(mode="after")
    def _validate_event_type(self) -> "ApprovalRequiredEvent":
        if self.event_type != EventType.APPROVAL_REQUIRED:
            raise ValueError("approval required event_type must be approval_required")
        return self


class ApprovalResolvedEvent(BasePlatformEvent):
    """A previously opened approval was resolved (approved or rejected)."""

    event_type: EventType = Field(default=EventType.APPROVAL_RESOLVED)
    approval_id: str
    resolution: str
    resolved_by: str
    decision_ref: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _validate_event_type(self) -> "ApprovalResolvedEvent":
        if self.event_type != EventType.APPROVAL_RESOLVED:
            raise ValueError("approval resolved event_type must be approval_resolved")
        return self

    @model_validator(mode="after")
    def _validate_resolution(self) -> "ApprovalResolvedEvent":
        if self.resolution not in {"approved", "rejected"}:
            raise ValueError("resolution must be 'approved' or 'rejected'")
        return self


class EvalCompletedEvent(BasePlatformEvent):
    """A benchmark / eval suite finished for a run. Carries the measured metrics."""

    event_type: EventType = Field(default=EventType.EVAL_COMPLETED)
    eval_suite_id: str
    stage_run_id: str | None = None
    passed: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    gate_passed: bool = True
    report_ref: str

    @model_validator(mode="after")
    def _validate_event_type(self) -> "EvalCompletedEvent":
        if self.event_type != EventType.EVAL_COMPLETED:
            raise ValueError("eval completed event_type must be eval_completed")
        return self


class AttackCompletedEvent(BasePlatformEvent):
    """A red-team / adversarial campaign finished. Carries ASR plus anti-forgetting signals."""

    event_type: EventType = Field(default=EventType.ATTACK_COMPLETED)
    campaign_id: str
    target_model_id: str
    stage_run_id: str | None = None
    success_rate: float = Field(ge=0.0, le=1.0)
    total_attempts: int = Field(ge=0)
    successful_attempts: int = Field(ge=0)
    retention_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    forgetting_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    vulnerability_patterns: list[str] = Field(default_factory=list)
    report_ref: str

    @model_validator(mode="after")
    def _validate_event_type(self) -> "AttackCompletedEvent":
        if self.event_type != EventType.ATTACK_COMPLETED:
            raise ValueError("attack completed event_type must be attack_completed")
        return self

    @model_validator(mode="after")
    def _validate_attempt_counts(self) -> "AttackCompletedEvent":
        if self.successful_attempts > self.total_attempts:
            raise ValueError("successful_attempts cannot exceed total_attempts")
        return self


class LessonPromotedEvent(BasePlatformEvent):
    """A LessonCard was promoted from a transient observation into a platform object."""

    event_type: EventType = Field(default=EventType.LESSON_PROMOTED)
    lesson_id: str
    source_run_id: str
    scope: str
    pattern_type: str
    prm_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    applicable_stages: list[str] = Field(default_factory=list)
    payload_ref: str

    @model_validator(mode="after")
    def _validate_event_type(self) -> "LessonPromotedEvent":
        if self.event_type != EventType.LESSON_PROMOTED:
            raise ValueError("lesson promoted event_type must be lesson_promoted")
        return self


class AuditCompletedEvent(BasePlatformEvent):
    """Outer-loop external audit of an inner-loop answer (dual-loop architecture).

    Implements AREX-style constraint-wise audit: the objective is decomposed into
    checkable constraints, each verified/partial/conflict/missing, and the audit emits a
    confidence ``s`` and a recoverability flag ``v`` that drive the
    Accept / Refine / Restart decision. The auditor runs on an *independent* model/context
    from the inner loop to break self-confirmation.
    """

    event_type: EventType = Field(default=EventType.AUDIT_COMPLETED)
    audit_id: str
    audited_target: str
    constraints: list[dict[str, object]] = Field(default_factory=list)
    unresolved_claims: list[str] = Field(default_factory=list)
    rejected_candidates: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    recoverable: bool = True
    audit_confidence: float = Field(ge=0.0, le=1.0)
    gate_passed: bool = True
    report_ref: str

    @model_validator(mode="after")
    def _validate_event_type(self) -> "AuditCompletedEvent":
        if self.event_type != EventType.AUDIT_COMPLETED:
            raise ValueError("audit completed event_type must be audit_completed")
        return self


class AuditFollowupEvent(BasePlatformEvent):
    """A researcher follow-up (clarification / question) on a single audit constraint (F6).

    The outer audit verdict (``AuditCompletedEvent``) is the *immutable* ground truth of the
    dual loop — the follow-up never mutates it. Instead it is a *supplementary* event that
    re-scores one constraint when a human supplies a clarification, or records an open question.
    v1 deliberately does NOT auto-feed the result back into ``IterationRouter`` (kept for later).
    """

    event_type: EventType = Field(default=EventType.AUDIT_FOLLOWUP)
    audit_id: str
    constraint_id: str
    question: str | None = None
    clarification: str | None = None
    prior_status: str
    new_status: str
    new_score: float = Field(ge=0.0, le=1.0)
    response: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    recommendation: str
    resolved: bool = False

    @model_validator(mode="after")
    def _validate_event_type(self) -> "AuditFollowupEvent":
        if self.event_type != EventType.AUDIT_FOLLOWUP:
            raise ValueError("audit followup event_type must be audit_followup")
        return self


class AgentStepEvent(BasePlatformEvent):
    """A single step in an inner-loop agent's *execution flow* (agent mode).

    When an external agent (Codex / Claude Code) drives the inner loop, every
    tool call it makes (and the final answer) is streamed back as one
    ``AgentStepEvent`` so the run record captures *what the agent actually did* —
    not just the final metric. The frontend renders these in a collapsible
    "Agent 执行流水" panel. ``seq`` orders steps within one agent turn.
    """

    event_type: EventType = Field(default=EventType.AGENT_STEP)
    seq: int = Field(default=0, ge=0)
    kind: str  # "tool_call" | "final"
    tool: str | None = None
    args_summary: str = ""
    result_summary: str = ""
    detail: str | None = None

    @model_validator(mode="after")
    def _validate_event_type(self) -> "AgentStepEvent":
        if self.event_type != EventType.AGENT_STEP:
            raise ValueError("agent step event_type must be agent_step")
        return self


class DebugEvent(BasePlatformEvent):
    """A single-stage debug result (inner-loop / outer-audit isolation).

    When the researcher runs a debug against a single stage of the dual loop —
    inner loop (eval) or outer audit — the result is persisted as a ``DebugEvent``
    so the frontend "调试" panel can show the outcome before the full autonomous
    experiment is launched.
    """

    event_type: EventType = Field(default=EventType.DEBUG_RESULT)
    stage: str  # "inner" | "outer"
    ok: bool = True
    summary: str = ""
    metrics: dict[str, float] = Field(default_factory=dict)
    verdict: dict[str, object] | None = None  # outer-audit: confidence/recommendation etc.
    report_ref: str | None = None
    detail: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_event_type(self) -> "DebugEvent":
        if self.event_type != EventType.DEBUG_RESULT:
            raise ValueError("debug event_type must be debug_result")
        return self


class ImprovementAppliedEvent(BasePlatformEvent):
    """Recursive-improvement meta-loop committed a change to the research *process*.

    Distinguishes the dual loop from a single loop: the improvement targets the *mechanism*
    (routing bias / prompt hint / experience / search order), not the artifact. Carries a
    rollback id so a degrading change can be reverted (validate-and-revert).
    """

    event_type: EventType = Field(default=EventType.IMPROVEMENT_APPLIED)
    improvement_id: str
    target_mechanism: str
    proposal_ref: str
    rollback_id: str
    validated_heldout: bool = True
    metrics_before: dict[str, float] = Field(default_factory=dict)
    metrics_after: dict[str, float] = Field(default_factory=dict)
    reverted: bool = False

    @model_validator(mode="after")
    def _validate_event_type(self) -> "ImprovementAppliedEvent":
        if self.event_type != EventType.IMPROVEMENT_APPLIED:
            raise ValueError("improvement applied event_type must be improvement_applied")
        return self


ALL_EVENT_MODELS: tuple[type[ContractModel], ...] = (
    BasePlatformEvent,
    WorkflowStatusChangedEvent,
    StageStatusChangedEvent,
    DecisionRecordedEvent,
    ArtifactPublishedEvent,
    ApprovalRequiredEvent,
    ApprovalResolvedEvent,
    EvalCompletedEvent,
    AttackCompletedEvent,
    LessonPromotedEvent,
    AuditCompletedEvent,
    AuditFollowupEvent,
    AgentStepEvent,
    DebugEvent,
    ImprovementAppliedEvent,
)
