"""通用指标计算函数库（指标目录的参考实现）。

每个函数都是**平台标准的通用计算方式**，供三处使用：

* 指标目录页（评估指标 tab）展示的参考实现源码；
* 任务详情中「指标/评估方式」的解释文档；
* 研究者自建评测脚本时直接 import 复用（``from safety_auto_research.benchmark_tasks
  import metric_lib``）。

约定：函数命名 = 指标 ID；参数统一使用朴素类型（list / numpy array），不依赖
任何框架，保证在沙箱镜像内可离线运行。
"""

from __future__ import annotations

from collections import Counter
from typing import Callable, Sequence


# --------------------------------------------------------------------------- #
# 分类指标
# --------------------------------------------------------------------------- #
def accuracy(y_true: Sequence, y_pred: Sequence) -> float:
    """准确率：预测正确的样本占比。适用于多分类与二分类。"""
    y_true, y_pred = list(y_true), list(y_pred)
    if not y_true:
        raise ValueError("y_true 为空")
    return sum(1 for t, p in zip(y_true, y_pred) if t == p) / len(y_true)


def _prf(y_true, y_pred, positive=None):
    labels = set(y_true) | set(y_pred)
    tp = fp = fn = 0
    for label in labels:
        if positive is not None and label != positive:
            continue
        if positive is None and label not in set(y_true):
            continue
        tp += sum(1 for t, p in zip(y_true, y_pred) if t == label and p == label)
        fp += sum(1 for t, p in zip(y_true, y_pred) if t != label and p == label)
        fn += sum(1 for t, p in zip(y_true, y_pred) if t == label and p != label)
    return tp, fp, fn


