# safety_auto_research 深度分析报告

> **分析日期**: 2026-07-29 | **分析范围**: 全项目（Python 后端 + TypeScript 前端）
> **测试基线**: 81/81 passed | **前端 tsc**: 0 errors

---

## 一、执行摘要

**safety_auto_research** 是一个架构设计相当扎实的 AI 安全自动化研究平台。双循环（内循环执行 → 外循环审计 → 元循环进化）的核心设计在概念上非常先进，代码整体结构清晰、分层合理。81 个单元测试全部通过，前端类型检查零错误。

然而，经过深度分析，项目在以下五个维度存在 **43 个可优化点**（合并 CODE_REVIEW.md 已知 25 条 + 新发现 18 条）：

| 维度 | 已知缺陷 | 新发现 | 合计 |
|------|---------|--------|------|
| 功能完整性 | 2 | 5 | 7 |
| 易用性 | 0 | 4 | 4 |
| 可靠性 | 10 | 5 | 15 |
| 前端流程合理性 | 4 | 3 | 7 |
| UI 界面 | 6 | 4 | 10 |

**P0 立即修复**: 5 项 | **P1 近期修复**: 19 项 | **P2 后续迭代**: 19 项

---

## 二、功能完整性分析

### 2.1 ✅ 已实现的核心功能（设计优良）

- **双循环驱动**：内循环 (kaggle_eval / agent open goal) → 外循环审计 (layer_11) → 元循环进化 (layer_09)，逻辑完整，隔离不变量设计得当
- **多竞赛泛化**：kaggle_eval 通过 preset 机制支持 titanic/spaceship/custom，数据管道解耦
- **Agent 集成**：RemoteAgentHarness + 多种 Transport（Codex/ClaudeCode/Subprocess/HTTP），设计灵活
- **研究榜单与记录**：自动 capture_run_record，per-task best-3 + 全局榜 + 一键复现
- **自定义任务注册**：schema 驱动的 8 种任务类型，前端动态表单渲染 + 实时校验
- **10 层基础设施**：文献检索 → Idea 生成 → 评测 → 实验设计 → 数据清洗 → 代码开发 → 实验执行 → 结果分析 → 自进化 → 对抗数据生成，能力目录完备
- **HITL 协作模式**：自主/每步确认/外循环确认三种模式，支持参数实时调整
- **调试面板**：内/外循环单独调试，不污染研究记录

### 2.2 🔧 功能不足点

| # | 问题 | 严重度 | 详情 |
|---|------|--------|------|
| **F1** | 双循环进度推送缺失 | P1 | 双循环在后台线程运行，前端只能通过 3 秒轮询获取状态。**无 WebSocket/SSE 实时推送**，用户无法看到实时进展。调试模式下更是完全靠 polling events 表来凑。 |
| **F2** | 仅 2 个平台原生任务可实跑 | P1 | 18 个内置任务中仅 titanic/spaceship 可实跑，其余 16 个全是 "tracked-only" 或依赖 agent 模式。平台实际能力与任务目录规模严重不匹配——用户选了任务却发现跑不了，体验很差。 |
| **F3** | agent 模式因无 Docker 而对 16 个任务实质不可用 | P1 | 非原生任务虽标「agent 模式执行」，但 agent 模式需要外部 Codex/ClaudeCode CLI，且任务原有 docker/Arbor 依赖被剥离后只剩"目标描述"——agent 拿到的是一个纯文本 prompt，没有真实执行环境。这导致 16 个任务的"agent 模式"等同于空跑。 |
| **F4** | 评测指标单一 | P1 | kaggle_eval 只做 accuracy + cv，不支持 F1/AUC/LogLoss 等常用表格分类指标。自定义任务虽然有 metric 白名单，但 kaggle_eval 执行器硬编码了 accuracy。 |
| **F5** | 缺少实验对比视图 | P2 | 多次 run 之间无法对比——看不到同一任务下不同模型/特征的指标趋势图。榜单只有 top-3，缺少每个 run 的完整指标历史。 |
| **F6** | 审计只能打分不能追问 | P2 | 外循环审计产生 unresolved claims 后只能决定 refine/restart，不能逐条追问——"这个 claim 为什么 unresolved？下一步应该做什么？" 这个信息丢失了。 |
| **F7** | 缺少 run 中止/暂停 UI 能力 | P2 | 后端起了一个 daemon 线程跑双循环，`cancel_workflow_run` 只是改状态但**不杀线程**。用户在前端点取消后 run 继续在后台消耗资源，直到自然结束。 |

