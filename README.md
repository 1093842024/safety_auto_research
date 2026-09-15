# safety_auto_research · 安全自动化研究平台

> 一个面向 **AI 安全研发** 的自动化研究平台：把多个开源 AI Auto-Research 项目的核心资产统一沉淀为「基础设施层」，并以「双循环」架构驱动 **可配置、可审计、可复现、越用越强** 的自进化研究闭环，配套研究人员友好的控制面板。

---

## 1. 项目目标

- **统一基础设施**：基于 10 个开源 AI Auto-Research 项目的深度分析，把文献检索、idea 生成、评估基准、实验设计、数据清洗、代码开发、实验执行、结果分析、自迭代进化、对抗数据生成这十个环节沉淀为可复用资产（`infrastructure/` 下的 skill / agent / 数据集 / 评测）。
- **统一契约与闭环**：用一套共享的平台契约（对象模型 + 事件模型 + 状态机）串起「控制平面 + 执行平面」，让研究流程成为可审计的状态流转，而非一次性脚本。
- **双循环自进化**：内循环做具体研究（评测 / 训练 / agent 编排），外循环做约束审计与递归改进，元循环做冻结式校验与回滚，形成「越用越强」的闭环。
- **研究人员友好的控制台**：提供 Web 控制面板，支持按类别组织历史记录、折叠/展开、时间排序，并对内循环参数做深度可视化配置。

设计理念（最小支点重构）：**平台不替 agent 决定"怎么做"**——控制平面 + 平台契约是 agent 的运行时与记忆，`PlatformSDK` 是工具面，`StageExecutor` 是兜底实现，权威决策者由 `AgentHarness` 担任。agent 模式**必须接入真实远程 Agent（RemoteAgentHarness）**才能执行；未接入时启动会**明确失败并终止**（创建 FAILED 状态的 run 并写明原因），**不会静默回退**到脚本化执行。

---

## 2. 核心架构

### 2.1 双循环（本平台的主干）

```
                 ┌──────────────────────── 外循环 (Outer Loop) ────────────────────────┐
                 │  layer_11 外部审计(constraint-wise) → Accept / Refine / Restart        │
                 │        │ Refine 时携带 frozen verifier，改变内循环参数                    │
                 │        ▼                                                                  │
  ┌──────────────┐   ┌──────────────────── 元循环 (Meta Loop) ─────────────────────┐     │
  │  内循环       │   │  layer_09 自迭代进化：冻结校验器 + 回滚 + 经验沉淀            │     │
  │ (Inner Loop) │◄──┤        ▼                                                    │     │
  │  kaggle_eval │   │  假设树 / 经验库 / 策略归档 (ResearchStateStore, SQLite)      │     │
  │  / agent     │   └─────────────────────────────────────────────────────────────┘     │
  └──────┬───────┘                                                                       │
         │  EvalCompletedEvent / AttackCompletedEvent / LessonPromotedEvent               │
         └───────────────────────────────────────────────────────────────────────────────┘
```

- **评分标准前置（`layer_12_rubric_induction`）**：内循环开始前，先由控制面确立**任务专属的可执行评分标准**并冻结（详见 §2.5）。未提供标准的任务**自动生成**，已提供标准的任务**自动审查**其准确性/完整性/科学性。同一份标准既作为内循环的**只读执行契约**，又作为外循环的**逐条判据**。
- **内循环**：具体研究执行，可由脚本化 `kaggle_eval` executor 或自主 agent（`mode="agent"`）驱动。
- **外循环**：`layer_11_external_audit` 对研究产出做约束审计（含 held-out 泛化一致性检查），给出 Accept / Refine / Restart 裁决；Refine 会真正改变内循环参数（否则重复同轨迹）；连续 REFINE 无进展的轨迹会被 RESTART 抛弃。
- **元循环（层⑨）**：`layer_09_self_iterative_evolution` 在**可编辑面白名单**内提出具体参数补丁，携带**可证伪预测**；orchestrator 应用补丁到下一轮内循环并**事后验证**——兑现标 `verified`、回归超容差则**真回滚**参数并标 `rolled_back`。

### 2.2 Harness 工程（对照 Weng《Harness Engineering for Self-Improvement》的三期升级）

| 机制 | 模块 | 说明 |
|------|------|------|
| **ACE 式 Playbook** | `control_plane/playbook.py` | 条目化研究上下文（策略/避坑/事实），确定性去重合并 + helpful/harmful 计数，跨 run 学习、只注入内循环（绝不进审计输入） |
| **失败模式挖掘** | `control_plane/failure_miner.py` | 事件日志聚类反复出现的失败模式；被拒方向（rejected_candidates）回流内循环，不再重复试错 |
| **策略生命周期** | `control_plane/store_tree.py` | StrategyArchive：`pending_verification → verified / rolled_back / expired_*`，全部带 run_id |
| **held-out 评估** | `capabilities/kaggle_eval_executor.py` | 15% 分层 held-out 一次性打分（CV 喂内循环，gate 冻结）；审计 `heldout_consistency` 约束抓过拟合 |
| **真实 LLM judge** | `control_plane/llm_judge.py` | `LLM_JUDGE_URL` 协议（score/rationale/evidence_refs），失败确定性回退 heuristic |
| **经验生命周期** | `store_tree.py::ExperienceBank` | 每轮自动蒸馏成功/失败经验；去重合并（uses/conf 强化）+ `conf×0.97^staleness` 衰减；dispatch 自动注入 top-3 |
| **多维预算** | `orchestrator.py` | `budget={max_seconds, max_capability_calls, max_cost}` 任一超额硬停 `exited_budget`；计数在 `run_capability` 本体，agent 调用无法绕过 |
| **并行进化搜索** | `control_plane/evolution.py` + `task_manager.py` | 种群式候选（适应度比例父代选择 ÷(1+后代数)、blake2b 稳定哈希新颖性拒绝 ≥0.92）；线程池并行评估（结果 JSON 落盘可断点恢复），候选写入 gen{g} 分支假设树，每代冠军过冻结审计 |
| **任务专属评分标准** | `rubric/` + `layer_12_rubric_induction` | 内循环前**生成 / 审查**任务专属可执行标准并冻结；7 条 criterion 中 6 条**程序化判定**；外审计**逐条**判定而非单一总分；硬失败**一票否决**（详见 §2.5） |

