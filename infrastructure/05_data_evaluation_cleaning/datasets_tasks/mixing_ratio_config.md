# 新老数据配比配置模板（Old/New Mixing Ratio）

> 层⑤ 阶段 4「新老数据配比」的产出模板。目标：用新 badcase 补强短板，同时不遗忘旧能力。
> 遗忘门控逻辑直接参考 AutoLab `grpo_multisource`（见层③ `datasets_tasks/autolab_tasks/grpo_multisource/`）。

## 1. 配比配置模板（YAML）

```yaml
mixing:
  old_data:
    path: /data/old_train.jsonl
    weight: 0.8            # 旧数据占比（防遗忘主力）
  new_data:               # 回流 badcase / 新分布补强
    path: /data/new_badcase_clean.jsonl
    weight: 0.2
  sampling: replay         # replay(回放) | curriculum(课程式) | mix
  curriculum:
    enabled: true
    hard_sample_boost: 1.5 # 难样本（badcase）加权
retention_gate:            # 遗忘门控：参考 grpo_multisource
  reference_metric: old_holdout_accuracy
  min_retention_ratio: 0.9 # 重训后保留集相对下降不得 > 10%
  on_violation: zero_score # 违反则该配比判 0，回退调高 old weight
```

## 2. 配比策略速查

| 新数据规模 | 偏移程度 | 建议 old:new | 策略 |
|-----------|----------|--------------|------|
| 小（<1k） | 中 | 10:1 ~ 5:1 | 高复用 + 轻量微调（LoRA） |
| 中（1k-10k） | 中 | 5:1 ~ 2:1 | replay + 难样本加权 |
| 大（>10k） | 高 | 2:1 ~ 1:1 | curriculum + 全量重训 |
| 极大（领域迁移） | 极高 | 1:1 ~ 1:2 | 课程式渐进，分步解冻 |

## 3. 配比有效性门控（离线小步验证）

- [ ] 在保留集（old holdout）上重训后准确率 ≥ `min_retention_ratio`（遗忘门控通过）
- [ ] 在 badcase 验证集上准确率较旧模型有提升（新能力补强）
- [ ] 未出现灾难性遗忘（旧能力无显著掉点）
- [ ] 配比经小步离线验证通过，再进入代码开发 ⑥ / 实验执行 ⑦

> 注：若遗忘门控失败，回到本阶段调高 `old_data.weight` 或增大 `replay` 比例，直至通过。
> 完整可运行基准见层③ `grpo_multisource`（多源 + 遗忘门控）与 `data_select_ifeval`（数据筛选评估）。
