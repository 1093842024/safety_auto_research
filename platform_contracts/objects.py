from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .base import ContractModel
from .enums import ArtifactType
from .enums import DecisionType
from .enums import GateResult
from .enums import Modality
from .enums import PiiStatus
from .enums import ProgramStatus
from .enums import RegistryStatus
from .enums import RiskTier
from .enums import RunType
from .enums import StageStatus
from .enums import TargetType
from .enums import Visibility
from .enums import WorkflowStatus


class ResearchProgram(ContractModel):
    program_id: str
    name: str
    domain: str
    owner: str
    goal_statement: str
    risk_tier: RiskTier
    budget_policy_ref: str
    default_policy_pack_ref: str
    status: ProgramStatus


class SafetyTarget(ContractModel):
    target_id: str
    target_type: TargetType
    modality: Modality
    task_family: str
    capabilities: list[str] = Field(default_factory=list)
    threat_model_refs: list[str] = Field(default_factory=list)
    acceptance_policy_ref: str


class WorkflowRun(ContractModel):
    run_id: str
    program_id: str
    run_type: RunType
    entry_stage: str
    target_id: str
    objective_snapshot: dict[str, Any] = Field(default_factory=dict)
    requested_outcomes: list[str] = Field(default_factory=list)
    status: WorkflowStatus
    started_at: datetime
    ended_at: datetime | None = None
    status_detail: str | None = None  # human-readable reason for terminal states (e.g. agent-mode failure)


class StageRun(ContractModel):
    stage_run_id: str
    run_id: str
    stage_code: str
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    executor_family: str
    reviewer_family: str | None = None
    gate_result: GateResult
    status: StageStatus
    retry_count: int = Field(ge=0, default=0)


class Artifact(ContractModel):
    artifact_id: str
    artifact_type: ArtifactType
    uri: str
    schema_version: str
    producer_ref: str
    lineage_parent_ids: list[str] = Field(default_factory=list)
    integrity_hash: str
    visibility: Visibility
    compliance_tags: list[str] = Field(default_factory=list)


class DatasetRelease(ContractModel):
    dataset_id: str
    artifact_ref: str
    source_mix: dict[str, float] = Field(default_factory=dict)
    label_schema_version: str
    pii_status: PiiStatus
    split_policy: str
    retention_policy: str
    quality_report_ref: str
    leakage_report_ref: str


class ModelVersion(ContractModel):
    model_id: str
    base_model: str
    adapter_stack: list[str] = Field(default_factory=list)
    training_recipe_ref: str
    safety_capabilities: list[str] = Field(default_factory=list)
    artifact_ref: str
    registry_status: RegistryStatus


class EvalSuite(ContractModel):
    eval_suite_id: str
    suite_type: str
    task_refs: list[str] = Field(default_factory=list)
    metric_defs: dict[str, str] = Field(default_factory=dict)
    pass_thresholds: dict[str, float] = Field(default_factory=dict)
    sandbox_policy_ref: str
    canary_policy_ref: str


class AttackCampaign(ContractModel):
    campaign_id: str
    target_model_id: str
    attack_taxonomy_refs: list[str] = Field(default_factory=list)
    generation_policy_ref: str
    risk_controls: list[str] = Field(default_factory=list)
    success_metrics: dict[str, float | str] = Field(default_factory=dict)
    replay_buffer_ref: str | None = None


class DecisionRecord(ContractModel):
    decision_id: str
    run_id: str
    decision_type: DecisionType
    target_stage: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    policy_hits: list[str] = Field(default_factory=list)
    approved_by: list[str] = Field(default_factory=list)


class LessonCard(ContractModel):
    lesson_id: str
    scope: str
    source_run_id: str
    pattern_type: str
    applicable_stages: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    payload_ref: str
    prm_score: float = Field(ge=0.0, le=1.0)


class AuditReport(ContractModel):
    """Constraint-wise external audit of an inner-loop answer (dual-loop outer loop)."""

    audit_id: str
    audited_target: str
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_claims: list[str] = Field(default_factory=list)
    rejected_candidates: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    recoverable: bool = True
    audit_confidence: float = Field(ge=0.0, le=1.0)
    recommendation: str
    report_ref: str


class ImprovementProposal(ContractModel):
    """A proposed change to the research *process* (meta-loop), not the artifact."""

    improvement_id: str
    target_mechanism: str
    title: str
    description: str
    patch: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    expected_effect: str
    rollback_id: str
    proposed_by: str = "self_evolution"


class HypothesisNode(ContractModel):
    """A node in the cumulative HypothesisTree (Arbor-style persistence)."""

    node_id: str
    parent_id: str | None = None
    hypothesis: str
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_ref: str | None = None
    insight: str = ""
    score: float = Field(ge=0.0, le=1.0, default=0.0)
    status: str = "active"
    branch: str = "main"
    node_kind: str = "config"  # "config" | "program" (program = OpenMLE atomic-operator node)
    run_id: str | None = None  # owning workflow run (per-run HypothesisTree isolation)


class ExperienceEntry(ContractModel):
    """A pass/fail lesson in the cross-run ExperienceBank (training-free replay)."""

    entry_id: str
    kind: str
    context: str
    lesson: str
    applicable_stages: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    # Lifecycle fields (Phase 2): dedup reinforcement counter + recency marker.
    # Defaults keep pre-Phase-2 persisted rows deserializable.
    uses: int = 0
    seq: int = 0
    # Provenance (M5 fix): the run that produced this lesson (per-run audit/clear).
    source_run_id: str | None = None


class PolicyPack(ContractModel):
    policy_pack_id: str
    domain: str
    rules: dict[str, Any] = Field(default_factory=dict)
    required_approvals: list[str] = Field(default_factory=list)
    model_separation_policy: str
    data_compliance_policy: str
    redteam_constraints: str


ALL_CONTRACT_MODELS: tuple[type[ContractModel], ...] = (
    ResearchProgram,
    SafetyTarget,
    WorkflowRun,
    StageRun,
    Artifact,
    DatasetRelease,
    ModelVersion,
    EvalSuite,
    AttackCampaign,
    DecisionRecord,
    LessonCard,
    PolicyPack,
    AuditReport,
    ImprovementProposal,
    HypothesisNode,
    ExperienceEntry,
)