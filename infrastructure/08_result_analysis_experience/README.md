# 基础设施层 ⑧：结果分析与经验生成（Result Analysis & Experience Generation）

> 统一梳理「结果分析」与「经验生成」环节的 **skill / agent / 数据集任务 / 评测方式**。
> 来源：《AI_Research_Infrastructure_Report.md》§3.2（MLEvolve 分析/记忆、Arbor 经验回传）、§4.7（MLEvolve 全局记忆）、§4.6（Arbor Backpropagate）。

## 1. 定位

把执行层证据转化为**可信结论 + 可复用经验**：解析指标、统计检验、跨分支融合/聚合、数据泄露检查，并把洞察沉淀为全局记忆供后续设计复用（自进化闭环）。对应通用流程闭环的 `结果分析` 节点与「知识沉淀/自进化」底座。

## 2. Skill（已梳理至 `skills/`）

- `result_analysis.md` — 结果分析 runbook：指标提取 → 统计检验 → 声明生成 → 经验沉淀；引用层② `result-to-claim`/`experiment-audit` 与层④ `stat_research_agent`。

## 3. Agent（已拷贝至 `agents/`）

- `mle_evolve_analysis/` — **MLEvolve 分析 + 全局记忆（经验生成核心）**：
  - `result_parse_agent.py`（日志/指标解析）、`fusion_agent.py`（跨分支融合）、`aggregation_agent.py`（多分支聚合）、`data_leakage_agent.py`（数据泄露检查）
  - `memory/`（`global_memory.py` BM25+FAISS 检索、`retriever.py`、`record.py`、`embedding_models.py`）— 经验驱动记忆
- `arbor_backpropagate/` — **Arbor 经验回传**（Backpropagate 步）：
  - `convergence.py`（收敛检测：score velocity / plateau）、`idea_tree.py`（洞察向上传播、归纳经验）

## 4. 数据集任务（已梳理至 `datasets_tasks/`）

- `memory_schema.md` — 经验/记忆 schema 与分析模板：MLEvolve `memory/record.py` 记录格式、分析输入/输出模板。

## 5. 评测方式（已梳理至 `eval_methods/`）

- `analysis_quality.md` — 分析质量：指标提取准确性、统计正确性、声明可证伪、防泄露。
- 经验复用增益：记忆命中率、跨任务迁移收益（自进化指标）。

## 6. 安全领域适配

- 结论须区分「攻击成功」与「防御侧安全性降级」，二者同时报告。
- 红队结论中的目标/载荷明文不进全局记忆明文；记忆以脱敏指纹存储。
- 被证伪的攻击声明沉淀为「已知无效方向」（`refuted`），避免重复探索。
