# 统一契约层实现说明

本目录承载“通用安全算法研发平台”的统一契约层第一版实现，目标是为后端控制面、执行平面 adapter、前端工作台提供一套共享的对象模型、事件模型和状态流转约束。

## 目录结构

- `base.py`
  - 契约层公共基类 `ContractModel`
- `enums.py`
  - 平台级枚举定义，包括对象状态、事件类型、决策类型、可见性与风险等级
- `objects.py`
  - 21 个核心对象模型
- `events.py`
  - 统一事件模型（16 个）
- `transitions.py`
  - `WorkflowRun`、`StageRun`、`DecisionRecord` 的状态流转 contract 与校验函数
- `export.py`
  - JSON Schema 内存导出与文件导出逻辑
- `export_schemas.py`
  - CLI 导出脚本
- `models.py`
  - 兼容聚合出口，供历史导入路径继续使用

## 当前覆盖范围

### 核心对象

当前已定义以下 **21 个**对象（12 个初版 + 4 个双循环累积态 + 5 个评分标准环节）：

**初版核心（1–12）**

1. `ResearchProgram`
2. `SafetyTarget`
3. `WorkflowRun`
4. `StageRun`
5. `Artifact`
6. `DatasetRelease`
7. `ModelVersion`
8. `EvalSuite`
9. `AttackCampaign`
10. `DecisionRecord`
11. `LessonCard`
12. `PolicyPack`

**双循环 / 累积态（13–16）**

13. `AuditReport` —— 外审计结论（constraints / unresolved_claims / confidence / recoverable）
14. `ImprovementProposal` —— 元循环改进提案（可证伪预测 + 回滚）
15. `HypothesisNode` —— 假设树节点（含 `node_kind` 区分 config/program 粒度）
16. `ExperienceEntry` —— 跨 run 经验卡（confidence 随 staleness 衰减）

**评分标准环节（17–21，2026-09-09 新增，详见 `../doc/design_notes.md` §4）**

17. `RubricGoal` —— 从任务指令派生的原子科研目标（携带 `instruction_evidence` 可追溯）
18. `RubricCriterion` —— 可执行判定条目：`requirement` / `dimension` / `priority` /
    `satisfaction_condition` / `check`（程序化判定规格）/ `blocked_reason`
19. `RubricFinding` —— 标准审查发现的缺陷（`dimension` / `severity` / `suggestion`）
20. `RubricReview` —— 三维审查结论：准确性 0.45 / 完整性 0.30 / 科学性 0.25 加权
21. `ExecutableRubric` —— 冻结的评分契约（`source` / `criteria` / `claims_to_avoid` / `integrity_hash`）

> `ArtifactType` 已扩至 14 种，含 `rubric`（评分标准）与 `paper_set`（文献集）。

### 统一事件

当前已实现 **16 个**事件模型（与架构 spec §9.2 的最小事件集合对齐）：

1. `BasePlatformEvent` —— 所有事件的公共基类（`event_id` / `event_type` / `run_id` / `occurred_at`，后两者带默认值）
2. `WorkflowStatusChangedEvent` —— 工作流状态变更（requested/started/finished + approval 信号）
3. `StageStatusChangedEvent` —— 阶段状态变更（queued/started/gate_passed/gate_failed/cancelled + approval 信号）
4. `DecisionRecordedEvent` —— 控制面决策下发
5. `ArtifactPublishedEvent` —— 统一产物发布
6. `ApprovalRequiredEvent` —— 打开 HITL 审批门，携带策略/角色/风险等级上下文
7. `ApprovalResolvedEvent` —— 审批被解决（approved / rejected）
8. `EvalCompletedEvent` —— 评测/基准套件完成，携带指标与门控结果
9. `AttackCompletedEvent` —— 红队/对抗活动完成，携带 ASR 与防遗忘信号（retention/forgetting rate）
10. `LessonPromotedEvent` —— 经验卡从临时观察升级为平台对象
11. `AuditCompletedEvent` —— 外审计完成，驱动 Accept / Refine / Restart
12. `AuditFollowupEvent` —— 对单条审计约束的定向追问与重评（F6 协议）
13. `ImprovementAppliedEvent` —— 元循环改进已应用（可回滚）
14. `AgentStepEvent` —— agent 执行流水单步（工具调用 / 终态）
15. `DebugEvent` —— 调试用事件（隔离单个 stage）
16. `RubricSynthesizedEvent` —— **评分标准已确立**（2026-09-09 新增）：携带 `rubric_id` /
    `source`（synthesized|reviewed）/ `criteria_count` / `review` / `integrity_hash`；
    **其索引必定早于该 run 的首个 `eval_completed`**（标准先于结果，有测试固化）

> `EventType` 枚举共 22 个值。
> `ApprovalRequired` / `ApprovalResolved` 以“专用领域事件 + 工作流/阶段状态变更事件”双写形式存在：前者携带 HITL 业务负载，后者是生命周期信号。
> 契约变更须同步重生成：`python -m safety_auto_research.platform_contracts.export_schemas` 与 `export_typescript`（前端经 `npm run gen:contracts` 同步 `contracts.ts`）。

### 状态流转 contract

当前已落地三类约束：

1. `validate_workflow_status_transition()`
2. `validate_stage_status_transition()`
3. `validate_decision_record_contract()`

其中 `DecisionRecord` 采用规则化 contract：

- `revisit` 必须带 `target_stage`
- `continue` / `exit_success` / `exit_budget` / `exit_converged` 不允许带 `target_stage`