---

## 三、易用性分析

### 3.1 ✅ 做得好的

- **新建研究向导**两步流程（选任务 → 配置参数）直观清晰
- **参数说明详细**：每个配置项都有 help text，审计严格度用 slider 可视化
- **调试 vs 完整实验分离**：可先调试再启动，设计贴心
- **协作模式说明**：三种模式的区别解释清楚

### 3.2 🔧 易用性不足

| # | 问题 | 严重度 | 详情 |
|---|------|--------|------|
| **U1** | 错误信息不可操作 | P2 | 所有错误都是一个红色的 `card error` 组件显示原始异常信息，没有"重试"按钮、没有"返回上一步"、没有建议的修复动作。用户看到错误只能手动回退。 |
| **U2** | "预算耗尽"术语不友好 | P2 | `exited_budget` 对非技术用户不够友好。应该显示为"已完成（达到最大迭代轮数）"并附带解释。 |
| **U3** | 侧边栏 run 列表缺少搜索/排序 | P2 | 随着 run 数量增长，侧边栏分组 + 折叠不够用——没有搜索框、没有按状态筛选、没有按时间排序的选项。 |
| **U4** | 启动失败的 agent 模式缺少引导 | P2 | 用户选了 agent 模式但没有配置 AGENT_COMMAND → 启动失败 → 显示一行错误信息。应该有前置检查 + 引导链接到配置文档。 |

---

## 四、可靠性分析

### 4.1 已知严重缺陷（来自 CODE_REVIEW.md 合并）

| # | 位置 | 严重度 | 问题 | 状态 |
|---|------|--------|------|------|
| **C1** | `api.py:844-850` | 🚨 Critical | 双层 `except Exception: pass` 吞没 `_drive()` 所有异常，run 永久卡死 | 未修复 |
| **C2** | `store_tree.py` 9处 | 🚨 Critical | SQLite 连接未用 context manager，异常时连接泄漏 | 未修复 |
| **C3** | `DualLoopLive.tsx:43` | 🚨 Critical | `maxOuter` 硬编码为 3，忽略用户配置 | 未修复 |
| **I1** | `store.py:56-58` | ⚠️ Important | corrupt 文件静默吞掉，所有历史数据不可逆丢失 | 未修复 |
| **I2** | `service.py:74,413,458` | ⚠️ Important | `_open_approvals` 不持久化，重启后审批丢失 | 未修复 |
| **I3** | `store.py:109-188` | ⚠️ Important | 读方法不加锁，read-while-write 可能读到不一致数据 | 未修复 |
| **I4** | `kaggle_eval_executor.py:80` | ⚠️ Important | `cv_folds` 未校验上限 vs 最小类别样本数 | 未修复 |
| **I5** | `kaggle_eval_executor.py:253` | ⚠️ Important | `df[target]` 不校验列名存在性 | 未修复 |
| **I7** | `orchestrator.py:408-412` | ⚠️ Important | `metrics.get("accuracy")` 假设是 dict | 未修复 |
| **M3** | `agent/transport.py:177-185` | ⚠️ Important | `proc.wait()` 无超时 → 子进程僵死 → 线程永久阻塞 | 未修复 |

### 4.2 新发现的可靠性问题