def f1_macro(y_true: Sequence, y_pred: Sequence) -> float:
    """宏平均 F1：先对每个类别算 F1 再取算术平均，对小类别一视同仁。"""
    y_true, y_pred = list(y_true), list(y_pred)
    labels = sorted(set(y_true) | set(y_pred))
    f1s = []
    for label in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == label and p == label)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != label and p == label)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == label and p != label)
        f1 = (2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else 0.0
        f1s.append(f1)
    return sum(f1s) / len(f1s) if f1s else 0.0


def roc_auc(y_true: Sequence, y_score: Sequence) -> float:
    """ROC-AUC：正样本排在负样本之前的概率（Mann-Whitney U 统计量的归一化）。"""
    # 升序排秩：分数越高秩越大（1-based）。
    pairs = sorted(zip(y_score, y_true), key=lambda x: x[0])
    pos = sum(1 for _, t in pairs if t == 1)
    neg = len(pairs) - pos
    if not pos or not neg:
        raise ValueError("roc_auc 需要同时存在正负样本")
    # 处理同分：同分对贡献平均秩（0.5 等价处理）
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2  # 1-based 平均秩
        for k in range(i, j):
            if pairs[k][1] == 1:
                rank_sum += avg_rank
        i = j
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def cv_accuracy(
    X,
    y,
    make_model: Callable[[], object],
    k: int = 5,
    seed: int = 42,
) -> dict:
    """k 折交叉验证准确率（平台 kaggle_eval 的主指标 cv_accuracy 的标准算法）。

    返回 {"cv_accuracy", "accuracy_std", "fold_scores"}；折划分使用均匀分桶
    （与平台 kaggle_eval 执行器一致的确定性划分，避免随机性影响审计）。
    """
    import numpy as np
    from sklearn.base import clone

    X, y = np.asarray(X), np.asarray(y)
    buckets = np.array_split(np.arange(len(y)), k)  # 确定性均匀分桶
    scores = []
    for i, test_idx in enumerate(buckets):
        train_idx = np.concatenate([b for j, b in enumerate(buckets) if j != i])
        model = make_model()
        model.fit(X[train_idx], y[train_idx])
        pred = model.predict(X[test_idx])
        scores.append(accuracy(y[test_idx], pred))
    return {
        "cv_accuracy": sum(scores) / len(scores),
        "accuracy_std": float(np.std(scores)),
        "fold_scores": scores,
    }


def heldout_split(n: int, frac: float = 0.15, seed: int = 42):
    """留出集划分（平台 heldout_frac=0.15 的标准实现，按索引确定性分桶）。"""
    import numpy as np

    idx = np.arange(n)
    m = max(1, int(n * frac))
    return idx[:-m], idx[-m:]


def generalization_gap(cv_score: float, heldout_score: float) -> float:
    """泛化差距 = CV 分数 − 留出集分数。平台外审计用 |gap| ≤ 0.05 检查过拟合。"""
    return cv_score - heldout_score


# --------------------------------------------------------------------------- #
# 系统性能 / 效率指标
# --------------------------------------------------------------------------- #
def runtime_seconds(fn: Callable, repeats: int = 11) -> float:
    """运行时长（秒）：重复执行取中位数，消除调度噪声（best-of-N 的标准做法）。"""
    import statistics
    import time

    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def speedup(baseline_time: float, solution_time: float) -> float:
    """加速比 = 参考实现中位耗时 / 优化实现中位耗时（correctness gate 通过后才有意义）。"""
    if solution_time <= 0:
        raise ValueError("solution_time 必须为正")
    return baseline_time / solution_time


def total_params(model) -> int:
    """模型参数总量：枚举常见框架的参数容器（sklearn coef_/拦截项、torch parameters）。"""
    n = 0
    for attr in ("coef_", "intercept_", "feature_importances_", "coefs_", "intercepts_"):
        obj = getattr(model, attr, None)
        if obj is None:
            continue
        import numpy as np

        arrs = obj if isinstance(obj, (list, tuple)) else [obj]
        for a in arrs:
            import numpy as _np

            n += int(_np.size(a))
    params = getattr(model, "parameters", None)
    if callable(params):
        try:
            n += sum(p.numel() for p in params())
        except Exception:
            pass
    return n


def bits_per_byte(compressed_bytes: int, original_bytes: int) -> float:
    """每字节压缩比特数 = 压缩后总比特 / 原始字节数（越低压缩率越好）。"""
    if original_bytes <= 0:
        raise ValueError("original_bytes 必须为正")
    return compressed_bytes * 8 / original_bytes


def ref_step_latency_s(step_fn: Callable, warmup: int = 3, repeats: int = 11) -> float:
    """训练步延迟（秒/步）：预热后重复执行取中位数（GRPO 类训练吞吐代理）。"""
    import statistics
    import time

    for _ in range(warmup):
        step_fn()
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        step_fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def serving_score(throughput_ratio: float, completion_ratio: float) -> float:
    """在线服务复合分 = 0.5×吞吐比 + 0.5×完成时间比（与官方 benchmark.py 同式）。"""
    return 0.5 * throughput_ratio + 0.5 * completion_ratio


# --------------------------------------------------------------------------- #
# 评测/对抗/agent 指标
# --------------------------------------------------------------------------- #
def max_abs_error(pred: Sequence, gold: Sequence) -> float:
    """最大绝对误差：max|pred − gold|（逐元素），科学计算类 gold-program 评测的标准判据。"""
    import numpy as np

    a, b = np.asarray(pred, dtype=float).ravel(), np.asarray(gold, dtype=float).ravel()
    if a.shape != b.shape:
        raise ValueError(f"形状不一致: {a.shape} vs {b.shape}")
    return float(np.max(np.abs(a - b))) if a.size else 0.0


def asr(success_flags: Sequence[bool]) -> float:
    """攻击成功率 = 攻击成功的样本占比（方向约定为越低越好 = 模型越鲁棒）。"""
    flags = [bool(x) for x in success_flags]
    if not flags:
        raise ValueError("success_flags 为空")
    return sum(flags) / len(flags)


def success_rate(success_flags: Sequence[bool], attempts_per_task: int = 1) -> float:
    """任务成功率 = 通过评测脚本的任务占比（agent-eval 主指标 SR）。"""
    flags = [bool(x) for x in success_flags]
    if not flags:
        raise ValueError("success_flags 为空")
    return sum(flags) / (len(flags) * max(1, attempts_per_task))


def medal_rate(medal_flags: Sequence[bool]) -> float:
    """奖牌率 = 达到奖牌线的竞赛占比（MLE-bench 主指标）。"""
    return asr(medal_flags)


def trigger_rate(triggered: Sequence[bool]) -> float:
    """技能触发率 = 应触发且正确触发的比例（skill 化 agent 的路由正确性）。"""
    return asr(triggered)


def rubric_weighted_score(
    metric_coverage: float,
    condition_coverage: float,
    hypothesis_coverage: float,
    plausibility: float,
) -> float:
    """研究产物 rubric 加权分 = 0.35 指标覆盖 + 0.20 条件覆盖 + 0.25 假设覆盖 + 0.20 合理性。"""
    w = (0.35, 0.20, 0.25, 0.20)
    vals = (metric_coverage, condition_coverage, hypothesis_coverage, plausibility)
    return sum(wi * float(v) for wi, v in zip(w, vals))


def recall_at_10(relevant: set, retrieved_top10: Sequence) -> float:
    """Recall@10：前 10 检索结果中命中相关项的比例（embedding/检索评测）。"""
    if not relevant:
        raise ValueError("relevant 为空")
    hit = sum(1 for x in list(retrieved_top10)[:10] if x in relevant)
    return hit / len(relevant)
