# safety_auto_research — 产品 / UX / 架构 活动待办

> **提炼自**：`safety_auto_research_深度分析报告.md`（2026-07-29 首轮审查的 18 条"新发现"）
> **提炼日期**：2026-08-07
> **状态说明**：原深度分析报告的**可靠性章节（原 §4，含 C1–C3 / I1–I12 / M1–M9 / R1–R5）已全部过时**——这些缺陷已在 `doc/code_review_STATUS.md`（附录 A/B）中重列并落地修复，**不纳入本待办**。本文件只保留仍未消化、且后续日期审查（聚焦可靠性/并发/RCE/安全）未覆盖的产品、易用性、前端流程、UI、架构债务类条目。
>
> **如何维护**：新发现直接追加；已修复的项把「状态」改为 `✅ 已修` 并注明来源审查。本文件是这些条目的唯一权威处，原深度分析报告仅作历史快照。
>
> **状态复核（2026-08-07 第二轮 · 逐行核对代码）**：本待办相对代码现状明显滞后——**U2 / UI2 / UI6 / F7 / FL2** 已在 08-05/08-06 前端与并发修复中落地；**F4** 多指标能力已在 OpenRSI/OpenMLE 阶段就绪（仅未显式透出）。**本轮新做**：F4 显式透出主指标 + A2 参数解析去重 + UI4 slider 档位。**F1 / F2 / F3 / A1 / A3** 为架构/环境级，标记「待设计 / 环境受限」，不盲改。
>
> **第三轮（2026-08-07 第三轮 · 本会话）**：继续推进 P2 前端项，逐行读代码核对。发现 **F5（实验对比）/ UI3（响应式）/ UI5（空状态）** 早已实现，属 stale backlog，仅补标 `✅ 已修`。**本轮真实新做**：UI1（轮询 loading + 后端不可达非阻塞 banner）、U3（侧边栏搜索 + 状态筛选）、U1（错误卡片加重试 + 后端不可达 warn banner）、U4（agent 模式前置要求 banner）、FL3（第二步取消回第一步保留选择）、FL4（调试按钮真实进度反馈 + 事件流桥接）。F6（审计追问）/ A4（端点集成测试）为架构/测试级，标记待设计/待处理，不盲改。
>
> **第五轮（2026-08-07 第五轮 · 本会话）**：推进 **F1 / A1 / A3**。**F1 复核为 stale**——SSE 全链路（progress_bus 单例 + `/stream` 端点 + 5 处 `progress_callback` + 前端 `useSSE`）早已落地，仅补标。**A1 + A3 本轮真实落地**：`api.py` 由 1975 行拆为 **73 行薄装配器** + `control_plane/deps.py`（DI 容器 `ControlPlaneDeps`）+ `control_plane/routers/` 7 个领域 router（57 个 handler 逐字搬运，本地别名绑定保证零逻辑改动）。零回归证明：重构前后 `openapi()` 的 52 条 path 与 components **逐字相等**；新增 `tests/test_control_plane_structure.py`（9 用例 + 15 subtests）冻结 HTTP 表面并防止 `api.py` 退化回巨型文件；25 个测试文件全量回归 287 例全绿。设计文档 `doc/design_notes.md`（§1）。
>
> **第四轮（2026-08-07 第四轮 · 本会话）**：**F6 设计完成**——见 `doc/design_notes.md`（§2）：审计追问交互协议（新增 `AuditFollowupEvent` + `AuditReport.followups`；`POST /workflow-runs/{run_id}/audits/{audit_id}/followup` 端点；复用 curated audit_input 保持外层独立性；前端 AuditBoard 内联追问表单）。**A4 已补**——新增 `tests/test_api_endpoints.py`（15 个用例，TestClient 端到端驱动 api.py 核心端点：WorkflowRun 生命周期 / DecisionRecord / 审计·改进观测端点 / agent protocol / benchmark 目录 / debug 端点发事件）。A4 原「端点集成测试为零」已不实（test_research_records 等早有覆盖），本轮补齐核心端点级集成测试。
>
> **第六轮（2026-08-10 · 本会话）**：F3 + F2 收尾。**F3** 在 08-07 已落地的本地 Docker 硬隔离 + run_capability 推广基础上，补**前端 launch 流程隔离提示 UI**——后端 `/benchmark-tasks` 与 `launch_benchmark_task` 透传 `sandbox_isolation` 4 级信号（`none`/`container-hard`/`container-soft`/`host`），前端 `BenchmarkCatalog`/`NewResearch` 显示 🐳 容器隔离徽标 + step-2 启动横幅，用户可明确看到「该任务将以容器隔离方式执行」。**F2 数据瓶颈突破**：`text_classification` 作为首个非 kaggle 真跑模态，用真实 Stanford SST2 数据（8000 行）在沙箱内 TF-IDF+logreg 跑通（f1_macro=0.8116，隔离 data_ro/net_blocked 全过），固化 `text_cls_sandbox` 能力 + 专用路由 + 回归测试；image/audio/LLM/embedding 仍受 torch/GPU/权重限制。设计见 `doc/design_notes.md` §3。

