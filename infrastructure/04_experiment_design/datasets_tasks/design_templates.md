# 实验设计模板与来源（Design Templates）

本目录沉淀「实验设计」环节的模板与上游来源索引。

## 1. AutoResearchClaw 22 阶段流水线（设计阶段 7–11）
- Phase 3: Design = 知识合成 → **实验设计** → 代码生成
- 对应 `agents/.../researchclaw_pipeline/stage_impls/_experiment_design.py`（见层⑦）
- 模板字段：领域选择 → 问题定义 → 文献依据 → 假设 → 数据集 → 基线 → 消融计划 → 度量 → 预算

## 2. NanoResearch 9 阶段（PLANNING）
- 实验计划设计：数据集 + 基线 + 消融
- 产出：可运行实验计划（进入 CODING → EXECUTION）

## 3. 统计实验设计 skill（来自 `agents/stat_research_agent/skills/`）
- `statistical-problem-formulation` — 把研究问题形式化为可检验的统计假设
- `statistical-method-design` — 设计实验/方法以回答假设
- `statistical-experimental-evaluation` — 评估实验证据强度
- `statistical-theory-analysis` — 理论保证分析
- `stat-result-validator` — 结果校验（防伪阳性）

## 4. Arbor 假设树节点 schema（来自 `agents/arbor_coordinator/idea_tree.py`）
```
hypothesis: str      # 可证伪假设
status: pending|running|success|pruned|merged
insight: str         # 实验后归纳的洞察
score: float         # 由收敛检测监控
code_ref: str        # 关联代码/实验证据
parent: node_id      # 祖先洞察追溯
```

## 5. MLEvolve 节点方案（来自 `agents/mle_evolve_design/planner/`）
- `planner_with_memory.py`：基于全局记忆（BM25+FAISS）生成带上下文的节点方案
- 与层⑧ `mle_evolve_analysis/memory/` 形成「设计→执行→分析→记忆→再设计」闭环

> 安全适配：所有模板须预注册假设与度量，红队模板目标字段置空/加密。
