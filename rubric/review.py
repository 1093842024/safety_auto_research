"""Three-dimensional audit of a task's *declared* evaluation standard.

This is the "对已提供评估标准的任务自动审查标准的准确性、完整性和科学性" half of the
rubric stage. It is fully deterministic (no network, no LLM) so the verdict is
reproducible and can run inline on every keystroke of the registration form.

Dimensions
----------
* **accuracy（准确性）** — is the standard internally consistent and machine-checkable?
  metric ↔ direction agreement, metric ↔ task-type appropriateness, gate keys parseable,
  threshold sane w.r.t. baseline / reference / the metric's natural range.
* **completeness（完整性）** — does it cover the things a scientific evaluation needs:
  a pass gate, a baseline to beat, generalization (held-out, not just CV), a comparison
  or ablation, statistical stability (folds / seeds), and a stated evaluation procedure?
* **scientificity（科学性）** — is success defined by *method and evidence correctness*
  rather than agreement with a desired result, and is the standard gaming-resistant?
  (single-metric-only standards, trivially-reachable gates, class-imbalance-blind
  accuracy, threshold-below-baseline, unreachable gates.)

Scoring: each dimension starts at 1.0 and every finding subtracts its severity weight;
the score is clamped to [0, 1]. ``overall`` is the weighted mean (accuracy is weighted
highest because an inconsistent standard is unusable regardless of coverage).
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..platform_contracts.objects import RubricFinding
from ..platform_contracts.objects import RubricReview
from .spec import TaskSpec
from .spec import normalize_metric

# Severity -> penalty subtracted from the dimension score.
_PENALTY = {"critical": 0.5, "important": 0.25, "minor": 0.1}
# Dimension weights for the overall score.
_DIM_WEIGHT = {"accuracy": 0.45, "completeness": 0.3, "scientificity": 0.25}

# Metrics whose natural range is [0, 1] — a gate outside it is a declaration error.
_UNIT_RANGE_METRICS = {
    "accuracy", "acc", "cv_accuracy", "top1_accuracy", "top5_accuracy",
    "balanced_accuracy", "f1", "f1_macro", "f1_micro", "f1_weighted",
    "precision", "recall", "auc", "roc_auc", "medal_rate", "success_rate",
    "win_rate", "recall_at_1", "recall_at_5", "recall_at_10", "ndcg",
    "contrastive_accuracy", "retrieval_acc", "benchmark_accuracy",
    "token_accuracy", "any_medal_percentage",
}
# Metrics that are minimized by definition (direction must be "lower").
_LOWER_METRICS = {
    "loss", "eval_loss", "perplexity", "ppl", "mse", "rmse", "mae", "kl", "kld",
    "total_params", "max_abs_error", "latency", "latency_ms", "cost",
}
# Metrics known to be *aggregate* accuracy — blind to class imbalance on their own.
_IMBALANCE_BLIND = {"accuracy", "acc", "cv_accuracy", "top1_accuracy", "balanced_accuracy"}


def _suffix_direction(metric: str) -> str | None:
    """Natural optimization direction inferred from the metric name (mirrors registry)."""

    if metric in _LOWER_METRICS:
        return "lower"
    if metric.endswith(("_loss", "mse", "rmse", "mae", "_kl", "kld", "ppl", "perplexity",
                        "_error", "_params", "_latency", "_cost")):
        return "lower"
    if metric.endswith(("_accuracy", "_acc", "_score", "_recall", "_f1", "_precision",
                        "_auc", "_reward", "_rate", "_ndcg", "_spearman", "_mrr",
                        "_percentage")):
        return "higher"
    if metric in _UNIT_RANGE_METRICS:
        return "higher"
    return None


def _is_unit_range(metric: str) -> bool:
    if metric in _UNIT_RANGE_METRICS:
        return True
    return metric.startswith("recall_at_") or metric.endswith(("_f1", "_auc", "_recall",
                                                               "_precision", "_rate"))


class _FindingBuilder:
    """Accumulates findings with stable, deterministic ids."""

    def __init__(self) -> None:
        self.items: list[RubricFinding] = []
        self._n = 0

    def add(
        self,
        dimension: str,
        severity: str,
        message: str,
        suggestion: str = "",
        field_name: str = "",
    ) -> None:
        self._n += 1
        self.items.append(
            RubricFinding(
                finding_id=f"F{self._n:02d}",
                dimension=dimension,
                severity=severity,
                message=message,
                suggestion=suggestion,
                field=field_name,
            )
        )


# --------------------------------------------------------------------------- #
# Accuracy (准确性)                                                            #
# --------------------------------------------------------------------------- #
def _check_accuracy(spec: TaskSpec, fb: _FindingBuilder, fixes: dict[str, Any]) -> None:
    metric = spec.metric
    direction = str(spec.direction).strip().lower()

    if not metric:
        fb.add("accuracy", "critical",
               "未声明评测指标（eval_metric），无法对研究结果做任何客观判定。",
               "填写一个可测量的指标名，如 accuracy / f1_macro / recall_at_10。",
               "eval_metric")
        return

    if direction not in ("higher", "lower"):
        fb.add("accuracy", "critical",
               f"指标方向 direction={spec.direction!r} 非法（应为 higher 或 lower）。",
               "按指标语义填写：分数类用 higher，误差/损失类用 lower。", "direction")
    else:
        natural = _suffix_direction(metric)
        if natural and natural != direction:
            fb.add("accuracy", "critical",
                   f"指标「{spec.eval_metric}」的自然优化方向是 {natural}，"
                   f"但声明为 {direction}——方向反了会让平台把最差结果选为最优。",
                   f"将 direction 改为 {natural}。", "direction")
            fixes["direction"] = natural

    # ---- gate keys must be parseable ----
    for key, raw in (spec.gates or {}).items():
        k = str(key)
        known = k in ("threshold", "target", "target_value", "tolerance", "seeds",
                      "attempts", "output", "medal")
        comparison = any(op in k for op in (">=", "<=", ">", "<"))
        if not known and not comparison:
            fb.add("accuracy", "minor",
                   f"门限键「{k}」无法被机器解析（既不是 threshold/target，也不含比较运算符）。",
                   f"改写为 `{k}>=` 形式，或并入 threshold。", "gates")
            continue
        if comparison and not isinstance(raw, (int, float)):
            fb.add("accuracy", "important",
                   f"门限「{k}」的取值 {raw!r} 不是数值，无法执行判定。",
                   "填写数值门限。", "gates")

    # ---- threshold sanity ----
    target = spec.target_value
    if target is not None:
        if _is_unit_range(metric) and not (0.0 <= target <= 1.0):
            # medal/percentage style metrics are sometimes reported as 0-100.
            if not (metric.endswith("_percentage") or metric in ("medal_rate", "success_rate")
                    and 0.0 <= target <= 100.0):
                fb.add("accuracy", "important",
                       f"指标「{spec.eval_metric}」取值域为 [0,1]，但门限 {target} 越界。",
                       "把门限换算到 [0,1]，或改用取值域一致的指标。", "gates")
        if spec.baseline is not None:
            improves = (target < spec.baseline) if spec.lower_is_better else (target > spec.baseline)
            if not improves:
                fb.add("accuracy", "critical",
                       f"通过门限 {target} 未超过已知基线 {spec.baseline}"
                       f"（方向 {direction}）——达到门限并不代表任何改进，"
                       "研究会在不改进的情况下被判为成功。",
                       "把门限设到严格优于 baseline 的水平。", "gates")
        if spec.reference is not None:
            beyond = (target < spec.reference) if spec.lower_is_better else (target > spec.reference)
            if beyond:
                fb.add("accuracy", "important",
                       f"通过门限 {target} 已超过参考上界 reference={spec.reference}，"
                       "门限很可能不可达，研究将必然判定失败。",
                       "把门限降到 baseline 与 reference 之间。", "gates")

    if spec.baseline is not None and spec.reference is not None:
        # baseline should be worse than reference in the declared direction.
        ref_better = (
            spec.reference < spec.baseline if spec.lower_is_better
            else spec.reference > spec.baseline
        )
        if not ref_better:
            fb.add("accuracy", "important",
                   f"baseline={spec.baseline} 与 reference={spec.reference} 的优劣关系"
                   f"与声明方向 {direction} 矛盾。",
                   "核对 baseline/reference，或修正 direction。", "baseline")

    # ---- metric appropriateness for the declared task type ----
    tt = spec.task_type
    if tt:
        try:
            from ..benchmark_tasks import registry as _reg

            if not _reg._metric_allowed(tt, metric):  # noqa: SLF001 - shared vocabulary
                allowed = sorted(_reg._VALID_METRICS.get(tt, set()))  # noqa: SLF001
                fb.add("accuracy", "important",
                       f"指标「{spec.eval_metric}」与任务类型「{tt}」不匹配。",
                       f"改用该类型的合法指标之一：{allowed[:8]}", "eval_metric")
        except Exception:  # registry is optional for the pure-engine unit tests
            pass


# --------------------------------------------------------------------------- #
# Completeness (完整性)                                                        #
# --------------------------------------------------------------------------- #
def _check_completeness(spec: TaskSpec, fb: _FindingBuilder, fixes: dict[str, Any]) -> None:
    if spec.target_value is None:
        fb.add("completeness", "important",
               "未声明通过门限（threshold / target_value），研究没有可判定的成功条件。",
               "给出一个严格优于 baseline 的门限值。", "gates")
    if spec.baseline is None:
        fb.add("completeness", "important",
               "未声明基线 baseline，无法判断结果是否构成真实改进（也无法识别退化）。",
               "填写当前已知成绩作为 baseline；确实无基线时应在评估方法中说明。",
               "baseline")

    text = " ".join([spec.eval_method, spec.dataset_desc, spec.objective]).lower()
    gate_keys = " ".join(str(k) for k in (spec.gates or {}))

    # ---- generalization: held-out / test split, not only CV ----
    # NOTE: the keywords must be specific. A bare "val" would match "kaggle_eval" and
    # silently mark every kaggle task as generalization-checked (false negative).
    has_generalization = any(
        kw in text for kw in ("held-out", "heldout", "hold-out", "留出", "测试集",
                              "test set", "test_public", "验证集", "validation",
                              "val set", "val split", "泛化")
    )
    if not has_generalization:
        fb.add("completeness", "important",
               "评估标准未要求在从未参与调优的留出集上复核（只有 CV / 训练内指标时，"
               "反复调参会把交叉验证分数刷上去而泛化性不变）。",
               "增加一条留出集复核要求，并限定 CV 与留出集指标的最大允许差距（如 ≤0.05）。",
               "eval_method")

    # ---- comparison / ablation ----
    has_comparison = (
        spec.baseline is not None
        or len(spec.comparison_gates) > 1
        or any(kw in text for kw in ("baseline", "基线", "对照", "消融", "ablation",
                                     "对比", "compare"))
    )
    if not has_comparison:
        fb.add("completeness", "important",
               "评估标准未要求任何对照（基线 / 消融 / 条件对比），无法归因效果来源。",
               "至少要求与一个基线方法对比，或对关键组件做消融。", "eval_method")

    # ---- statistical stability ----
    has_stability = (
        (spec.cv_folds or 0) >= 2
        or "seeds" in gate_keys
        or any(kw in text for kw in ("交叉验证", "cross-valid", "cv", "seed", "随机种子",
                                     "sem", "std", "置信区间", "显著"))
    )
    if not has_stability:
        fb.add("completeness", "minor",
               "评估标准未涉及统计稳定性（多折 / 多随机种子 / 波动范围），"
               "单次运行的提升可能只是噪声。",
               "要求 ≥3 折交叉验证或 ≥3 个随机种子并报告均值±波动。", "eval_method")

    # ---- data leakage ----
    has_leakage_guard = any(
        kw in text for kw in ("泄漏", "泄露", "leakage", "leak", "去重", "dedup",
                              "污染", "contamination", "split")
    )
    if not has_leakage_guard:
        fb.add("completeness", "minor",
               "评估标准未提及数据泄漏 / 训练测试重叠的防护。",
               "要求声明划分方式并检查 train/eval 无重叠样本。", "eval_method")

    # ---- stated procedure ----
    if not spec.eval_method.strip():
        fb.add("completeness", "minor",
               "未描述评估执行过程（用什么数据、怎么算、谁来算），标准不可复现。",
               "补充一段评估方法说明：数据划分、指标计算方式、执行脚本或流程。",
               "eval_method")


# --------------------------------------------------------------------------- #
# Scientificity (科学性)                                                       #
# --------------------------------------------------------------------------- #
def _check_scientificity(spec: TaskSpec, fb: _FindingBuilder, fixes: dict[str, Any]) -> None:
    metric = spec.metric
    text = " ".join([spec.eval_method, spec.objective]).lower()

    # ---- single-point metric is gameable ----
    n_checks = len(spec.comparison_gates) + (1 if spec.target_value is not None else 0)
    if n_checks <= 1:
        fb.add("scientificity", "important",
               "评估仅依赖单一指标的单点门限，容易被「只优化这一个数」的取巧策略刷过"
               "（如牺牲少数类召回换取总体准确率）。",
               "增加约束型指标（如同时要求 unsafe_recall ≥ x），或改用对不平衡稳健的指标。",
               "gates")

    # ---- imbalance-blind accuracy ----
    if metric in _IMBALANCE_BLIND and not spec.comparison_gates:
        fb.add("scientificity", "important",
               f"以「{spec.eval_metric}」为唯一指标对类别不平衡不敏感：多数类占优时，"
               "全部预测为多数类也能拿到高分。",
               "改用 f1_macro / balanced_accuracy，或额外约束各类召回。", "eval_metric")
        fixes.setdefault("suggested_metric", "f1_macro")

    # ---- outcome-fitting language: success defined as "getting the desired answer" ----
    _OUTCOME_WORDS = ("必须证明", "必须显著", "证明假设成立", "prove the hypothesis",
                      "must confirm", "confirm that", "验证结论成立", "必须得到提升")
    if any(w in text for w in _OUTCOME_WORDS):
        fb.add("scientificity", "critical",
               "评估标准以「得到期望结论」定义成功，这会激励结果拟合与选择性汇报；"
               "正确的科学标准应以方法与证据的正确性定义通过（阴性/不确定结论"
               "在问题表述恰当时同样是合格结果）。",
               "把通过条件改写为「按预注册方法完成分析并给出可核验证据」，"
               "而非「指标必须提升 / 假设必须成立」。", "eval_method")

    # ---- evidence requirement ----
    has_evidence_req = any(
        kw in text for kw in ("证据", "evidence", "报告", "report", "产出", "artifact",
                              "图", "表", "日志", "log", "可复现", "reproduc")
    )
    if not has_evidence_req:
        fb.add("scientificity", "minor",
               "评估标准未要求可核验的证据产出（结果文件 / 指标报告 / 可复现命令），"
               "无法区分「真的跑出来」和「声称跑出来」。",
               "要求每个结论关联一份可核验产出（评测报告 / 结果文件 / 复现命令）。",
               "eval_method")

    # ---- trivially reachable gate ----
    target = spec.target_value
    if target is not None and _is_unit_range(metric) and not spec.lower_is_better:
        if target <= 0.5:
            fb.add("scientificity", "important",
                   f"门限 {target} 对 [0,1] 区间的「{spec.eval_metric}」而言过低"
                   "（二分类随机猜测约 0.5），无法证明任何能力。",
                   "把门限提到显著高于随机基线的水平。", "gates")
        elif target >= 0.999:
            fb.add("scientificity", "minor",
                   f"门限 {target} 接近满分，通常意味着任务过易或存在数据泄漏。",
                   "核查是否存在标签泄漏；必要时改用更难的划分。", "gates")


# --------------------------------------------------------------------------- #
# Public entry                                                                 #
# --------------------------------------------------------------------------- #
def _score(dimension: str, findings: list[RubricFinding]) -> float:
    s = 1.0
    for f in findings:
        if f.dimension == dimension:
            s -= _PENALTY.get(f.severity, 0.1)
    return round(max(0.0, min(1.0, s)), 4)


def _verdict(overall: float, findings: list[RubricFinding]) -> str:
    if any(f.severity == "critical" for f in findings):
        return "unusable" if overall < 0.5 else "needs_work"
    if overall >= 0.9:
        return "sound"
    if overall >= 0.7:
        return "acceptable"
    return "needs_work"


def review_standard(spec: TaskSpec) -> RubricReview:
    """Audit a task's declared evaluation standard along the three dimensions.

    Works for both cases required by the feature:

    * standard **provided** -> a genuine review (accuracy / completeness / scientificity);
    * standard **absent**   -> the same machinery reports *what is missing*, which is
      exactly the input the synthesizer needs (and ``standard_provided=False`` tells
      the caller a rubric must be induced rather than merely audited).
    """

    fb = _FindingBuilder()
    fixes: dict[str, Any] = {}
    _check_accuracy(spec, fb, fixes)
    _check_completeness(spec, fb, fixes)
    _check_scientificity(spec, fb, fixes)

    findings = fb.items
    acc = _score("accuracy", findings)
    comp = _score("completeness", findings)
    sci = _score("scientificity", findings)
    overall = round(
        acc * _DIM_WEIGHT["accuracy"]
        + comp * _DIM_WEIGHT["completeness"]
        + sci * _DIM_WEIGHT["scientificity"],
        4,
    )
    verdict = _verdict(overall, findings)
    provided = spec.has_declared_standard()

    n_crit = sum(1 for f in findings if f.severity == "critical")
    n_imp = sum(1 for f in findings if f.severity == "important")
    n_min = sum(1 for f in findings if f.severity == "minor")
    if provided:
        head = f"已提供评估标准，审查结论 {verdict}"
    else:
        head = f"未提供可用评估标准（将自动生成任务专属评分标准），现状审查 {verdict}"
    summary = (
        f"{head}；总分 {overall:.2f}"
        f"（准确性 {acc:.2f} / 完整性 {comp:.2f} / 科学性 {sci:.2f}）；"
        f"缺陷 {n_crit} 严重 / {n_imp} 重要 / {n_min} 轻微。"
    )

    rid = "rv-" + hashlib.sha256(
        f"{spec.task_id}|{spec.eval_metric}|{spec.direction}|{sorted((spec.gates or {}).items(), key=str)}"
        f"|{spec.baseline}|{spec.reference}|{spec.task_type}".encode("utf-8")
    ).hexdigest()[:12]

    return RubricReview(
        review_id=rid,
        task_id=spec.task_id,
        standard_provided=provided,
        accuracy=acc,
        completeness=comp,
        scientificity=sci,
        overall=overall,
        verdict=verdict,
        findings=findings,
        suggested_fixes=fixes,
        summary=summary,
    )