---

## 一、功能完整性（F1–F7）

| # | 问题 | 优先级 | 详情 | 状态 |
|---|------|--------|------|------|
| **F1** | 双循环进度推送缺失 | P1 | 双循环在后台线程运行，前端仅靠 3 秒轮询。无 WebSocket/SSE 实时推送，用户看不到实时进展，调试模式完全靠 polling events 表凑。 | ✅ 已修（stale：SSE 早已全链路落地——`control_plane/progress_bus.py` 跨线程 `ProgressBus` 单例；`GET /workflow-runs/{run_id}/stream` 返回 `text/event-stream`；5 处 `progress_callback=_progress_bus.emit`（dual-loop / evolution / program-evolution / MEA / full-experiment）；前端 `useSSE.ts` + `RunDashboard.tsx`。复核见 `doc/design_notes.md`（§1 附录）） |
| **F2** | 仅 2 个平台原生任务可实跑 | P1 | 18 个内置任务中仅 titanic/spaceship 可实跑，其余 16 个全是 tracked-only 或依赖 agent 模式。任务目录规模与实际能力严重不匹配。 | 部分解（F3 本地 Docker 硬隔离已落地：kaggle 类 + text_classification（SST2，沙箱内 f1=0.8116，隔离三项全过）已真跑；其余 image/audio/LLM/embedding 受 torch/GPU/数据可得性限制，沙箱仅接诚实失败路径；设计见 `doc/design_notes.md` §3「F2 数据瓶颈突破」） |
| **F3** | agent 模式因无 Docker 对 16 个任务实质不可用 | P1 | 非原生任务标「agent 模式执行」，但需外部 Codex/ClaudeCode CLI，且任务原有 docker/Arbor 依赖被剥离后只剩纯文本 prompt，等同于空跑。 | ✅ 已修（本地 Colima/Docker 硬隔离 + 已推广到 `run_capability` 的 agent 模式：`execution_plane/capabilities/sandbox_executor.py` 的 `SandboxResearchExecutor` 在 agent 调用 `run_capability` 时把研究命令放进一次性容器（`scripts/build_agent_sandbox.sh`）真跑并回写真实 `EvalCompletedEvent`；新增 `kaggle_eval_sandbox`/`run_research_sandbox` 能力，16 个非原生任务经 `research_cmd` 接入容器内真跑（无脚本则诚实失败，不再静默空跑）；`run_f3_sandbox_demo.py` 端到端验证 titanic/spaceship 隔离三项全过；`tests/test_capabilities.py` 加 9 例沙箱回归；设计见 `doc/design_notes.md` §3；前端 launch 流程已加 🐳 容器隔离徽标 + step-2 启动横幅，透传 `sandbox_isolation` 4 级信号，用户可明确看到「该任务将以容器隔离方式执行」） |
| **F4** | 评测指标单一 | P1 | kaggle_eval 只做 accuracy + cv，不支持 F1/AUC/LogLoss 等常用表格分类指标；执行器硬编码 accuracy。 | ✅ 已修（`_SCORERS` 支持 accuracy/f1/precision/recall/roc_auc/log_loss，按 `eval_metric` 选主指标；事件 metrics 按指标名携带主指标值，artifact + record_metric 带 eval_metric 名） |
| **F5** | 缺少实验对比视图 | P2 | 多次 run 之间无法对比——看不到同任务下不同模型/特征的指标趋势图；榜单只有 top-3，缺完整指标历史。 | ✅ 已修（CompareRuns.tsx 已实现多 run 指标对比：下拉选择 + 柱状图 + 明细表 + 排序；App.tsx 有「📊 实验对比」入口，空状态「暂无已完成的 run」；stale backlog，代码早已具备） |
| **F6** | 审计只能打分不能追问 | P2 | 外循环审计产生 unresolved claims 后只能 refine/restart，不能逐条追问。 | ✅ 已修（设计 `doc/design_notes.md`（§2）；落地：`AuditFollowupEvent` + `EventType.AUDIT_FOLLOWUP` + `AuditReport.followups`、`audit_executor.evaluate_constraint` 单约束重评 helper、`POST/GET /workflow-runs/{id}/audits/{audit_id}/followup(s)`、前端 AuditBoard 逐条内联追问表单 + 追问历史药丸；`tests/test_audit_followup.py` 8 例全绿，原审计事件保持不可变） |
| **F7** | 缺少 run 中止/暂停 UI 能力 | P2 | 后端 daemon 线程跑双循环，`cancel_workflow_run` 只改状态不杀线程，取消后仍在后台消耗资源直到自然结束。 | ✅ 已修（`cancel_workflow_run` 置 `cancel_event`；orchestrator 4 个检查点 `is_set()` 即停；前端取消按钮 08-05 已接入） |

