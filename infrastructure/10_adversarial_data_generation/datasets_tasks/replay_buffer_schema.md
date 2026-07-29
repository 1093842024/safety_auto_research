# Replay Buffer Schema — 经验回放缓冲（防遗忘核心，层⑩）

> 持续对抗重训中保护"已掌握知识"的样本级缓冲。对应 `adv_antiforgetting.md` 与
> 原 doc "反思经验回放池"。每条样本按能力维度分桶，带 mastery 与保留标志。

## 数据结构

```json
{
  "sample_id": "replay_3301",
  "text": "<已掌握样本，敏感内容须经层⑤ PII 脱敏>",
  "gold_label": "unsafe",
  "capability_dim": "tool_use_safety",
  "mastery_score": 0.98,
  "last_seen_round": 7,
  "retain_flag": true,
  "fingerprint": "fp:sha256:cd34...",
  "source": "benign_sensitive_set | eval_gold | prev_adv_passed"
}
```

## 字段语义

| 字段 | 含义 | 更新规则 |
|------|------|---------|
| `capability_dim` | 能力维度（RUBAS 四维 / 业务风险类） | 固定 |
| `mastery_score` | 当前模型在该样本上的稳定正确率 | 每轮 ③ 后滚动更新 |
| `last_seen_round` | 最近一次进入训练/评估的轮次 | 采样时更新 |
| `retain_flag` | 是否强制保留（永不丢弃） | `mastery_score ≥ retention_floor[dim]` 时置 true |
| `fingerprint` | 内容指纹（不落明文危险 payload） | 固定 |

## 采样策略（与 retention_config 联动）

- **优先级采样**：高 `mastery_score` 波动的样本优先（防回退）。
- **多样性采样**：覆盖所有 `capability_dim`，避免只回放单一维。
- **时间衰减**：`last_seen_round` 久的样本权重略降，但 `retain_flag=true` 样本**永不丢弃**。
- **反思经验回放**（Self-Play）：从缓冲采失败/边界案例，LLM 反思"为何旧能力被威胁" → 反哺权重。

## 与层⑤分工

- 层⑤ `mixing_ratio_config`：数据集/配比层粗粒度保留率。
- 本缓冲：样本级回放 + 正则信号（EWC/L2-SP），二者共用 `retention_config`。

## 监控

`forgetting_rate[dim] = max(0, prev_acc[dim] − curr_acc[dim])`；跨维超阈值 → 层⑨ R10 路由。
