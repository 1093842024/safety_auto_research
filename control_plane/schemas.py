from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import Field

from ..platform_contracts.enums import DecisionType
from ..platform_contracts.enums import GateResult
from ..platform_contracts.enums import RunType
from ..platform_contracts.enums import StageStatus


class CreateWorkflowRunRequest(BaseModel):
    program_id: str
    run_type: RunType
    entry_stage: str
    target_id: str
    objective_snapshot: dict[str, Any] = Field(default_factory=dict)
    requested_outcomes: list[str] = Field(default_factory=list)


class DualLoopRequest(BaseModel):
    """Body for the dual-loop driver (inner research -> outer audit -> recursive improvement)."""

    inner_capability: str = "kaggle_eval"
    inner_params: dict[str, Any] = Field(default_factory=dict)
    audit_params: dict[str, Any] = Field(default_factory=dict)
    max_outer_iters: int = 3
    agent_inner: bool = False


class InnerLoopConfig(BaseModel):
    """Researcher-facing configuration of the *inner research loop*.

    The inner loop is where the actual optimization happens (the agent / experiment that
    tries to beat the baseline). Until now it exposed almost no knobs, which made deeper
    optimization impossible. This config surfaces the full surface:

    * ``mode``        — ``scripted`` (deterministic executor, e.g. ``kaggle_eval``) or
                        ``agent`` (an autonomous inner-loop agent). ``agent`` mode requires a
                        ``RemoteAgentHarness`` to be wired into the orchestrator; otherwise it
                        falls back to ``scripted`` so the run still executes.
    * Data knobs      — ``preset`` / ``data_dir`` / ``cv_folds`` / ``threshold`` / ``drop_cols``
                        are consumed by the scripted path *and* forwarded to the agent.
    * Agent knobs     — ``system_prompt`` / ``skills`` / ``tools`` / ``step_plan`` are only used
                        when ``mode == "agent"`` and a remote agent is configured. They describe
                        the prompt, the skills the agent may load, the capabilities/tools it may
                        invoke, and the ordered steps it should follow.

    Everything is persisted into ``objective_snapshot.config.inner_loop`` so a run is fully
    reproducible from its recorded settings.
    """

    mode: str = Field(default="scripted", pattern="^(scripted|agent)$")
    # --- data / task knobs (used by scripted path, also forwarded to an agent) ---
    preset: str | None = None  # titanic | spaceship | None => use the task's own preset
    model: str = "gbm"  # gbm | gbm-strong | rf | logreg
    fe: str = "basic"  # basic | rich
    cv_folds: int = Field(default=5, ge=1, le=20)
    threshold: float | None = None  # target-metric gate; None => task's default gate
    drop_cols: list[str] = Field(default_factory=list)
    data_dir: str | None = None  # override the dataset directory
    # --- agent-mode only (forwarded to a RemoteAgentHarness when configured) ---
    system_prompt: str = ""
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)  # enabled capability / tool ids
    step_plan: list[str] = Field(default_factory=list)  # ordered capability ids
    # Which agent CLI drives the inner loop when mode == "agent".
    # "codex" | "claude_code" | None (= use env AGENT_COMMAND generic transport).
    agent_cli: str | None = None
    # Human-in-the-loop collaboration mode:
    # "autonomous"  — fully automatic (default)
    # "step_confirm"— pause after each major step (inner / audit / outer) for human review
    # "outer_confirm"— pause only after each outer-loop iteration before the next one
    collaboration_mode: str = Field(default="autonomous", pattern="^(autonomous|step_confirm|outer_confirm)$")


class LaunchBenchmarkRequest(BaseModel):
    """Optional researcher-facing configuration when launching a benchmark task as a research run.

    Every field has a sane default so the endpoint stays backward-compatible with clients that
    send no body at all. The chosen values are persisted into the run's ``objective_snapshot.config``
    and forwarded into the dual-loop driver. The richer inner-loop settings live in ``inner_loop``;
    the top-level ``model`` / ``fe`` are kept only for backward compatibility and are folded into
    ``inner_loop`` when ``inner_loop`` is not provided.
    """

    audit_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    max_outer_iters: int = Field(default=3, ge=1, le=10)
    model: str = "gbm"
    fe: bool = False
    inner_loop: InnerLoopConfig | None = None
    # Agent CLI selection for agent mode (codex / claude_code / None => env AGENT_COMMAND).
    agent_cli: str | None = None


