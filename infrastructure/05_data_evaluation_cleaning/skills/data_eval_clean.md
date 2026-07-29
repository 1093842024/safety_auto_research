---
name: data-eval-clean
description: 数据驱动重训范式的数据评估与清洗 runbook。针对"已优化模型 + 回流 badcase"场景，执行 数据评估 → 清洗 → 标签优化适配 → 新老数据配比，产出可供训练的高质量数据集与配比配置。Use when a trained model surfaces badcases and you need to clean/relabel/rebalance data before retraining.
---

# 数据评估与清洗 Runbook（层⑤ ★核心 playbook）

> 适用：模型已成型、生产/评测回流 badcase，需要数据层面的迭代而非改架构。
> 出口：清洗后数据集 + 配比配置 → 代码开发 ⑥（训练代码）→ 实验执行 ⑦（重训）。

## 触发条件

满足任一即进入本层：
- 线上/评测回流 badcase 数量达到阈值（建议 ≥ 50 条且占比 > 1%）。
- 结果分析 ⑧ 判定根因为"数据质量/分布偏移/标签噪声"（经层⑨ 路由回本层）。
- 新任务/新分布接入，需复用旧模型能力（新旧数据配比问题）。

## 阶段 1 — 数据评估（Diagnose）

目标：量化"现在的数据到底哪里不行"，而非盲目清洗。

- **badcase 分型**：按错误类型聚类（答非所问 / 格式违规 / 知识错误 / 安全越界 / 分布外），产出 `badcase_taxonomy.json`。
- **分布偏移度量**：badcase 分布 vs 训练集分布（KS/PSI 或类别占比差）；标记偏移最大的维度。
- **现有数据质检**：覆盖率、重复率、标签噪声率（小样本人工抽检估计）、泄露率（复用层⑧ `data_leakage_agent`）。
- **缺口定位**：哪些能力维度在 badcase 中高频失败但训练集样本稀少 → 决定补数据与清洗优先级。

✅ 门控：产出 `data_quality_report.md`（见 `datasets_tasks/data_quality_checklist.md`）。

## 阶段 2 — 数据清洗（Clean）

目标：降噪、去重、归一，保留信号。

- **去重**：精确去重 + 语义去重（embedding 近邻）；剔除与验证/测试集重叠样本（防泄露）。
- **去噪/纠偏**：过滤乱码、截断、无效格式；修正明显错误样本。
- **规模化 ETL**：大批量走 NanoResearch `ray-data`（流式、CPU/GPU 混合、支持 Parquet/JSON/多模态）。
- **泄露门控**：训练/验证/测试按 group 与时间隔离；跑 `data_leakage_agent` 复核。

✅ 门控：清洗后数据集 `clean_train.jsonl` + `clean_eval.jsonl`，去重率/噪声率下降记录。

## 阶段 3 — 标签优化适配（Re-label）

目标：让标签体系适配新问题，而非套用旧标签。

- **标签体系设计/修订**：基于阶段 1 的 badcase 分型，设计或修订 taxonomy（见 `datasets_tasks/label_schema_template.md`）。
- **重新标注/纠偏**：对 badcase 与冲突样本重标；统一标签语义（消歧义、合并稀有类）。
- **标注一致性**：双人/双模型标注，算 inter-annotator agreement（Kappa/Fleiss）；低于阈值回流人工。
- **安全标签**：有害内容分级等安全标签须人工复核，禁止纯自动定级。

✅ 门控：新标签 schema + 标注一致性 ≥ 阈值（见 `eval_methods/data_quality_metrics.md`）。

## 阶段 4 — 新老数据配比（Re-balance）

目标：用新 badcase 补强短板，同时不遗忘旧能力。

- **比例设计**：old : new 比例（常见 1:1 ~ 10:1，取决于新数据规模与偏移程度）；小新数据用高复用 + 轻量微调。
- **回放（replay）/课程式（curriculum）**：旧数据按比例回放防遗忘；难样本课程式加权。
- **遗忘门控**：以保留率约束新配比——参考 AutoLab `grpo_multisource` 的"遗忘门控"（retention gate）：
  若重训后在保留集准确率下降超阈值（如 >10% 相对），该配比判 0 分，须回退调高 old 占比。
- **配置产物**：`mixing_config.yaml`（old/new 路径、比例、采样权重、课程调度）。

✅ 门控：配比经离线小步验证（retention gate 通过）后再进入 ⑥。

## 阶段 5 — 交接（Handoff）

- 产出：`clean_train.jsonl` + `clean_eval.jsonl` + `mixing_config.yaml` + `data_quality_report.md`。
- 交给 **代码开发 ⑥**：训练代码读取上述路径与配比；交给 **实验执行 ⑦**：按配比重训。
- 若阶段 1 评估显示问题不在数据（如架构/训练策略），经层⑨ 路由到 ④/⑥ 而非本层重做。

## 失败回退（REVISIT）

- 清洗后仍高噪声 → 回阶段 2 加重清洗/人工抽检。
- 标注一致性低 → 回阶段 3 修订 schema / 加双标。
- 重训遗忘旧能力 → 回阶段 4 调高 old 占比 / 加 replay。
- 实验执行 ⑦ 或结果分析 ⑧ 反馈根因在数据 → 层⑨ 路由回本层对应子阶段。
