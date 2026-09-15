"""评估指标目录（评估指标 tab / 任务详情「指标详解」的单一数据源）。

每个条目：

* ``name``          — 展示名
* ``description``   — 指标含义：它度量的是什么、为什么这样度量
* ``computation``   — 计算方式：公式 / 计算流程 / 评测协议
* ``direction``     — higher | lower
* ``typical_range`` — 常见取值范围与判读
* ``applicable``    — 适用的任务类型 / 模态
* ``func``          — :mod:`benchmark_tasks.metric_lib` 中的标准参考实现（API
  返回时用 ``inspect.getsource`` 提取源码，保证文档与代码永不漂移）

任务 ``eval_metric`` 与目录 ID 对齐（别名条目如 ``f1`` 直接引用同一实现）。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from . import metric_lib as lib

_ENTRY = dict[str, Any]


def _e(name, description, computation, direction, typical_range, applicable, func):
    return {
        "name": name,
        "description": description,
        "computation": computation,
        "direction": direction,
        "typical_range": typical_range,
        "applicable": applicable,
        "func": func,
    }


METRIC_CATALOG: dict[str, _ENTRY] = {
    "cv_accuracy": _e(
        "交叉验证准确率",
        "k 折交叉验证下的平均分类准确率：把训练集切成 k 份，轮流用 k−1 份训练、1 份验证，"
        "取 k 次准确率的均值。它度量模型在**未见数据上的期望正确率**，比单次切分稳定，"
        "是平台 kaggle_eval 内循环的主优化指标。",
        "acc_i = (第 i 折验证集预测正确数) / (该折样本数)；"
        "cv_accuracy = (1/k)·Σ acc_i，同时报告折间标准差 accuracy_std。"
        "平台用确定性均匀分桶（非随机洗牌），保证审计可复现。",
        "higher",
        "0.5–1.0；0.5 相当于二分类随机猜，>0.85 通常算强基线",
        ["分类（图文音）", "表格", "平台原生任务"],
        lib.cv_accuracy,
    ),
    "accuracy": _e(
        "准确率",
        "预测正确的样本占全部样本的比例。直观、可解释，但类别不均衡时会被多数类主导"
        "（如 99:1 的数据全预测多数类即得 0.99）——不均衡场景应配合 f1_macro / roc_auc。",
        "accuracy = TP+TN / (TP+TN+FP+FN) = 预测正确数 / 总样本数。",
        "higher",
        "0–1；二分类随机水平 0.5",
        ["分类（图文音）", "多分类", "二分类"],
        lib.accuracy,
    ),
    "f1_macro": _e(
        "宏平均 F1",
        "先对每个类别分别计算 F1（精确率与召回率的调和平均），再对类别取算术平均。"
        "「宏」平均让小类别与大类别权重相同，是不均衡分类下比 accuracy 更公平的指标。",
        "对类别 c：P_c=TP_c/(TP_c+FP_c)，R_c=TP_c/(TP_c+FN_c)，"
        "F1_c=2·P_c·R_c/(P_c+R_c)；f1_macro=(1/|C|)·Σ_c F1_c。",
        "higher",
        "0–1；不均衡任务 0.6+ 已可用",
        ["分类（文本/图像/音频）", "多分类", "不均衡数据"],
        lib.f1_macro,
    ),
    "f1": _e(
        "F1（二分类）",
        "二分类正类的 F1 分数，精确率与召回率的调和平均：既要求找得准（精确率）又要求找得全（召回率）。",
        "F1 = 2·P·R/(P+R)，P=TP/(TP+FP)，R=TP/(TP+FN)。",
        "higher",
        "0–1",
        ["二分类", "信息检索"],
        lib.f1_macro,
    ),
    "roc_auc": _e(
        "ROC-AUC",
        "ROC 曲线下面积：随机取一正一负样本，正样本得分高于负样本的概率。"
        "与阈值无关，衡量模型对正负样本的**排序能力**。",
        "AUC = (正样本秩和 − n_pos·(n_pos+1)/2) / (n_pos·n_neg)；同分对按平均秩贡献 0.5。",
        "higher",
        "0.5（随机）–1.0（完美排序）",
        ["二分类", "排序/打分"],
        lib.roc_auc,
    ),
    "heldout_accuracy": _e(
        "留出集准确率",
        "从训练数据中切出 15%（held-out，平台标准比例）从未参与训练，只在该子集上评测一次的准确率。"
        "它是对 CV 准确率的独立复核——CV 看过的数据同样参与了模型选择，held-out 从未参与。",
        "把样本按索引确定性切出最后 15% 作为留出集；在留出集上跑一次预测计准确率。",
        "higher",
        "0–1；应与 cv_accuracy 接近",
        ["分类", "平台原生任务"],
        lib.accuracy,
    ),
    "generalization_gap": _e(
        "泛化差距",
        "CV 准确率与留出集准确率之差。差距大说明模型/调参「记住」了交叉验证能看见的信息"
        "（过拟合或评估集泄漏），平台外审计用 |gap| ≤ 0.05 作为硬约束抓过拟合。",
        "generalization_gap = cv_accuracy − heldout_accuracy；审计约束为 |gap| ≤ 0.05。",
        "lower",
        "0–0.05 为健康；>0.1 视为过拟合信号",
        ["分类", "一切 CV 类评测"],
        lib.generalization_gap,
    ),
    "total_params": _e(
        "模型参数总量",
        "模型可学习参数的数量，是模型体积/内存占用的直接度量。安全路由器任务在保住准确率与"
        "两类召回门限的前提下最小化它——研究的是容量与安全性的权衡。",
        "枚举模型参数容器（sklearn 的 coef_/intercept_、torch 的 parameters()）逐元素计数求和。",
        "lower",
        "如 2 层 MLP 基线 16641，平台参考 2081",
        ["模型压缩", "MLP/小型网络"],
        lib.total_params,
    ),
    "runtime_seconds": _e(
        "运行时长（秒）",
        "研究实现完成一次任务的总耗时。核优化/加密吞吐类任务的延迟代理：真实测量但硬件与"
        "优化后的 C 内核不同，数值不可跨硬件比较。",
        "重复执行 repeats 次（默认 11）取**中位数**，消除调度与缓存噪声（best-of-N 标准做法）。",
        "lower",
        "任务门限各自声明（如 FlashAttention ≤5s）",
        ["系统优化", "内核/密码学", "CPU 代理任务"],
        lib.runtime_seconds,
    ),
    "runtime_ms": _e(
        "运行时长（毫秒）",
        "同 runtime_seconds，单位毫秒。NTT 数论变换任务在 n=65536 上计时，"
        "要求 bit-exact（往返正确性断言）后才有意义。",
        "重复取中位数；先断言 forward+inverse 往返 bit-exact。",
        "lower",
        "基线 109.8ms（GPU 参考不可直接比）",
        ["CUDA/内核代理"],
        lib.runtime_seconds,
    ),
    "ref_step_latency_s": _e(
        "训练步延迟（秒/步）",
        "GRPO 类训练一步（rollout 采样 + 优势归一化 + 策略梯度更新）的中位耗时，"
        "是训练吞吐的代理指标。原任务为 GPU 微调 7B 模型，本平台用真实 CPU 参考步代替。",
        "预热 3 步后重复执行取中位数；指标名带 ref_ 前缀以示为参考测量。",
        "lower",
        "门限 ≤10s/步",
        ["RL 训练系统", "CPU 代理任务"],
        lib.ref_step_latency_s,
    ),
    "speedup": _e(
        "加速比",
        "优化实现相对参考实现的中位耗时之比。AlgoTune 范式的核心指标："
        "正确性门限（独立校验器在全部实例上验证）通过后，加速比才是有意义的分数——任一实例失败计 0。",
        "speedup = median(reference_time) / median(solution_time)；correctness gate 先行。",
        "higher",
        "≥1；>>10 需复核正确性",
        ["算法效率优化", "kNN/数值计算"],
        lib.speedup,
    ),
    "bits_per_byte": _e(
        "每字节压缩比特数",
        "压缩后总比特数 / 原始字节数：信息论标准压缩率度量，越低越好。"
        "0 bpb 完美压缩，8 bpb 等于没压（1 字节仍 1 字节）。",
        "bits_per_byte = compressed_bytes × 8 / original_bytes；在 9 族官方可见序列上按字节加权计整体分。",
        "lower",
        "基线 5.0、参考 3.8、门限 ≤6.0",
        ["无损压缩", "序列建模"],
        lib.bits_per_byte,
    ),
    "max_abs_error": _e(
        "最大绝对误差",
        "预测值与 gold 参考值逐元素绝对误差的最大值：科学计算 gold-program 评测的标准判据，"
        "单一元素偏差即不通过，杜绝「平均正确但局部错误」。",
        "max_abs_error = max_i |pred_i − gold_i|；通过条件 ≤ 容差（如 1e-4）。",
        "lower",
        "0–容差（1e-4）内通过",
        ["科学计算", "agent-eval（SAB）"],
        lib.max_abs_error,
    ),
    "asr": _e(
        "攻击成功率（ASR）",
        "攻击者视角的越狱/注入/绕过成功样本占比。**本平台约定方向为 lower**："
        "对齐良好的受害者模型 ASR≈0 表示攻击未果（模型鲁棒），攻击落地时评测门诚实判 FAIL。"
        "成功判定严格排除假阳性（如复述注入指令、'Sure, 但我不能…'）。",
        "asr = 攻击成功样本数 / 总样本数；每样本查询预算有上限（如 200 queries）。",
        "lower",
        "0（鲁棒）–1（全被攻破）",
        ["对抗/越狱", "安全评测"],
        lib.asr,
    ),
    "success_rate": _e(
        "任务成功率（SR）",
        "agent 产出的程序/答案通过官方评测脚本的占比，agent-eval 的主指标。"
        "SAB 每任务 3 次尝试；可视化等主观输出由 judge 模型辅助评分。",
        "SR = 通过评测的任务数 / 总任务数（每任务 attempts 次机会）。",
        "higher",
        "SAB 基线 32.4%（Claude-3.5-Sonnet self-debug）",
        ["科研 agent 评测", "代码生成"],
        lib.success_rate,
    ),
    "medal_rate": _e(
        "奖牌率",
        "MLE-bench 主指标：agent 的 CSV 提交在官方 grade.py 下达到 Kaggle 铜牌线的竞赛占比，"
        "衡量端到端 ML 工程能力（读数据/建模/训练/提交全链路）。",
        "按 low/medium/high 难度分档汇总；≥3 seeds 取均值 ± SEM；官方有数据泄漏标注剔除。",
        "higher",
        "o1-preview 基线 16.9%",
        ["ML 工程", "Kaggle 竞赛"],
        lib.medal_rate,
    ),
    "any_medal_percentage": _e(
        "任一奖牌占比",
        "同 medal_rate（官方套件命名）：75 个离线 Kaggle 竞赛中达到任一奖牌的百分比。",
        "同 medal_rate；lite 子集（158GB 原始数据）可先行验证。",
        "higher",
        "官方基线 16.9%，榜首 64.44%",
        ["ML 工程", "Kaggle 竞赛"],
        lib.medal_rate,
    ),
    "trigger_rate": _e(
        "技能触发率",
        "skill 化 agent 的路由正确性：给定 (技能, 查询) 样本集（含不应触发的负样本），"
        "技能描述被正确触发/不误触发的比例。跨技能误触发率作为次要指标。",
        "对每条样本合成 stream-json 交由官方 classify 判 trigger/confusion/miss，汇总触发正确占比。",
        "higher",
        "0–1；负样本误触发率越低越好",
        ["工具型元评测", "skill 工程"],
        lib.trigger_rate,
    ),
    "rubric_weighted_score": _e(
        "研究产物 rubric 加权分",
        "开放式研究智能体产出结果的加权评分：指标覆盖 + 条件覆盖 + 假设覆盖 + 合理性"
        "（数值有限/非负且跨 condition 有差异）。衡量 agent 的开环研究能力。",
        "0.35×指标覆盖 + 0.20×条件覆盖 + 0.25×假设覆盖 + 0.20×合理性；声明指标键须可验证。",
        "higher",
        "0–1",
        ["科研 agent 评测", "开放研究"],
        lib.rubric_weighted_score,
    ),
    "absolute_correctness_success_rate": _e(
        "产物理解正确率",
        "科研产物理解评测：对论文级问题（理解/复现/扩展三类）作答，与 gold 关键 token"
        "（数字 + 重要方法/实体词）做 contains-check/rubric 代理评分，单题重叠 ≥0.5 计成功。",
        "success = (答案与 gold 关键 token 重叠 ≥ 0.5) 的题目占比；闭卷协议。",
        "higher",
        "0–1",
        ["科研 agent 评测", "论文理解"],
        lib.success_rate,
    ),
    "serving_score": _e(
        "在线服务复合分",
        "LLM 推理服务的综合质量：连续批处理相对串行基线的吞吐比与完成时间比各占一半。"
        "研究批处理策略对服务质量的杠杆。",
        "serving_score = 0.5×throughput_ratio + 0.5×completion_ratio（与官方 benchmark.py 同式）。",
        "higher",
        "≥1 即优于串行基线",
        ["LLM 推理服务", "系统优化"],
        lib.serving_score,
    ),
    "recall_at_10": _e(
        "Recall@10",
        "检索/embedding 评测：前 10 个检索结果中命中相关项的比例，衡量表示学习把相关内容"
        "排进头部的召回能力。",
        "recall@10 = |top10 ∩ relevant| / |relevant|。",
        "higher",
        "0–1",
        ["embedding 对比学习", "检索"],
        lib.recall_at_10,
    ),
}

# 别名：任务里出现的历史指标名 → 目录条目（不新增实现）
_ALIASES = {
    "f1": "f1",
}


def get_metric_detail(metric_id: str) -> dict[str, Any] | None:
    """指标详情（含参考实现源码）。未收录的指标返回 None。"""
    entry = METRIC_CATALOG.get(metric_id)
    if entry is None:
        return None
    return _detail(metric_id, entry)


def _detail(metric_id: str, entry: _ENTRY) -> dict[str, Any]:
    func: Callable = entry["func"]
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        source = ""
    return {
        "metric_id": metric_id,
        "name": entry["name"],
        "description": entry["description"],
        "computation": entry["computation"],
        "direction": entry["direction"],
        "typical_range": entry["typical_range"],
        "applicable": entry["applicable"],
        "implementation": source,
        "library": "benchmark_tasks.metric_lib",
    }


def all_metrics() -> list[dict[str, Any]]:
    """全部指标的目录列表（按指标 ID 排序）。"""
    out = [_detail(mid, e) for mid, e in sorted(METRIC_CATALOG.items())]
    return out


def metric_ids() -> list[str]:
    return sorted(METRIC_CATALOG)