## 二、易用性（U1–U4）

| # | 问题 | 优先级 | 详情 | 状态 |
|---|------|--------|------|------|
| **U1** | 错误信息不可操作 | P2 | 所有错误都是一个红色 `card error` 显示原始异常，无重试/返回上一步/修复建议。 | ✅ 已修（App.tsx 错误卡片加「重试」按钮；后端不可达时改为非阻塞 warn banner + 立即重试；ErrorBoundary/Toast 已于 08-05 接入；NewResearch 启动失败显具体原因） |
| **U2** | "预算耗尽"术语不友好 | P2 | `exited_budget` 对非技术用户不友好，应显示为"已完成（达到最大迭代轮数）"并附解释。 | ✅ 已修（client.ts `STATUS_LABEL`: `exited_budget` → "已完成 · 已达最大轮数"） |
| **U3** | 侧边栏 run 列表缺少搜索/排序 | P2 | 随 run 数量增长，分组+折叠不够用——无搜索框、无按状态筛选、无按时间排序。 | ✅ 已修（App.tsx 侧边栏加搜索框 + 状态筛选下拉；分组按时间排序原有；筛选在分组前生效，空态区分"加载中…/无匹配记录/暂无记录"） |
| **U4** | 启动失败的 agent 模式缺少引导 | P2 | 选了 agent 模式但没配 `AGENT_COMMAND` → 启动失败显示一行错误，应前置检查+引导链接。 | ✅ 已修（NewResearch.tsx 一旦选 agent 模式即在配置区顶部显示前置要求 banner：需接入 Codex/ClaudeCode/AGENT_COMMAND，否则启动明确失败并终止；附启动失败具体原因） |

## 三、前端流程合理性（FL1–FL4）

| # | 问题 | 优先级 | 详情 | 状态 |
|---|------|--------|------|------|
| **FL1** | 研究设定参数散落在 2 个独立对象 | P1 | `LaunchConfig` 与 `InnerLoopConfig` 字段重叠（model, fe），`handleLaunch` 又做一层展平映射——同一 model 出现在 config/inner/payload 三层，极易不一致。 | ✅ 已修（不一致风险闭环：`inner_loop` 为唯一真源，legacy `model/fe` 仅作向后兼容兜底，后端优先用 `inner_loop`；完整 schema 合并为破坏性变更，待设计） |
| **FL2** | 「立即运行完整实验」checkbox 语义不清楚 | P2 | 勾选=立即启动双循环；不勾选=只创建 REQUESTED run 需手动启动。新用户看不懂后果。 | ✅ 已修（NewResearch.tsx checkbox 下方随勾选状态显示不同后果说明） |
| **FL3** | 取消按钮行为不一致 | P2 | 新建向导第一步/第二步取消都回欢迎页，用户可能期望第二步取消回到第一步（保留已选任务）。 | ✅ 已修（NewResearch.tsx 向导第二步「取消」改为「返回选择」，回到第一步并保留已选任务；完全取消仍可在第一步点取消） |
| **FL4** | 调试面板与仪表盘状态脱节 | P2 | 调试在 debug 子标签触发，结果需切到 events 标签才看得到；点"运行内循环调试"后按钮变"运行中…"但无进度反馈。 | ✅ 已修（RunDashboard.tsx 调试按钮保持"运行中…"直到 `debug_result` 事件真正到达才释放；面板加"查看完整事件流 →"跳转 events 标签桥接脱节；运行时显示"调试运行中…结果随事件流自动出现"） |

## 四、UI 界面（UI1–UI6）

