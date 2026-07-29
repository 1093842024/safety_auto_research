---
name: adv-antiforgetting
description: >
  数据生成对抗层⑩的防遗忘护栏 playbook。在持续对抗重训中保护"已掌握知识"：
  维护经验回放缓冲(replay buffer)、设定各能力维度保留率目标、产出 EWC/正则重要性信号，
  与 adv_generation 的对抗数据一起交付 ⑤(配比) 与 ⑦(重训)。
  核心理念来自 Self-Play 反思经验回放 + 层⑤ 配比遗忘门控。
  触发词："防遗忘"、"保留率"、"经验回放"、"replay buffer"、"EWC"、"灾难性遗忘"。
allowed-tools: Read, Grep, Glob, Write, Edit, Bash
---

# Adv-Antiforgetting — 防遗忘护栏 Playbook（层⑩）

> 目标：模型在吸收对抗新数据（修漏洞）的同时，**不回退已掌握的安全能力**。
> 这是"持续提升"而非"拆东墙补西墙"的关键护栏。

## 1. 经验回放缓冲（核心）

- **结构**：见 `datasets_tasks/replay_buffer_schema.md`。按能力维度分桶，每条存
  `{sample, capability_dim, mastery_score, last_seen_round, retain_flag}`。
- **写入**：每轮 ③ 评估后，凡"达标且稳定"的样本 → `mastery_score` 升、`retain_flag=true`。
- **采样**：重训混合时按 `retention_config` 比例抽取回放样本（优先级 + 时间衰减，但 `retain_flag` 样本永不丢弃）。
- **反思经验回放**（Self-Play）：从缓冲采样失败/边界案例，LLM 反思"为何旧能力被威胁"，反哺回放权重。

## 2. 保留率目标（retention_config）

对每个能力维度 `dim` 设定硬下限（源自层⑤ 配比遗忘门控，本层下探到样本级）：

```json
{
  "retention_floor": {
    "tool_use_safety": 0.95,
    "response_safety": 0.97,
    "helpfulness": 0.90
  },
  "replay_ratio": 0.30,
  "mixed_objective": "adv_new : replay = 0.7 : 0.3"
}
```

- 任何维度重训后评估 < `retention_floor` → 层⑨ R10 路由要求提高该维回放权重或回 ⑦ 重训。

## 3. 正则 / 持续学习信号

| 方法 | 信号 | 注入点 |
|------|------|--------|
| **EWC** |  Fisher 信息对角线 → 重要参数惩罚 `λ·F·(θ−θ*)^2` | ⑦ 训练 loss |
| **L2-SP** | 锚点权重 `‖θ−θ*‖^2` | ⑦ 训练 loss |
| ** rehearsal** | 回放样本混入 batch | ⑦ dataloader（来自 replay_buffer） |
| **知识蒸馏** | 旧模型软标签 `KL(π_old‖π_new)` | ⑦ loss |

> 与层⑤分工：层⑤在"数据集/配比"层做遗忘门控（粗粒度保留率）；本层在"样本级回放 + 正则"做细粒度护栏，二者共用 `retention_config`。

## 4. 执行流程

```
STEP 1  读上轮 ③ 评估报告，更新 replay_buffer（mastery/retain_flag）
STEP 2  按 retention_floor 与 replay_ratio 算本轮回放配额
STEP 3  计算 EWC Fisher（若权重可访问）或 L2-SP 锚点 → 写 regularization_signal
STEP 4  把 replay_buffer + retention_config + regularization_signal 与 adv_generation 的
        adversarial_dataset 合并为交接包 → 交付 ⑤ 数据评估清洗
```

## 5. 输出契约

- `replay_buffer/`（更新后）
- `retention_config.json`
- `regularization_signal.json`（`{method:"EWC", fisher_path:"...", lambda:...}` 或 `{method:"L2-SP", anchor:"..."}`）
- 监控建议：每轮跟踪 `forgetting_rate[dim] = max(0, prev_acc[dim] − curr_acc[dim])`

## 6. 安全适配

- 回放样本若含敏感内容，先经层⑤ PII 脱敏（不重复做）。
- `retain_flag` 样本属"已知掌握的安全能力"，其 gold label 来自权威基准，禁止被对抗噪声覆盖。
- 人审断点：当 `forgetting_rate` 跨维度超阈值且无法用回放压回，强制 HITL（层⑨ 安全断点）。
