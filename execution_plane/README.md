# Execution Plane — Adapters (spec §11)

把现有 01-10 层以 **adapter** 形式收编进平台，而不要求立刻内部重写（spec §11.2）。
每个 adapter 只做四件事（spec §9.4）：

1. `load_object(ref)` 读取输入对象
2. 执行本层逻辑
3. `publish_artifact(...)` 发布输出对象
4. `emit_event(event)` 发送平台事件

本包把 `EvalCompletedEvent` / `AttackCompletedEvent` / `LessonPromotedEvent` 接到**真实可运行**
的 adapter，并形成闭环：

```
评测(EvalCompletedEvent) ─► 决策(AgentHarness, router=参考策略+护栏)
        │  R10: gate 通过 → 进入对抗硬化
        ▼
红队(AttackCompletedEvent) ─► 决策(AgentHarness)  ── ASR>天花板 → 继续硬化(loop) ──┐
        │  R10-exit: ASR≤天花板                                            ◄──────────┘
        ▼
经验(LessonPromotedEvent) ─► register_lesson 回注 ─► 决策(CONTINUE / EXIT)
```

> **范式定位（2026-07-20 最小支点重构）**：平台不替 agent 决定"怎么做"。控制面 + 平台契约是
> agent 的**运行时与记忆**，`PlatformSDK` 是 agent 的**工具面**，`StageExecutor` 是**默认/兜底
> 实现**，`IterationRouter` 降级为**参考策略 + 安全校验器**。权威决策者由 `AgentHarness` 担任
> （见 [AgentHarness](#agentharness)）。`mode="scripted"`（默认）保持原有确定性行为，向后兼容。

## 目录结构

```
execution_plane/
├── __init__.py                 # 导出 ClosedLoopOrchestrator / AdapterRegistry / PlatformSDK
├── sdk.py                      # PlatformSDK：adapter 唯一允许的接口（§9.4 六方法）
├── base.py                     # StageExecutor 抽象基类 + ExecResult + StageTaskSpec
├── capabilities/               # 基础设施层能力注册表（agent 可调工具）
│   ├── base.py                 #   InfraCapability 数据类 + 目录条目
│   ├── executors.py            #   StubCapabilityExecutor（确定性占位，产出真实 artifact）
│   ├── registry.py             #   CapabilityRegistry + default_capability_registry()（10 层）
│   ├── literature_research_executor.py  # 层① 真实 arXiv/GitHub 检索（Phase 2 起，非桩）
│   ├── kaggle_eval_executor.py #   真实 Kaggle 评测（CV + held-out）
│   ├── audit_executor.py       #   layer_11 外审计（消费 rubric criteria）
│   ├── rubric_executor.py      #   layer_12 评分标准环节（2026-09-09 新增）
│   ├── sandbox_executor.py     #   Docker 沙箱内执行研究命令
│   ├── auto_label_executor.py  #   LLM 自动标注（B 飞轮第 2 步）
│   ├── badcase_retrain_executor.py  # 坏例回放重训 + 回归门
│   ├── self_evolution_executor.py   # 层⑨ 自迭代进化
│   └── data_pipeline_executor.py    # 层⑤ 数据清洗/去重/PII
├── registry.py                 # AdapterRegistry + default_registry()
├── orchestrator.py             # ClosedLoopOrchestrator：dispatch + 决策 + 闭环（agent / scripted 双模式）
├── agent/
│   ├── __init__.py             # 导出 AgentHarness / *Harness / protocol / transport
│   ├── harness.py              # AgentHarness 抽象 + 本地兜底 + 接入 Codex/WorkBuddy 的 seam
│   ├── protocol.py             # agent↔platform 线协议消息（pydantic）+ all_schemas()
│   ├── transport.py            # Transport 抽象 + Callable / Subprocess / Http 三种实现
│   └── PROTOCOL.md             # 接真实 agent 的契约文档（消息/工具/传输/接线）
├── executors/
│   ├── eval_executor.py        # 层③ Benchmark Plane → EvalCompletedEvent
│   ├── attack_executor.py      # 层⑩ Adversarial Plane → AttackCompletedEvent
│   └── lesson_executor.py      # 层⑧ 经验 → LessonPromotedEvent（回注）
└── decision/
    └── router.py               # IterationRouter：事件 → DecisionRecord（§10.2 决策）
```

## PlatformSDK（adapter 唯一接口）

| 方法 | 作用 |
|------|------|
| `load_object(ref)` | 读 `run/stage/decision/events:/decisions:/artifact/lesson` |
| `publish_artifact(payload, schema_version, metadata)` | 创建 `Artifact` + 发 `ArtifactPublishedEvent` |
| `emit_event(event)` | 向事件日志追加平台事件 |
| `request_approval(payload)` | 开 HITL 门（高危红队用） |
| `record_metric(name, value, tags)` | 记录观测指标 |
| `register_lesson(payload)` | 提升 `LessonCard` + 发 `LessonPromotedEvent`（回注信号） |

## 三个 executor（真实逻辑，非桩）

- **EvalExecutor**（`stage_codes: 03/eval/benchmark`）：从 run id 哈希取种子生成可复现 benchmark
  指标（accuracy / robustness），与目标阈值比较，发 `EvalCompletedEvent` + eval_report artifact。
- **AttackExecutor**（`stage_codes: 10/attack/redteam/adversarial`）：模拟红队 campaign，ASR 随已知
  漏洞族数量 + 种子噪声上升；retention/forgetting 由是否启用 replay buffer 决定；发
  `AttackCompletedEvent` + attack_report artifact。
- **LessonExecutor**（`stage_codes: 08/lesson/experience/result_analysis`）：读取本 run 的事件与决策，
  抽取高频失败/漏洞模式，提升为 `LessonCard`（置信度/PRM 分随频次升高），发 `LessonPromotedEvent`。

## IterationRouter（参考策略 + 安全校验器）

> **角色变化**：`IterationRouter` 不再是唯一决策者。它的 `propose_route` 仅产出**参考策略**
> （R1-R10，原 `decide` 的别名），`validate_route` 是**硬护栏**——任何决策（agent 做的或参考策略
> 做的）若违反决策记录契约（如 `CONTINUE` 带 `target_stage`、`REVISIT` 缺目标）在提交前即被拒。

| 输入事件 | 条件 | 决策 | 目标环节 | 路由 |
|----------|------|------|----------|------|
| `EvalCompletedEvent` | gate 通过 + 对抗硬化 | REVISIT | ⑩ | R10 |
| `EvalCompletedEvent` | gate 失败 | REVISIT | ③ | R7 |
| `EvalCompletedEvent` | gate 通过 + 非硬化 | EXIT_SUCCESS | — | EXIT-S |
| `AttackCompletedEvent` | ASR > 0.15 | REVISIT | ⑩ | R10（继续硬化） |
| `AttackCompletedEvent` | ASR > 0.15 且 retention < 0.90 | REVISIT | ⑤ | R10+R6（遗忘→数据清洗） |
| `AttackCompletedEvent` | ASR ≤ 0.15 | CONTINUE | — | R10-exit |
| `LessonPromotedEvent` | 任意 | CONTINUE/EXIT | — | REINJECT |

阈值：`ASR_CEILING=0.15`、`RETENTION_FLOOR=0.90`（可在 `decision/router.py` 调整）。

## AgentHarness（agent 驱动范式）

`ClosedLoopOrchestrator` 支持两种模式：

- `mode="scripted"`（默认）：`IterationRouter` 决策、确定性 executor 执行——向后兼容。
- `mode="agent"`：权威决策来自 `AgentHarness`；`IterationRouter.propose_route` 仅作**建议**，
  `IterationRouter.validate_route` 作**硬护栏**（任何非法决策在提交前被拒）。

`AgentHarness` 抽象两个方法：

| 方法 | 作用 |
|------|------|
| `decide(run, event, suggestion) -> RouteDecision` | 读事件+状态，推理出下一步；`suggestion` 是 router 的参考策略，agent 可采纳/改写/否决 |
| `run_stage(spec: StageTaskSpec, sdk) -> ExecResult` | 自主完成一个 stage 的**任务规格**（目标 + SDK 工具 + 上下文），而非执行固定脚本 |

两种实现：

- **`LocalAgentHarness`（默认后端）**：用 router 参考策略做决策、调用确定性 executor 跑 stage。
  即"无外部 agent 时也能完整跑通"，也是未接入远端 agent 时的兜底。
- **`RemoteAgentHarness`（接入 Codex / WorkBuddy 的真实 seam）**：把决策/执行委托给外部 agent
  运行时，二者通过 **线协议**（`agent/protocol.py`）通信。平台发 `TaskDecide` / `TaskRunStage`，
  agent 可经 `ToolCall` / `ToolResult` 调用平台工具，最终回 `AgentDecision` / `AgentStageResult`。
  传输层（`agent/transport.py`）提供 `CallableTransport`（测试/嵌入）、`SubprocessTransport`
  （CLI agent，行分隔 JSON + 工具调用交互）、`HttpTransport`（远程 HTTP agent 骨架）。
  未配置 `agent_command` / `transport` 时显式抛 `NotImplementedError`——这里就是接自主 agent 的点。

`StageTaskSpec`（`base.py`）是交给 agent 的"任务规格"：含 `stage_run / params / available_tools
(agent 工具面) / run_context`，并用 `to_dict()` 序列化发给远端 agent。开放目标场景下再带
`open_goal` 与 `candidate_capabilities`，让 agent 自由编排。

> 接真实 agent 的**完整契约**（消息形状、工具签名、传输、接线示例）见
> [`agent/PROTOCOL.md`](agent/PROTOCOL.md)。运行时平台在 `GET /agent/protocol` 直接返回这套
> JSON Schema，agent 可拉取后自行校验/生成调用代码。

## Capabilities（基础设施层能力注册为 agent 可调工具）

这是让 agent **端到端自主编排** 十个基础设施层的关键一层。每个层在 `capabilities` 注册为一个
`InfraCapability`（id / 层码 / 描述 / 参数 schema / 绑定的 executor）。agent 通过 agent 工具面
新增的 **`run_capability(run_id, capability_id, params)`** 调用任意层：

- **真实 executor**（截至 2026-09-09）：① 文献检索（arXiv/GitHub，缺 `query` 时从研究目标派生，
  见下）、③ 评估与基准、⑤ 数据评估清洗、⑧ 结果分析与经验、⑨ 自迭代进化、⑩ 数据生成对抗，
  外加 `kaggle_eval` / `badcase_retrain` / `auto_label` / 四个沙箱能力 / `layer_11` / `layer_12`；
- **仍为 `StubCapabilityExecutor`**：② ④ ⑥ ⑦（确定性占位，仍产出真实 artifact + 指标，保持可审计）；
  待各层真实 skill/agent 接入后替换即可，契约不变。
  > ⚠️ 维护红线：把某层从桩换成真实 executor 时，**必须同步更新本清单、调用方的假设注释与
  > 相关测试**。2026-09-09 修过一个真实教训 —— ① 早已换成真实 arXiv executor，但
  > `control_plane/task_state.py` 的注释仍写「layer_01 是 StubCapabilityExecutor」，掩护了
  > 「契约只传 `subtask_type`、不传研究目标」这一失效前提，使 MEA 的文献步骤长期不可用。
- **保留能力（内循环不可调用）**：`layer_11_external_audit`、`layer_09_self_iterative_evolution`、
  **`layer_12_rubric_induction`** —— 三者同属外层/元层，进入 `OUTER_LOOP_RESERVED_CAPS`，
  agent 工具面与 HTTP 端点**双层拦截**。理由：内循环是有目标的优化 agent，若允许它自审计或
  **自定评分标准**，就会出现比自确认更严重的「自己给自己放宽标准」。
- `run_capability` 内部 = 创建该层 `StageRun` → 转 `running` → 执行 executor → 转终态 → 发事件，
  **走与 `dispatch_stage` 完全相同的受控、校验、审计路径**，所以 agent 自由编排也不脱离护栏。
- 能力目录由 `GET /agent/protocol` 的 `capabilities` 字段实时返回，agent 可在运行时发现能调用什么。
- 开放目标：`ClosedLoopOrchestrator.dispatch_open_goal(run_id, goal, candidates)` 把目标交给
  agent，由它自行决定依次调用哪些 `run_capability`；平台只负责用容器 `StageRun` 包住这一轮。

## 评分标准环节（`layer_12_rubric_induction`，2026-09-09 新增）

在**内循环开始之前**确立「这次研究按什么标准判定」，并把同一份标准交给内循环当**只读执行契约**、
交给 `layer_11` 当**审计判据**。设计详见 [`doc/design_notes.md`](../doc/design_notes.md) §4。

```
run_dual_loop / run_evolutionary_loop
  └─ [iteration 0 之前] layer_12 ──► ExecutableRubric（frozen + integrity_hash）
        ├─► inner_params["rubric_context"]   内循环只读
        └─► audit_input["rubric"] ──► layer_11 逐条判定 + 硬性一票否决
```

**双模式**：任务未提供可用标准 → `synthesize` 生成；已提供 → `review` 三维审查（准确性/完整性/科学性）后规范化。

**判定优先程序化**：criterion 携带 `check` 规格（`metric_present` / `metric_threshold` /
`metric_improves` / `real_eval` / `metric_gap` / `config_min` / `judge`），平台可实跑任务生成的
7 条中 6 条由代码判定，不依赖语义猜测。

**两条不可违背的判定规则**（代价换来的，务必保留）：

1. 不可判定的 criterion（`blocked_reason` 或 `missing`）→ `evaluable=False`，**权重归 0**，
   但仍如实报告为未达标。否则会把「任务定义缺少声明」误归因成「研究没做好」。
2. `primary_metric` 或任一 high-priority criterion 处于 `conflict` → **一票否决**，无论标量多高。
   否则新增的易通过条目会把硬失败**平均掉**。

**前置环节自身的守卫**：rubric 前置会消耗一次能力调用，因此**必须先过 cancel + budget 检查**，
否则预算对它无效。

**开关**：`rubric_stage`（默认 `True`，关闭即完全回到改动前行为）、`rubric_visible_to_inner`
（默认 `True`，关闭可盲跑做 A/B 对比）、`RUBRIC_LLM=1`（可选 LLM 补充条目，append-only 且失败降级）。

## 控制面接线

`control_plane/api.py` 暴露：

- `POST /workflow-runs/{run_id}/dispatch` —— 单步派发一个 stage 到 adapter，发事件 + 自动决策
- `POST /workflow-runs/{run_id}/capabilities/{capability_id}/run` —— **运行一个基础设施层能力**（agent 工具面 `run_capability` 的 HTTP 等价）
- `POST /workflow-runs/{run_id}/closed-loop?max_rounds=4` —— 跑完整闭环，返回 trace 摘要
- `GET  /workflow-runs/{run_id}/lessons` —— 列出已提升的 LessonCard（回注对象）
- `GET  /agent/protocol` —— **返回 agent↔platform 线协议的 JSON Schema + 工具清单 + 能力目录**（接 Codex/WorkBuddy 用）

## 运行

```bash
# 服务（http://127.0.0.1:8000）
.venv/bin/python -m safety_auto_research.control_plane.server

# 测试
.venv/bin/python -m unittest safety_auto_research.tests.test_execution_plane -v
```

## 端到端演示（真实 Kaggle + Codex 驱动）

`scripts/run_kaggle_codex_demo.py` 把"用真实 Kaggle 任务 + 真实外部 agent 跑通整个自动化研究流程"
落为可复现脚本，且通过 `kaggle_eval` capability 的 `preset` 参数支持**任意**表格竞赛：

- 建 `ControlPlaneService` + `ClosedLoopOrchestrator(mode="agent", harness=RemoteAgentHarness(transport=CodexTransport(...)))`
- 以某个 Kaggle 竞赛为 objective 建 workflow run 并 `start_workflow_run`
  （`--competition titanic` 阈值 0.82；`--competition spaceship` 阈值 0.80）
- `dispatch_open_goal(...)` 把开放目标交给 Codex；Codex 通过 `run_capability("kaggle_eval",
  {"preset":..., "target":..., "data_dir":..., "model":...})` 真实训练 sklearn 模型
  （GBM / LogReg / RF 等），平台把每次调用记成真实 `StageRun` + `EvalCompletedEvent`
- 解析闭环 trace，提取最终 CV 指标，与金牌对比，生成 `REPORT.md` / `TRACE.json`
  （位于 `data/kaggle/<competition>/`）

真实竞赛数据来自 Hugging Face（需 `HF_TOKEN` + 直连 `huggingface.co`；镜像对 gated 仓库返回 404）：
如 `Hugo0133/Spaceship-Titanic` → `data/kaggle/spaceship-titanic/`。
> 注：OpenAI 的 `openai/mle-bench`（75 竞赛原始数据）在本账号下不可达（仓库不存在 / license 未接受），
> 故演示采用公开的真实表格竞赛；`kaggle_eval` 已泛化，换竞赛只需补 `preset` + 数据。

运行（需装有 scikit-learn 的 Python；本机用 miniforge 3.10，managed 3.13 缺 sklearn）：

```bash
# 真实 Codex 驱动 Spaceship-Titanic（需要本机 codex CLI + 可达的模型代理，approval=never）
/opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    safety_auto_research/scripts/run_kaggle_codex_demo.py --competition spaceship

# 不接 Codex 时，用脚本化兜底仍跑真实 kaggle_eval（报告内明确标注为 fallback-scripted）
/opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    safety_auto_research/scripts/run_kaggle_codex_demo.py --competition spaceship --no-codex --models gbm,logreg
```

脚本化兜底已验证：
- **Titanic**：GBM 5 折 CV accuracy = 0.8316 ± 0.0167、LogReg = 0.8215，均超过 0.82 金牌，`gate_passed=true`。
- **Spaceship-Titanic**：GBM = 0.7966 ± 0.0099、RF = 0.7899、LogReg = 0.7863，最佳逼平 0.80 金牌线
  （CV 略低于 held-out 榜单，属正常方差），报告如实给出 SILVER 判定（见 `data/kaggle/spaceship-titanic/REPORT.md`）。

两段都证明 `kaggle_eval` 喂的是**真实、无泄漏**的经验评测，而非 mock hash。

## 下一步

- **接入真实 agent（协议骨架已完成）**：用 `RemoteAgentHarness(agent_command="codex ...")`
  或 `HttpTransport(endpoint)` 替换确定性决策/执行，让 agent 真正自主决定论文调研、idea 生成与
  验证、实验设计、写码、跑实验、跑评测。agent 需实现 `agent/PROTOCOL.md` 描述的线协议（含新增的
  `run_capability` 工具）。平台职责收敛为：状态、记忆、工具（SDK 六方法 + `run_capability`）、
  护栏（HITL + 状态机校验）。
- **把剩余桩替换为真实 adapter**：② ④ ⑥ ⑦ 目前仍是 `StubCapabilityExecutor`（① ③ ⑤ ⑧ ⑨ ⑩ 已是真实
  executor），接入真实 skill/agent/数据集/评测后，在 `capabilities/registry.py` 把对应层绑定到真实
  `StageExecutor` 即可，契约与事件流无需改动。**替换时务必同步更新本文档的能力清单、
  `control_plane/task_state.py` 的调用方假设注释与相关测试**（见「Capabilities」节红线）。
- 在 `AttackExecutor` 检测到 critical ASR 时自动触发 HITL 断点（`request_approval`）。
  在 agent 模式下它同样是交给 agent 的一个 `StageTaskSpec`。
- `request_approval` 目前仅在 SDK 暴露；可在 `RemoteAgentHarness` 决策出 critical ASR 时触发 HITL 断点。
- 用真实执行（AutoLab / MLEvolve / Arbor）替换 executor 内的模拟逻辑，保持同样的事件/决策契约。
