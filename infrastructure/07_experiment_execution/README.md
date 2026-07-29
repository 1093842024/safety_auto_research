# 基础设施层 ⑥：实验执行（Experiment Execution）

> 统一梳理「实验执行」环节的 **skill / agent / 数据集任务 / 评测方式**。
> 来源：《AI_Research_Infrastructure_Report.md》§3.2（MLEvolve engine、AutoResearchClaw 22 阶段、Arbor Executor、NanoResearch）、§4.3（自动化实验流水线）。

## 1. 定位

在隔离、可复现环境中**真实运行**代码并收集证据（非 LLM 编造）。对应通用流程闭环中的 `实验执行` 节点。评测/基准（层③）的 Harbor 沙箱是本层的容器化底座。

## 2. Skill（已梳理至 `skills/`）

- `execution_runbook.md` — 实验执行 runbook：隔离环境 → 运行 → 证据收集 → 回传，引用层③ AutoLab Harbor 沙箱。

## 3. Agent（已拷贝至 `agents/`）

- `mle_evolve_engine/` — **MLEvolve MCGS 执行引擎**（#1）：
  - `engine/`（`agent_search.py` 协调器、`search_node.py` 节点、`node_selection.py` UCT 选择、`execution.py`/`executor.py` 隔离执行、`evaluation.py` 度量、`conditions.py`/`solution_manager.py`）
  - `run.py` / `run_single_task.sh` — 单任务/全流程入口
- `researchclaw_pipeline/` — **AutoResearchClaw 22 阶段流水线**（含执行阶段 `_execution.py`、设计 `_experiment_design.py`、分析 `_analysis.py`）
- `arbor_executor/` — **Arbor Executor**（实现代码变更 → 运行实验 → 汇报证据，隔离工作树）
- `nanoresearch_execution/` — **NanoResearch 执行**（GPU 真实计算 + 自动修复循环：`local_runner`/`cluster_runner`/`repair_*`）

## 4. 数据集任务（已梳理至 `datasets_tasks/`）

- `compute_budgets.md` — 计算资源配置：GPU 规格与预算（来自 AutoLab README：System Opt 需 AMD Ryzen 9 9950X/64GB；CUDA 需 H100；Model Dev 需 H100/L40S）。

## 5. 评测方式（已梳理至 `eval_methods/`）

- `execution_reproducibility.md` — 执行可复现/隔离：`harbor-canary`、固定 seed、环境快照、隔离网络。详细评分见层③ `scoring.md`。

## 6. 安全领域适配

- 红队执行**必须**在 Harbor/Docker 隔离容器内，`allow_internet=false`，目标/载荷不外传。
- 含 `harbor-canary` GUID 防训练数据污染与评测作弊。
- 执行日志仅记录指标与错误，不下落红队目标明文。
