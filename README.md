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

### 2.3 分层模块

| 层 | 目录 | 职责 |
|----|------|------|
| **平台契约层** | `platform_contracts/` | 12 个核心对象模型、10+ 事件模型、状态机校验（`transitions.py`）、JSON Schema / TypeScript 导出 |
| **控制平面** | `control_plane/` | `ControlPlaneService`（状态机服务）+ `Repository`（持久化仓储）+ FastAPI 应用，暴露工作流 / 阶段 / 审批 / 决策 / 事件 / 双循环端点 |
| **执行平面** | `execution_plane/` | `StageExecutor` 抽象、`PlatformSDK`（adapter 唯一接口）、`ClosedLoopOrchestrator`（编排）、`AgentHarness`（决策）、`capabilities/`（10 层能力注册为可调工具） |
| **基础设施层** | `infrastructure/` | 十个研究环节的可复用资产（skill / agent / 数据集任务 / 评测），详见 `infrastructure/README.md` |
| **前端** | `frontend/` | React 18 + Vite + TypeScript 研究人员控制面板（侧边栏分类画廊 + 新建向导 + run 仪表盘） |
| **持久化** | `data/` | `control_plane_store.json`（run/stage/decision/event 等）+ `research_state.db`（双循环假设树/经验库/策略归档） |

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
├── execution_plane/               # 执行平面（adapter / orchestrator / agent / capabilities）
│   ├── orchestrator.py            #   ClosedLoopOrchestrator + run_dual_loop
│   ├── agent/                     #   AgentHarness + 线协议 + 传输层（接 Codex/WorkBuddy 的 seam）
│   ├── capabilities/              #   10 个基础设施层能力注册为可调工具
│   ├── executors/                 #   eval / attack / lesson 真实 executor
│   └── decision/router.py        #   IterationRouter（参考策略 + 安全护栏）
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
- **研究记录分类组织**：控制面板侧边栏按任务类别分组（谜题挑战 / 对抗越狱 / 效率基准 / 平台原生 / 模型开发 / 系统优化 / …），组头可折叠/展开，组内与组间均按时间排序。
- **内循环参数深度可配（InnerLoopConfig）**：
  - *数据与方法*（脚本化与 agent 共用）：`preset` / `model`(gbm·gbm-strong·rf·logreg) / `fe`(基础·增强) / `cv_folds` / `threshold` / `drop_cols` / `data_dir`。
  - *自主 Agent 模式*：`mode` 切换脚本化↔agent；agent 模式额外可配 `system_prompt`、技能标签、工具多选（自动剔除护栏能力）、有序步骤编排。
- **Benchmark 任务目录**：从 9 个上游项目扫描出 18 个「明确数据集 + 评测」任务，覆盖 11 个类别，作为新建研究的起点。
- **双循环审计与可观测**：每个 run 的假设树（hypo-tree）、外部审计结论（audit）、改进项（improvements）均可实时查看；「进化观察」子 Tab 展示 Playbook 条目、策略补丁生命周期与进化种群。
- **研究榜单与一键复现**：每次研究自动沉淀记录，按指标方向取每任务最优 3 条高亮；支持「复现并启动」（按配置快照即刻重跑完整研究）。
- **SSE 实时进度**：`GET /workflow-runs/{id}/stream` 推送 inner_done / audit_done / generation_done / finished 事件，前端实时刷新。
- **实验对比**：勾选最多 8 个 run 横向对比指标与配置。
- **基准套件**：`benchmark_tasks/suites/` 内置 ScienceAgentBench（102 任务）与 MLE-bench（75 竞赛，含 Known-Issues 泄漏标注剔除）两套套件级清单与论文基线数据。
- **Agent 工具面与线协议**：10 个基础设施层注册为 agent 可调工具（`run_capability`），并提供 `GET /agent/protocol` 返回 JSON Schema，便于接入 Codex / WorkBuddy 等外部 agent。
- **HITL 审批门**：高危动作（如关键红队）可触发人工审批断点，平台在审批解决前不推进。

---

## 5. 内置研究任务总览

平台内置 **18 个研究任务**（`benchmark_tasks/` 目录），覆盖 11 个类别、来自 8 个上游项目；控制面板「新建研究」向导 Step 1 即从该目录渲染任务卡片。每个任务的完整细节（任务定义、目标、训练/测试数据、模型方案、评价指标与脚本、基线、性能）见 **[`doc/benchmark_tasks.md`](doc/benchmark_tasks.md)**。

| 来源 | 任务数 | 类别 | 可双循环直接执行 |
|------|--------|------|------------------|
| 平台原生 | 2 | 平台原生（Titanic / Spaceship-Titanic） | ✅ 是 |
| AutoLab | 7 | 谜题 / 模型开发 / 系统优化 / CUDA | ❌ 需 Harbor 沙箱 |
| Claudini | 3 | 对抗 / 越狱 | ❌ 需上游运行环境 |
| Arbor | 1 | 效率基准（kNN speedup） | ❌ 需 Arbor |
| AutoResearchClaw | 1 | 科研 Agent 评测（ARC-Bench 55 主题） | ❌ |
| ARA | 1 | 科研 Agent 评测（制品理解） | ❌ |
| Auto-claude | 1 | 工具型元评测（skill 触发率） | ❌ |
| MLEvolve | 1 | 模型开发（MLE-bench 75 任务，外部数据） | ❌ 外部依赖 |

