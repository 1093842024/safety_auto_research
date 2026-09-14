"""Induce a task-specific **executable** rubric from a task definition.

This is the "对于未提供评估标准的任务生成正确、有效、科学的评分标准" half of the
rubric stage, and it also *extends* a provided-but-incomplete standard so a reviewed
task ends up with the same executable contract shape.

Pipeline (mirrors AutoSciRub's ``phi_inst`` -> ``phi_data`` -> ``phi_syn``, collapsed
into a deterministic rule engine because this platform's task definitions are already
structured):

1. **goal skeleton** — atomic goals derived from the task instruction + declared metric
   (what the task must address; no methods prescribed yet).
2. **feasibility profile** — what the platform can actually measure for this task type
   (executable vs tracked-only, CV available, held-out available, artifacts published).
3. **criterion synthesis** — one criterion per goal, each carrying a *programmatic*
   ``check`` whenever the platform can evaluate it, an explicit satisfaction condition,
   and provenance. Requirements that cannot be measured here are RETAINED as blocked
   rather than silently dropped (AutoSciRub rule: never drop the user's requirement).

Determinism: identical input -> identical rubric (ids, order, hash). No network.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..platform_contracts.objects import ExecutableRubric
from ..platform_contracts.objects import RubricCriterion
from ..platform_contracts.objects import RubricGoal
from ..platform_contracts.objects import RubricReview
from .spec import TaskSpec

# Default generalization-gap tolerance (CV -> held-out). Matches the existing
# audit_executor heuristic band so behaviour stays consistent with what the platform
# already considered acceptable.
DEFAULT_GAP_TOLERANCE = 0.05
# Minimum improvement over baseline that counts as non-trivial (not measurement noise).
DEFAULT_IMPROVEMENT_MARGIN = 0.005
# Minimum folds / seeds for a statistically meaningful claim.
MIN_FOLDS = 3


def _metric_keys(spec: TaskSpec) -> list[str]:
    """Metric keys (in preference order) the platform may report for this task.

    ``kaggle_eval`` and the sandbox executors report ``accuracy`` as the canonical
    primary key plus ``primary``; custom metrics also appear under their own name.
    """

    keys: list[str] = []
    m = spec.metric
    if m:
        keys.append(m)
        # cv_accuracy is reported as plain "accuracy" by kaggle_eval.
        if m.startswith("cv_"):
            keys.append(m[3:])
    keys += ["accuracy", "primary"]
    out: list[str] = []
    for k in keys:
        if k not in out:
            out.append(k)
    return out


def _goal_skeleton(spec: TaskSpec) -> list[RubricGoal]:
    """Atomic scientific goals implied by the task definition."""

    goals: list[RubricGoal] = []
    instr = spec.objective or spec.name or spec.task_id
    direction_zh = "越低越好" if spec.lower_is_better else "越高越好"

    goals.append(RubricGoal(
        goal_id="G1",
        title="达成主指标目标",
        requirement=(
            f"在任务声明的评测协议下测得主指标 {spec.eval_metric or '(未声明)'}"
            f"（{direction_zh}），并达到通过门限。"
        ),
        instruction_evidence=[instr[:300]],
    ))
    goals.append(RubricGoal(
        goal_id="G2",
        title="证明改进真实且可泛化",
        requirement=(
            "结果必须是真实测量而非模拟，且在未参与调优的数据上复核，"
            "确认提升不是对评测集的过拟合。"
        ),
        instruction_evidence=[f"baseline={spec.baseline}", f"reference={spec.reference}"],
    ))
    goals.append(RubricGoal(
        goal_id="G3",
        title="结论有可核验证据支撑",
        requirement=(
            "每个结论都关联可核验产出（评测报告 / 结果文件 / 可复现配置），"
            "且不包含超出证据的断言。"
        ),
        instruction_evidence=[spec.eval_method[:300] or "任务未声明评估方法"],
    ))
    if spec.comparison_gates:
        goals.append(RubricGoal(
            goal_id="G4",
            title="满足全部约束型门限",
            requirement=(
                "在优化主指标的同时不破坏任务声明的约束型门限："
                + "、".join(f"{k}{v}" for k, v in sorted(spec.comparison_gates.items()))
            ),
            instruction_evidence=[json.dumps(spec.gates, ensure_ascii=False, sort_keys=True)[:300]],
        ))
    return goals


def _c(  # noqa: PLR0913 - a criterion genuinely has this many facets
    cid: str,
    goal_ids: list[str],
    requirement: str,
    satisfaction: str,
    *,
    dimension: str,
    priority: str = "medium",
    weight: float = 1.0,
    metrics: list[str] | None = None,
    analysis: list[str] | None = None,
    comparisons: list[str] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    check: dict[str, Any] | None = None,
    provenance: dict[str, list[str]] | None = None,
    blocked_reason: str = "",
) -> RubricCriterion:
    return RubricCriterion(
        criterion_id=cid,
        goal_ids=goal_ids,
        requirement=requirement,
        dimension=dimension,
        metrics=metrics or [],
        required_analysis=analysis or [],
        comparisons=comparisons or [],
        expected_artifacts=artifacts or [],
        satisfaction_condition=satisfaction,
        priority=priority,
        weight=weight,
        check=check or {"kind": "judge"},
        provenance=provenance or {},
        blocked_reason=blocked_reason,
    )


def _synthesize_criteria(spec: TaskSpec, review: RubricReview) -> list[RubricCriterion]:
    """Turn goals + declared standard + platform feasibility into executable criteria."""

    out: list[RubricCriterion] = []
    metric_label = spec.eval_metric or "primary"
    keys = _metric_keys(spec)
    op = "le" if spec.lower_is_better else "ge"
    target = spec.target_value
    prov_std = ["任务声明: eval_metric/direction/gates"]
    prov_syn = ["规则引擎: 未声明标准，按任务类型补全"]

    # ---- C1: primary metric measured at all (always programmatic) -------------
    out.append(_c(
        "C1", ["G1"],
        f"主指标 {metric_label} 必须由真实评测测得并上报。",
        f"运行产出的指标字典中存在 {metric_label}（或等价键 {keys[:3]}）且为有限数值。",
        dimension="correctness", priority="high", weight=2.0,
        metrics=[metric_label],
        analysis=["在任务声明的评测协议下运行评测并上报指标"],
        artifacts=[{"type": "number", "name": metric_label,
                    "must_show": "真实测量得到的主指标数值"}],
        check={"kind": "metric_present", "metric": keys},
        provenance={"instruction": prov_std},
    ))

    # ---- C2: primary metric meets the gate -----------------------------------
    if target is not None:
        cmp_zh = "≤" if spec.lower_is_better else "≥"
        out.append(_c(
            "C2", ["G1"],
            f"主指标 {metric_label} 必须达到通过门限（{cmp_zh} {target}）。",
            f"{metric_label} {cmp_zh} {target}。",
            dimension="correctness", priority="high", weight=2.0,
            metrics=[metric_label],
            analysis=["与通过门限比较"],
            comparisons=[f"门限 {target}"],
            check={"kind": "metric_threshold", "metric": keys, "op": op, "value": target},
            provenance={"instruction": prov_std},
        ))
    else:
        out.append(_c(
            "C2", ["G1"],
            f"主指标 {metric_label} 必须达到一个事先声明的通过门限。",
            "任务未声明门限，需研究者或平台先固定门限再判定（当前无法程序化判定）。",
            dimension="correctness", priority="high", weight=1.0,
            metrics=[metric_label],
            analysis=["先声明门限，再比较"],
            check={"kind": "judge"},
            provenance={"instruction": prov_syn},
            blocked_reason="任务未声明通过门限（threshold / target_value），无法程序化判定达标。",
        ))

    # ---- C3: non-trivial improvement over baseline ---------------------------
    if spec.baseline is not None:
        out.append(_c(
            "C3", ["G1", "G2"],
            f"结果必须相对基线 baseline={spec.baseline} 构成非平凡改进"
            f"（至少 {DEFAULT_IMPROVEMENT_MARGIN} 的方向性提升）。",
            f"{metric_label} 在 {spec.direction} 方向上优于 {spec.baseline} 且差距 ≥ "
            f"{DEFAULT_IMPROVEMENT_MARGIN}。",
            dimension="correctness", priority="high", weight=1.5,
            metrics=[metric_label],
            analysis=["与基线对比"],
            comparisons=[f"baseline={spec.baseline}"],
            check={"kind": "metric_improves", "metric": keys, "op": op,
                   "value": spec.baseline, "margin": DEFAULT_IMPROVEMENT_MARGIN},
            provenance={"instruction": prov_std},
        ))
    else:
        out.append(_c(
            "C3", ["G1", "G2"],
            "结果必须相对一个明确的对照（基线方法 / 默认配置）构成可归因的改进。",
            "报告中给出对照配置及其指标，且本次结果优于该对照。",
            dimension="correctness", priority="medium",
            metrics=[metric_label],
            analysis=["建立并测量一个对照配置"],
            comparisons=["自建基线"],
            check={"kind": "judge"},
            provenance={"instruction": prov_syn},
            blocked_reason="任务未声明 baseline，需研究过程自行建立对照后才能程序化判定。",
        ))

    # ---- C4: evaluation is genuine, not mocked -------------------------------
    out.append(_c(
        "C4", ["G2", "G3"],
        "指标必须来自真实评测执行，不得来自模拟 / 占位 / 硬编码值。",
        "存在指向真实评测执行的报告引用（report_ref）且标记为真实评测。",
        dimension="integrity", priority="high", weight=1.5,
        analysis=["检查评测执行来源"],
        artifacts=[{"type": "file", "name": "eval_report",
                    "must_show": "真实评测执行的报告引用"}],
        check={"kind": "real_eval"},
        provenance={"instruction": ["平台不变量：禁止以模拟结果冒充测量"]},
    ))

    # ---- C5: generalization (CV -> held-out gap) ------------------------------
    gap_tol = DEFAULT_GAP_TOLERANCE
    if spec.executable:
        out.append(_c(
            "C5", ["G2"],
            f"调优所用指标与未参与调优的留出集指标差距不得超过 {gap_tol}"
            "（防止反复调参把评测分数刷上去而泛化性不变）。",
            f"|调优指标 − 留出集指标| ≤ {gap_tol}；缺少留出集测量时本条不通过。",
            dimension="generalization", priority="high", weight=1.5,
            metrics=[metric_label, "heldout_accuracy"],
            analysis=["在从未用于调优的留出集上复核一次"],
            comparisons=["交叉验证 vs 留出集"],
            check={"kind": "metric_gap", "metric": keys,
                   "baseline_metric": ["heldout_accuracy", "heldout_primary", "test_accuracy"],
                   "value": gap_tol},
            provenance={"instruction": ["科学性要求：泛化性复核"]},
        ))
    else:
        out.append(_c(
            "C5", ["G2"],
            "必须在未参与调优的数据划分上复核结果，并报告与调优指标的差距。",
            "报告中给出留出集 / 测试集指标及其与调优指标的差距。",
            dimension="generalization", priority="high", weight=1.5,
            metrics=[metric_label],
            analysis=["留出集复核"],
            check={"kind": "judge"},
            provenance={"instruction": ["科学性要求：泛化性复核"]},
            blocked_reason="该任务由外部 harness 执行，平台无法直接读取留出集指标。",
        ))

    # ---- C6: statistical stability -------------------------------------------
    folds = spec.cv_folds
    if folds is not None and folds >= MIN_FOLDS:
        out.append(_c(
            "C6", ["G2"],
            f"结论必须建立在 ≥{MIN_FOLDS} 折交叉验证之上，而非单次划分。",
            f"评测使用 {folds} 折交叉验证（≥{MIN_FOLDS}）。",
            dimension="rigor", priority="medium",
            analysis=[f"{folds} 折交叉验证"],
            check={"kind": "config_min", "field": "cv_folds", "value": MIN_FOLDS},
            provenance={"instruction": [f"任务配置 cv_folds={folds}"]},
        ))
    else:
        out.append(_c(
            "C6", ["G2"],
            f"结论必须建立在重复测量之上（≥{MIN_FOLDS} 折交叉验证或 ≥{MIN_FOLDS} 个随机种子），"
            "并报告波动范围。",
            f"报告给出 ≥{MIN_FOLDS} 次重复测量的均值与波动。",
            dimension="rigor", priority="medium",
            analysis=["多折 / 多种子重复测量"],
            check={"kind": "config_min", "field": "cv_folds", "value": MIN_FOLDS},
            provenance={"instruction": prov_syn},
        ))

    # ---- C7: claims supported by evidence (judge-backed by design) -----------
    out.append(_c(
        "C7", ["G3"],
        "报告中的每个结论都必须由本次运行产出的证据支撑，不得出现超出证据的断言。",
        "结论与产出证据一致；阴性 / 不确定结论在方法正确时同样合格。",
        dimension="reporting", priority="medium",
        analysis=["逐条核对结论与证据"],
        artifacts=[{"type": "text_analysis", "name": "conclusion_evidence_map",
                    "must_show": "每个结论对应的证据引用"}],
        check={"kind": "judge"},
        provenance={"instruction": ["科学性要求：证据支撑（通过方法正确性而非期望结果判定）"]},
    ))

    # ---- C8+: constraint gates must not be broken ----------------------------
    idx = 8
    for gate_key, gate_val in sorted(spec.comparison_gates.items()):
        base = str(gate_key)
        if ">=" in base:
            g_metric, g_op = base.split(">=")[0], "ge"
        elif "<=" in base:
            g_metric, g_op = base.split("<=")[0], "le"
        elif ">" in base:
            g_metric, g_op = base.split(">")[0], "gt"
        else:
            g_metric, g_op = base.split("<")[0], "lt"
        g_metric = g_metric.strip()
        cmp_zh = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<"}[g_op]
        out.append(_c(
            f"C{idx}", ["G4", "G1"],
            f"约束型门限不得被破坏：{g_metric} {cmp_zh} {gate_val}"
            "（优化主指标不得以牺牲该约束为代价）。",
            f"{g_metric} {cmp_zh} {gate_val}。",
            dimension="correctness", priority="high", weight=1.5,
            metrics=[g_metric],
            analysis=["测量并检查约束指标"],
            comparisons=[f"{g_metric} 门限 {gate_val}"],
            check={"kind": "metric_threshold", "metric": [g_metric], "op": g_op,
                   "value": gate_val},
            provenance={"instruction": [f"任务 gates: {base}={gate_val}"]},
        ))
        idx += 1

    # ---- criteria that close the review's blocking gaps ----------------------
    for f in review.findings:
        if f.severity != "critical":
            continue
        out.append(_c(
            f"C{idx}", ["G3"],
            f"必须先修复评估标准的严重缺陷再判定研究结论：{f.message}",
            f"缺陷已修复（建议：{f.suggestion or '按审查建议调整任务定义'}）。",
            dimension="integrity", priority="high", weight=1.0,
            analysis=["修正任务评估标准声明"],
            check={"kind": "judge"},
            provenance={"instruction": [f"标准审查缺陷 {f.finding_id}（{f.dimension}）"]},
            blocked_reason=f.message,
        ))
        idx += 1

    return out


def _claims_to_avoid(spec: TaskSpec, review: RubricReview) -> list[str]:
    out = [
        "在没有留出集复核的情况下断言「模型泛化良好」。",
        "把单次运行的指标波动描述为「显著提升」。",
        "在未做对照 / 消融的情况下把提升归因到某个具体组件。",
        "把「达到门限」表述为「达到 SOTA」或「超过参考上界」。",
        "以「结论符合预期」替代「方法与证据正确」作为成功依据。",
    ]
    if spec.metric in ("accuracy", "acc", "cv_accuracy", "top1_accuracy"):
        out.append("在类别不平衡数据上仅用总体准确率断言「各类别均表现良好」。")
    if spec.reference is not None:
        out.append(f"在未超过 reference={spec.reference} 的情况下声称达到参考水平。")
    if not review.standard_provided:
        out.append("把平台自动生成的评分标准描述为任务官方标准。")
    return out


def _rubric_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def synthesize_rubric(spec: TaskSpec, review: RubricReview) -> ExecutableRubric:
    """Build the frozen, executable rubric for *spec* (deterministic)."""

    goals = _goal_skeleton(spec)
    criteria = _synthesize_criteria(spec, review)
    rubric = ExecutableRubric(
        rubric_id="",  # filled below (depends on the content hash)
        task_id=spec.task_id,
        source="reviewed" if review.standard_provided else "synthesized",
        objective=spec.objective or spec.name or spec.task_id,
        goals=goals,
        criteria=criteria,
        claims_to_avoid=_claims_to_avoid(spec, review),
        provided_standard=spec.provided_standard(),
        review=review,
        generator="rule_engine",
        frozen=True,
    )
    content = rubric.model_dump(mode="json", exclude={"rubric_id", "integrity_hash"})
    h = _rubric_hash(content)
    rubric.integrity_hash = h
    rubric.rubric_id = f"rb-{h[:12]}"
    return rubric
