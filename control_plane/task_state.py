"""Explicit trusted task state for the MEA loop (Phase P1 of the LongHorizon-Harness upgrade).

The paper (arXiv:2608.01964v1) reformulates long-horizon execution as *task-state
management*: a single structured ``TaskState`` lives OUTSIDE the executor's context and
is updated ONLY with facts independently verified by the (read-only) auditor. The
executor's own summary (``o_i``) never mutates the persisted state directly — a record is
marked ``completed`` only when an ``AuditVerdict`` (``v_i``) supports it with evidence.

This module is the source of truth the Manager reads/writes and the Auditor verifies
against. It is intentionally model-free and deterministic so it can be unit-tested
without any LLM/CLI backend.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel
from pydantic import Field

# Per-run task state is persisted here (mirrors the paper's ``runs/<run-id>/`` isolation).
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "task_state")

# Default research plan: the Manager decomposes the objective into these subtasks.
# Each maps (best-effort) to a capability the executor may run; the deterministic path
# simply records completion when no capability runner is wired.
DEFAULT_PLAN = [
    "literature_search",
    "hypothesis_gen",
    "experiment_design",
    "experiment_code",
    "eval_metrics",
]

# subtask_type -> capability_id (P4: now wired to the REAL registered capabilities so
# the MEA executor actually runs them end-to-end instead of raising NotFoundError).
# Every target is offline-safe: layer_01/02/04/06 are StubCapabilityExecutor (write a
# real artifact through the SDK), layer_03_eval is the deterministic EvalExecutor
# (hashed-seed metric + emits EvalCompletedEvent). layer_08/09/10 bind real executors
# that operate on in-repo state. kaggle_eval (network/HF data) is intentionally NOT
# used by the default plan — swap it in only when HF data is available.
SUBTASK_TO_CAPABILITY: dict[str, str] = {
    "literature_search": "layer_01_literature_research",
    "hypothesis_gen": "layer_02_idea_generation_evaluation",
    "experiment_design": "layer_04_experiment_design",
    "experiment_code": "layer_06_code_development",
    "eval_metrics": "layer_03_eval",
    "self_evolve_patch": "layer_09_self_iterative_evolution",
    "evolution_orchestrate": "layer_10_adversarial_data_generation",
    "report_synthesis": "layer_08_result_analysis_experience",
}

# Canonical R0–R10 research-step catalog (doc §5). Drives both the per-step agent
# routing and the cost model: the *reserved strong reasoning model* is spent only on
# budget-saving meta/decision steps (R0 Manager + R7 meta-decider); execution steps
# (R1–R5, R8–R10) use their cheap/appropriate agent; R6 Auditor uses an INDEPENDENT
# model (anti-self-confirmation), not the reserved strong model.
RESEARCH_STEPS: dict[str, dict] = {
    "R0":  {"label": "目标拆解 + 状态维护 (Manager)", "role": "manager", "subtask_type": None,
            "uses_strong_model": True,
            "rationale": "长程规划+策略，需强推理；决策经 validate_route 守卫"},
    "R1":  {"label": "文献 / SOTA 调研", "role": "executor", "subtask_type": "literature_search",
            "uses_strong_model": False,
            "rationale": "需联网/检索/embedding，编码 agent 不擅长"},
    "R2":  {"label": "假设生成 / idea", "role": "executor", "subtask_type": "hypothesis_gen",
            "uses_strong_model": False,
            "rationale": "发散+可证伪性判断（用推理 LLM，非最贵 meta 模型，归 mid 档）"},
    "R3":  {"label": "实验设计 / 协议", "role": "executor", "subtask_type": "experiment_design",
            "uses_strong_model": False,
            "rationale": "把假设转成可评测协议（mid 档推理 LLM）"},
    "R4":  {"label": "实现 / 写代码", "role": "executor", "subtask_type": "experiment_code",
            "uses_strong_model": False,
            "rationale": "fresh-context 编码 agent，避免长上下文漂移"},
    "R5":  {"label": "评测 / 指标计算", "role": "executor", "subtask_type": "eval_metrics",
            "uses_strong_model": False,
            "rationale": "指标必须可复现、零幻觉；默认确定性（无 LLM）"},
    "R6":  {"label": "外部审计 (Auditor)", "role": "auditor", "subtask_type": None,
            "uses_strong_model": False, "independent_model": True,
            "rationale": "★防自确认：审计与执行异模型+确定性约束+只读"},
    "R7":  {"label": "元决策 / 路由", "role": "meta_decider", "subtask_type": None,
            "uses_strong_model": True,
            "rationale": "与 R0 可同模型；输出经 validate_route 守卫；只在省轮次调用"},
    "R8":  {"label": "自进化补丁提案", "role": "executor", "subtask_type": "self_evolve_patch",
            "uses_strong_model": False,
            "rationale": "谨慎 LLM；必须过冻结 verifier + 可证伪预测 + 真回滚"},
    "R9":  {"label": "进化搜索编排", "role": "executor", "subtask_type": "evolution_orchestrate",
            "uses_strong_model": False,
            "rationale": "种群管理确定性；算子后端可配"},
    "R10": {"label": "报告综合", "role": "executor", "subtask_type": "report_synthesis",
            "uses_strong_model": False,
            "rationale": "综合已验证 findings 成稿，不引入新事实"},
}

# Steps that consume the reserved strong reasoning model (R0 Manager + R7 meta-decider).
STRONG_MODEL_STEPS = frozenset(
    s for s, v in RESEARCH_STEPS.items() if v.get("uses_strong_model")
)

# Acceptance criteria per subtask type (used by the deterministic Manager fallback
# and as a default when the LLM Manager omits them).
_ACCEPTANCE: dict[str, list[str]] = {
    "literature_search": ["产出 SOTA 综述与关键引用", "记录信息来源"],
    "hypothesis_gen": ["产出 ≥1 条可证伪假设", "假设含可观测预测"],
    "experiment_design": ["产出可评测实验协议", "定义主指标与阈值"],
    "experiment_code": ["代码可运行并产出指标", "评测为真实（非 mock）"],
    "eval_metrics": ["给出主指标数值", "held-out 一致 gap 受控"],
    "self_evolve_patch": ["补丁经冻结 verifier 验证", "含可证伪预测且可真回滚"],
    "evolution_orchestrate": ["种群按代进化且多样性保持", "最优个体已入档"],
    "report_synthesis": ["报告仅含已验证 findings", "不引入新事实"],
}


class SubtaskSpec(BaseModel):
    """One node of the Manager's decomposition (paper's planned subtask).

    Produced by the Manager agent (LLM) or a deterministic fallback. ``depends_on``
    lists ``subtask_type`` names that must finish first; the MEA loop schedules around
    it so the Executor always runs a dependency-ready, bounded contract.
    """

    subtask_type: str
    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    boundary_constraints: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


def default_plan(objective: str) -> list[SubtaskSpec]:
    """Deterministic fallback decomposition: a linear literature→…→eval chain.

    Used when no LLM Manager is wired (or it fails); keeps the MEA loop working with a
    sensible, dependency-ordered plan so the Executor never runs a subtask before its
    prerequisites are verified ``completed``.
    """

    specs: list[SubtaskSpec] = []
    prev: str | None = None
    for st in DEFAULT_PLAN:
        spec = SubtaskSpec(
            subtask_type=st,
            goal=f"{objective}\n\n子任务 {st}",
            acceptance_criteria=list(_ACCEPTANCE.get(st, ["子任务产出已记录"])),
            boundary_constraints=[
                "不得修改受保护 artifacts（harness 源码、其他 run 的产物、gold 数据）",
                "不得调用 layer_11 / layer_09 自评自身结果",
            ],
            depends_on=[prev] if prev else [],
        )
        specs.append(spec)
        prev = st
    return specs


class StateRecord(BaseModel):
    """One trusted fact in the task state.

    ``status`` is only ever promoted to ``completed`` by an ``AuditVerdict`` carrying
    evidence (``evidence_refs``). The executor's own summary cannot set it.
    """

    kind: str  # "requirement" | "artifact" | "fact"
    key: str
    status: str = "pending"  # completed | pending | blocked | untrusted
    content: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    updated_by_audit: str | None = None


class SubtaskContract(BaseModel):
    """A bounded subtask the Manager hands to a fresh-context Executor.

    Mirrors the paper's ``c_i``: goal + acceptance criteria + boundary constraints +
    prior evidence. ``record_key`` links it back to the ``StateRecord`` it must update.
    """

    subtask_type: str
    goal: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    boundary_constraints: list[str] = Field(default_factory=list)
    prior_evidence_refs: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    capability_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    record_key: str | None = None


class AuditVerdict(BaseModel):
    """The read-only Auditor's verified finding (paper's ``v_i``).

    Carries three finding classes (paper §2.4): completion / integrity / state-update.
    """

    completion: str = "incomplete"  # complete | incomplete | blocked
    integrity: str = "clean"  # clean | suspect | violation
    state_updates: list[StateRecord] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    rationale: str = ""
    audit_run_id: str | None = None


class TaskState(BaseModel):
    """The single trusted task state for one research run."""

    run_id: str
    objective: str
    records: dict[str, StateRecord] = Field(default_factory=dict)
    plan: list[SubtaskSpec] = Field(default_factory=list)  # Manager's decomposition (DAG)
    open_gaps: list[str] = Field(default_factory=list)
    audit_log: list[str] = Field(default_factory=list)  # verified audit run ids + evidence refs

    # ----------------------------------------------------------------- build
    @classmethod
    def from_plan(cls, run_id: str, objective: str, plan: list[SubtaskSpec]) -> "TaskState":
        """Build the trusted state from the Manager's decomposition.

        One ``requirement`` record per ``SubtaskSpec`` (keyed by ``subtask.<type>``), so
        the loop can schedule around ``depends_on`` edges and the Auditor can update the
        right record by ``record_key``.
        """

        records: dict[str, StateRecord] = {}
        for spec in plan:
            key = f"subtask.{spec.subtask_type}"
            records[key] = StateRecord(
                kind="requirement",
                key=key,
                status="pending",
                content={
                    "subtask_type": spec.subtask_type,
                    "depends_on": list(spec.depends_on),
                    "order": 0,
                },
            )
        return cls(run_id=run_id, objective=objective, records=records, plan=list(plan))

    @classmethod
    def from_objective(
        cls, run_id: str, objective: str, plan: list[str] | None = None
    ) -> "TaskState":
        """Back-compat: build from an explicit list of subtask types, or the default."""
        if plan is None:
            specs = default_plan(objective)
        else:
            specs = [
                SubtaskSpec(
                    subtask_type=st,
                    goal=f"{objective}\n\n子任务 {st}",
                    acceptance_criteria=list(_ACCEPTANCE.get(st, ["子任务产出已记录"])),
                )
                for st in plan
            ]
        return cls.from_plan(run_id, objective, specs)

    # ----------------------------------------------------------- manager: next
    def next_subtask_contract(self) -> SubtaskContract | None:
        """Manager step: pick the next dependency-ready, pending subtask and bound it.

        Honors the decomposition order and ``depends_on`` edges: a subtask is only
        offered once every subtask it depends on has been verified ``completed`` (paper
        §2.3 — the Manager emits bounded, dependency-ordered contracts ``c_i``).
        """

        if not self.plan:
            # Legacy state built without a plan: fall back to record insertion order.
            return self._next_from_records()
        for spec in self.plan:
            key = f"subtask.{spec.subtask_type}"
            rec = self.records.get(key)
            if rec is None or rec.status != "pending":
                continue
            if all(self._dep_met(d) for d in spec.depends_on):
                return self._contract_for(spec, key)
        return None

    def _dep_met(self, dep_subtask_type: str) -> bool:
        rec = self.records.get(f"subtask.{dep_subtask_type}")
        return rec is not None and rec.status == "completed"

    def _contract_for(self, spec: SubtaskSpec, key: str) -> SubtaskContract:
        prior: list[str] = []
        for r in self.records.values():
            if r.status == "completed" and r.evidence_refs:
                prior.extend(r.evidence_refs)
        return SubtaskContract(
            subtask_type=spec.subtask_type,
            goal=spec.goal or f"{self.objective}\n\n子任务 {spec.subtask_type}",
            acceptance_criteria=spec.acceptance_criteria or list(
                _ACCEPTANCE.get(spec.subtask_type, ["子任务产出已记录"])
            ),
            boundary_constraints=spec.boundary_constraints or [
                "不得修改受保护 artifacts（harness 源码、其他 run 的产物、gold 数据）",
                "不得调用 layer_11 / layer_09 自评自身结果",
            ],
            prior_evidence_refs=prior[:10],
            params={**spec.params, "subtask_type": spec.subtask_type},
            capability_id=SUBTASK_TO_CAPABILITY.get(spec.subtask_type),
            depends_on=list(spec.depends_on),
            record_key=key,
        )

    def _next_from_records(self) -> SubtaskContract | None:
        pending = [
            r for r in self.records.values()
            if r.kind == "requirement" and r.status == "pending"
        ]
        candidates = pending or [
            r for r in self.records.values()
            if r.kind == "requirement" and r.status == "blocked"
        ]
        if not candidates:
            return None
        rec = candidates[0]
        st = rec.content.get("subtask_type", "unknown")
        return self._contract_for(
            SubtaskSpec(subtask_type=st, goal=f"{self.objective}\n\n子任务 {st}"), rec.key
        )

    @staticmethod
    def _acceptance_for(st: str) -> list[str]:
        return _ACCEPTANCE.get(st, ["子任务产出已记录"])

    # ------------------------------------------------------- auditor: apply
    def apply_verdict(self, v: AuditVerdict) -> None:
        """Update the trusted state using ONLY the auditor's verified findings.

        The executor's summary is never consulted here. An integrity violation blocks any
        promotion to ``completed`` (downgraded to ``untrusted``).
        """

        if v.audit_run_id:
            self.audit_log.append(v.audit_run_id)
        if v.evidence_refs:
            self.audit_log.extend(v.evidence_refs)
        blocked_by_integrity = v.integrity in ("suspect", "violation")
        for upd in v.state_updates:
            rec = self.records.get(upd.key)
            if rec is None:
                # Auditor may introduce new artifact/fact records.
                self.records[upd.key] = upd
                rec = upd
            if blocked_by_integrity and upd.status == "completed":
                rec.status = "untrusted"
            else:
                rec.status = upd.status
            rec.evidence_refs = list(v.evidence_refs)
            rec.updated_by_audit = v.audit_run_id
            rec.content = {**rec.content, **upd.content}
        # Surface gaps so the next fresh executor focuses on what is missing.
        if v.completion in ("incomplete", "blocked") and v.evidence_refs:
            self.open_gaps = list(v.evidence_refs)
        elif v.completion == "complete":
            self.open_gaps = []

    def all_requirements_met(self) -> bool:
        reqs = [r for r in self.records.values() if r.kind == "requirement"]
        return bool(reqs) and all(r.status == "completed" for r in reqs)

    # -------------------------------------------------------- persistence
    def save(self, data_dir: str | None = None) -> str:
        d = data_dir or DATA_DIR
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{self.run_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))
        return path

    @classmethod
    def load(cls, run_id: str, data_dir: str | None = None) -> "TaskState | None":
        d = data_dir or DATA_DIR
        path = os.path.join(d, f"{run_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return cls.model_validate_json(f.read())