| # | 问题 | 优先级 | 详情 | 状态 |
|---|------|--------|------|------|
| **UI1** | 侧边栏 4 秒轮询无 loading 状态 | P2 | `refreshRuns` 后台静默拉取，无 loading 提示；后端挂了列表停在上次成功数据上毫无提示。 | ✅ 已修（App.tsx `refreshRuns` 区分首加载与轮询失败：首加载失败显硬错误+重试；后续轮询失败显非阻塞 warn banner「显示上次缓存·自动重试」+立即重试；run-list 空态显「加载中…」） |
| **UI2** | `DECISION_LABEL` 不完整 | P2 | 缺 `audit_refine`/`audit_restart`/`continue` 等 router 可能产出的裁决类型。 | ✅ 已修（client.ts `DECISION_LABEL` 已含 `audit_refine`/`audit_restart`/`continue`） |
| **UI3** | 缺少响应式断点适配 | P2 | 侧边栏窄屏固定 260px 无折叠按钮，笔记本半屏/平板横屏占太多空间。 | ✅ 已修（styles.css 已有 `@media (max-width:880px)` 折叠侧边栏、`<600px` 隐藏侧边栏的响应式规则；stale backlog，代码早已具备） |
| **UI4** | 表单输入无即时反馈 | P2 | 审计严格度 slider 改值只显示数字，无文字说明（如"中等严格 (0.75)"）。 | ✅ 已修（audit_threshold slider 数值旁加档位 宽松/中等/严格） |
| **UI5** | 缺少空状态设计 | P2 | 假设树/改进时间线/审计记录为空时只不渲染内容，用户以为加载失败。 | ✅ 已修（HypothesisTree/ImprovementTimeline/AuditBoard 均已内置空状态组件；stale backlog，代码早已具备） |
| **UI6** | DualLoopLive 判决标签与 Dashboard 不一致 | P2 | DualLoopLive 用自己硬编码的中文字符串映射，Dashboard 用 `DECISION_LABEL` 常量，同一 `exit_success` 两处显示文字不同。 | ✅ 已修（DualLoopLive.tsx 改用共享 `DECISION_LABEL`/`STATUS_LABEL`） |

## 五、架构债务（A1–A4）

| # | 问题 | 优先级 | 详情 | 状态 |
|---|------|--------|------|------|
| **A1** | 双循环 driver 与 API 层耦合 | P1 | `api.py` 的 `launch_benchmark_task` 内嵌 200+ 行参数解析+orchestrator builder+线程启动；`api.py` 已 1279 行（后增至 1975 行），所有端点+驱动逻辑塞在一个文件。应拆到 `orchestrator.py` 或独立 `driver.py`。 | ✅ 已修（`api.py` 1975 → **73 行**薄装配器；57 个 handler 按领域逐字搬入 `control_plane/routers/` 7 个模块；driver 逻辑（`build_run_ctx`/`spawn_full_experiment`/`make_agent_orchestrator`/`resolve_eval_params`）下沉 `deps.py`。OpenAPI 52 path 与 components **逐字相等**；设计见 `doc/design_notes.md`（§1）。余项：`launch_benchmark_task` 247 行仍可再下沉） |
| **A2** | `api.py` 参数解析逻辑重复 | P1 | `launch_benchmark_task`（L594-855）与 `_build_run_ctx`（L967-1061）约 60 行几乎相同的参数解析（preset/fe 规范化/threshold 默认/custom task data_dir），变更需同时改两处。 | ✅ 已修（抽取 `_resolve_eval_params` 单一来源，`launch_benchmark_task` / `_build_run_ctx` 两处调用，消除双改风险） |
| **A3** | 缺少依赖注入容器 | P2 | `create_app` 手动组装 svc/state_store/orchestrator/agent_command/per-run orchestrator，依赖链变长，加新能力更难维护。 | ✅ 已修（新增 `control_plane/deps.py`：`ControlPlaneDeps` 聚合 svc/state_store/orchestrator/run_cancel_events/shutdown_event/progress_bus + 三个 helper 方法；`build_deps(service)` 复刻原装配顺序；router 用本地别名绑定使 handler 体零改动；`app.state.deps` 暴露容器供测试注入与内省） |
| **A4** | 测试覆盖不均衡 | P2 | 81 个测试中执行平面较全，但 `api.py` 端点级集成测试为零（全靠前端手工测）；`audit_executor.py`/`kaggle_eval_executor.py` 缺独立单测。 | ✅ 已补（`tests/test_api_endpoints.py`：15 用例 TestClient 端到端驱动 api.py 核心端点——WorkflowRun 生命周期 / DecisionRecord / 审计·改进观测端点 / agent protocol / benchmark 目录 / debug 端点发事件；全绿。注：A4 原「端点集成测试为零」已不实，test_research_records 等早有覆盖，本轮补齐核心端点级） |

---

## 附：已从本待办移除的可靠性条目（已在日期审查中修复）

- **原 §4.1 已知严重缺陷** C1–C3 / I1–I12 / M1–M9（来自已删除的 `CODE_REVIEW.md`）：全部在 `doc/code_review_STATUS.md`（附录 A/B）重列并落地修复。
- **原 §4.2 新发现可靠性** R1–R5（daemon 线程 SIGTERM 损坏 / 协作锁排队 / 系统性 except 吞异常 / start 冲突吞异常 / collab_pause race）：R1、R3 已在 08-05/08-06 修复；R2/R4/R5 为低概率/非崩溃，留作后续观察。
- **原 §5.3 前端已知缺陷** I8–I12（外循环轨迹 undefined / API 响应无运行时校验 / 拒绝无确认 / 轮询静默 catch / 多路径上传覆盖）：已在 08-05 前端健壮性修复（空安全 / 取消按钮 / ErrorBoundary·Toast / AbortController 超时）中处理。
