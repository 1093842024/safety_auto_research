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
    # F6 follow-up protocol: supplementary researcher clarifications/questions attached to
    # individual constraints. Forward-compatible aggregation; the canonical record is the
    # AuditFollowupEvent stream (the original audit verdict stays immutable).
    followups: list[dict[str, Any]] = Field(default_factory=list)


class RubricCriterion(ContractModel):
    """One *executable* criterion of a task-specific scoring rubric (layer_12).

    Follows the AutoSciRub ``phi_syn`` criterion contract: a criterion names the
    required analysis, the evidence artifacts it expects, and an observable
    satisfaction condition — so verification can report *which* criterion is unmet
    and *why*, instead of collapsing everything into one opaque score.

    ``check`` is what makes the criterion **executable** on this platform: when it is
    populated, the outer audit evaluates the criterion PROGRAMMATICALLY against the
    inner loop's measured metrics / published artifacts, instead of asking an
    LLM judge whether the claim "sounds" supported. Supported check kinds:

    * ``metric_threshold``  — ``metric`` compared against ``value`` using ``op``
      (``ge`` / ``gt`` / ``le`` / ``lt``).
    * ``metric_present``    — ``metric`` must be reported at all (non-null, finite).
    * ``metric_gap``        — ``abs(metric - baseline_metric) <= value`` (e.g. the
      CV → held-out generalization gap).
    * ``metric_improves``   — ``metric`` beats ``value`` in the objective's direction
      by at least ``margin`` (non-trivial improvement over a baseline).
    * ``artifact_exists``   — an artifact of ``artifact_type`` was published.
    * ``judge``             — no programmatic check available; fall back to the
      LLM/heuristic claim judge (explicitly marked as such, never silently).
    """

    criterion_id: str
    goal_ids: list[str] = Field(default_factory=list)
    requirement: str
    dimension: str = "correctness"  # correctness | generalization | rigor | integrity | reporting
    data_sources: list[str] = Field(default_factory=list)
    required_analysis: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    comparisons: list[str] = Field(default_factory=list)
    expected_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    satisfaction_condition: str
    priority: str = "medium"  # high | medium | low
    weight: float = Field(ge=0.0, default=1.0)
    # Programmatic check spec (see class docstring). ``kind`` is required when set.
    check: dict[str, Any] = Field(default_factory=dict)
    # Traceability: where this criterion came from (instruction / literature / data).
    provenance: dict[str, list[str]] = Field(default_factory=dict)
    # Set when the criterion cannot be satisfied with the currently available inputs
    # (the user's requirement is RETAINED as blocked rather than silently dropped).
    blocked_reason: str = ""


class RubricGoal(ContractModel):
    """An atomic scientific goal derived from the task instruction (``phi_inst``)."""

    goal_id: str
    title: str
    requirement: str
    instruction_evidence: list[str] = Field(default_factory=list)


class ExecutableRubric(ContractModel):
    """A task-specific, executable scoring rubric — the evaluation contract of a run.

    Produced once per run by ``layer_12_rubric_induction`` and then FROZEN: the inner
    loop may read it (it is the execution contract) but can never regenerate or mutate
    it, so an optimizing agent cannot relax its own grading standard.
    """

    rubric_id: str
    task_id: str
    schema_version: str = "1.0"
    # "synthesized" — no usable standard was provided, the rubric was induced;
    # "reviewed"    — a standard was provided and audited (then normalized/extended).
    source: str = "synthesized"
    objective: str = ""
    goals: list[RubricGoal] = Field(default_factory=list)
    criteria: list[RubricCriterion] = Field(default_factory=list)
    claims_to_avoid: list[str] = Field(default_factory=list)
    # The declared evaluation standard this rubric was built from / audited against.
    provided_standard: dict[str, Any] = Field(default_factory=dict)
    # Review of the provided standard (always present; for a synthesized rubric it
    # reports what was missing in the task definition).
    review: "RubricReview | None" = None
    generator: str = "rule_engine"  # rule_engine | llm | llm+rule_engine
    frozen: bool = True
    integrity_hash: str = ""

    def weighted_criteria(self) -> list[tuple[RubricCriterion, float]]:
        """Criteria paired with their effective weight (priority-scaled)."""

        scale = {"high": 2.0, "medium": 1.0, "low": 0.5}
        return [(c, c.weight * scale.get(c.priority, 1.0)) for c in self.criteria]


class RubricFinding(ContractModel):
    """One defect found while auditing a *provided* evaluation standard."""

    finding_id: str
    dimension: str  # accuracy | completeness | scientificity
    severity: str  # critical | important | minor
    message: str
    suggestion: str = ""
    field: str = ""  # which task field the finding is about (eval_metric / gates / ...)


class RubricReview(ContractModel):
    """Three-dimensional audit of a task's declared evaluation standard.

    * ``accuracy``      — is the standard internally consistent and machine-checkable?
      (metric ↔ direction agreement, gate keys parseable, thresholds vs baseline/reference)
    * ``completeness``  — does it cover generalization, comparison/ablation, statistical
      stability, data-leakage protection, and reporting of evidence?
    * ``scientificity`` — does it define success by *method and evidence correctness*
      rather than by agreement with a desired result, and is it gaming-resistant?
    """

    review_id: str
    task_id: str
    # True when the task itself declared a usable evaluation standard (so this is a
    # review); False when nothing usable was declared (so a rubric had to be induced).
    standard_provided: bool = True
    accuracy: float = Field(ge=0.0, le=1.0, default=0.0)
    completeness: float = Field(ge=0.0, le=1.0, default=0.0)
    scientificity: float = Field(ge=0.0, le=1.0, default=0.0)
    overall: float = Field(ge=0.0, le=1.0, default=0.0)
    verdict: str = "acceptable"  # sound | acceptable | needs_work | unusable
    findings: list[RubricFinding] = Field(default_factory=list)
    # Concrete, machine-applicable corrections (field -> suggested value).
    suggested_fixes: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""

    def blocking_findings(self) -> list[RubricFinding]:
        return [f for f in self.findings if f.severity == "critical"]


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
    RubricGoal,
    RubricCriterion,
    RubricFinding,
    RubricReview,
    ExecutableRubric,
)

# ``ExecutableRubric.review`` is a forward reference to ``RubricReview`` (declared
# after it in source order); resolve it so the model is fully usable.
ExecutableRubric.model_rebuild()