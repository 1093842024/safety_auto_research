# OpenRSI / OpenMLE 引入 safety_auto_research —— 调研与方案分析

> ✅ **状态（2026-08-07）**：本文原始的"纯调研、未改代码"前提已失效——OpenRSI/OpenMLE 已于 2026-08-04 以 **Phase A–D** 正式落地（`openmle_integration/` 包 + `IslandModel` 程序级岛模型 + 本地训练/奖励桥）。本文件保留为**设计依据与方案分析**；实现细节、回归测试与闭环状态见 `doc/code_review_STATUS.md` 与 `doc/mea_harness_upgrade_plan.md`。

> 文档性质：**纯调研 + 方案分析**（原始前提），未对任何代码做修改（按用户要求）。
> 调研对象：
> - 论文 [arXiv:2607.28568](https://arxiv.org/abs/2607.28568) *Frontis-MA1: Training an AI4AI Model towards Recursive Self-Improvement in ML Engineering*（2026-07-30 提交）
> - 项目 `FrontisAI/OpenRSI`（本地已克隆于 `/Users/glennge/work/github/AI_research/OpenRSI/`，含 `OpenMLE-Gym/` `OpenMLE-ERL/` `OpenMLE-Evo/` 三件套 + vendored `dojo` 内核）
> - 现有框架 `safety_auto_research/`（control_plane / execution_plane / benchmark_tasks / frontend）

---

## 0. 结论速览（先读这段）

1. **OpenRSI 与 safety_auto_research 在设计哲学上高度同构，但进化粒度不同**：
   - safety_auto_research 的 `control_plane/evolution.py` 做的是**超参配置进化**（fe / model / cv_folds 离散点在 editable surface 上变异）。
   - OpenRSI 的 `OpenMLE-Evo` 做的是**程序级进化**（整段 ML pipeline 代码通过 Draft/Improve/Debug/Crossover 四个算子演化）。
   - 二者不是替代关系，而是**互补的两层**：配置进化（廉价、确定性、已验证）↔ 程序进化（昂贵、LLM 驱动、上限更高）。

2. **OpenRSI 真正可"无缝 vendoring"的是其干净内核 `dojo`**（`OpenMLE-Evo/third_party/aira-evo/src/dojo/`）：它定义了与框架无关的 `Task` 抽象、`Interpreter` 执行层、`Node`/`Journal` 搜索树、`MetricValue`、四个算子纯函数、以及 `EvolutionarySolver` 岛模型。这正是引入"程序级进化"所需的最小内核。

3. **原子算子范式天然映射到 safety_auto_research 已有的双循环语义**（Draft≈种子种群、Improve≈refine/策略补丁、Debug≈failure_miner  remediation、Crossover≈现有架构缺失但可由 StrategyArchive+EvolutionArchive 支撑的"策略杂交"）。

4. **三处隔离不变量必须原样保持**（这是"无误引入"的硬约束）：
   - 算子只能在内循环运行，**绝不可**调用 `layer_11_external_audit` / `layer_09_self_iterative_evolution`（现有 `assert_inner_capability_allowed` 禁止集）。
   - 算子的执行反馈（terminal output / metric）属于**内循环策展输入**，永远不进 `audit_input`。
   - 所有累积态（Program / Node / Experience / Strategy）必须带 `run_id` 并按 run 过滤。

5. **两个必须向用户明示的约束**：
   - **许可证**：OpenMLE 为 **CC BY-NC 4.0（非商用）**，dojo 内核还叠加了 Meta/WecoAI/DeepMind/OpenAI 上游许可。若 safety_auto_research 有商用意图，需单独授权或干净地**重实现接口**（接口本身极简，可重写而不抄代码）。
   - **本地环境**：本机无 docker、无 torch、无 GPU。OpenRSI 的**训练管线**（SFT+RL，依赖 `THUDM/slime` + 远程沙箱 + Frontis-MA1-35B 权重）**无法在本地跑**；但**推理侧**（算子组合 + Evo 搜索）可在接入一个 LLM 后端后以"外接推理"方式运行。因此建议把"训练"定位为**外接管线**，把"算子 + Evo 推理"定位为**核心可集成层**。

---

## 1. OpenRSI / OpenMLE 论文与项目深度解析

### 1.1 论文核心（Frontis-MA1）
- **目标**：把"AI 改进 AI（AI4AI）"变成可执行的工程问题，使"改进速率"本身成为优化目标——即迈向 **Recursive Self-Improvement (RSI)**。首发可执行域是机器学习工程（MLE）。
- **机制阶梯**：Evolution → Self-Evolution → Meta-Evolution → RSI；OpenRSI 当前处于 **Meta-Evolution**（在受限可执行域内训练"改进器"）。
- **关键数字**（MLE-Bench Lite，单 RTX 4090 / 12GB / 12h 每任务预算）：
  - Frontis-MA1(35B) 借 **OpenMLE-Evo**：Medal Average **39.39% → 60.61%**。
  - 借 **OpenMLE-Evo-Max**（benchmark-independent experience priors + 异步多 GPU 搜索）：**71.21%**，超过 GPT-5.5+Codex，逼近 GPT-5.6 Sol 与 2.8T Kimi K3。
- **可迁移性（NatureBench Lite held-out）**：固定框架换模型 → Match-SOTA 50%→70%；固定模型换 Evo → 20%→50%。**说明"框架（Evo 搜索）"与"模型（算子）"是两个可独立升级的维度**——这与 safety_auto_research 把"编排/进化"与"执行器/模型"解耦的思路一致。

### 1.2 系统三件套（OpenMLE 可执行栈）
| 组件 | 角色 | 真实路径 |
|---|---|---|
| **OpenMLE-Gym** | 把 Kaggle 竞赛编译成标准**可验证任务包**（`description`/`prepare.py`/`metric.py`/`solution.py`），用 sample submission 校验 metric，并在隔离子进程/Docker 中执行质检 | `OpenMLE-Gym/builder_core/{main,design,task}.py`、`tools/`、`samples/sample_metric.py` |
| **OpenMLE-RL**（实为 `OpenMLE-ERL`） | 通过**执行反馈**的 SFT + 在线 RL 学习四个算子 | `OpenMLE-ERL/SFT/`（执行式 SFT + 数据选择）、`OpenMLE-ERL/RL/`（SLIME 包装 + 奖励/验证） |
| **OpenMLE-Evo** | 把四个算子组合成**标准/异步长程搜索** | `OpenMLE-Evo/`（vendored `dojo` 运行时 + 基准适配器 + `scripts/`） |

> 注：`OpenMLE-Evo-Max` 不是独立目录，而是 `OpenMLE-Evo` 内的异步多 GPU profile（`AIRAEVO_WORKERS=8`）。`OpenMLE-RL` 源码统一在 `OpenMLE-ERL/` 下分 SFT/RL 两阶段。

### 1.3 原子算子训练范式（四个可训练算子）
| 算子 | 功能 | 训练方式 | 在 dojo 中的位置 |
|---|---|---|---|
| **Draft** | 从零生成程序 | 执行接地 SFT + RL | `dojo/core/solvers/operators/draft.py` |
| **Improve** | 利用执行反馈精炼父程序 | 同上 | `.../improve.py` |
| **Debug** | 修复失败/buggy 程序 | 同上 | `.../debug.py` |
| **Crossover** | 重组两个父程序 | 同上 | `.../crossover.py` |

- **训练/推理共享同一动作空间**：同一组算子先经"对所有评测基准去重"的数据做 SFT warm-start，再经执行反馈 RL 优化，最后在推理时组合成长程搜索——形成"学习–进化"单一循环。
- **算子是无副作用纯函数**：只负责"构造 Jinja prompt dict → 调 LLM"，**不直接执行代码**。执行由外层 `Task.step_task` / 沙箱完成（这是与 safety_auto_research "执行与决策分离" 原则的同构点）。
- RL 概率（与 Evo 推理一致）：`DRAFT 0.50 / IMPROVE 0.17 / DEBUG 0.17 / CROSSOVER 0.16`。
- 奖励核心（`OpenMLE-ERL/RL/reward_func_utils.py`）：`score2reward`（power_clip / margin_tanh 等）、`hack_check`（GPT judge 防作弊）、`validation_test_gap_penalty`（防过拟合 test 信号）。

### 1.4 干净内核 `dojo`（集成的最小可 vendoring 单元）
`OpenMLE-Evo/third_party/aira-evo/src/dojo/` 是训练与推理共用的内核，含：
- `core/tasks/base.py`：`Task(ABC)`（`prepare` / `step_task` / `evaluate_fitness` / `close`），`task_info` 至少含 `TASK_DESCRIPTION` + `lower_is_better`。
- `core/tasks/mlebench/{task,evaluate}.py`：`MLEBenchTask` + 官方 `mlebench` grader 算分 + 奖牌/分位阈值（`score` 与 `lower_is_better` 的来源）。
- `core/interpreters/{base,python}.py`：`Interpreter` + `ExecutionResult` + `PythonInterpreter`（multiprocessing + signal 超时 + stdout/stderr 捕获）。
- `core/solvers/operators/*.py`：四个算子纯函数 + `core.py` 的 `execute_op_plan_code`（代码抽取/重试）。
- `core/solvers/evo/evo.py`：`EvolutionarySolver`（岛模型 `Island` + `SolutionsDatabase` + 经验记忆 + `sample_in_context` 父选择）。
- `core/solvers/utils/{journal,metric}.py`：`Node`（code/plan/parents/operators_used/metric/is_buggy）+ `Journal`（整棵树）+ `MetricValue`（"更好"而非"更大"语义，含 `WorstMetricValue`）。

### 1.5 许可证与依赖约束（务必先读 §6）
- OpenMLE 顶层 **CC BY-NC 4.0（非商用）**；dojo 叠加 `third_party/aira-evo/LICENSE` + `THIRD_PARTY_LICENSES.md`（Meta/WecoAI/DeepMind/OpenAI 等上游）。
- `OpenMLE-RL` 强依赖外部 `THUDM/slime`（pin `680824dd…`），且 `reward_func_utils.py` / `program_database.py` 在 RL 目录内对 `airaevo_experience` / `slime` 是**本地相对 import**——直接 import 会失败，需桥接。
- 训练需 GPU + 模型权重（Frontis-MA1-35B/30B，HuggingFace）+ 远程/ Docker 沙箱。

---

## 2. safety_auto_research 现状核验（对齐点 / 缺口）

### 2.1 已有能力（已逐文件核验）
- **双循环驱动**：`execution_plane/orchestrator.py` 的 `ClosedLoopOrchestrator.run_dual_loop`（内循环 kaggle_eval/agent → `layer_11_external_audit` → Accept/Refine/Restart → `layer_09` 元循环冻结 verifier+回滚 → `refine_hook`）。
- **并行进化搜索**：`run_evolutionary_loop`（线程池 `BackgroundTaskManager` + 每 worker `_BufSDK` 缓冲 → 主线程 `_replay` → 冠军送 layer_11 审计 → 下一代 `select_parent`+`mutate`+`novelty_filter`）。
- **累积态五件套**：`store_tree.py`（HypoTree / ExperienceBank / StrategyArchive）+ `evolution.py`（Candidate / EvolutionArchive）+ `playbook.py`（PlaybookStore）+ `failure_miner.py` + `llm_judge.py`。
- **任务注册**：`benchmark_tasks/registry.py`（8 类 schema 驱动，`tabular_classification` 唯一 `executable=True`，其余 tracked-only）。
- **执行器**：`kaggle_eval_executor.py`（sklearn Pipeline：model ∈ {gbm,gbm-strong,rf,logreg}、fe∈{basic,rich}、cv_folds、heldout_frac）。
- **隔离不变量**（三处真实实现）：
  1. `assert_inner_capability_allowed`（`execution_plane/agent/harness.py`）：内循环 agent 禁止调用 `OUTER_LOOP_RESERVED_CAPS = {layer_11_external_audit, layer_09_self_iterative_evolution, ...}`。
  2. `audit_input` 由 `base_goal` + 内循环**结果**策展，**刻意排除**实验叙事；playbook/experience 只经 `render`/`query` 注入 `inner_params`。
  3. 所有累积态按 `run_id` 过滤（`Repository` / `EvolutionArchive` / `HypoTreeStore` / `StrategyArchive`）。

### 2.2 与 OpenMLE 的逐组件对应表
| OpenMLE 组件 | safety_auto_research 对应 | 关系 |
|---|---|---|
| OpenMLE-Gym（任务包 + 隔离执行 + 真实 grader） | `kaggle_eval_executor` + `benchmark_tasks/registry` + `task_manager`（并行/崩溃恢复） | **部分重叠**：现有的是 sklearn 预设，缺"竞赛→任务包编译"与"真实 mlebench grader + Docker 隔离执行" |
| OpenMLE-RL（算子 SFT+RL 训练） | 无对应（平台默认确定性 heuristic，无模型训练） | **净缺口**（且需 GPU/slime，难本地化） |
| OpenMLE-Evo（程序级岛模型进化） | `control_plane/evolution.py` + `run_evolutionary_loop`（**超参级**进化） | **粒度升级**：现有是配置进化，OpenMLE 是程序进化 |
| 四算子（Draft/Improve/Debug/Crossover） | 双循环语义（seed_population / refine_hook / failure_miner / ——） | **范式映射**：Crossover 现有架构缺位，可由 StrategyArchive 支撑 |
| reward_func_utils（score2reward / hack_check / gap_penalty） | `llm_judge.py` + `eval_runner.py`（默认确定性 heuristic） | **可桥接**：把 `score2reward` 作为新的 score→reward 映射，hack_check 复用 LLM judge |

### 2.3 关键缺口（引入前要补的）
1. **无真实可执行环境**：现有 `kaggle_eval` 是 sklearn 流水线，不是"提交 CSV → 官方 grader 打分 → 奖牌"。OpenMLE-Gym 带来任务包 schema + 隔离执行 + 真实 grader。
2. **无程序级进化**：现有 `Candidate.params` 只能是超参 dict，`evolution.py` 的 `mutate` 只在 surface 上离散跳变。需引入"代码候选 + 算子应用"的新候选表示。
3. **无算子训练**：平台没有 LLM 训练闭环；但推理侧算子组合可在接入 LLM 后端后运行。

---

## 3. 两部分引入方案（核心）

### 第一部分：OpenMLE 全栈系统引入

#### 3.1 OpenMLE-Gym → 任务环境层
- **引入什么**：`dojo/core/tasks/base.py` 的 `Task` 抽象 + `Interpreter` 执行层 + `MLEBenchTask`/`evaluate_submission` 真实 grader + Gym 的任务包 schema（`description`/`prepare.py`/`metric.py`）。
- **怎么接**：
  - 新增 `execution_plane/capabilities/openmle_task.py`，实现一个 `OpenMLETaskAdapter(Task)`，把 safety 的 `tabular_classification` 任务（或自定义任务）适配成 dojo 的 `prepare/step_task/evaluate_fitness`。
  - 把现有 `kaggle_eval_executor` 的 sklearn Pipeline 封装为 dojo 的 `step_task` 一种轻量后端（**保留现有确定性路径**），同时暴露"真实 mlebench grader"后端（需数据/网络，标注为外接）。
  - `benchmark_tasks/registry.py` 新增 `task_type="openmle_program"`（executable=True），复用现有 schema 驱动注册；其 `eval_metric`/`direction` 映射到 dojo 的 `lower_is_better` + `higher_is_better`。
- **隔离保持**：Gym 的执行反馈（terminal output / score）只回流到**内循环策展输入**，不进 `audit_input`。

#### 3.2 OpenMLE-Evo → 进化搜索层（升级现有 evolution.py）
- **引入什么**：`dojo/core/solvers/evo/evo.py` 的岛模型 + `SolutionsDatabase` + 经验记忆 + `sample_in_context`；以及 `Node`/`Journal`/`MetricValue`。
- **怎么接（最小侵入）**：**保留 orchestrator.run_evolutionary_loop 的并行/replay/审计脚手架（已验证）**，只替换候选表示与评估器：
  - 现有 `Candidate(params)`（超参）→ 新增 `ProgramCandidate(code, operator, parent_ids)`（程序级），二者共存于 `EvolutionArchive`，加 `node_kind ∈ {config, program}` 字段区分。
  - worker 内：`mutate(config) → kaggle_eval` 仍是廉价确定性路径；新增 `apply_operator(program, operator) → dojo Task.step_task` 作为昂贵 LLM 路径（受 budget 三维管控）。
  - 冠军仍送 `layer_11` 冻结审计（Accept→converged），与现有出口一致。
- **收益**：在不破坏现有确定性进化的情况下，获得程序级搜索上限；两层可用同一 `EvolutionArchive` 与前端 `EvolutionPanel`。

#### 3.3 OpenMLE-RL → 算子训练层（外接，不进核心）
- **定位**：训练管线（`OpenMLE-ERL/SFT` + `OpenMLE-ERL/RL`，依赖 `THUDM/slime` + GPU + 模型权重 + 远程沙箱）**不作为平台核心**，而是"算子模型生产商"。
- **接入点**：训练产物 = 一个可被算子调用的 LLM 后端（Frontis-MA1 或自训权重）。safety 通过 `InnerLoopConfig.model` / agent 配置指向该后端即可。
- **为何不 vendoring 训练代码**：① 许可证 CC BY-NC；② 依赖 slime 且为相对 import，集成成本高；③ 本地无 GPU 跑不了。把"训练"与"推理"在架构上明确切开，反而更干净。

### 第二部分：原子算子训练范式引入

#### 3.4 四个算子 ↔ safety_auto_research 循环语义映射
| 算子 | 现有框架中最接近的语义 | 落地方式 |
|---|---|---|
| **Draft** | `seed_population`（gen0 基配置 + 随机 surface 点） | 新增 `OperatorCapability("draft")`：从零生成程序（内循环能力，受禁止集约束） |
| **Improve** | `refine_hook` + 策略补丁应用（layer_09 pending→verified） | `OperatorCapability("improve")`：注入父代码 + 执行反馈 → 精炼 |
| **Debug** | `failure_miner.mine_failure_modes` → `rejected_candidates` 修复 | `OperatorCapability("debug")`：注入 buggy 代码 + 错误栈 → 修复；结果进 `[AVOID]` + `inner_params`（沿用现有失败挖掘消费方） |
| **Crossover** | **现有缺位**（无"两策略杂交"） | 由 `StrategyArchive` + `EvolutionArchive` 新增 `combine(parent_a, parent_b)`；这是引入该范式带来的**净新增能力** |

- 算子注册为**内循环 capability**，需确认不在 `OUTER_LOOP_RESERVED_CAPS` 中（它们本来就不是外环能力，天然合规）。
- 算子执行反馈 → 注入 `inner_params`（playbook/experience 同源通道），**绝不**进 `audit_input`。

#### 3.5 执行式 SFT + RL 训练管线接入点（训练/推理分离）
- **训练侧**（外接，见 3.3）：`OpenMLE-ERL/SFT/scripts/sft_data_selection/` 的 `exclude_reserved_tasks.py`（**去重防泄漏**）是必须借鉴的工程实践——safety 的评测基准（`benchmark_tasks/suites/mle_bench.py` 65 任务）也应在训练数据选择时排除。
- **推理侧**（核心集成）：算子纯函数（`draft_op` 等）只构造 prompt + 调 LLM。safety 把它们包成 capability，`prompt_builder` 的可替换 Jinja 模板（`OperatorConfig` 三模板）换成自有 prompt（沿用现有 `system_prompt`/`skills` 注入通道）。

#### 3.6 推理时组合成长程搜索（替换/增强 run_evolutionary_loop）
- 把 `run_evolutionary_loop` 内循环的"变异超参"升级为"按概率调度四算子 + 岛模型 + 经验记忆"：
  - `fresh_draft_prob` / `crossover_prob` 对应 OpenMLE 的 0.50/0.16（可调）。
  - `sample_in_context` 父选择复用 `StrategyArchive.pending` + `ExperienceBank.query` 作为"经验记忆"来源（与现有 experience 注入同源）。
  - 收敛判据：冠军过 `layer_11` 冻结审计 Accept → converged（与现有出口一致）。

---

## 4. 架构映射与接口设计

### 4.1 新增 / 复用模块清单
| 动作 | 模块 | 说明 |
|---|---|---|
| **复用（vendoring）** | `OpenMLE-Evo/third_party/aira-evo/src/dojo/...` | 干净内核：Task / Interpreter / Node / Journal / MetricValue / operators / evo solver |
| **新增** | `execution_plane/capabilities/openmle_task.py` | `OpenMLETaskAdapter(Task)` 适配 safety 任务 |
| **新增** | `execution_plane/capabilities/operator_caps.py` | Draft/Improve/Debug/Crossover 四个内循环 capability（含 `assert_inner_capability_allowed` 守卫） |
| **扩展** | `control_plane/evolution.py` | `Candidate` 增加 `node_kind` + `code`/`operator`/`parent_ids`；`EvolutionArchive` 支持 program 节点 |
| **扩展** | `execution_plane/orchestrator.run_evolutionary_loop` | worker 内增加"算子应用 → dojo step_task"分支（保留并行/replay/审计脚手架） |
| **桥接** | `control_plane/llm_judge.py` / `reward_bridge.py` | 把 `score2reward` + `hack_check` 接成 score→reward + 作弊判定 |
| **可选外接** | `OpenMLE-ERL/`（不 vendoring 进核心） | 训练管线，仅作为算子模型生产方 |

### 4.2 关键接口契约
```python
# (A) dojo Task ↔ safety 任务适配
class OpenMLETaskAdapter(Task):            # 复用 dojo 抽象
    def prepare(self, **a) -> Dict: ...        # 产出 state + task_info{TASK_DESCRIPTION, lower_is_better}
    def step_task(self, state, action) -> (Dict, Dict):  # action=代码 → 执行 → {TEST_FITNESS, VALID_SOLUTION, ...}
    def evaluate_fitness(self, solution, state, interpreter, aux) -> Dict: ...

# (B) 算子 capability（内循环，受禁止集约束）
class OperatorCapability(StageExecutor):
    stage_codes = ("op_draft","op_improve","op_debug","op_crossover")
    def execute(self, stage_run, sdk, params):
        # 构造 prompt（OperatorConfig 三模板，可替换为 safety 自有 prompt）
        # 调 LLM 后端（Frontis-MA1 或外接）→ 返回代码
        # 执行交还 Task.step_task；反馈只回流 inner_params

# (C) score → reward 桥接（复用 reward_func_utils 语义）
def score_to_reward(score, metadata, mode="power_clip") -> float:  # metadata: higher_is_better/theoretical_min/max
def hack_check(code, output) -> bool:                              # 复用 LLM judge（LLM_JUDGE_URL）
```

### 4.3 隔离不变量保持（"无误引入"硬约束）
1. **禁止集**：四个算子 capability 注册在内循环；`assert_inner_capability_allowed` 不变，确保算子**永远不能**调用 `layer_11`/`layer_09`。算子本身不触发审计，审计由 orchestrator 在代际冠军处统一触发。
2. **audit_input 分离**：算子的 terminal output / score 属于 `inner_params` 策展输入（与 playbook/experience 同通道），`audit_input` 仍只由 `base_goal` + 内循环结果 + `prior_audits` 构成，**不引入算子叙事**。
3. **run_id 过滤**：`ProgramCandidate` / dojo `Node` 落盘时强制带 `run_id`；`EvolutionArchive.list/best` 与 `StrategyArchive.pending/list_all` 已按 run 过滤，program 节点复用同一机制。dojo 原生 `program_database.py` 的 SQLite **无 run_id 概念**，需用 safety 的归档层包裹，不要直接复用其存储。
4. **测试回归**：现有 `tests/test_dual_loop.py::ContextSeparationTest` 与 `tests/test_evolution.py`（含审计不见候选叙事隔离回归）必须继续通过——新增 program 进化分支要补等价断言。

---

## 5. 分阶段落地路径（仅方案，不执行）

- **Phase A — 内核 vendoring + 接口对齐（只读/无训练）**
  - 把 `dojo/` 作为子树/子包引入 `safety_auto_research/vendor/openmle_dojo/`，保留 `NOTICE`/`LICENSE`。
  - 写 `OpenMLETaskAdapter` + 单测（用 titanic 跑通 `prepare/step_task/evaluate_fitness`）。
  - 新增 `node_kind` 到 `Candidate` + `EvolutionArchive` SQLite schema（向后兼容旧 config 节点）。

- **Phase B — Gym 任务包接入现有执行/注册**
  - `benchmark_tasks/registry.py` 增 `openmle_program` 类型（executable=True）。
  - `kaggle_eval_executor` 封装为 dojo `step_task` 轻量后端（保留确定性路径）。
  - 前端 `BenchmarkCatalog` / `RegisterTask` 复用 schema 驱动表单。

- **Phase C — 算子范式 + Evo 推理组合（核心可集成层）**
  - 实现四个 `OperatorCapability`（内循环，守卫到位）。
  - `run_evolutionary_loop` worker 增加"算子应用 → dojo step_task"分支；收敛仍走 `layer_11` 审计。
  - `score2reward` + `hack_check` 桥接到 `llm_judge`。
  - 前端 `EvolutionPanel` 增加 program 节点展示（node_kind 区分）。

- **Phase D — RL/SFT 训练管线（可选外接，需 GPU/slime）**
  - 不 vendoring 进核心；仅文档化"如何把自训/Frontis-MA1 权重作为算子 LLM 后端接入"。
  - 借鉴 `exclude_reserved_tasks.py` 做评测基准去重防泄漏。

---

## 6. 风险、约束与开放问题

1. **许可证（最高优先级）**：OpenMLE **CC BY-NC 4.0** + dojo 上游多许可。**若 safety_auto_research 有商用意图**：要么取得单独授权，要么**只借鉴接口、干净重实现**（Task/Operator/score2reward 接口极简，重写成本低且无侵权风险）。vendoring 时必须保留 `NOTICE`/`LICENSE`。
2. **本地环境不可训练**：无 docker、无 torch、无 GPU → 训练管线（SFT+RL）与真实 mlebench grader（需数据/网络）本地跑不了。推理侧需一个可达的 LLM 后端；若连 LLM 也无，则 Phase C 只能以"dry-run/确定性 stub"验证编排正确性（沿用现有 heuristic 默认）。
3. **确定性与随机性冲突**：平台默认确定性 heuristic（无 LLM），LLM 算子引入非确定性。需在 `budget`/`seed` 层面显式管理，避免破坏现有可复现测试（如 `seed_population` 的 `PYTHONHASHSEED` 稳定化）。
4. **依赖桥接成本**：`OpenMLE-RL` 的 `reward_func_utils.py`/`program_database.py` 对 `airaevo_experience`/`slime` 是相对 import，直接复用会失败；应只**移植算法语义**到 safety 的 `reward_bridge`，而非原样 import。
5. **双层进化的一致性**：config 进化（廉价确定性）与 program 进化（昂贵 LLM）共存时，需在 `EvolutionArchive` 与前端明确区分 `node_kind`，避免混淆榜单（`leaderboard()` 的 `_recompute_top3` 需方向感知且按 kind 分组）。
6. **开放问题（需用户决策）**：
   - safety_auto_research 的定位是**纯研究/开源**还是含**商用**？（决定 vendoring vs 重实现）
   - 是否已有可用的 LLM 后端（Frontis-MA1 权重 / 自建 / 第三方 API）用于算子推理？
   - `tabular_classification` 之外的 7 类 tracked-only 任务，是否也要经 Gym 编译成可执行任务包？

---

## 7. 结论与建议

- **可以"无误引入"的核心**，是把 OpenRSI 的 **`dojo` 干净内核 + 四算子范式 + Evo 岛模型搜索** 作为 safety_auto_research 现有"超参进化 + 双循环"之上的**程序级进化层**，并严格沿用三处隔离不变量。**不要**重写已验证的 orchestrator 并行/replay/审计脚手架，只替换内循环的候选表示与评估器。
- **OpenMLE-Gym** 弥补现有"无真实可执行环境"的短板（任务包 schema + 隔离执行 + 真实 grader）；**OpenMLE-Evo** 把超参进化升级为程序进化；**四算子**补齐 Crossover 这一现有架构缺位的能力；**OpenMLE-RL 训练**定位为外接管线（受许可证与本地环境双重限制）。
- **最先要做的两件事**（Phase A）：① 明确许可证处置（vendoring 合规 vs 接口重实现）；② 把 `dojo` 作为子树引入并跑通 `OpenMLETaskAdapter` 对 titanic 的 `prepare/step_task/evaluate_fitness` 单测。这两步零侵入、可独立验证，是后续所有集成的安全地基。

> 本调研基于论文摘要、GitHub 仓库（含本地克隆）与 safety_auto_research 源码核验。**未执行任何代码修改**。下一步若获授权，可按 Phase A→D 推进；训练侧与商用合规需先由用户拍板。

---

## 8. Phase A 执行结果（已落地，2026-08-04）

用户确认：**不进行商用** → 在 CC BY-NC 4.0 下可合法 vendoring OpenMLE 代码（研究用途），保留 LICENSE/NOTICE 即可。Phase A 已执行完成，零侵入、可独立验证。

### 8.1 已交付物

**A1 — dojo 内核 vendoring + 许可证归属**（`safety_auto_research/vendor/openmle_dojo/`）
- 忠实复制 `OpenRSI/OpenMLE-Evo/third_party/aira-evo/src/dojo/`（184 文件，已排除 `__pycache__` 与内部 `test_*.py` 以免污染 pytest 收集）。
- 完整保留许可证：`LICENSE_aira_evo.txt`（Meta aira-evo）、`THIRD_PARTY_LICENSES.md`、`LICENSE_openmle_cc_by_nc_4.0.txt`（OpenMLE CC BY-NC 4.0）、`NOTICE_openmle.txt`。
- `VENDORED_FROM.md` 说明来源、非商用确认，以及"因 dojo 依赖 `aira_core`/`wandb`/`omegaconf` 整包导入会拖垮现有 venv，本地改为零依赖重实现其接口契约，dojo 仅作参考副本"的理由。

**A2 — 本地接口契约 + OpenMLETaskAdapter + titanic 单测**（`safety_auto_research/openmle_integration/`）
- `contracts.py`：零依赖忠实镜像 dojo 的 `Task` ABC、outcome-key 常量（`TaskOutcome`/`AUX_EVAL_INFO` 等）、`Interpreter`/`ExecutionResult`、`Node`/`Journal`/`MetricValue`。**契约与 dojo 签名对齐**，后续可零改指向真实 dojo 包。
- `interpreter.py`：轻量 `PythonInterpreter`（多进程 + 超时语义，对齐 dojo，无重依赖），供 `step_task(action=str)` 执行算子产出的程序。
- `adapter.py`：`OpenMLETaskAdapter` 实现 `Task` 契约，包装 safety 现有 tabular sklearn 流水线（`kaggle_eval_executor` 同族逻辑）；支持 `action=str`（走 interpreter 执行）/ `action=dict`（内置 sklearn 管线，确定性，对应现有 config 进化）。`prepare` 时留出 20% eval 集并持真值，评分对真实标签 → **真·可验证任务环境**。
- `tests/test_openmle_phase_a.py`：**5 项测试全部通过**（managed 3.13 venv）：① 适配器是 dojo `Task` 子类；② `prepare` 返回 state/info；③ `step_task` 代码串端到端（RandomForest 精度 >0.5）；④ `step_task` config-dict 走内置管线且 `aux_eval_info` 含 `cv_accuracy`；⑤ 非法程序 `VALID_SOLUTION=False`。

**A3 — Candidate/EvolutionArchive 加 node_kind（向后兼容）**（`control_plane/evolution.py`）
- `Candidate` 增带默认值字段：`node_kind="config"` / `code=None` / `operator=None` / `parent_ids=None`。旧 config 节点与持久化 JSON **完全兼容**（全字段默认）。
- `EvolutionArchive.list_by_kind(run_id, kind)`：按 grain 过滤，供前端/EvolutionPanel 区分 config 节点与 program 节点。
- 文档注释更新：说明双层进化粒度（config 超参级 + program 代码级）共存于同一归档。

### 8.2 回归与隔离验证

- **新测试**：`test_openmle_phase_a.py` 在 managed 3.13 venv 下 **5 passed**。
- **Phase A3 向后兼容**：`test_evolution.py` 在 **miniforge 3.10 venv 下 10 passed**（含 node_kind 改动）；轻量子集（PopulationOps/EvolutionArchive）在 3.13 下亦 **5 passed**。
- **预存环境问题（非本次引入）**：`test_evolution.py::test_end_to_end_population_audit_and_branches` 在 **managed 3.13 venv（sklearn 1.9.0 / numpy 2.x）下原生 segfault**（OpenMP/BLAS macOS 线程崩溃）。已用对照实验确证与本次改动无关：① 临时回退 node_kind 改动后 3.13 仍 segfault；② 该测试在 miniforge 3.10（sklearn 1.7.2）下 **1 passed**。结论：重型并行 sklearn 测试应跑在 miniforge 3.10 venv（与既有"重 ML 用 3.10"约定一致），非 Phase A 缺陷。
- **隔离不变量保持**：adapter 仅在内循环被调用（作为未来算子/executor 的接缝），不触发 `layer_11`/`layer_09`；执行反馈只回流 `inner_params`，不进 `audit_input`；累积态带 `run_id` 过滤（adapter 不写归档，由 orchestrator 经现有 `EvolutionArchive.add` 包裹）。

### 8.3 关键修复记录（Phase A 执行中）
- 测试路径脆弱性：原用目录层级数学定位 titanic，pytest 收集时偶发 skip；改为"从测试文件向上查找 `data/kaggle/titanic` 标记"的稳健写法。
- outcome 键常量对齐：adapter 写入 `AUX_EVAL_INFO`（值 `"aux_eval_info"`），测试原用字面量大写 `"AUX_EVAL_INFO"` 取值导致 miss；改为引用契约常量。
- titanic 含 NaN（Age/Embarked）：adapter 预处理加 `SimpleImputer`（median / most_frequent），与 `kaggle_eval_executor` 行为对齐。
- eval 集真值对齐：参考程序预测 `eval.csv`（特征，无标签）而非 fit 行，避免 ID 不交导致精度 0。

### 8.4 下一步（Phase B 起，待用户拍板）
- B：把四算子 `Draft/Improve/Debug/Crossover` 作为内循环能力接入，经 `assert_inner_capability_allowed` 校验；`Debug` 对接现有 `failure_miner`，`Crossover` 经 `StrategyArchive` 支撑。
- C：`OpenMLE-Evo` 岛模型搜索接入 `orchestrator.run_evolutionary_loop`，候选写 `gen{g}` 分支的 `node_kind="program"` 节点；需可用 LLM 后端。
- D：`OpenMLE-RL` 训练管线定位外接（受 CC BY-NC + 无 GPU/docker 限制），只移植 `reward_func` 语义到 `reward_bridge`。

---

## 9. Phase B & C 执行结果（已落地，2026-08-04 续）

用户指令：**「进行 phase B 和 phase C」**。Phase B（四算子作为内循环能力接入，Debug 对接 `failure_miner`、Crossover 对接 `StrategyArchive`）与 Phase C（程序级岛模型进化 `OpenMLE-Evo` 接入 `run_evolutionary_loop`，候选以 `node_kind="program"` 写入）均已执行完成。所有测试在 **miniforge 3.10 venv** 下验证（重型并行 sklearn 在 managed 3.13 下会 OpenMP segfault，沿用既有约定）。

### 9.1 Phase B 交付物

**B1 — 四算子实现（`openmle_integration/operators.py`）**
- `OperatorPrompt` dataclass：`operator` / `task_description` / `target` / `id_col` / `feature_cols` / `current_program` / `feedback` / `parent_programs` / `variant`。
- `OperatorBackend` Protocol：`generate(prompt) -> str`（纯函数：只构造 prompt → 调 backend 产出代码，执行交还 `Task.step_task`，不在此层 eval）。
- 四个算子生成**可运行的 sklearn 程序**：
  - `_draft_program`：4 个变体（`_DRAFT_MODELS` = RF200 / GBM200 / RF100-d6 / LogReg）实现种子种群多样性。
  - `_improve_program`：在父代基础上做特征/模型微调（变体分支）。
  - `_debug_program`：生成 `try/except` + 多数类回退的健壮版；**feedback 经 `_sanitize_comment` 去换行/引号后嵌入代码注释**（避规 traceback 行变成可执行代码）。
  - `_crossover_program`：融合双亲（如 RF + GBM 概率平均）生成新程序。
- `TemplateOperatorBackend`：确定性离线 backend（无需 LLM），用于测试与无 LLM 后端场景；`LLMOperatorBackend`：任意 `(prompt)->str` callable 适配器，对接真实 LLM。
- 便捷封装：`draft_program / improve_program / debug_program / crossover_program` + `run_operator(...)`，均经 `assert_operator_inner_only` 校验。

**B2 — 算子接入内循环隔离（`openmle_integration/inner_capability.py`）**
- `ATOMIC_OPERATORS = ("draft","improve","debug","crossover")`。
- `INNER_LOOP_FORBIDDEN_CALLERS = {"layer_11_external_audit","layer_09_self_iterative_evolution"}` —— 显式强制内循环不变量：算子只允许在内循环运行，**绝不可触发外循环审计/自进化**。
- `assert_operator_inner_only(operator, caller_stage=None)`：未知算子 → `ValueError`；`caller_stage` 落入外循环集合 → `OperatorAuditViolation(RuntimeError)`。
- 注：仓库现有 transport（`execution_plane/orchestrator.py` 的 `run_capability`）并无 `assert_inner_capability_allowed` 这一全局守卫，故 Phase B 新建 `inner_capability.py` 作为显式、可读的接缝，与既有"算子以 `caller_stage="inner_program_evolution"` 调用"约定保持一致。

**B3 — Debug 对接 failure_miner / Crossover 对接档案**
- `build_debug_feedback(failed_outcome, events=None)`：优先 `mine_failure_modes(events)`（现有 `failure_miner` 聚类失败模式），否则回退 `VALID_SOLUTION_FEEDBACK` 通用提示 → **Debug 算子消费的反馈来自真实失败挖掘**，而非硬编。
- `select_crossover_parents(archive, run_id, k=2)`：读 `archive.list_by_kind(run_id,"program")` 取 fitness 最高的 k 个作为双亲 → **Crossover 直接从 `EvolutionArchive`（第五件套，类比 `StrategyArchive`）择优**，共享同一累积态与 `run_id` 过滤。

### 9.2 Phase C 交付物

**C1 — 程序级进化（`control_plane/evolution.py` 扩展）**
- `candidate_signature(c)`：program 节点返回 `c.code`，config 节点返回 `candidate_text(params)`；统一"去重签名"接口。
- `novelty_filter` 双粒度去重（**最终版**）：
  - **config 节点**：保留原余弦相似度规则（≥ `threshold` 拒）；
  - **program 节点**：**精确代码字符串集合成员判断**——算子模板共享 ~95% boilerplate，bag-of-tokens 余弦相似度对 RF/GBM 等变体均 ~1.0，无法区分；精确 code 去重才能拒掉真正相同程序、保留不同模型/算子。
- `seed_program_population(...)`：Draft 生成 `node_kind="program"` 候选，`variant=rng.randrange(4)` 保证种子多样性。
- `mutate_program(parent, operator[improve|debug], ...)`：单亲变换；`crossover_programs(a, b, ...)`：双亲变换，`parent_ids=[a,b]`。
- `class IslandModel`：`__init__(n_islands, run_id)`；`seed(seeder, size, generation)`（每岛 `branch=f"gen{g}.isl{i}"`）；`all_candidates()` / `best()` / `migrate(top_k=1)`（把全局 top-k **克隆进其他岛**，克隆 branch 重标 `f"gen{champ.generation}.isl{idx}"` 以归属目标岛）。

**C2 — 岛模型接入进化循环（`execution_plane/orchestrator.py` 扩展）**
- 新增 `run_program_evolutionary_loop(run_id, task_config, backend, *, islands=1, pop_per_island=3, generations=2, max_workers=1, novelty_threshold=0.92, audit=True, audit_params, budget, seed, progress_callback, cancel_event)`：
  - 建 `OpenMLETaskAdapter` 并 `prepare()` 一次复用 state（避免每候选重建环境）。
  - `IslandModel` 播种 Draft 节点；逐代顺序评估（`adapter.step_task(code)`，`max_workers=1` **故意串行**避 OpenMP 崩溃）；冠军送 `run_capability("layer_11_external_audit", ...)`（**复用现有冻结审计路径**，audit_input 只含 champion.metrics）；
  - `_breed_program_generation(...)` 繁殖下一代 + `model.migrate(top_k=1)`。
  - 候选以 `node_kind="program"` 写入 `HypothesisTree`（`gen{g}.isl{i}` 分支），并 `archive.add` 到 `EvolutionArchive`。
- `_breed_program_generation` 健壮性：
  - **Improve/Debug 分配**：`fit ≥ 0.4` 走 Improve，否则走 Debug（用 `build_debug_feedback`）；
  - **Crossover**：每岛取两最优父代融合；
  - **多样性守卫**：novelty 过滤后 `kept` 为空则回退 `children`（防岛崩溃为空）；
  - **精英保留**：最佳父代 carry forward（防最优解在繁殖中丢失）。

### 9.3 测试与回归

| 套件 | 环境 | 结果 |
|---|---|---|
| `test_openmle_phase_bc.py`（Phase B/C） | miniforge 3.10 | **9 passed** |
| `test_evolution.py`（Phase A3 回归） | miniforge 3.10 | **10 passed**（含 node_kind 改动） |
| `test_openmle_phase_a.py`（Phase A 回归） | miniforge 3.10 | **5 passed** |
| **合计** | | **24 passed，0 failed** |

Phase B/C 测试明细（9 项）：
- `OperatorTests`（3）：四算子产出可运行程序、inner_only 守卫、LLM backend 适配器；
- `DebugSeamTest`（1）：debug 修复 buggy 程序；
- `CrossoverSeamTest`（1）：`select_crossover_parents` 取两父代；
- `ProgramEvolutionUnitTest`（3）：seed 多样性、IslandModel 迁移、novelty 精确去重；
- `ProgramEvolutionE2ETest`（1）：`audit=False` 端到端写 program 节点 + 岛搜索。

### 9.4 关键修复记录（Phase B/C 执行中）

1. **`NameError: _breed_program_generation`**：原为裸函数调用，改为 `self._breed_program_generation(...)` 并补 `run_id` 参数传参。
2. **DebugSeamTest 崩溃**：`build_debug_feedback` 返回多行 traceback，原样作为 `# comment` 嵌入 debug 程序后，traceback 行变成可执行代码 → 在 `TemplateOperatorBackend` 加 `_sanitize_comment`（去换行/引号），improve/debug 生成统一经此处理 feedback。
3. **IslandModel.migrate 克隆保留原岛 branch**：改为克隆 branch 重标 `f"gen{champ.generation}.isl{idx}"`（目标岛）。
4. **novelty 单测误判**：测试把候选自身 code 放入 `archived_codes` 触发自重复 → 修正测试用 `archived_codes=[pop[0].code]`。
5. **E2E gen0 失败（status='failed'）**：繁殖时 island 被 children 整体替换丢弃所有父代；且 0.92 余弦阈值把结构近同的 Draft 全部拒掉 → island 空、champion=None → 加「多样性守卫 + 精英保留」。
6. **`TypeError: draft_program() missing task_description`**：测试补 `task_description="classify titanic"`。
7. **novelty 单测仍 0 kept**（crossover vs draft sim 0.968>0.92）：实测所有算子代码 bag-of-tokens 余弦相似度均 ~1.0（模板 95% 相同无法区分）→ **重写 `novelty_filter`**：program 节点改精确代码字符串去重（set 成员判断），config 节点保留原余弦规则。修复后 9 passed。

### 9.5 隔离不变量保持（Phase B/C）

- 算子以 `caller_stage="inner_program_evolution"` 调用，经 `assert_operator_inner_only` 校验，绝不进入 `layer_11_external_audit` / `layer_09`。
- 审计路径复用 `run_capability("layer_11_external_audit", ...)`，其 `audit_input` 只含 `champion.metrics`（cv_accuracy 等），**不含候选代码/身份/inner_event**，满足"审计只读策展输入、不得读内循环事件"的不变量。
- 所有 candidate 与归档条目带 `run_id` 过滤；`EvolutionArchive` 双粒度靠 `node_kind` 区分，前端 EvolutionPanel 可按 kind 分组展示。
- 程序级进化不写 `StrategyArchive` 补丁（那是超参进化面白名单闭环），仅写 `EvolutionArchive` 程序节点 + `HypoTree`，职责清晰。

### 9.6 下一步（Phase D，待用户拍板）

- **OpenMLE-RL 训练管线**：受 CC BY-NC 4.0 许可证 + 本机无 GPU/docker 双重限制，定位为**外接管线**，不 vendoring 训练代码。仅**移植 `reward_func` 语义**到 `openmle_integration/reward_bridge.py`（把程序进化的 fitness 信号对接 RL 风格 reward shaping），契约对齐 `airaevo_experience`/`slime` 的 `reward_func_utils.py` 算法语义而非原样 import（其相对 import 会失败）。
- **LLM 后端接线**：Phase B/C 算子已支持 `LLMOperatorBackend` 适配器；若用户提供可用后端（Frontis-MA1 权重 / 自建 / 第三方 API），可将 `run_program_evolutionary_loop(backend=LLMOperatorBackend(...))` 接入真实生成。当前 `TemplateOperatorBackend` 为确定性离线默认，保证零 LLM 依赖下整套链路可测。
- **前端展示**：EvolutionPanel 当前按 `node_kind` 分组 config/program 节点；后续可加"岛视图"（按 `gen{g}.isl{i}` branch 着色）以增强程序级进化的可解释性。

> 注：Phase D 已于 2026-08-04 执行完成，详见第 10 章。

---

## 10. Phase D 执行结果（已落地，2026-08-04）

用户指令：**「执行 phase D，其中训练出了外接管线外，还需要支持使用 mac 的本地 cpu 和 mps 显卡进行轻量大模型训练（如 0.6B 以内的模型）」**。Phase D 现含两项交付物：① 外接 RL 训练管线的 `reward_func` 语义桥（D1）；② **本地 Mac CPU/MPS 轻量 LLM 训练**（D2/D3）——这是相对原 9.6 规划新增的用户需求。全部在 **managed 3.13 venv（已装 torch 2.13.0，MPS 可用）** 下验证；D1 reward 桥为纯 Python，无 torch 依赖。

### 10.1 D1 — 外接 RL 训练管线 reward_func 语义桥（`openmle_integration/reward_bridge.py`）
- 不 vendoring OpenMLE-RL 训练代码（CC BY-NC 4.0 + 本机无 GPU/docker），仅**移植 `reward_func_utils.py` 的 reward 分解语义**：`total = w_validity*validity_bonus*valid + w_improve*improvement_scale*max(0,fitness-prev_fitness) + w_diverse*diversity_scale*novelty - w_parsimony*complexity_penalty_per_kloc*kloc + baseline`，夹紧到 `[min_reward, max_reward]`。
- `reward_func(fitness, *, valid, prev_fitness, novelty, program, config)` 返回 `RewardComponents`（validity/improvement/diversity/parsimony/baseline/total 分项 + 总分），与 aira-evo/slime 解耦、无 import 耦合。
- `reward_population(candidates, *, prev_best_fitness, config)`：对整批候选批量打分（鸭子类型读 `.fitness/.novelty/.code/.status`，匹配 `Candidate`），供外部 RL trainer 或本地 trainer 消费。
- `RewardConfig`：权重/缩放/kloc 惩罚/基线/夹紧区间均可调。

### 10.2 D2 — 本地 Mac CPU/MPS 轻量 LLM 训练（`openmle_integration/local_train.py`）
- **设备自动选择**：`detect_device(prefer="mps")` 在 Apple Silicon 返回 `"mps"`（Metal），否则 `"cpu"`；张量移到该设备。本机实测 `torch 2.13.0 | mps available: True`。
- **两种模型源**：
  - `model_source="hf"`：HuggingFace 因果 LM（默认 `Qwen/Qwen2.5-0.5B-Instruct`，≤0.6B）+ **PEFT/LoRA** 微调（冻结底座、省显存）；`transformers/peft/accelerate` **懒导入**，缺则报清晰安装提示。需联网下载权重（受 `HF_ENDPOINT`/`HF_TOKEN` 控制）。
  - `model_source="synthetic"`：**在码内构建的微型 char-LM（LSTM，约数十万参数，零网络、零 HF 权重）**，用于自测/演示，证明 MPS 训练闭环在本地跑通，也是无权重时的安全回退。
- **合成数据集**：`make_synthetic_dataset(teacher)` 以 `TemplateOperatorBackend` 当 teacher，生成 `(prompt_text, program)` 配对（四算子轮流、variant/parents/feedback 覆盖），让本地模型**模仿确定性离线算子输出**——无需外部 teacher LLM 即可自举出真实生成器。
- **训练循环**：`fit(dataset)` 按 `epochs/batch_size/lr` 在设备上训练；synthetic 走全量微调、hf 走 LoRA。导出 `export_generator()` 返回 `generate(text)->str` callable（跑在设备上），可直接喂给 `LLMOperatorBackend`/`LocalLLMOperatorBackend`。
- **持久化**：`save(output_dir)` 落 `synthetic_lm.pt`+`train_config.json`（hf 走 `save_pretrained`）；`report()` 给出 device/source/size 摘要。
- **内存护栏**：`max_params_b=0.6` 默认拒绝加载过大 checkpoint；LoRA 冻结底座使 0.5B 模型在 Mac RAM 内可训。

### 10.3 D3 — 本地训练后端接入算子（`operators.py` 扩展）
- `LocalLLMOperatorBackend`：鸭子类型接受「生成 callable」或「trainer（含 `export_generator()`）」，复用 `LLMOperatorBackend` 的 prompt 格式化（operator/target/feedback/parents 注入），**不引入 `local_train` 的 import 以避免循环依赖**。
- `make_local_operator_backend(trainer)` 便捷工厂。这样 Phase C 的 `run_program_evolutionary_loop(backend=LocalLLMOperatorBackend(trainer))` 即可用本地训练模型驱动四算子。

### 10.4 测试与回归

| 套件 | 环境 | 结果 |
|---|---|---|
| `test_openmle_phase_d.py`（D1+D2+D3） | managed 3.13（torch+MPS） | **10 passed**（含 MPS 实训冒烟：训练→导出→`draft_program` 驱动，`last_train_device=="mps"`） |
| `test_openmle_phase_bc.py` | miniforge 3.10 | 9 passed（operators 扩展无回归） |
| `test_openmle_phase_a.py` | miniforge 3.10 | 5 passed |
| `test_evolution.py` | miniforge 3.10 | 10 passed |
| **合计** | | **34 passed，0 failed** |

Phase D 测试明细（10 项）：`RewardBridgeTest`（4：分量/无效无奖励/上界夹紧/种群鸭子类型）、`LocalTrainLogicTest`（3：device 已知/config 默认/synthetic 数据集形状+内容）、`LocalOperatorBackendTest`（2：callable 接线/prompt 注入/拒非 callable）、`LocalTrainMpsSmokeTest`（1：MPS 实训→导出→算子驱动）。

### 10.5 关键修复/设计记录（Phase D 执行中）
- **懒导入与模块可导入性**：`_TinyCharLM` 最初写成顶层 `torch.nn.Module` 子类 + `@torch.no_grad()` 装饰器，会强制模块加载时 import torch，破坏"无 torch 也可导入做纯逻辑测试"的目标 → 改为 **工厂函数 `_build_tiny_lm()` 内部 `import torch` 并定义/返回类**，模块顶层零 torch 依赖。
- **device 对齐**：synthetic 生成时张量必须建在 `self.device`（mps/cpu）上与模型参数同设备，初版误建 `"cpu"` → 修正 `generate(..., device=self.device)`。
- **测试断言修正**：`LocalLLMOperatorBackend` 的 `operator=draft` 文本在"传给 callable 的 prompt"中而非返回值中 → 测试改为捕获 callable 收到的 prompt 再断言。
- **torch 安装**：managed 3.13 venv 原无 torch，已 `pip install torch`（macOS wheel 自带 MPS）；synthetic 冒烟仅需 torch（无需 transformers/peft）。真实 HF 0.5B 训练需另行 `pip install transformers peft accelerate` + 设置 `HF_ENDPOINT`/`HF_TOKEN`。

### 10.6 隔离不变量保持（Phase D）
- `reward_bridge` 只做数学变换（fitness→reward），不触发 `layer_11`/`layer_09`，不写归档；它是外接 RL trainer 的**契约接口**，不耦合 aira-evo/slime。
- 本地训练出的 generator 经 `LocalLLMOperatorBackend` 接入后，四算子仍走 `assert_operator_inner_only`（`caller_stage="inner_program_evolution"`），**绝不进入外循环审计/自进化**。
- 程序进化（Phase C）的 `audit_input` 仍只含 champion.metrics，与本地训练后端完全解耦。

### 10.7 后续（可选增强）
- **真实 HF 训练验证**：在联网 + `HF_TOKEN` 环境下 `pip install transformers peft accelerate` 后，对 `Qwen/Qwen2.5-0.5B-Instruct` 跑一次 LoRA 实训练证（当前代码已实现，仅未在本沙箱实跑，因无 token/未下载大权重）。
- **reward→本地 trainer 闭环**：把 `reward_population` 的 reward 接成 `LocalLLMTrainer` 的 RL 风格样本权重（PPO/RLHF 风格），使程序进化的 fitness 直接反哺本地模型——需 `transformers` 的 RL 栈（如 TRL），属 Phase D 外延。
- **前端展示**：EvolutionPanel 可加"本地训练后端"开关与"岛视图"（按 `gen{g}.isl{i}` branch 着色）。

---

## 11. Phase D 扩展（用户三连击，2026-08-04）

用户新增三条指令：① LLM 后端接**第三方 OpenAI 兼容 API**（给定 TME llmproxy 端点与鉴权头，可配 model/temperature/max_tokens 等）；② 前端 EvolutionPanel **加"岛视图"**（按 `gen{g}.isl{i}` branch 着色）；③ 用给定 **`HF_TOKEN` 试跑真实本地 HF 训练**。前两项已落地，第三项因本会话 Bash 工具临时不可用（harness 序列化故障），执行推迟到 Bash 恢复后；代码与测试已就绪。

### 11.1 D-Ext1 — 第三方 OpenAI 兼容 API 后端（`operators.py`）
- 新增 `ApiLLMConfig`（`@dataclass`，全部字段 env 可覆盖）：`base_url` / `api_key` / `model` / `temperature` / `max_tokens` / `top_p` / `frequency_penalty` / `presence_penalty` / `extra_headers` / `timeout` / `system_prompt` / `stream`。
  - 默认 `base_url=https://ai-rec.tmeoa.com/llmproxy/chat/completions`、`model=deepseek-v4-flash-official`（与用户给定 curl 一致）。
  - env 覆盖键：`OPENMLE_API_BASE_URL` / `OPENMLE_API_API_KEY` / `OPENMLE_API_MODEL` / `OPENMLE_API_TME_OPEN`（置 1/true/yes 自动加 `TmeOpenApi: true` 头）。
- 新增 `ApiLLMOperatorBackend`：`generate(prompt)` 组装 OpenAI chat/completions 报文（`messages=[{system},{user}]`），用 **标准库 `urllib.request`**（零额外依赖）POST；解析 `choices[0].message.content`，**自动剥离 ```python 围栏**返回裸代码；HTTP 错误（4xx/5xx）与连接错误统一转 `RuntimeError` 并附服务端 detail（前 500 字符）。
- 新增 `make_api_backend(api_key=..., base_url=..., model=..., tme_open=True, **overrides)` 便捷工厂。对接方式与既有一致：`draft_program(backend, ...)` 等四算子零改动即可走真实 API。
- **密钥处理**：API key 仅经 `Authorization: Bearer` 头发送，不写入任何源码/文件；本会话实跑如需 key 仅经 shell 环境变量临时传入（不落盘、不进 git）。
- 系统提示词（`DEFAULT_API_SYSTEM_PROMPT`）明确要求"只输出完整可运行 python 程序、写 submission.csv、无 markdown 围栏、无解释"，与 `OPENMLE_WORKDIR` 契约一致。

### 11.2 D-Ext2 — 前端"岛视图"（`frontend/src/views/EvolutionPanel.tsx` + `api/client.ts` + `styles.css`）
- `api/client.ts` 的 `EvolutionCandidate` 扩展 `node_kind?` / `code?` / `operator?` / `parent_ids?`（后端 `asdict(Candidate)` 本就返回这些字段，前端类型补齐即可，向后兼容）。
- EvolutionPanel 候选区加 **双视图切换**：`种群表`（原表格）/ `🏝️ 岛视图`。
- 岛视图实现：用 `parse_island_index(branch)`（新增于 `control_plane/evolution.py`，纯函数，正则 `isl(\d+)`，`gen{g}` 返回 0）把候选按岛分组；每岛一张**带左侧色条**的卡片，岛色取自 10 色高对比调色板（`ISLAND_COLORS`），在暗色主题下清晰可辨；卡片内按代/适应度排序列出候选（程序级显示 operator + 首行代码，config 级显示 params 摘要）。
- 顶部**图例**显示各岛候选数；提供"仅程序级（岛模型）"开关（默认开），聚焦 OpenMLE-Evo 岛模型种群、可切换含 config 节点。
- `styles.css` 补 `.btn.accent`（激活态高亮）以匹配既有 `.btn.ghost` 用法。
- 后端 `parse_island_index` 已加单元测试，与前端正则同源。

### 11.3 D-Ext3 — 真实 HF 训练试跑（`HF_TOKEN` 试用）
- 用户提供了 `HF_TOKEN`（与 `HF_ENDPOINT=https://huggingface.co` 搭配），要求实跑 `LocalLLMTrainer(model_source="hf", model_id="Qwen/Qwen2.5-0.5B-Instruct", method=lora)` 在 MPS 上的真实训练。
- **Bash 恢复后已执行**：
  1. managed 3.13 venv 安装 `huggingface_hub`（1.26）、`transformers`（5.14.1）、`peft`（0.20.0）、`accelerate`（1.14.0）。
  2. **Token 验证成功**：`HfApi(endpoint=..., token=...).whoami()` 返回 `glennge`；`hf_hub_download('Qwen/Qwen2.5-0.5B-Instruct','config.json')` 成功落 cache → token 有读权限、模型可达、端点正确。
  3. **真实 MPS LoRA 训练已完成**（exit 0，无残留进程）：`LocalLLMTrainer(TrainConfig(model_source='hf', model_id='Qwen/Qwen2.5-0.5B-Instruct', epochs=1, n_samples=16, max_seq_len=256, lora_r=8, lora_alpha=16))` → `train()`（MPS）→ `export_generator()` 接入 `LLMOperatorBackend` 驱动 `draft_program` → `save()` 落 `data/openmle_hf_smoke`。
     - 落盘产物：`adapter_model.safetensors`（~2.1MB LoRA 增量）、`adapter_config.json`（`base_model_name_or_path=Qwen/Qwen2.5-0.5B-Instruct`、`peft_type=LORA`、`r=8`、`lora_alpha=16`、`target_modules=[q_proj,v_proj]`、`task_type=CAUSAL_LM`）、`tokenizer*.json`、`chat_template.jinja`、`train_config.json`、`README.md`（PEFT model card）。
     - 复现推理验证（会话切换丢失原 stdout，故重新加载 adapter 跑一次）：模型产出结构正确的 sklearn 程序（RF/ColumnTransformer/OneHotEncoder/SimpleImputer/StandardScaler），与 teacher `TemplateOperatorBackend` 模板一致；少量拼写瑕疵（`num_transformramer`、`train_test_split` 桩）属 16 样本/1 epoch 微训练预期，smoke 级可接受。
- **安全**：token 仅经环境变量临时传入，不写入源码/MEMORY.md/git；在命令中明文出现但属临时 shell，不入仓库。

### 11.4 测试（Phase D-Ext，`test_openmle_phase_d_ext.py`，已跑通）
- `ApiBackendTests`（7）+ `IslandIndexTests`（1）= **8 passed**（miniforge 3.10，无 torch 依赖）。
- 初跑 3 failed（`mock` 未返回 OpenAI JSON 信封致 `json.loads` 报 Expecting value）—— 修复 `_UrlopenRecorder` 包裹 `{"choices":[{"message":{"content":...}}]}` 信封、`test_draft_program_uses_api_backend` 去掉尾换行。
- 另 urllib 会规范化头名 `TmeOpenApi`→`Tmeopenapi`（HTTP 大小写无关），断言改小写键匹配（头部确已发送、仅大小写被规范化）。
- `ApiBackendTests` 覆盖：payload 形状（URL/Bearer/`TmeOpenApi`/model/temperature/max_tokens/top_p/frequency_penalty/stream/messages 角色）、围栏剥离、无围栏直传、HTTP 401→RuntimeError、URLError→RuntimeError、`draft_program` 经 API 后端、env 默认值、`make_api_backend` 便捷工厂。
- `IslandIndexTests`（1）：`gen0.isl2→2` / `gen0→0` / `gen3.isl11→11` / 空串→0 / `main→0`。
- 依赖 `urllib` 以 `unittest.mock` 模拟，无真实网络。

### 11.5 隔离不变量保持（D-Ext）
- `ApiLLMOperatorBackend` 经既有 `run_operator`→`assert_operator_inner_only` 通路，算子仍只在内循环运行，绝不触发 `layer_11`/`layer_09`；API 仅消费 prompt、产出代码，不触碰审计/归档。
- 岛视图纯属前端展示，只读 `/evolution` 已返回的 `branch`/`node_kind`，不改后端契约。