| # | 位置 | 严重度 | 问题 | 详情 |
|---|------|--------|------|------|
| **R1** | `api.py:848` | 🚨 Critical | `threading.Thread(daemon=True).start()` — daemon 线程在进程退出时强制终止，**不保证 finish**。如果 `run_dual_loop` 正在写数据库/JSON，会留下 corrupt 状态。应改用非 daemon 线程 + shutdown event 优雅退出。 | daemon 线程被 SIGTERM 杀掉时 `_persist()` 可能写一半，导致 store JSON 损坏，配合 I1 静默吞掉 → 永久数据丢失。 |
| **R2** | `orchestrator.py:59-60` | ⚠️ Important | `_COLLAB_PAUSES` 是模块级 `dict`，多 run 并发协作时共享同一把 `_COLLAB_LOCK`。如果一个 run 的 approve 处理耗时较长（比如前端提交了大量调整参数），会 block 其他 run 的协作暂停。 | 高频使用场景下产生排队效应。 |
| **R3** | `api.py:819-848`, `api.py:1070-1094`, `api.py:1117-1200` | ⚠️ Important | 三个不同的 `_drive` / `_debug_thread` 函数中都有 `except Exception: pass` 或类似吞异常模式。**C1 不是孤立问题，而是系统性的异常处理缺陷**。 | 任何一个未被捕获的异常都会导致线程静默死亡 + run 僵尸。 |
| **R4** | `api.py:422-423` | 💡 Minor | `except (ConflictError, ValueError): pass` 在双循环启动时吞掉 start 冲突。如果 run 已经在 terminal 状态但代码走到了这里，后续 `run_dual_loop` 仍会尝试创建 stage run — 在 terminal workflow 上创建 stage run 是语义错误。 | 低概率，但一旦发生就是静默异常。 |
| **R5** | `orchestrator.py:597-633` | 💡 Minor | `_collab_pause` 的 while 循环 `evt.wait(timeout=2)` — 如果 HTTP 端 resolve 在 `wait` 刚返回 false 到下一次 `get_workflow_run` 之间发生，会多等 2 秒才退出。虽不会死锁但有延迟。 | race condition window 很小，但在高负载下可能累积延迟。 |

---

## 五、前端流程合理性分析

### 5.1 ✅ 做得好的

- **App 路由设计**：`welcome → new → run` 的线性流程 + 侧边栏快速导航，信息架构清晰
- **任务目录 → 新建研究的跳转**：选任务后预填 + 直接跳到步骤 2，体验流畅
- **双循环进度可视化**：内/外循环事件独立卡片，可折叠详情
- **8 个仪表盘子标签**：信息分层合理（总览/调试/审计/假设树/改进/审批/Agent流水/事件）
- **自定义任务注册的 schema 驱动表单**：选类型 → 动态渲染字段 → 实时校验，设计非常现代

### 5.2 🔧 流程问题

| # | 问题 | 严重度 | 详情 |
|---|------|--------|------|
| **FL1** | 研究设定参数**散落在 2 个独立对象**中 | P1 | `LaunchConfig`（audit_threshold, max_outer_iters, model, fe）和 `InnerLoopConfig`（mode, model, fe, cv_folds...）有字段重叠（model, fe），且 `handleLaunch` 里又做了一层展平映射。**同一个 model 出现在三个层级**：`config.model`、`inner.model`、`payload.model`。——极易造成不一致。 |
| **FL2** | 「立即运行完整实验」checkbox 的默认语义不清楚 | P2 | 勾选=立即启动双循环；不勾选=只创建 REQUESTED run，需手动到仪表盘点"开始完整自主实验"。**新用户看不懂这个 checkbox 的后果**。 |
| **FL3** | 取消按钮行为不一致 | P2 | 在新建向导第一步点"取消"回到欢迎页；第二步点"取消"也是回到欢迎页。用户可能期望第二步取消时回到第一步（保留已选任务）。 |
| **FL4** | 调试面板与仪表盘状态脱节 | P2 | 调试在 `debug` 子标签里触发，但调试结果需要**切换到 events 标签**才能看到详情——用户点击"运行内循环调试"后，按钮变"运行中…"但没有进度反馈，不知道自己该做什么。 |

### 5.3 已知前端缺陷（来自 CODE_REVIEW.md）

| # | 位置 | 严重度 | 问题 |
|---|------|--------|------|
| I8 | `DualLoopLive.tsx:153-155` | ⚠️ Important | 外循环轨迹访问不存在的 `e.event_type` → 全部 `undefined` |
| I9 | `client.ts:7,17` | ⚠️ Important | 裸 `as T` 无运行时校验 → 后端变更即前端崩溃 |
| I10 | `ApprovalConsole.tsx:56-69` | ⚠️ Important | 拒绝操作无确认对话框 |
| I11 | 7 个视图 poll catch | ⚠️ Important | 所有轮询 `catch {/*ignore*/}` → 网络故障无声 |
| I12 | `RegisterTask.tsx:216-348` | ⚠️ Important | 多路径字段上传写入同一 key → 覆盖 |

