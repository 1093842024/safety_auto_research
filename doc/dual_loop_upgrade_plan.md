# 双循环架构升级方案：内部研究 → 外部审计 → 递归改进

> 目标：把当前 `safety_auto_research` 从"单环（agent 线性研究 + 评测→红队→经验）"升级为
> **内部研究（Inner Loop）→ 外部审计（Outer Audit）→ 递归改进（Recursive Improvement）** 的双循环架构，
> 系统性提升自主研究的质量、可验证性与跨轮累积能力。
>
> 核心论文：**AREX** (arXiv:2607.21461, "Towards a Recursively Self-Improving Agent for Deep Research")。
> 借鉴：Arbor (2606.11926)、Bilevel Autoresearch、AI-Scientist v2、递归自改进综述 (2607.07663)、验证器层级、HITL 治理。

---

## 0. 为什么是双循环（来自 AREX 的核心洞察）

AREX 的出发点是 **发现—验证不对称（discovery–verification asymmetry）**：

> 找到满足多重约束的答案代价很高，但**验证**一个候选答案通常可以拆成可逐个检查的约束（constraint-wise checks）。

由此推导：研究 agent 不该只是"搜得更久"，而应该**用验证结果反哺搜索**——交替进行：

- **内循环（inner research loop）**：收集证据、构造一个"临时答案" `r = (y, E, s)`（答案、证据、0–100 置信度）。
- **外循环（outer self-improvement loop）**：对答案做**约束级审计**，识别未解决的声明，并据此启动**针对性跟进研究**，而不是重头再来。

AREX 用置信度 `s` + 轨迹可恢复性 `v` 做三选一决策：**Accept / Refine / Restart**：

- `s ≥ τ` → Accept（接受）；
- `s < τ 且 v=1` → Refine（保留已验证发现，转成针对性目标继续）；
- `s < τ 且 v=0` → Restart（丢弃轨迹，仅从原始问题重启）。

更重要的是它学了一个**自主上下文压缩工具 `update_context`**：在转折点压缩轨迹，保留「已验证发现+来源、当前候选、未解约束、被拒候选、下一步计划」，**不依赖外部模型**。

我们的项目当前已具备单环骨架与护栏，但：外循环只做"评测→红队→经验"的串接、没有独立审计者、没有对**研究过程本身**的递归改进、没有跨轮累积状态、没有前端。本方案把 AREX/Arbor/Bilevel 的模式落到现有 `capability_registry`、`IterationRouter`、`ClosedLoopOrchestrator`、`PlatformSDK`、`RemoteAgentHarness` 上。

---

## 1. 目标架构（一张图讲清双循环）

```
                         ┌──────────────────────────────────────────────────────┐
                         │             OUTER LOOP  (审计 + 递归改进)             │
                         │                                                        │
  原始研究问题 x ───────► │  ① 外部审计 Auditor（独立验证器/评审 agent）          │
                         │       constraint-wise 审计 → AuditReport             │
                         │       (未解声明, 证据冲突, 置信度 s, 可恢复性 v)       │
                         │            │ Accept / Refine / Restart               │
                         │            ▼                                          │
                         │  ② 递归改进 Meta-Loop（改进"研究过程"而非"工件"）      │
                         │       机制载体: 路由策略 / prompt / skill库 / 搜索策略  │
                         │       validate-and-revert 注入 → 写回 HypothesisTree │
                         └───────────┬───────────────────────┬──────────────────┘
                                     │ Refine: 针对性目标     │ 改进信号
                                     ▼                        ▼
   ┌───────────────────────────────────────────┐     ┌──────────────────────────┐
   │  INNER LOOP  (内部研究, 现有 agent 管线)    │     │  Cumulative Research State │
   │  00 编排 → 01 文献 → 02 idea → 04 设计     │     │  HypothesisTree (Arbor式)  │
   │  → 06 代码 → 07 执行 → 03 评测(真实)       │     │  + Experience/ Lesson Bank  │
   │  Codex/WorkBuddy 经 run_capability 编排      │     │  跨 run 持久化, 回注下一轮 │
   │  → 08 经验(register_lesson) → 10 红队       │     └──────────────────────────┘
   └───────────────────────────────────────────┘
```

