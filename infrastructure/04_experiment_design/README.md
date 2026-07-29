# 基础设施层 ④：实验设计（Experiment Design）

> 统一梳理「实验设计」环节的 **skill / agent / 数据集任务 / 评测方式**。
> 来源：《AI_Research_Infrastructure_Report.md》§3.2（各项目关键工具）、§4.3（自动化实验流水线）、§4.6（Arbor 假设树）、§4.7（MLEvolve MCGS）。

## 1. 定位

实验设计衔接「Idea 生成」与「代码生成」：把可辩护的 Idea Card 转化为**可证伪的假设 + 可复现的实验协议**（数据集 / 基线 / 消融 / 度量）。
对应通用流程闭环中的 `实验设计` 节点（§3.1 图）。

## 2. Skill（已梳理至 `skills/`）

- `experiment-bridge` — 设计↔实现的桥梁：把实验方案落成可实现、可验收的代码任务。
- `research-refine` — 实验方案精化：基于反馈迭代修正假设与协议。
- `research-pipeline` — 22 阶段流水线编排（含设计阶段 7–11）。

## 3. Agent（已拷贝至 `agents/`）

- `arbor_coordinator/` — **假设树驱动的实验调度核心**（Arbor）：
  - `orchestrator.py`（研究总监：维护 Idea Tree、驱动 Arbor 六步循环、下发实验）
  - `idea_tree.py`（假设树：hypothesis / status / insight / score / code_ref）
  - `convergence.py`（收敛检测：score velocity 监控、plateau 检测）
  - `context_prune.py` / `checkpoint.py` / `config.py` / `hitl.py` / `contamination.py` / `tools/`（隔离工作树、完整性校验、经验记录）
- `stat_research_agent/` — **统计实验设计 7 角色**（AutoResearchClaw）：
  - `stat-problem-formulator`（问题形式化）、`stat-method-proposer`（方法提议）、`stat-experiment-designer`（实验设计）
  - `stat-theory-analyzer`（理论分析）、`stat-quality-auditor`（质量审计）、`stat-comparison-analyst`（对比分析）、`stat-result-synthesizer`（结果综合）
  - 配套 `skills/`：统计问题形式化 / 方法设计 / 理论分析 / 实验评估 / 结果校验。
- `mle_evolve_design/` — **MCGS 实验设计**（MLEvolve #1）：
  - `planner/`（`base_planner.py` / `planner_with_memory.py` — 带全局记忆的节点方案规划）
  - `evolution_agent.py`（停滞时基于分支进化轨迹改进设计）

## 4. 数据集任务（已梳理至 `datasets_tasks/`）

- `design_templates.md` — 实验设计模板：AutoResearchClaw 22 阶段设计阶段（知识合成→实验设计→代码生成）、
  NanoResearch PLANNING（数据集+基线+消融）、统计实验设计 skill 清单。

## 5. 评测方式（已梳理至 `eval_methods/`）

- `experimental_design_quality.md` — 设计质量四维：① 假设可证伪性 ② 消融/对照组完整性 ③ 基线覆盖 ④ 度量可复现性。

## 6. 安全领域适配

- 安全实验须预注册假设与度量，避免「p-hacking / 事后挑指标」。
- 攻击/防御实验设计须包含**对照（无防御基线 vs 有防御）**与**多种子**以排除偶然性。
- 红队实验设计受控：目标/载荷细节不外传，协议在隔离环境执行。