## JSON Schema 导出

导出命令：

```bash
/Users/glennge/work/github/AI_research/.venv/bin/python -m safety_auto_research.platform_contracts.export_schemas
```

默认输出目录：

- `safety_auto_research/platform_contracts/generated/objects/*.json`
- `safety_auto_research/platform_contracts/generated/events/*.json`
- `safety_auto_research/platform_contracts/generated/manifest.json`

这些文件面向：

1. 前端生成类型或表单约束
2. 后端 API 请求/响应校验
3. 执行平面 adapter 的 schema 对齐

## TypeScript 类型与表单约束生成

从同一套 schema 直接生成前端可用的 TypeScript 类型 + zod 表单约束（单一 `.ts` 文件，含枚举、对象、事件）：

```bash
/Users/glennge/work/github/AI_research/.venv/bin/python -m safety_auto_research.platform_contracts.export_typescript
```

默认输出：`safety_auto_research/platform_contracts/generated/typescript/contracts.ts`

特性：

- 枚举 → `z.enum([...])` + `z.infer` 推导类型
- `str | None`、`dict[str, X]`、`list[X]`、`datetime` 等正确映射为 `z.optional()` / `z.record` / `z.array` / `z.string()`
- pydantic 字段约束（`ge` / `le` / `min_length` …）映射为 `.min()` / `.max()`，且总是应用于 `.optional()` 之前
- 生成物为单一事实来源，前端 `import { z } from "zod"` 后直接用，不与后端各自维护类型

## 控制面骨架（control_plane）

`safety_auto_research/control_plane/` 是架构 spec §10 的最小控制面实现，把 `WorkflowRun` / `StageRun` / `DecisionRecord` 接入一个经验证的**状态机服务 + 最小 API**：

- `store.py` —— 进程内仓储（workflow/stage/decision/artifact/lesson/metrics）+ 平台事件日志
- `service.py` —— `ControlPlaneService`：所有写操作经 `platform_contracts.transitions` 校验，并写入对应平台事件
- `api.py` —— FastAPI 应用（创建/启动/取消工作流、阶段状态流转、HITL 审批、决策记录、事件查询；并暴露执行平面派发端点）
- `schemas.py` —— API 请求/响应模型（复用契约枚举）
- `server.py` —— `python -m safety_auto_research.control_plane.server` 以 uvicorn 启动（默认 `127.0.0.1:8000`）

启动与自测：

```bash
/Users/glennge/work/github/AI_research/.venv/bin/python -m safety_auto_research.control_plane.server
/Users/glennge/work/github/AI_research/.venv/bin/python -m unittest safety_auto_research.tests.test_control_plane -v
```

## 执行平面 adapter（execution_plane）

`safety_auto_research/execution_plane/` 把三个事件接到**真实可运行**的 adapter 并形成闭环
（详见 `execution_plane/README.md`）：

- `sdk.py` —— `PlatformSDK`：adapter 唯一接口（`load_object` / `publish_artifact` / `emit_event` / `request_approval` / `record_metric` / `register_lesson`，spec §9.4）
- `registry.py` + `base.py` —— `AdapterRegistry` + `StageExecutor` 抽象
- `executors/` —— `EvalExecutor`(层③→`EvalCompletedEvent`) / `AttackExecutor`(层⑩→`AttackCompletedEvent`) / `LessonExecutor`(层⑧→`LessonPromotedEvent` 回注)
- `decision/router.py` —— `IterationRouter`：事件 → `DecisionRecord`（对齐层⑨ 路由表 R7/R10）
- `orchestrator.py` —— `ClosedLoopOrchestrator`：dispatch + 自动决策 + 评测→红队→经验闭环

API 端点：`POST /workflow-runs/{run_id}/dispatch`、`POST /workflow-runs/{run_id}/closed-loop`、`GET /workflow-runs/{run_id}/lessons`。

## 测试

当前测试文件：

- `safety_auto_research/tests/test_platform_contracts.py`

验证内容包括：

1. 21 个核心对象最小实例化（对象清单以**集合相等**断言冻结，新增/删除对象必须同步本文件与测试）
2. 事件模型实例化（事件清单同样以集合相等冻结）
3. 状态流转校验
4. JSON Schema 导出结构
5. JSON Schema 文件落盘

运行方式：

```bash
/Users/glennge/work/github/AI_research/.venv/bin/python -m unittest safety_auto_research.tests.test_platform_contracts -v
```

## 下一步建议

1. 为每个对象增加更细的字段级 validator，例如 `artifact_ref`、`run_id`、`policy_pack_id` 的引用格式约束。
2. 控制面 API 的请求/响应对象已纳入同一契约层（复用 `platform_contracts` 枚举与对象）；`Artifact` / `LessonCard` 已接入编排（来自 adapter 发布）。可进一步把 `ResearchProgram` / `EvalSuite` / `AttackCampaign` / `ModelVersion` 也接入控制面。
3. ✅ `EvalCompletedEvent` / `AttackCompletedEvent` / `LessonPromotedEvent` 已接到真实执行平面 adapter，形成“评测→决策 / 红队→决策 / 经验→回注”闭环（见 `execution_plane/`）。下一步：把层⑤数据评估清洗也接成 executor（router 已可路由到 ⑤）；在 `AttackExecutor` 检测到 critical ASR 时自动触发 HITL 断点。
4. 用持久化存储（Registry + 事件存储）替换 `store.py` 的进程内实现，进入 spec §6 数据与观测底座。