**与现有代码映射：**
- Inner Loop = 现有 `dispatch_open_goal` + `run_capability`，由 `RemoteAgentHarness`(Codex) 驱动，10 层能力 + `kaggle_eval`。
- Outer Audit = 新增 `layer_11_external_audit` 能力 + `AuditCompletedEvent` + 扩展 `IterationRouter` 的 Accept/Refine/Restart。
- Recursive Improvement = 把现在 stub 的 `layer_09_self_iterative_evolution` 做成真实 meta-loop（机制注入），并用 `HypoTreeStore` 持久化。
- Cumulative State = 现有 `LessonCard` 升级为 HypothesisTree 节点，落到持久化 store（当前 `store.py` 是内存）。

---

## 2. 现状盘点（可复用资产 vs 缺口）

### 2.1 可直接复用的资产
| 资产 | 位置 | 双循环中的角色 |
|---|---|---|
| `ControlPlaneService` + `Repository` | `control_plane/` | 状态机 + 事件总线（外循环决策均落为 `DecisionRecord`/事件） |
| `ClosedLoopOrchestrator` (`run_closed_loop`, `dispatch_open_goal`, `run_capability`, `decide_and_record`) | `execution_plane/orchestrator.py` | 双循环驱动引擎（扩展点） |
| `IterationRouter.propose_route` / `validate_route` | `execution_plane/decision/router.py` | `validate_route` 即**硬护栏**；扩展 `propose_route` 实现 Accept/Refine/Restart |
| `PlatformSDK`（6 方法）+ `run_capability` 工具 | `execution_plane/sdk.py` | agent 受控工具面（审计/压缩/改进都可做成新工具） |
| `RemoteAgentHarness` + `CodexTransport` | `execution_plane/agent/` | 外部 agent 接入（inner loop 已由它驱动跑通） |
| `capability_registry`（11 能力） | `execution_plane/capabilities/registry.py` | 审计/改进能力注册为 agent 可调工具 |
| 事件模型 `EvalCompletedEvent` / `AttackCompletedEvent` / `LessonPromotedEvent` / `ApprovalRequiredEvent` | `platform_contracts/events.py` | 审计新增 `AuditCompletedEvent` 即可对齐 |
| `LessonCard` + `register_lesson` | `platform_contracts/objects.py` | 经验回注 → 升级为 HypothesisTree 叶子 |
| FastAPI `create_app` + `/agent/protocol` | `control_plane/api.py` | 前端数据与协议入口（已就绪） |
| `platform_contracts/generated/typescript/contracts.ts` | （zod 类型） | **前端类型基座**，改完 Python 契约用 `export_typescript.py` 重新生成 |
| `kaggle_eval` 真实评测 | `capabilities/kaggle_eval_executor.py` | 内循环可验证评测（强验证信号，已在 Spaceship/Titanic 跑通） |

### 2.2 关键缺口（本方案要补）
1. **无独立外部审计者**：当前"审计"由同一 agent 自报（self-confirming 风险）。需独立验证器/评审 agent + 程序化校验。
2. **无对"研究过程"的递归改进**：`layer_09_self_iterative_evolution` 是 stub，仅改工件不改 harness。
3. **无跨轮累积状态**：`store.py` 是**内存 dict**，run 间不持久；无 HypothesisTree。
4. **无上下文压缩/改进态**：`run_context` 是原始 dict，没有 AREX 式"转折点压缩、保留被拒候选"。
5. **无前端**：仓库无 React/Vue/JS 应用（仅一处静态 doc）。双循环不可观测。
6. **验证层级单一**：评测真实，但审计信号弱；缺少"形式验证 > 程序化评估 > PRM > LLM-judge > 自评"的层级与 HITL 门。

---

## 3. 借鉴的设计模式（从哪里搬、搬什么）