> **要点**：两个**平台原生**任务（Kaggle Titanic / Spaceship-Titanic）可由双循环端到端执行（脚本化 `kaggle_eval`，实测 GBM CV 0.83+ / 0.80 级）；其余 15 个任务（autolab / claudini / Arbor / ARA / AutoResearchClaw / Auto-claude / MLEvolve 等）**已统一改为 agent 模式执行**——平台仅保留任务目标 / 定义 / 数据 / 评估方式 / 指标等核心信息，docker / Arbor / Harbor 依赖被剥离。agent 模式需接入远程 Agent（设置 `AGENT_COMMAND`），未接入时启动会**明确失败并终止**（不再静默回退）。

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

控制平面共暴露约 27 个端点，关键路径：

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
| GET | `/workflow-runs/{run_id}/evolution` | 进化候选列表（适应度/新颖度/血缘） |
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

---

## 9. 关键概念与不变量

- **双循环隔离不变量（强制）**：
  - 外循环审计只读「策展输入」（objective + result_metrics + gate + real_eval + prior_audits + constraints），**不读**内循环事件日志（hypothesis/experience/lesson）。
  - 内循环 agent 禁止自审/自改：`layer_11_external_audit` 与 `layer_09_self_iterative_evolution` 在 harness 内为不可用工具，由控制平面直接调用。
  - 假设树/改进态按 `run_id` 隔离，跨 run 累积对象必须带 `run_id`。
- **内循环护栏**：配置内循环工具时，自动剔除 `layer_11`（外循环审计）与 `layer_09`（自迭代进化）——内循环不得越权调用元/外循环能力。
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

> `create_app` 自建 service 时才会启用落盘；测试中以注入式 `Repository()`/`ControlPlaneService()` 构造的 service 仍为纯内存态，确保单测不触碰文件。

---

## 11. 测试

```bash
cd /Users/glennge/work/github/AI_research
/Users/glennge/.workbuddy/binaries/python/envs/default/bin/python -m pytest \
    safety_auto_research/tests/ -q
```

当前基线 **132 passed**。主要覆盖：`control_plane`（状态机/API）、`dual_loop`（双循环隔离与终态）、`playbook`（ACE 合并/反思/策略生命周期/审计隔离）、`phase2`（held-out/LLM judge/经验生命周期/预算）、`evolution`（种群/选择/新颖性/并行回放/审计隔离）、`benchmark_registry`（任务注册校验）、`benchmark_suites`（套件清单/基线）、`research_records`（榜单/复现）、`capabilities`、`execution_plane`、`platform_contracts`。前端 `npx tsc --noEmit` 0 errors。

---

## 12. 开发环境约束与已知限制

- **Python 运行环境**：受管 venv 路径为 `/Users/glennge/.workbuddy/binaries/python/envs/default/bin/`（已装 `sklearn 1.9 / numpy 2.x`）；该 venv **未装 torch**，但当前任务为纯 numpy，无需 torch。重 ML 任务可回退系统 miniforge 3.10。
- **启动目录**：后端必须从 `safety_auto_research` 的父目录以完整包路径启动（相对导入约束，见 5.2）。
- **HuggingFace 镜像**：`hf-mirror.com` 对 gated 仓库返回 404；访问真实竞赛数据须 `HF_ENDPOINT=https://huggingface.co` + `HF_TOKEN`。
- **Docker / Harbor**：本机未安装 docker 与 harbor CLI，无法真正起 Harbor 沙箱；autolab 类任务改用本地 numpy 链路验证评测。
- **Agent 模式真实接入**：通过环境变量 `AGENT_COMMAND`（Codex / WorkBuddy CLI）接入 `RemoteAgentHarness` 后，agent 模式（含全部 harness 依赖任务）即可真正执行自主内循环——`system_prompt/skills/tools/step_plan` 与任务核心信息（`task_spec`）会完整传给外部 agent；未接入时启动明确失败并终止。
- **代理环境变量**：本机若设置 `HTTP_PROXY`，对 `127.0.0.1`/`localhost` 的 curl 会被拦截返回 502，需加 `--noproxy '*'` 或用 `localhost`（浏览器不受此影响）。
- **前端代理**：Vite 开发服务器将 `/api` 代理到 `:8000`；若后端未运行，前端请求会返回 500，应先确认后端存活。
- **数据文件**：`data/` 为运行时生成，建议纳入 `.gitignore`。

---

## 13. 相关文档索引

- `platform_contracts/README.md` — 统一契约层（对象/事件/状态机/Schema 导出）
- `execution_plane/README.md` — 执行平面、双循环、AgentHarness、能力注册
- `execution_plane/agent/PROTOCOL.md` — 接入外部 agent 的线协议契约
- `infrastructure/README.md` — 十层研究基础设施资产总览
- `doc/unified_safety_rd_platform_architecture_spec.md` — 平台架构 spec
- `doc/dual_loop_upgrade_plan.md` — 双循环升级方案
- `doc/harness_gap_analysis_and_upgrade_plan.md` — **Harness 工程差距分析与三期升级规划**（对照 Weng 综述，三期全部落地）
- `doc/code_review_2026-07-30_round3.md` — 全项目代码审查 Round3（32 项缺陷 + 批 1→4 修复执行结果）
- `doc/code_review_2026-08-04.md` — 全项目代码审查（P1×4 + P2×12 修复执行记录 + 五.3 补测试 5 项落地）
- `doc/benchmark_suites_integration.md` — 基准套件（SAB / MLE-bench）集成说明
- `doc/benchmark_tasks.md` — **内置 18 个研究任务的逐任务详解**（定义/数据/模型/指标/基线/性能）

---

*最后更新：2026-08-04 · 全项目代码审查（P1×4 程序循环方向 / 算子消费父程序 / 榜单 lower-is-better 回退 / reward NaN + P2×12 功能与隔离加固）+ 五.3 补测试 5 项落地；回归 177 passed + 补测试 49 passed，前端 tsc 0 errors。详见 `doc/code_review_2026-08-04.md`。*