### 2.3 分层模块

| 层 | 目录 | 职责 |
|----|------|------|
| **平台契约层** | `platform_contracts/` | **21 对象模型 / 16 事件模型** / 22 枚举 + 状态机校验（`transitions.py`）+ JSON Schema / TypeScript 导出 |
| **控制平面** | `control_plane/` | `ControlPlaneService`（状态机服务）+ `Repository`（持久化仓储）+ `task_state.py`（MEA 任务态）+ FastAPI 应用（`api.py` 薄装配器 + `routers/` 10 个域路由），暴露工作流 / 阶段 / 审批 / 决策 / 事件 / 双循环 / 进化 / MEA / **评分标准** 端点 |
| **执行平面** | `execution_plane/` | `StageExecutor`/`PlatformSDK`/`ClosedLoopOrchestrator`/`AgentHarness`/`capabilities/`（**21 个能力**）；新增 `mea.py`（MEA 主控）+ `agents/`（RoleAgent 注册 / 适配器） |
| **评分标准层** | `rubric/` | `spec`（任务归一化）/ `review`（三维审查）/ `synthesize`（criterion 合成）/ `checks`（程序化判定器）/ `engine`（编排 + LLM 可选） |
| **OpenRSI / OpenMLE 集成层** | `openmle_integration/` | OpenRSI 程序级岛模型集成（Phase A–D）：`contracts`(dojo 镜像) / `operators`(四算子) / `adapter` / `interpreter` / `inner_capability`(算子护栏) / `reward_bridge` / `local_train`；参考副本 `vendor/openmle_dojo/` |
| **基础设施层** | `infrastructure/` | 十个研究环节的可复用资产（skill / agent / 数据集任务 / 评测），详见 `infrastructure/README.md` |
| **前端** | `frontend/` | React 18 + Vite + TypeScript 研究人员控制面板（侧边栏分类画廊 + 新建向导 + run 仪表盘 + 进化岛视图） |
| **持久化** | `data/` | `control_plane_store.json` + `research_state.db`（假设树/经验库/策略归档/**进化种群 config+program 双粒度**） |

---

### 2.4 最新演进：OpenRSI/OpenMLE 集成 + MEA 控制循环（2026-08-04 ~ 08-05）

平台在双循环主干之外，于 2026-08-04 接入 **OpenRSI / OpenMLE**（源自 Frontis-MA1 的"AI 改进 AI"递归自改进框架），并于 2026-08-05 落地 **MEA（Manage-Execute-Audit）控制循环**（源自 LongHorizon-Harness）。两者均为"越用越强"闭环的能力扩展，且都受同一套隔离不变量约束。

**OpenRSI / OpenMLE（程序级进化）— 四阶段落地：**

- **Phase A · 契约**：`openmle_integration/contracts.py` 零依赖镜像 dojo 的 `Task`/`Interpreter`/`MetricValue`/`Node`/`Journal`（与上游签名对齐，未来可零改指向真包）；`vendor/openmle_dojo/` 仅作参考副本（因整包 import 会拖垮 venv，本地用契约重实现）。
- **Phase B · 四原子算子**：`operators.py` 的 `draft_program` / `improve_program` / `debug_program` / `crossover_program` 为**纯函数**（只构造 prompt 调 LLM 产出代码，不执行）；由 `run_operator` 统一分发，每次必经 `assert_operator_inner_only`（白名单 `INNER_LOOP_ALLOWED_CALLERS`，fail-closed）。离线 `TemplateOperatorBackend` 确定性拼装 sklearn 程序，真实 LLM 时走 `LLMOperatorBackend` / `ApiLLMOperatorBackend`（零依赖 `urllib` 对接第三方 API）。
- **Phase C · 程序级岛模型**：`control_plane/evolution.py::IslandModel`（`seed`/`best`/`migrate` 跨岛迁移，branch `gen{g}.isl{i}`）+ `orchestrator.run_program_evolutionary_loop`（:1451）+ `_breed_program_generation`（:1711）。与 config 进化（`run_evolutionary_loop` 扁平种群）并存于 `EvolutionArchive`，靠 `Candidate.node_kind`（config/program）区分、双粒度新颖性过滤（config 余弦≥0.92 拒；program 精确代码字符串 set 去重）。
- **Phase D · 本地训练 + 奖励桥**：`reward_bridge.py`（`reward_func`/`reward_population`，validity/improvement/diversity/parsimony）把 fitness 翻成 RL 风格 reward；`local_train.py`（`LocalLLMTrainer`，`detect_device` MPS/CPU，≤0.6B LoRA）在 Mac 上训练轻量 generator 反哺算子后端；`ApiLLMOperatorBackend` 对接第三方 OpenAI 兼容 API。

> ✅ **接线状态（2026-08-07 修复 缺陷7）**：程序级进化已正式接入 REST —— `POST /workflow-runs/{run_id}/program-evolution`（后台化驱动 `run_program_evolutionary_loop`，沿用 缺陷2 的取消/终态/record 收尾模式）+ `GET` 同路径（`node_kind="program"` 候选 + 岛视图谱系）。`backend_type` 支持 `template`（默认，离线确定性）/ `llm`（OpenAI 兼容 API）；`task_config` 缺 `data_dir` 时回落到内置 titanic preset。孤儿路径终结：前端 `EvolutionPanel` 已接入 **「🚀 启动程序进化」按钮**（可配 islands/pop_per_island/generations/max_workers/新颖度阈值/后端类型/审计，后台执行并轮询结果落入十色 `gen{g}.isl{i}` 岛视图，带「停止」取消），`tsc --noEmit` 0 errors、`vite build` 通过。

**MEA（Manage-Execute-Audit）控制循环：**

- **三角色**：`Manager`（持持久 `TaskState`，产出有界子任务契约 `c_i`：goal+acceptance+boundary+prior-evidence，决策 `{execute,done,blocked,ask}`）/ `Executor`（唯一可改环境，fresh、budget-bounded context 只做当前子任务）/ `Auditor`（**只读**独立检查，产出 completion/integrity/state-update 三类 findings）。
- **核心杠杆**：`Auditor` 必须与 `Executor` **异模型 / 异后端**（`RoleAgentRegistry.require_different_from` 硬约束），从架构层消除自确认。评测角色（R5）强制 `DeterministicAdapter`（无 LLM），审计角色（R6）强制异模型。
- **落地**：`execution_plane/mea.py`（`run_mea_loop_core` + `MeaMetrics`）+ `orchestrator.run_mea_loop`（:321）+ `control_plane/task_state.py`（`StateRecord`/`TaskState`/`AuditVerdict`）+ `execution_plane/agents/{adapter,registry}.py` + `config/role_agents.yaml`（role→backend/model/budget）。`run_mea_endpoint`（api.py）已暴露但当前为同步阻塞、缺取消支持（见第 9 节缺陷 2）。

---

### 2.5 最新演进：任务专属可执行评分标准（`layer_12_rubric_induction`，2026-09-09）

**要解决的问题**：此前研究「做到什么算成功」由两个**与任务本身无关**的东西决定——任务级只有一个单指标五元组（`eval_metric`/`direction`/`baseline`/`reference`/`gates`），而外审计 `layer_11` 只有 4 条**硬编码**约束（`primary_metric`/`eval_is_real`/`heldout_consistency`/`claims_supported`）对所有任务一视同仁。更关键的是，`audit_executor.py` 里预留的 `constraints` 扩展点**从未被任何调用方填充**——它是一个死插槽。于是「要求同时保住三个召回门限」的任务与「只求 top1 准确率」的任务被同一套标准评判。

**方法来源**：AutoSciRub（zjunlp，*Learning to Evaluate Before Improving*，ResearchClawBench 33.2 Pass@1）。该仓库**无一行 LLM 代码**——整套 rubric 生成/校验以「SKILL.md 自然语言契约 + JSON 产物 schema」表达，由宿主 Agent 执行。本平台移植其三步核心（`phi_inst` 目标骨架 → `phi_syn` criterion 合成 → `Verify` 逐条验证），并做**两处针对性改造**：

| AutoSciRub 原版 | 本平台 | 原因 |
|---|---|---|
| criterion 由 LLM 语义判定 | criterion 带 `check` 规格，**优先程序化判定**（6/7 条） | 本平台有真实 metrics，能做真正「可执行」的判定 |
| 单 agent 自产出自用 | 控制面产出并**冻结**，内循环只读 | 内循环是有目标的优化 agent，若标准可自产就等于「自己给自己放宽标准」——比自审计更严重的自确认 |

**双模式**：未提供可用标准 → **生成**；已提供 → **审查**（准确性 0.45 / 完整性 0.30 / 科学性 0.25 加权）后规范化为同一份契约。

**生成的标准**（平台可实跑任务，7 条 / 6 条可程序化判定）：

| ID | 维度 | 强度 | 判定方式 |
|---|---|---|---|
| C1 | correctness | 硬性 | `metric_present` |
| C2 | correctness | 硬性 | `metric_threshold` |
| C3 | correctness | 硬性 | `metric_improves`（margin 0.005，非平凡改进） |
| C4 | integrity | 硬性 | `real_eval`（禁止模拟冒充测量） |
| C5 | generalization | 硬性 | `metric_gap`（CV↔留出集 ≤0.05，**反过拟合**） |
| C6 | rigor | 一般 | `config_min`（≥3 折） |
| C7 | reporting | 一般 | judge（结论↔证据一致；**阴性结论在方法正确时同样合格**） |

**核心价值：报告「哪一条没达到、为什么」而非只给一个总分**（AutoSciRub README:56-57）。当前环境无法测量的要求**保留并标注 `blocked_reason`**，不静默丢弃也不静默通过。

**两条不可违背的判定规则**（实施中付出代价才发现，务必保留）：

1. **不可判定的 criterion 权重归 0**（`evaluable=False`）。否则「任务定义缺少声明」会被误归因成「研究没做好」——所有 tracked-only 任务将永远无法通过。性质变为：rubric 只在真实检查失败时收紧判定，绝不因缺数据收紧。
2. **硬失败一票否决**：`primary_metric` 或任一 high-priority criterion 处 `conflict` 时禁止 accept。否则新增的易通过条目会把硬失败**平均掉**（实测：弱模型从 REFINE 变成 ACCEPT）。

**隔离**：`layer_12` 与 `layer_11`/`layer_09` 同属保留能力，进入 `OUTER_LOOP_RESERVED_CAPS`（agent 工具面 + HTTP 端点双层拦截）与 `INNER_LOOP_FORBIDDEN_CALLERS`。标准**先于**结果确立（事件日志中 `rubric_synthesized` 索引必早于首个 `eval_completed`，有测试固化），冻结后带 `integrity_hash`。

> 完整设计见 [`doc/design_notes.md`](doc/design_notes.md) §4；交付与缺陷记录见 [`doc/code_review_STATUS.md`](doc/code_review_STATUS.md) 附录 E。

---

## 3. 目录结构

```
safety_auto_research/
├── README.md                      # 本文档
├── platform_contracts/            # 统一契约层（对象/事件/状态机/Schema 导出）
├── control_plane/                 # 控制平面（service + repository + api + schemas）
│   ├── api.py                     #   FastAPI 应用（create_app 工厂）
│   ├── service.py                 #   ControlPlaneService 状态机服务
│   ├── store.py                   #   Repository（内存 + JSON 落盘，锁内原子写）
│   ├── store_tree.py              #   ResearchStateStore 五件套（SQLite 落盘）
│   ├── playbook.py                #   ACE 式 Playbook（条目化/去重合并/计数）
│   ├── failure_miner.py           #   失败模式聚类（事件日志 → 避坑条目）
│   ├── evolution.py               #   进化搜索（种群/选择/变异/新颖性/归档）
│   ├── task_manager.py            #   并行任务管理器（JSON 落盘，崩溃恢复）
│   ├── llm_judge.py               #   真实 LLM judge 客户端（证据链 + 回退）
│   ├── progress_bus.py            #   SSE 进度总线（多订阅者，跨线程安全）
│   └── schemas.py                 #   API 请求/响应模型（含 InnerLoopConfig）
├── execution_plane/               # 执行平面（adapter / orchestrator / agent / capabilities / mea / agents）
│   ├── orchestrator.py            #   ClosedLoopOrchestrator + run_dual_loop / run_evolutionary_loop / run_program_evolutionary_loop / run_mea_loop
│   ├── agent/                     #   AgentHarness + 线协议 + 传输层（接 Codex/WorkBuddy 的 seam）
│   ├── capabilities/              #   10 个基础设施层能力注册为可调工具
│   ├── executors/                 #   eval / attack / lesson 真实 executor
│   ├── decision/router.py        #   IterationRouter（参考策略 + 安全护栏）
│   ├── mea.py                     #   MEA（Manage-Execute-Audit）主控：run_mea_loop_core + MeaMetrics
│   └── agents/                    #   RoleAgentAdapter / RoleAgentRegistry（require_different_from 异模型硬约束）
├── rubric/                        # 评分标准层（2026-09-09 新增）
│   ├── spec.py                    #   TaskSpec：目录任务/注册表单/运行快照的统一归一化视图
│   ├── review.py                  #   三维审查（准确性/完整性/科学性，纯确定性）
│   ├── synthesize.py              #   目标骨架 → 可行性 → criterion 合成（含 check 规格）
│   ├── checks.py                  #   7 类程序化判定器 + 逐条汇总
│   └── engine.py                  #   RubricEngine（review/induce，LLM 可选且失败降级）
├── openmle_integration/           # OpenRSI / OpenMLE 集成层（程序级岛模型，Phase A–D）
│   ├── contracts.py               #   零依赖镜像 dojo：Task/Interpreter/MetricValue/Node/Journal
│   ├── operators.py               #   四原子算子 Draft/Improve/Debug/Crossover + 各 LLM 后端
│   ├── adapter.py                 #   OpenMLETaskAdapter（sklearn 流水线包装 + step_task）
│   ├── interpreter.py             #   PythonInterpreter（多进程 + 超时，⚠ C1 沙箱待闭环）
│   ├── inner_capability.py        #   ATOMIC_OPERATORS + assert_operator_inner_only（白名单 fail-closed）
│   ├── reward_bridge.py           #   reward_func / reward_population（RL 风格奖励桥）
│   └── local_train.py             #   LocalLLMTrainer（Mac MPS/CPU ≤0.6B LoRA）
├── config/                        # role_agents.yaml（role→backend/model/budget 分配）
├── vendor/openmle_dojo/           # OpenMLE-dojo 参考副本（仅参考，不 vendoring 训练栈）
├── infrastructure/                # 十层研究基础设施资产（详见其 README）
├── benchmark_tasks/               # 任务目录（18 个任务 / 11 个类别 + suites/ 基准套件）
├── frontend/                      # 研究人员控制面板（React + Vite + TS）
├── scripts/                       # 端到端演示脚本（如 run_kaggle_codex_demo.py）
├── evaluation/                    # 原始 idea 质量 / 评测资产（已部分沉淀到 infrastructure）
├── doc/                           # 设计文档与架构 spec
├── tests/                         # 单元测试（control_plane / dual_loop / capabilities / execution_plane 等）
└── data/                          # 运行时持久化数据（自动生成，可纳入 .gitignore）
```

---

## 4. 功能特性

- **研究记录持久化**：控制平面与双循环状态均落盘（JSON + SQLite），**重启服务后研究记录自动加载**，不再随进程消失。
- **研究记录分类组织**：控制面板按任务类别组织记录。**v2 分类体系（2026-09）**：主分类只描述「任务研究什么」，共 4 类——机器学习建模（`ml_modeling`）/ 性能与效率优化（`perf_opt`）/ 安全与对抗（`safety_adversarial`）/ 智能体能力评测（`agent_eval`），主分类下另有二级子类（表格/沙箱建模、算子内核、算法加速、压缩、LLM 系统、攻击越狱、科学发现、ML 工程、开放式科研、元评测）。执行方式（可实跑 / agent 模式）与来源（精选移植 / 外部套件 / 自定义注册）作为正交维度，不再混入分类。旧版分类的历史记录统一归入「归档（旧分类）」组。
- **内循环参数深度可配（InnerLoopConfig）**：
  - *数据与方法*（脚本化与 agent 共用）：`preset` / `model`(gbm·gbm-strong·rf·logreg) / `fe`(基础·增强) / `cv_folds` / `threshold` / `drop_cols` / `data_dir`。
  - *自主 Agent 模式*：`mode` 切换脚本化↔agent；agent 模式额外可配 `system_prompt`、技能标签、工具多选（自动剔除护栏能力）、有序步骤编排。
- **Benchmark 任务目录**：从上游项目挖掘并批量接线出 40 个「明确数据集 + 评测」任务，按 v2 分类体系（4 个主分类 + 二级子类）组织，作为新建研究的起点。
- **双循环审计与可观测**：每个 run 的假设树（hypo-tree）、外部审计结论（audit）、改进项（improvements）均可实时查看；「进化观察」子 Tab 展示 Playbook 条目、策略补丁生命周期与进化种群。
- **研究榜单与一键复现**：每次研究自动沉淀记录，按指标方向取每任务最优 3 条高亮；支持「复现并启动」（按配置快照即刻重跑完整研究）。
- **任务专属评分标准（2026-09-09）**：
  - *注册期*：填写评分标准时即时审查「准确性 / 完整性 / 科学性」三维，列出缺陷与修正建议，并预览将生成的判定条目（**只告知、不阻断**——表单合法但标准弱正是要暴露的情况）。
  - *运行期*：内循环开始前确立标准并冻结；审计台上逐条展示「通过 / 未通过 + 判定依据 + 通过条件」，未达标时指出**具体哪一条**而非只给总分。
  - *可选 LLM*：设 `RUBRIC_LLM=1` 时用 LLM 补充条目（**只能追加、不能削弱**，强制 `judge` 判定、priority 上限 medium，故障静默降级回规则引擎）。
- **SSE 实时进度**：`GET /workflow-runs/{id}/stream` 推送 inner_done / audit_done / generation_done / finished 事件，前端实时刷新。
- **实验对比**：勾选最多 8 个 run 横向对比指标与配置。
- **基准套件**：`benchmark_tasks/suites/` 内置 ScienceAgentBench（102 任务）与 MLE-bench（75 竞赛，含 Known-Issues 泄漏标注剔除）两套套件级清单与论文基线数据。
- **Agent 工具面与线协议**：10 个基础设施层注册为 agent 可调工具（`run_capability`），并提供 `GET /agent/protocol` 返回 JSON Schema，便于接入 Codex / WorkBuddy 等外部 agent。
- **HITL 审批门**：高危动作（如关键红队）可触发人工审批断点，平台在审批解决前不推进。

---

## 5. 内置研究任务总览

平台内置 **40 个研究任务**（`benchmark_tasks/` 目录），按 **v2 分类体系**（主分类 = 任务研究什么；执行方式 / 来源为正交维度）组织；控制面板「新建研究」向导 Step 1 即从该目录渲染任务卡片。每个任务的完整细节（任务定义、目标、训练/测试数据、模型方案、评价指标与脚本、基线、性能）见 **[`doc/benchmark_tasks.md`](doc/benchmark_tasks.md)**。

| 主分类 | 子类 | 任务数 | 代表任务 | 可双循环直接执行 |
|--------|------|--------|----------|------------------|
| 机器学习建模 `ml_modeling` | 表格建模 | 5 | Kaggle Titanic / Spaceship / Wine / Iris / Breast-Cancer | ✅ 是 |
| 机器学习建模 `ml_modeling` | 沙箱建模（自定义注册） | 4 | 文本/图像/音频分类、Embedding 对比学习 | ✅ 是 |
| 性能与效率优化 `perf_opt` | 算子与内核 | 2 | Flash Attention、NTT Butterfly | ❌ 需沙箱 |
| 性能与效率优化 `perf_opt` | 算法加速 | 2 | AlgoTune kNN speedup、AES-128-CTR 吞吐 | ❌ 需沙箱 |
| 性能与效率优化 `perf_opt` | 模型/编码压缩 | 2 | Smallest Safety Router、Adaptive Compression | ❌ 需沙箱 |
| 性能与效率优化 `perf_opt` | LLM 训练与服务 | 2 | GRPO 训练步延迟、LLM Online Serving | ❌ 需沙箱 |
| 安全与对抗 `safety_adversarial` | 攻击与越狱 | 3 | Claudini 后缀攻击 / 提示注入 / 护栏绕过 | ✅ 是（tmeoa 端口） |
| 智能体能力评测 `agent_eval` | 科学发现 | 15 | ScienceAgentBench（套件 + 14 个本地实例） | ❌ |
| 智能体能力评测 `agent_eval` | ML 工程 | 2 | MLE-bench（本地 harness + 官方套件） | ❌ 外部依赖 |
| 智能体能力评测 `agent_eval` | 开放式科研 | 2 | ARA 制品理解、ARC-Bench 55 主题 | ❌ |
| 智能体能力评测 `agent_eval` | 元评测 | 1 | ARIS skill 触发率 | ❌ |

> **要点**：表格建模类任务（Kaggle Titanic / Spaceship-Titanic 等）可由双循环端到端执行（脚本化 `kaggle_eval`，实测 GBM CV 0.83+ / 0.80 级）；其余任务**已统一改为 agent 模式执行**——平台仅保留任务目标 / 定义 / 数据 / 评估方式 / 指标等核心信息，docker / Arbor / Harbor 依赖被剥离。agent 模式需接入远程 Agent（设置 `AGENT_COMMAND`），未接入时启动会**明确失败并终止**（不再静默回退）。
>
> **v2 分类变更（2026-09）**：旧 11 类（模型开发 / 系统优化 / 谜题 / CUDA 内核 / 对抗越狱 / 效率基准 / 科研 Agent 评测 / 想法质量评测 / 工具型元评测 / 平台原生 / 自定义）退役——其中「平台原生」「自定义」是执行方式 / 来源属性，「CUDA」是实现技术属性，均不再作为分类；原各类任务按研究对象重新归入上表 4 个主分类。历史研究记录中的旧分类值不做映射，前端统一显示为「归档（旧分类）」。

## 6. 快速开始

### 6.1 前置条件

- **Python**：推荐使用受管 venv（已预装 `sklearn`、`numpy`、`uvicorn`、`fastapi` 等）：
  `/Users/glennge/.workbuddy/binaries/python/envs/default/bin/python`
- **Node**：22.x（`/Users/glennge/.workbuddy/binaries/node/versions/22.22.2/bin/node`）
- **包路径约束**：因控制平面使用相对导入（`..platform_contracts`），**必须从 `safety_auto_research` 的父目录启动**后端，否则报 `ImportError: attempted relative import beyond top-level package`。

### 6.2 启动后端（端口 8000）

> 后端为同步应用，双循环等耗时操作在线程池中执行；建议用后台方式托管。

方式一（推荐，uvicorn 工厂）：

```bash
# 注意：在 safety_auto_research 的【父目录】执行
cd /Users/glennge/work/github/AI_research
/Users/glennge/.workbuddy/binaries/python/envs/default/bin/uvicorn \
    safety_auto_research.control_plane.api:create_app --factory \
    --host 127.0.0.1 --port 8000
```

方式二（模块入口，等价）：

```bash
cd /Users/glennge/work/github/AI_research
/Users/glennge/.workbuddy/binaries/python/envs/default/bin/python \
    -m safety_auto_research.control_plane.server
```

### 6.3 启动前端（端口 5173）

```bash
cd /Users/glennge/work/github/AI_research/safety_auto_research/frontend
npm install        # 首次
npm run dev        # Vite 开发服务器，/api 代理到 8000
```

打开 `http://localhost:5173` 即为研究人员控制面板。

### 6.4 验证

```bash
curl --noproxy '*' -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/workflow-runs   # 期望 200
curl --noproxy '*' -o /dev/null -w "%{http_code}\n" http://localhost:5173/                  # 期望 200
```

> **注意**：若本机环境设置了 `HTTP_PROXY`，对 `127.0.0.1`/`localhost` 的 curl 会被代理拦截返回 502，需加 `--noproxy '*'`（或浏览器直接访问）。详见第 11 节。

---

## 7. 使用指南

### 7.1 新建研究（两步入门向导）

1. 左侧「＋新建研究」→ **Step 1 选任务**：任务目录按类别展示卡片，选择其一。
2. **Step 2 配置**：
   - *外循环*：审计严格度（滑块）、预算（最大外循环迭代轮数）。
   - *内循环 · 数据与方法*：选择 `preset`、模型、特征工程强度、`cv_folds`、`threshold`、剔除列、数据目录。
   - *内循环 · 自主 Agent 模式*：切换「脚本化 / 自主 Agent」；选 Agent 模式后可展开系统提示词、技能标签、工具多选、有序步骤编排。
3. 点击「启动」→ 自动进入该 run 的仪表盘。

### 7.2 管理研究记录

- **侧边栏 run 画廊**：按类别分组、组头可折叠/展开、按时间倒序排序，便于收拢同类历史。
- **run 仪表盘**：状态头显示 `inner_loop` 配置摘要；子 Tab 含总览 / 审计结论 / 假设树 / 改进 / 审批台 / 事件。

### 7.3 持久化与重启

- 所有写操作（建 run / stage / decision / artifact / lesson / metric / event）实时写入 `data/control_plane_store.json`；双循环累积状态写入 `data/research_state.db`（SQLite）。
- 重启后端后上述数据自动加载，`GET /workflow-runs` 返回历史记录，类别分组、配置、假设树、审计结论均在。
- 落盘路径可用环境变量覆盖（见第 9 节）。

---

## 8. API 概览

控制平面共暴露 **63 条路径 / 71 个 method**（`GET /openapi.json` 为准；路由冻结表见
`tests/test_control_plane_structure.py::EXPECTED_ROUTES`）。关键路径：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/workflow-runs` | 创建工作流 run |
| POST | `/workflow-runs/{run_id}/start` | 启动 run |
| GET | `/workflow-runs` | 列出全部 run（持久化加载） |
| GET | `/workflow-runs/{run_id}` | run 详情 |
| POST | `/workflow-runs/{run_id}/request-approval` | 触发 HITL 审批 |
| POST | `/workflow-runs/{run_id}/resolve-approval` | 解决审批 |
| POST | `/workflow-runs/{run_id}/stages` | 创建阶段 |
| GET | `/workflow-runs/{run_id}/stages` | 列出阶段 |
| POST | `/workflow-runs/{run_id}/decisions` | 记录决策 |
| GET | `/workflow-runs/{run_id}/decisions` | 列出决策 |
| GET | `/agent/protocol` | agent 线协议 JSON Schema + 能力目录 |
| POST | `/workflow-runs/{run_id}/dispatch` | 单步派发 stage |
| POST | `/workflow-runs/{run_id}/capabilities/{capability_id}/run` | 运行某个基础设施层能力 |
| POST | `/workflow-runs/{run_id}/dual-loop` | 跑双循环（核心） |
| POST | `/workflow-runs/{run_id}/evolution` | 跑并行进化搜索（种群/新颖性拒绝/代冠军审计） |
| GET | `/workflow-runs/{run_id}/evolution` | 进化候选列表（适应度/新颖度/血缘，含 `node_kind`/岛分支 `gen{g}.isl{i}`） |
| POST | `/workflow-runs/{run_id}/mea` | 跑 MEA（Manage-Execute-Audit）控制循环 |
| GET | `/workflow-runs/{run_id}/mea` | MEA 运行态（Manager/Executor/Auditor 角色与 findings） |
| GET | `/workflow-runs/{run_id}/hypo-tree` | 假设树 |
| GET | `/workflow-runs/{run_id}/audit` | 外部审计结论 |
| GET | `/workflow-runs/{run_id}/improvements` | 改进项 |
| GET | `/workflow-runs/{run_id}/stream` | SSE 实时进度推送 |
| GET | `/playbook` | ACE Playbook 条目（可按 scope 过滤） |
| GET | `/strategies` | 策略补丁生命周期（pending→verified/rolled_back） |
| GET | `/research-records` · `/research-records/leaderboard` | 研究记录 / 全局榜 |
| POST | `/research-records/{id}/reproduce?autostart=true` | 复现并即刻重跑 |
| GET | `/benchmark-suites[/{id}][/tasks][/baselines]` | 基准套件（SAB / MLE-bench） |
| GET | `/benchmark-tasks` | 任务目录 |
| POST | `/benchmark-tasks/{task_id}/launch` | 按任务启动（含 inner_loop 配置） |
| POST | `/benchmark-tasks/validate` | 表单校验 **+ 评分标准审查 + 标准预览**（不落库） |
| POST | `/benchmark-tasks/review-standard` | 审查已声明评分标准并预览将生成的标准（不落库） |
| GET | `/benchmark-tasks/{task_id}/rubric` | 目录任务的可执行评分标准 |
| GET | `/workflow-runs/{run_id}/rubric` | 该 run 的冻结标准 + 每轮逐条判定结果 |

---

## 9. 关键概念与不变量

- **双循环隔离不变量（强制）**：
  - 外循环审计只读「策展输入」（objective + result_metrics + gate + real_eval + prior_audits + constraints），**不读**内循环事件日志（hypothesis/experience/lesson）。
  - 内循环 agent 禁止自审/自改：`layer_11_external_audit` 与 `layer_09_self_iterative_evolution` 在 harness 内为不可用工具，由控制平面直接调用。
  - 假设树/改进态按 `run_id` 隔离，跨 run 累积对象必须带 `run_id`。
- **内循环护栏**：配置内循环工具时，自动剔除 `layer_11`（外循环审计）、`layer_09`（自迭代进化）与 **`layer_12`（评分标准生成）**——内循环不得越权调用元/外循环能力。
- **评分标准不变量（2026-09-09）**：
  - 标准由控制面在**内循环开始之前**生成并冻结，内循环**只读不可改**（否则优化 agent 可自定标准，是自确认的加强版）。
  - 事件日志中 `rubric_synthesized` **必早于**该 run 的首个 `eval_completed`（有测试固化）。
  - 不可判定的 criterion 权重归 0 但仍报告为未达标 —— 判定只反映研究质量，不反映任务定义的完备性。
  - `primary_metric` 或任一 high-priority criterion 处于 `conflict` 时**一票否决** accept。
- **算子护栏（OpenRSI）**：四原子算子（Draft/Improve/Debug/Crossover）每次调用必经 `assert_operator_inner_only`（白名单 `INNER_LOOP_ALLOWED_CALLERS`，fail-closed），杜绝算子越权调用外层能力或自审。
- **MEA 自确认消除**：Auditor 角色强制与 Executor 异模型 / 异后端（`RoleAgentRegistry.require_different_from`），审计 findings 只读、不写环境。
- **状态机枚举**：`WorkflowRun` / `StageRun` / `DecisionRecord` 的合法流转由 `platform_contracts/transitions.py` 校验；决策类型含 `continue` / `revisit` / `exit_success` / `exit_budget` / `exit_converged`，终态必须为合法枚举名（旧字符串如 `dual_loop_accepted` 已废弃）。
- **任务类别枚举**：`puzzle` / `adversarial` / `efficiency` / `platform_native` / `model_dev` / `system_opt` / `cuda` / `agent_eval` / `tooling` 等（未分类兜底为 `未分类`）。
- **无 RemoteAgentHarness 时**：agent 模式（含所有 harness 依赖任务）启动会**明确失败并终止**——创建 FAILED 状态的 run 并在 `status_detail` 写明原因（需设置 `AGENT_COMMAND` 接入远程 Agent），**不再静默回退**脚本化。

---

## 10. 配置与持久化

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `CONTROL_PLANE_STORE` | `safety_auto_research/data/control_plane_store.json` | 控制平面 JSON 落盘路径 |
| `RESEARCH_STATE_DB` | `safety_auto_research/data/research_state.db` | 双循环 SQLite 落盘路径 |
| `HF_ENDPOINT` | `https://huggingface.co` | 真实竞赛数据下载（镜像对 gated 仓库返回 404，需直连 + `HF_TOKEN`） |
| `LLM_JUDGE_URL` | 未设置 | 真实 LLM judge 端点（POST JSON → score/rationale/evidence_refs）；未设置时用确定性 heuristic |
| `LLM_AUDIT_JUDGE` | 未设置 | 置 `1` 时外审计的 claim-support 评分走 `LLM_JUDGE_URL`（故障自动回退 heuristic） |
| `RUBRIC_LLM` | 未设置 | 置 `1` 时评分标准生成启用 LLM 补充条目（**仅追加**，故障静默降级回规则引擎） |
| `AGENT_SANDBOX` | 未设置 | 置 `1` 时 agent 模式把研究命令放进一次性 Docker 容器执行 |

**评分标准环节开关**（`run_dual_loop` / `run_evolutionary_loop` 参数）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `rubric_stage` | `True` | 关闭则完全回到改动前行为（外审计只跑 4 条内置约束） |
| `rubric_visible_to_inner` | `True` | 关闭则内循环盲跑，便于做「标准可见 vs 不可见」的 A/B 对比 |

> `create_app` 自建 service 时才会启用落盘；测试中以注入式 `Repository()`/`ControlPlaneService()` 构造的 service 仍为纯内存态，确保单测不触碰文件。

---

## 11. 测试

```bash
cd /Users/glennge/work/github/AI_research
/Users/glennge/.workbuddy/binaries/python/envs/default/bin/python -m pytest \
    safety_auto_research/tests/ -q
```

当前基线 **570 passed, 0 failed**（2026-09-09；较 2026-08-07 的 255 净增来自 B 飞轮型 Phase 1/1.5/1.6、`integrity_suite` 系列、`test_rubric_stage`(41) 与 `test_literature_research` 15→25 等）。

主要覆盖：`control_plane`、`dual_loop`（双循环隔离与终态 + `ContextSeparationTest`）、`playbook`、`phase2`（held-out/LLM judge/经验生命周期/预算）、`evolution`（种群/选择/双粒度新颖性/IslandModel/候选序列化）、`benchmark_registry`、`benchmark_suites`、`research_records`（榜单方向感知）、`capabilities`、`execution_plane`、`platform_contracts`、`openmle_phase_a/bc/d/d_ext`（OpenRSI 四阶段）、`mea_framework`(44)、`p2_fixes`/`review_supplementary`/`fix_regression_2026_08_05`/`fix_regression_2026_08_11`（审查回归）、`program_evolution_endpoint`（缺陷7 REST 接线）、`integrity_*`(115)、`flywheel`/`flywheel_scheduler`/`llm`(B 飞轮)、**`rubric_stage`(41，2026-09-09 新增)**。前端 `tsc --noEmit` 0 errors、`vite build` 通过。

> ⚠️ 全量套件一次性加载会因内存（pandas/pyarrow）触发 SIGKILL（exit 137），CI 须按模块分批运行；受管 venv 已加 `tests/conftest.py` 设 `future.infer_string=False` 规避 pandas 3.0 的 pyarrow segfault。

---

## 12. 开发环境约束与已知限制

- **Python 运行环境**：受管 venv 路径为 `/Users/glennge/.workbuddy/binaries/python/envs/default/bin/`（已装 `sklearn 1.9 / numpy 2.x`）；该 venv **未装 torch**，但当前任务为纯 numpy，无需 torch。重 ML 任务可回退系统 miniforge 3.10。
- **启动目录**：后端必须从 `safety_auto_research` 的父目录以完整包路径启动（相对导入约束，见 5.2）。
- **HuggingFace 镜像**：`hf-mirror.com` 对 gated 仓库返回 404；访问真实竞赛数据须 `HF_ENDPOINT=https://huggingface.co` + `HF_TOKEN`。
- **Docker / Harbor**：本机未安装 docker 与 harbor CLI，无法真正起 Harbor 沙箱；autolab 类任务改用本地 numpy 链路验证评测。
- **Agent 模式真实接入**：通过环境变量 `AGENT_COMMAND`（Codex / WorkBuddy CLI）接入 `RemoteAgentHarness` 后，agent 模式（含全部 harness 依赖任务）即可真正执行自主内循环——`system_prompt/skills/tools/step_plan` 与任务核心信息（`task_spec`）会完整传给外部 agent；未接入时启动明确失败并终止。
- **代理环境变量**：本机若设置 `HTTP_PROXY`，对 `127.0.0.1`/`localhost` 的 curl 会被拦截返回 502，需加 `--noproxy '*'` 或用 `localhost`（浏览器不受此影响）。
- **前端代理**：Vite 开发服务器将 `/api` 代理到 `:8000`；若后端未运行，前端请求会返回 500，应先确认后端存活。
- **OpenRSI 解释器沙箱（C1，待闭环）**：`openmle_integration/interpreter.py` 执行 LLM 生成的不可信代码时，当前把宿主全部环境变量（含 `OPENAI_API_KEY` / `LLM_JUDGE_URL` / DB 路径）透传给子进程，存在 RCE / 密钥泄露风险。短期需最小 env 白名单（清掉 `*_KEY`/`*_TOKEN`），长期需 seccomp / 容器沙箱隔离。详见 `doc/code_review_STATUS.md`（C1 严重项，附录 A/B 亦含相关修复）。
- **数据文件**：`data/` 为运行时生成，建议纳入 `.gitignore`。

---

## 13. 相关文档索引

- `platform_contracts/README.md` — 统一契约层（**21 对象 / 16 事件** / 状态机 / Schema 导出）
- `execution_plane/README.md` — 执行平面、双循环、AgentHarness、**21 个能力注册**、**评分标准环节**
- `execution_plane/agent/PROTOCOL.md` — 接入外部 agent 的线协议契约
- `infrastructure/README.md` — 十层研究基础设施资产总览
- **设计文档总索引**：[`doc/README.md`](doc/README.md)（按「活动待办 / 代码审查 / 架构设计 / 研究调研 / 基准任务 / 历史归档」分类，含状态列）
- [`doc/design_notes.md`](doc/design_notes.md) — **功能设计单一权威**（§1 控制面拆分 / §2 审计追问 / §3 agent 沙箱隔离 / **§4 任务专属可执行评分标准**）
- `doc/unified_safety_rd_platform_architecture_spec.md` — 平台目标架构 spec（Draft v1）
- `doc/benchmark_tasks.md` — **内置 18 个研究任务的逐任务详解**（定义/数据/模型/指标/基线/性能）
- `doc/code_review_STATUS.md` — **全量代码审查与修复状态（权威活文档，后续审查只更新此文件）**；附录 E 为 2026-09-09 评分标准环节交付 + layer_01 缺陷修复记录

---

*最后更新：2026-09-09 · 新增 §2.5「任务专属可执行评分标准（`layer_12_rubric_induction`）」——未提供标准的任务自动生成、已提供标准的任务自动审查准确性/完整性/科学性，标准冻结后同时作为内循环的只读执行契约与外审计的逐条判据（设计见 `doc/design_notes.md` §4，交付与缺陷记录见 `doc/code_review_STATUS.md` 附录 E）。功能状态：OpenRSI/OpenMLE 四阶段集成 + MEA 控制循环 + B 飞轮型 Phase 1/1.5/1.6 + **评分标准环节** 已落地，测试基线 **570 passed / 0 failed**（前端 `tsc --noEmit` 0 errors、`vite build` 通过）。仍待闭环：C1 沙箱层（LLM 生成代码以宿主全权限执行）、L5（非整数标签，设计暂缓）、F2 数据瓶颈（仅 kaggle 类与 text_classification 已可在沙箱真跑）。详见 `doc/code_review_STATUS.md`（含附录 E 的「坑（勿再踩）」清单）、`doc/design_notes.md`。*