| 来源 | 可借用机制 | 落到本项目 |
|---|---|---|
| **AREX (2607.21461)** | 内/外双环、constraint-wise 审计、Accept/Refine/Restart、置信度 `s`+可恢复性 `v`、`update_context` 压缩（保留被拒候选） | `layer_11_external_audit` + `AuditCompletedEvent`；扩展 `IterationRouter`；新增 `compact_research_state` 工具 |
| **Arbor (2606.11926)** | 长生命周期 Coordinator + 短生命周期 Executor；**Hypothesis Tree**（假设↔工件↔证据↔洞察跨时间链接）；洞察反向传播；**held-out merge gate** | `HypoTreeStore`（持久化假设树）；`layer_08` 经验回注改为树上洞察反向传播；评测用 held-out 门防 dev 过拟合 |
| **Bilevel Autoresearch (2603.23420)** | 外循环读取内循环代码/轨迹，注入新搜索机制（Tabu/Bandit）作为"机制载体"（代码/prompt/skill/记忆 schema），validate-and-revert | `layer_09` 真实化：捕获成功搜索策略→写回路由策略/prompt/skill 库；validate-and-revert 保证不退化 |
| **AI-Scientist v2 (2504.08066)** | 自动化同行评审（Area-Chair 集成 judge）；Best-First Tree Search 剪枝失败分支 | 外部审计用多 judge 投票 + 顺序打乱；HypothesisTree 失败分支显式保留 |
| **递归自改进综述 (2607.07663)** | 验证层级（形式>程序>PRM>judge>自评）；"研究方向设定"瓶颈应保留人类 | 验证层级 + HITL 仅在方向设定/高风险声明开闸 |
| **Darwin Gödel Machine (2505.22954)** | archive 保留"垫脚石"（弱祖先也可作未来父代）；沙箱 + 错误回滚 | 改进注入带快照/回滚；保留被拒策略作 archive |
| **Karpathy autoresearch** | 验证器隔离：指标模块内循环**不可改**（防"让测试变容易"） | `kaggle_eval` 阈值/数据路径冻结，agent 不能碰 |
| **Contextual Experience Replay (ACL'25)** | 免训练经验银行（文本）：合成为 pass/fail "该知道什么" | `ExperienceBank`：跨 run 检索训练无关经验回注内循环 |

---

## 4. 详细模块设计

### 4.1 新增 `layer_11_external_audit` —— 外部审计能力
- **Capability**：`layer_11_external_audit`，绑定真实 `AuditExecutor`（初始用"独立模型 prompt + 程序化校验"；与内循环 agent 用**不同模型/上下文**，破除自确认）。
- **输入**：内循环产出的临时答案 `r=(y, E, s)` + 原始目标约束清单 + 当前 `HypoTree` 快照。
- **行为**（constraint-wise 审计，借鉴 AREX）：
  1. 把目标拆成可检查约束 `{c₁..cₙ}`；
  2. 逐条核对证据覆盖（来源、时效、权威性、一致性），标 `verified / partial / conflict / missing`；
  3. 输出 `AuditReport`：`unresolved_claims[]`、`confidence s`、`recoverable v`、`rejected_candidates[]`、审计者置信 `audit_confidence`；
  4. 发 `AuditCompletedEvent`（新事件类型，加入 `events.py` + `contracts.ts`）。
- **独立验证器原则**：凡能程序化校验的（如 `kaggle_eval` 的 CV 指标、代码可运行性）走程序化；主观/论证质量走 LLM-judge 且**多 judge 投票 + 顺序打乱**；高 `risk_tier` 声明强制 `request_approval`（HITL）。

### 4.2 扩展 `IterationRouter` —— Accept / Refine / Restart
在 `router.py` 增加 `AuditCompletedEvent` 分支（借鉴 AREX 决策律）：
```
if audit.gate_passed and audit.confidence >= τ:
    -> EXIT_SUCCESS (Accept)            # 研究完成
elif audit.confidence < τ and audit.recoverable:
    -> REFINE: 生成针对性目标 q_{k+1} = unresolved_claims + rejected_candidates
               (保留 HypoTree 已验证节点, 进入内循环下一轮)
else:
    -> RESTART: 丢弃本轮轨迹, 仅从 x 重启
```
`validate_route` 不变（仍是硬护栏）。`propose_route` 变"参考策略"，agent 决策经 `validate_route` 后才提交。

### 4.3 真实化 `layer_09_self_iterative_evolution` —— 递归改进 Meta-Loop
- 把 stub 改为 `SelfEvolutionExecutor`，在每轮 Accept/Refine 后运行：
  - 从 `HypoTree` + `AuditReport` + `DecisionRecord` 抽取**机制载体**：哪些层被调用、搜索顺序、prompt 模板、是否触发了某 skill、阈值/超参；
  - 与"archive"（历史策略快照，含被拒策略）比较，用 held-out 指标选优；
  - 生成**改进提案** `ImprovementProposal`（patch 形式的路由策略/prompt/skill），经 `validate_and_revert` 注入：先沙箱验证不退化再提交；
  - 发 `ImprovementAppliedEvent`（新事件），写回 `HypoTreeStore.meta`。
- 借鉴 Bilevel：`outer loop` 改进的是"**内循环如何搜索**"，不是"工件"——这正是 AREX/综述里"研究过程自改进"的那一环。

### 4.4 持久化 `HypoTreeStore` + `ExperienceBank`（跨轮累积）
- 新增 `control_plane/store_tree.py`（或扩展 `store.py`）：用 **SQLite / JSONL** 替换内存 dict，持久化：
  - `HypothesisTree` 节点：`{hypothesis, evidence_refs, artifact_ref, insight, score, status(active/merged/pruned), branch}`；
  - `ExperienceBank`：`(pass/fail, context, lesson, applicable_stages)`，支持 in-context 检索回注（借鉴 CER）；
  - `StrategyArchive`：机制载体快照（支持回滚）。
- `HypoTreeStore` 提供 `observe/ideate/select/dispatch/backpropagate/decide`（对应 Arbor 六步），替代当前扁平 `LessonCard` 列表。
- 内循环每完成一次评测/审计，`backpropagate` 把洞察沿路径上溯，更新祖先节点（失败分支显式保留为"垫脚石"）。

### 4.5 新增 `compact_research_state` 工具（AREX `update_context`）
- `PlatformSDK` 增加 `compact_research_state(run_id, keep=["verified","unresolved","rejected_candidates","next_plan"])`，在转折点（解决子问题/剔除候选后）压缩 `run_context` → `improvement_state`，保留被拒候选（**破局部最优**）。
- 内循环 agent 在合适时机自主调用；压缩结果进 `HypoTreeStore.improvement_state`。

### 4.6 验证层级 + HITL 门（治理）
- 在 `AuditExecutor` 内实现验证层级：程序化 > PRM/规则 > LLM-judge > 自评；弱信号不可单独 Accept。
- `request_approval`（已存在）用于：① 高风险声明；② **研究方向设定**（综述强调人类保留的瓶颈）；③ 改进提案的"机制注入"在 critical 级需人工确认。
- 审计/改进事件全量入 `events` 流，前端可审计追溯。

### 4.7 后端 API 扩展（`control_plane/api.py`）
- `GET /workflow-runs/{id}/hypo-tree` —— 假设树快照（前端主视图）
- `GET /workflow-runs/{id}/audit` —— 最近 `AuditCompletedEvent` + `AuditReport`
- `GET /workflow-runs/{id}/improvements` —— 改进提案时间线
- `POST /workflow-runs/{id}/audit/run` —— 触发外部审计（agent 工具 `run_capability("layer_11_external_audit", ...)` 同源）
- `GET /runs` 跨 run 经验/策略检索（ExperienceBank）
- 现有 `/agent/protocol` 自动包含新能力（registry 已暴露全部能力）。

---

## 5. 前端平台升级方案（用户明确要求）

### 5.1 技术选型
- **栈**：Vite + **React + TypeScript**；校验用现有 `contracts.ts`（zod）→ 后端改契约后跑 `export_typescript.py` 重新生成，前后端单一事实源。
- **实时**：FastAPI 加 `EventSource`/WebSocket 端点推送 `events` → 前端用 SSE 订阅双循环推进。
- **状态**：轻量（Zustand 或 React Query 缓存 API）；图用 `reactflow`（假设树/双循环有向图）。
- **样式**：与 IDE 主题联动（本会话 dark，按主题变量走）；无暗色硬编码。

### 5.2 页面/视图（对应双循环可观测性）
1. **双循环总览（Dual-Loop Live）**：左=内循环（文献→idea→代码→执行→评测→红队时间线），右=外循环（审计发现 + 改进提案）；中缝显示 Accept/Refine/Restart 决策。
2. **审计看板（Audit Board）**：`AuditReport` 可视化——约束清单逐条状态（verified/partial/conflict/missing）、未解声明、置信度 `s`、可恢复性 `v`、被拒候选；支持"采纳/退回"操作（写回 `DecisionRecord`）。
3. **假设树浏览器（Hypothesis Tree）**：`reactflow` 渲染 `HypoTreeStore`，节点=假设/工件/证据/洞察；点击看回溯证据与分数；失败分支灰色保留。
4. **递归改进时间线（Improvement Timeline）**：展示 meta-loop 如何演化路由策略/prompt/skill（含 archive 与回滚点），证明"研究过程在变好"。
5. **HITL 审批台（Approval Console）**：列出 `ApprovalRequiredEvent`，展示风险等级、证据，提供批准/拒绝（调 `resolve-approval`）。
6. **跨轮经验库（Experience Bank）**：检索跨 run 的 pass/fail 经验，可一键回注内循环。
7. **运行/事件追踪（Trace）**：现有 `TRACE.json` 的增强可视化（时间线 + 事件类型着色）。

### 5.3 目录结构（建议新建 `frontend/`）
```
frontend/
  package.json, vite.config.ts, tsconfig.json
  src/
    contracts.ts            # 由 export_typescript.py 生成
    api/client.ts           # 封装现有 FastAPI 端点 + SSE
    views/DualLoopLive.tsx
    views/AuditBoard.tsx
    views/HypothesisTree.tsx
    views/ImprovementTimeline.tsx
    views/ApprovalConsole.tsx
    views/ExperienceBank.tsx
    components/EventStream.tsx
```
> 注：`frontend/` 与 `control_plane/api.py` 同仓库；建议后端以 `uvicorn` 起，前端 `vite dev` 代理 `/api`。

---

## 6. 分阶段落地路线（含验收标准）

**Phase 0 — 契约与持久化（地基）**
- 新增 `AuditCompletedEvent` / `ImprovementAppliedEvent` 到 `events.py`；重生成 `contracts.ts`。
- `store.py` 内存 → SQLite/JSONL（先不影响现有测试，保留内存回退）。
- 验收：全量测试 48 passed 仍绿；新增事件可落库。

**Phase 1 — 外部审计循环（Outer Audit）**
- 实现 `layer_11_external_audit` + `AuditExecutor`（独立验证器：程序化校验 + 多 judge）；扩展 `IterationRouter` 接受 `AuditCompletedEvent` 做 Accept/Refine/Restart。
- 验收：在 Spaceship-Titanic 上跑通"内循环产出 → 外部审计 → Refine 针对性跟进 → Accept"；审计事件可见、决策经 `validate_route`。

**Phase 2 — 递归改进 Meta-Loop（Recursive Improvement）**
- 真实化 `layer_09`：`SelfEvolutionExecutor` 抽取机制载体 + `validate_and_revert` 注入 + `ImprovementAppliedEvent`。
- 验收：连续 2+ 轮后，系统能展示"搜索策略被改写且 held-out 不退化"；有回滚点。

**Phase 3 — 跨轮累积状态（Cumulative State）**
- `HypoTreeStore` + `ExperienceBank`；内循环接 `compact_research_state`；`layer_08` 改为树上洞察反向传播 + held-out merge gate。
- 验收：跨多次 run，`ExperienceBank` 可被检索回注；假设树跨轮累积且失败分支保留。

**Phase 4 — 前端双循环平台**
- 按 §5 搭 `frontend/`，先 DualLoopLive + AuditBoard + ApprovalConsole，再 Tree/Timeline/Bank。
- 验收：能实时看到内/外循环、审计看板可操作、HITL 可审批。

**Phase 5 — 验证与对比（证明有效）**
- 同任务（Spaceship-Titanic）A/B：单环（当前）vs 双环（本方案），指标 = 最终评测 vs 金牌、审计发现的真实漏洞数、跨轮收敛轮数。
- 验收：双环在"发现内循环自评漏掉的漏洞""跨轮不退化"上显著优于单环。

---

## 7. 风险与护栏（来自文献，提前布防）

| 风险 | 文献来源 | 本方案对策 |
|---|---|---|
| **自确认循环**（审计者与内循环同模型，置信度虚高） | AREX、RSI 综述 | 外部审计用**独立模型/上下文**；程序化校验优先；多 judge 投票+顺序打乱 |
| **奖励黑客 / 验证器泄漏**（让测试变容易） | AlphaEvolve、Karpathy | `kaggle_eval` 阈值/数据路径**冻结**，内循环不可改；审计指标独立 |
| **多样性崩溃 / 局部最优** | Bilevel、DGM | 保留被拒候选（`compact_research_state`）；`StrategyArchive` 留弱祖先 |
| **理解债**（指标涨但无机制洞察） | Arbor | 树上洞察反向传播 + held-out merge gate；改进提案必须附因果说明 |
| **自改导致崩溃** | Gödel/DGM | `validate_and_revert` + 沙箱 + 全量编辑轨迹；critical 级 HITL |
| **验证强度上限**（弱验证封顶） | RSI 综述 | 验证层级：尽量推到程序化/形式可检；自评仅作提示不单独 Accept |
| **LLM-judge 偏差** | LLM-as-Judge 综述 | 多 judge + 顺序交换 + 抗提示鲁棒 prompt |

---

## 8. 验证方案（如何证明升级有效）

1. **端到端跑通**：复用 `scripts/run_kaggle_codex_demo.py`，扩展 `--mode dual-loop`，在 Spaceship-Titanic 上让 Codex 经 `run_capability` 驱动内循环，平台自动触发外部审计与递归改进。
2. **对照指标**：
   - 主指标：最终 `kaggle_eval` CV vs 金牌线（0.80）；
   - 质量指标：外部审计**真实发现**内循环自评漏掉的漏洞数（人工抽样核）；
   - 累积指标：跨轮后 held-out 是否不退化（held-out merge gate）；
   - 过程指标：搜索策略/路由被 meta-loop 改写的次数与回滚次数。
3. **报告**：生成 `REPORT.md`（在现有基础上加"审计轨迹 + 改进时间线 + 对照"章节）。

---

## 9. 参考（可引用）

- **AREX** — arXiv:2607.21461 (双环 + constraint-wise 审计 + update_context 压缩)
- **Recursive Self-Improvement 综述** — arXiv:2607.07663 (验证层级、研究方向设定瓶颈)
- **Arbor** — arXiv:2606.11926 (Hypothesis Tree, Coordinator/Executor, held-out merge gate; 代码 github.com/RUC-NLPIR/Arbor)
- **AI-Scientist v2** — arXiv:2504.08066 (自动化同行评审、BFTS)
- **Bilevel Autoresearch** — arXiv:2603.23420 (外循环注入搜索机制)
- **Darwin Gödel Machine** — arXiv:2505.22954 (archive 垫脚石、沙箱回滚)
- **Gödel Agent** — arXiv:2410.04444 (自指元规则改写)
- **AlphaEvolve / FunSearch** — 验证器驱动进化
- **Contextual Experience Replay** — ACL 2025 (免训练经验银行)
- **Karpathy autoresearch** — 验证器隔离（指标模块不可被优化对象修改）

---

## 10. 一句话总结

把现有"受控能力 + 护栏 + Codex 驱动的单环"升级为 **内循环研究 / 外循环审计 / 递归改进** 三层：
审计独立（破自确认）、改进作用于"研究过程"（破单环只改工件）、假设树与经验银行跨轮累积（破记忆不持久）、
前端把双循环与 HITL 完全可观测——全部落在已验证的 `capability_registry` / `IterationRouter` / `ClosedLoopOrchestrator` / `PlatformSDK` 扩展点上，不推翻现有架构。