---

## 六、UI 界面分析

### 6.1 ✅ 做得好的

- **暗色/亮色主题自适应**：通过 `prefers-color-scheme` 自动切换，CSS 变量设计规范
- **状态可视化一致**：pill（运行中/成功/失败）+ dot（侧边栏状态点）贯穿全局
- **任务卡片 hover tooltip**：数据/模型/基线一目了然
- **假设树可视化**：branch + status + insight 结构化展示
- **CSS 变量体系**：`--bg / --panel / --border / --text / --accent` 等 14 个变量，修改主题只需改 `:root`

### 6.2 🔧 UI 问题

| # | 问题 | 严重度 | 详情 |
|---|------|--------|------|
| **UI1** | 侧边栏 4 秒轮询无 loading 状态 | P2 | `refreshRuns` 在后台静默拉取，用户不知道列表是否在更新。如果后端挂了，列表会停留在上次成功获取的旧数据上，毫无提示。 |
| **UI2** | `DECISION_LABEL` 不完整 | P2 | 缺少 `audit_refine`、`audit_restart`、`continue` 等 router 可能产出的裁决类型。`EXIT_SUCCESS` 和 `continue` 在 `DECISION_CLASS` 中已补但只在 RunDashboard 中，DualLoopLive 中硬编码了自己的映射。 |
| **UI3** | 缺少响应式断点适配 | P2 | `.grid2` 在 880px 以下变为单列，但侧边栏在窄屏下宽度固定 260px，没有折叠按钮。——在笔记本半屏或平板横屏场景下占太多空间。 |
| **UI4** | 表单输入无即时反馈 | P2 | 审计严格度 slider 改了值，但没有显示对应的文字说明（如"中等严格 (0.75)"）。只有数字显示。 |
| **UI5** | 缺少空状态设计 | P2 | 研究记录列表为空时显示"暂无记录"，但假设树为空、改进时间线为空、审计记录为空时只是不渲染内容，用户以为加载失败。 |
| **UI6** | DualLoopLive 的判决标签与 Dashboard 不一致 | P2 | DualLoopLive 使用自己的 `decisionLabel` 映射（硬编码中文字符串），Dashboard 使用 `DECISION_LABEL` 常量。同样一个 `exit_success` 在两个视图中显示的文字不一样。 |

---

## 七、架构与代码质量补充观察

### 7.1 ✅ 架构优点

- **三层分离清晰**：`platform_contracts`（对象模型） → `control_plane`（状态机+API） → `execution_plane`（编排+执行），职责分明
- **Agent 护栏设计**：`OUTER_LOOP_RESERVED_CAPS` 阻止内循环调用审计能力，科学隔离有效
- **Python→TypeScript Schema 自动导出**：`export_typescript.py` 生成 `contracts.ts`（Zod schema），减少前后端不一致
- **可插拔评测**：`eval_runner.py` 支持 LLM judge 和 heuristic fallback，且被设计为无 LLM 依赖的兜底

### 7.2 🔧 架构债务

| # | 问题 | 严重度 | 详情 |
|---|------|--------|------|
| **A1** | 双循环 driver 与 API 层耦合 | P1 | `api.py` 的 `launch_benchmark_task` 内嵌了 200+ 行的参数解析 + orchestrator builder + 线程启动逻辑。这应该是 `orchestrator.py` 或独立 `driver.py` 的责任。当前 `api.py` 已经是 1279 行——所有端点 + 驱动逻辑全部塞在一个文件里。 |
| **A2** | `api.py` 中的大量参数解析逻辑重复 | P1 | `launch_benchmark_task`（L594-855）和 `_build_run_ctx`（L967-1061）有 **约 60 行几乎相同的参数解析代码**（preset 解析、fe_value 规范化、threshold 默认值、custom task data_dir 处理）。任何参数逻辑变更需要同时改两处。 |
| **A3** | 缺少依赖注入容器 | P2 | `api.py` 的 `create_app` 手动组装了 svc / state_store / orchestrator / agent_command / per-run orchestrator，依赖关系链越来越长。后续加新能力（如 metric collector / notification）会更难维护。 |
| **A4** | 测试覆盖不均衡 | P2 | 81 个测试中，执行平面（orchestrator/agent/capabilities）测试较全，但 `api.py` 的端点级集成测试为零——所有 API 端点都是"手工通过前端测试"。`audit_executor.py` 和 `kaggle_eval_executor.py` 也缺少独立单元测试。 |

