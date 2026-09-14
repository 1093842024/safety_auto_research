from __future__ import annotations

from enum import Enum


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ProgramStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class TargetType(str, Enum):
    CLASSIFIER = "classifier"
    JUDGE = "judge"
    ROUTER = "router"
    RAG_GUARD = "rag_guard"
    AGENT_GUARD = "agent_guard"
    MULTIMODAL_DETECTOR = "multimodal_detector"


class Modality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    CODE = "code"
    MULTIMODAL = "multimodal"


class RunType(str, Enum):
    STANDARD_RESEARCH = "standard_research"
    BADCASE_RETRAIN = "badcase_retrain"
    ADVERSARIAL_HARDENING = "adversarial_hardening"
    # B 飞轮型：方案冻结，数据飞轮（badcase 回流 → 回放重训 → 回归门）。见
    # doc/auto_research_task_taxonomy.md §四/§七.1 与 badcase_retrain_executor.py。
    FLYWHEEL = "flywheel"
    # A 探索型：Discovery Loop，从零到方案（数据集构建 + 文献检索 + 开放域多方案探索）。
    # 见 doc/auto_research_task_taxonomy.md §五.A。``layer_01_literature_research`` 与
    # ``layer_05_data_evaluation_cleaning`` 由 stub 升级为真实能力后，run_type=DISCOVERY
    # 的工作流可直接串联它们。
    DISCOVERY = "discovery"


class WorkflowStatus(str, Enum):
    REQUESTED = "requested"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXITED_BUDGET = "exited_budget"
    EXITED_CONVERGED = "exited_converged"
    CANCELLED = "cancelled"


class StageStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GateResult(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    WAIVED = "waived"


class ArtifactType(str, Enum):
    PAPER_SET = "paper_set"
    IDEA_CARD = "idea_card"
    DESIGN_SPEC = "design_spec"
    DATASET_RELEASE = "dataset_release"
    CODE_PATCH = "code_patch"
    MODEL_CHECKPOINT = "model_checkpoint"
    EVAL_REPORT = "eval_report"
    ATTACK_REPORT = "attack_report"
    LESSON_CARD = "lesson_card"
    AUDIT_REPORT = "audit_report"
    IMPROVEMENT_REPORT = "improvement_report"
    HYPOTHESIS_TREE = "hypothesis_tree"
    EXPERIENCE_BANK = "experience_bank"
    # Task-specific executable scoring rubric (layer_12_rubric_induction). Produced
    # ONCE per run, frozen, and consumed by the outer audit as its constraint set.
    RUBRIC = "rubric"


class Visibility(str, Enum):
    PRIVATE = "private"
    RESTRICTED = "restricted"
    INTERNAL = "internal"
    PUBLIC = "public"


class PiiStatus(str, Enum):
    UNKNOWN = "unknown"
    REDACTED = "redacted"
    BLOCKED = "blocked"
    APPROVED_SENSITIVE = "approved_sensitive"


class RegistryStatus(str, Enum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    PRODUCTION = "production"
    ARCHIVED = "archived"


class DecisionType(str, Enum):
    CONTINUE = "continue"
    REVISIT = "revisit"
    EXIT_SUCCESS = "exit_success"
    EXIT_BUDGET = "exit_budget"
    EXIT_CONVERGED = "exit_converged"
    HITL_REQUIRED = "hitl_required"


class EventType(str, Enum):
    PROGRAM_CREATED = "program_created"
    WORKFLOW_REQUESTED = "workflow_requested"
    WORKFLOW_STARTED = "workflow_started"
    STAGE_QUEUED = "stage_queued"
    STAGE_STARTED = "stage_started"
    ARTIFACT_PUBLISHED = "artifact_published"
    GATE_PASSED = "gate_passed"
    GATE_FAILED = "gate_failed"
    EVAL_COMPLETED = "eval_completed"
    ATTACK_COMPLETED = "attack_completed"
    DECISION_ISSUED = "decision_issued"
    LESSON_PROMOTED = "lesson_promoted"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    WORKFLOW_FINISHED = "workflow_finished"
    STAGE_CANCELLED = "stage_cancelled"
    AUDIT_COMPLETED = "audit_completed"
    AUDIT_FOLLOWUP = "audit_followup"
    IMPROVEMENT_APPLIED = "improvement_applied"
    AGENT_STEP = "agent_step"
    DEBUG_RESULT = "debug_result"
    # A task-specific executable rubric was induced (or an existing standard reviewed)
    # by layer_12 before the research loop started.
    RUBRIC_SYNTHESIZED = "rubric_synthesized"