class RegisterTaskRequest(BaseModel):
    """Register a custom research task in the console.

    ``task_type`` selects one of the schema-driven task-type specs
    (see ``benchmark_tasks.registry.TASK_TYPE_SPECS``); ``values`` carries the
    filled-in registration form (common fields + type-specific fields). The
    backend validates values against the spec and persists the task so it shows
    up in the ``/benchmark-tasks`` catalog (category=custom).
    """

    task_type: str
    values: dict[str, Any] = Field(default_factory=dict)


class ValidateTaskRequest(BaseModel):
    """Live validation of a task-registration form (no persistence)."""

    task_type: str
    values: dict[str, Any] = Field(default_factory=dict)


class ReportMetricRequest(BaseModel):
    """Manually report a metric for a (tracked-only) run to enter the leaderboard."""

    metric_name: str
    direction: str = "higher"  # higher | lower
    score: float
    config: dict[str, Any] = Field(default_factory=dict, alias="config_snapshot")


class EvaluateRunRequest(BaseModel):
    """Trigger task-type-appropriate evaluation of an external run's outputs."""

    eval_dataset_path: str | None = None
    predictions_path: str | None = None
    ranked_lists_path: str | None = None
    judge_url: str | None = None


class DebugRequest(BaseModel):
    """Run one stage of the dual loop in isolation for debugging.

    - ``stage``: "inner" (inner loop eval) or "outer" (external audit).
    - ``audit_input_override``: optional manual audit input for outer-stage debug
      (when absent, auto-derived from the most recent inner-loop result).
    """

    stage: str  # "inner" | "outer"
    audit_input_override: dict[str, Any] | None = None


class CollaborationAdjustments(BaseModel):
    """Adjustments a human makes during collaboration pauses.

    Fields are optional — only provided values take effect in the next inner-loop iteration.
    """

    model: str | None = None
    fe: str | None = None  # "basic" | "rich"
    cv_folds: int | None = None
    threshold: float | None = None
    audit_threshold: float | None = None
    data_dir: str | None = None
    action: str = "continue"  # "continue" | "abort" | "restart"
    note: str | None = None


class ResolveCollaborationRequest(BaseModel):
    """Resolve a collaboration pause (approve/reject with optional adjustments)."""

    resolution: str  # "approved" | "rejected"
    reviewer: str = "human"
    adjustments: CollaborationAdjustments | None = None


class CreateStageRunRequest(BaseModel):
    stage_code: str
    executor_family: str
    reviewer_family: str | None = None
    gate_result: GateResult = GateResult.PENDING
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)


class UpdateStageStatusRequest(BaseModel):
    to_status: StageStatus


class RecordDecisionRequest(BaseModel):
    decision_type: DecisionType
    target_stage: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    policy_hits: list[str] = Field(default_factory=list)
    approved_by: list[str] = Field(default_factory=list)


class RequestApprovalRequest(BaseModel):
    subject_type: str
    reason: str
    policy_ref: str
    required_roles: list[str] = Field(default_factory=list)
    risk_tier: str | None = None


class ResolveApprovalRequest(BaseModel):
    resolution: str  # "approved" | "rejected"
    resolved_by: str
    note: str | None = None


class MessageResponse(BaseModel):
    detail: str
    run_id: str | None = None
    stage_run_id: str | None = None
    decision_id: str | None = None
    approval_id: str | None = None
    event_id: str | None = None


class DispatchRequest(BaseModel):
    """Dispatch a single stage to its execution-plane adapter."""

    stage_code: str
    executor_family: str = "default"
    params: dict[str, Any] = Field(default_factory=dict)


class CapabilityRunRequest(BaseModel):
    """Run an infrastructure-layer capability (the agent-facing ``run_capability`` tool)."""

    params: dict[str, Any] = Field(default_factory=dict)
