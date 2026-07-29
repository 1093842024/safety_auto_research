# 标签 Schema 模板（Label Optimization & Adaptation）

> 层⑤ 阶段 3「标签优化适配」的产出模板。针对 badcase 暴露的新问题，设计/修订标签体系并规范重新标注。

## 1. 标签体系设计原则

- **可标注性**：每个标签有可操作的判定规则，避免主观模糊边界。
- **互斥且完备（MECE）**：类别间尽量互斥；保留 `other/unknown` 兜底类。
- **适配新问题**：基于 `badcase_taxonomy.json` 增补新类，而非套用旧 schema。
- **安全分级**：有害内容分级等安全标签须显式定义级别与人工复核要求。

## 2. Schema 定义模板（YAML）

```yaml
schema_version: 1
task: <任务名，如 instruction_following / safety_classification>
labels:
  - name: format_violation
    definition: 输出违反长度/关键词/结构约束
    examples: [...]
    requires_human_review: false
  - name: harmful_content
    definition: 含有害/越狱内容
    examples: [...]
    requires_human_review: true      # 安全标签强制人工复核
  - name: unknown
    definition: 无法归入上述任何类
    examples: [...]
    requires_human_review: false
conflict_resolution:
  - rule: 安全类优先于格式类
  - rule: 歧义样本归入 unknown 而非猜测
```

## 3. 重新标注协议（Relabeling）

- **范围**：badcase + 与 badcase 冲突的旧样本。
- **双标注**：每个样本至少 2 个标注源（人/模型），记录各自标签。
- **一致性计算**：Cohen's κ（双人）/ Fleiss' κ（多人）；κ < 0.6 触发仲裁。
- **仲裁**：κ 不达标样本交第 3 方（人或强模型）定标。
- **可追溯**：每条标注记录 `annotator_id / timestamp / source_sample_id`，供层⑨ 经验回注。

## 4. 标签优化适配检查清单

- [ ] 新标签覆盖 badcase 主要错误类型
- [ ] 语义歧义类已合并/消歧
- [ ] 稀有类处理策略（合并/过采样）已定义
- [ ] 安全标签已设 `requires_human_review: true`
- [ ] 双标注 κ ≥ 0.6（否则仲裁后重算）
- [ ] 旧标签到新标签的映射表已建立（旧数据可批量迁移）