---

## 八、优先级排序与修复路线图

### 🔴 P0 — 立即修复（5 项，阻塞生产可用性）

| # | 问题 | 影响 |
|---|------|------|
| C1 | `api.py` 异常吞没 → run 僵尸 | run 失败后永久卡 running |
| C2 | SQLite 连接泄漏 | 高并发时服务不可用 |
| C3 | `DualLoopLive.tsx` 硬编码 maxOuter | 用户看到错误进度 |
| R1 | daemon 线程被 SIGTERM 强制杀 → store 损坏 | 进程重启后数据丢失 |
| I5 | `df[target]` 不校验列名 → KeyError 被 C1 吞没 | 用户拼错 target 即 run 僵尸 |

**P0 修复后的状态**：系统在异常场景下至少不会静默失败 + 数据不会损坏。

### 🟡 P1 — 近期修复（10 项，提升可用性和数据正确性）

| # | 问题 | 类型 |
|---|------|------|
| I1 | corrupt store 静默吞掉 | 可靠性 |
| I2 | 审批不持久化 → 重启丢失 | 可靠性 |
| I3 | 读方法不加锁 | 可靠性 |
| I4 | cv_folds 无上限校验 | 可靠性 |
| I7 | metrics dict 类型假设 | 可靠性 |
| I8 | 外循环轨迹 undefined | 前端 |
| I9 | API 响应无运行时校验 | 前端 |
| I10 | 拒绝无确认对话框 | 前端 |
| FL1 | LaunchConfig/InnerLoopConfig 参数三层重复 | 前端流程 |
| F1 | 缺少 WebSocket/SSE 实时进度推送 | 功能 |

### 🟢 P2 — 后续迭代（12 项，改善体验和完善功能）

| # | 问题 | 类型 |
|---|------|------|
| F2-F7 | 功能扩展（更多可执行任务、多指标评测、实验对比、线程中止等）| 功能 |
| U1-U4 | 易用性改进（错误恢复、侧边栏搜索、agent 引导等）| 易用性 |
| FL2-FL4 | 流程微调 | 前端流程 |
| UI1-UI6 | UI 细节打磨 | UI 界面 |
| A1-A4 | 架构重构（API 拆解、参数重复消除、DI 容器、测试补充）| 架构 |

---

## 九、总结

`safety_auto_research` 的**核心理念和架构设计非常出色**——双循环自进化是 AI 安全研究中极具前瞻性的范式。81 个测试全部通过说明基础逻辑是稳固的。

当前最大的问题是**异常处理链的断裂**（P0 级别），表现为：
1. 后台线程吞掉所有异常 → run 不可观测地死亡
2. SQLite 连接未正确释放 → 资源泄漏
3. daemon 线程被强制终止 → 持久化数据损坏

这三个问题加在一起形成了一个致命组合：**一个异常 → run 僵尸 + 数据损坏 + 用户完全不知情**。

其次是**功能广度 vs 深度不匹配**：18 个任务中仅 2 个可真正运行，其余是"纸上谈兵"。建议优先增加至少 3-5 个可实跑的公开数据集任务（如 Adult Census、Boston Housing、Wine Quality 等），让平台的实际价值立即可见。

前端方面，**缺少 WebSocket 实时推送**是最大的体验瓶颈——当前的 3 秒轮询 + 调试结果靠读 events 表的方式，在双循环运行中完全看不到实时进度。这是"能用"和"好用"之间的关键差距。

---

*报告结束。建议按 P0 → P1 → P2 顺序分三阶段推进修复和优化。*
