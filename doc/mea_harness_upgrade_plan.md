# LongHorizon-Harness → safety_auto_research 升级方案
## 把「长程执行=任务状态管理」范式套到自主研究 harness，让每个环节跑最优 agent

> 论文来源：arXiv:2608.01964v1 *LongHorizon-Harness: Advancing Long-Horizon Agents for Real-World Tasks* (Ma et al., 2026-08-03)
> GitHub：https://github.com/AMAP-ML/LongHorizon-Harness
> 分析对象：safety_auto_research 自主研究平台（代码级事实核查，见 `execution_plane/orchestrator.py`、`execution_plane/agent/harness.py`、`control_plane/`）
> 配套文档：本文与 `doc/harness_gap_analysis_and_upgrade_plan.md`（基于 Lilian Weng 文章，三期已落地）互补——本文从「MEA 外置状态 + 每角色最优 agent」这一**新角度**给出下一轮升级蓝图。

---

## 0. TL;DR（一句话结论）

safety_auto_research 已经是一个**隐式 MEA 系统**（router/layer_09≈Manager、inner loop≈Executor、layer_11≈Auditor），但缺三件事：①**没有一等公民的「可信任务状态」对象**——状态散落在 goal 字符串、`audit_input` 策展 dict、`ResearchStateStore` 五件套里，没有「仅用被独立验证的事实更新」的单一真相源；②**没有「每角色/每环节最优 agent」分配机制**——目前 manager/executor/auditor 共用一个 harness、共用一种后端；③**executor 不是严格「每子任务 fresh context + bounded contract」**。本次升级把这三件事补齐，并把「**审计 agent 必须与执行 agent 解耦（不同模型/后端）**」作为硬约束，目标是让长程自主研究更快、更稳地收敛到更优结果。

---

## 1. 论文 LongHorizon-Harness 要点（与本研究直接相关的部分）

论文核心论点：**长程执行应被重构为 task-state management 问题**，而不是把执行、状态、完成度评估都堆在同一段增长的 context 里（后者会让错误的自评一路传播）。

### 1.1 Manage-Execute-Audit (MEA) 循环
每一轮 `i` 的形式化（原文式 1–3）：
- **Manager**：`(S_{i+1}, q_{i+1}, c_{i+1}) = Φ_mgr(𝒯, S_i, V_i)` —— 拥有持久 task state，无环境直连接口；产出控制决策 `q ∈ {execute, done, blocked, ask}` 与**有界子任务契约** `c_i`（goal + acceptance criteria + boundary constraints + 相关 prior evidence）。
- **Executor**：`(e_i, o_i) = Φ_exec(𝒯, S_i, c_i; e_{i-1})` —— 唯一允许改环境的角色；在 **fresh、budget-bounded context** 中只做这一个子任务。
- **Auditor**：`v_i = Φ_aud(𝒯, S_i, c_i, o_i; e_i)` —— **只读**，独立检查环境，产出 `v_i`（completion / integrity / state-update 三类 findings）。

> 原文：「The manager reads the current task state and defines one subtask … An executor performs only this subtask in a fresh context, while a read-only auditor independently inspects the environment to determine what changed, what was completed, and what remains unmet.」

### 1.2 任务状态显式外置、只由验证事实更新
- Task state 是结构化记录集合：每条记录有 `requirement / artifact / fact` 三类之一，状态标记 `completed / pending / blocked / untrusted`，并保留指向审计证据的引用。
- **executor 的声明（`o_i`）绝不直接改持久状态**；只有 auditor 的 verified findings（`v_i`）才能把记录标 completed。
- 跨轮只持久化 `S_i` 与 `V_i`，executor 原始轨迹丢弃。

### 1.3 AgentAdapter：模型/后端与原生 agent loop 解耦
- 通用 adapter 接口：给定 contract + 角色专属环境接口 + 预算，把既有后端（Claude Code / Codex / OpenClaw）启动为一个 bounded episode；**harness 不替换后端的原生执行循环，只控制：注入的 context、可用工具、环境权限、执行预算、返回的 report**。
- 三角色都可经同一接口实例化，且**可分配独立预算**（论文默认 executor 1800s/轮，mgr+aud 各 300s）。

### 1.4 Auditor 的接受/拒绝准则（防自确认）
- `v_i` 三类结论：
  1. **completion**：`complete / incomplete / blocked`（对照 acceptance criteria）
  2. **integrity**：`clean / suspect / violation`（检查 workspace 变更、artifact 有效性/来源、删除约束）
  3. **state-update**：verified facts + evidence + gaps
- **只读权威**：auditor 不能 create/edit/overwrite/move/delete 受保护 artifact，不能执行改变待测结果的命令/GUI 动作；检测到 mutation 即记 integrity violation，该报告不能支撑 completed。

### 1.5 失败续跑：总是从「最近一次被审计过的状态」恢复
- stalled interaction、误判完成被拒、缺失前置证据——全部作为 unresolved gaps 写回 task state，下轮 fresh executor 直接聚焦缺失证据，而非回放失败轨迹。

### 1.6 关键数字（证明「状态管理」本身能带来增益，与模型无关）
| Benchmark | Backbone | Baseline → LH | Gain |
|---|---|---|---|
| WeaveBench (PassRate) | Qwen3.7-Plus+ClaudeCode | 51.8% → **80.7%** | +28.9pp |
| Terminal-Bench 2.1 | Qwen3.7-Plus+CC | 69.7% → **77.2%** | +7.5pp（且 token −24%） |
| OSWorld 2.0 (Binary) | Qwen3.7-Plus | 2.8% → **8.3%** | 3.0× |
| OSWorld 2.0 subset | Claude Opus 4.7 | 20.6% → **35.3%** | +14.7pp |

> 重要：论文默认三角色**同模型**以隔离「状态管理」的净效应；但 AgentAdapter 架构允许异模型。这正是本研究要利用的杠杆——**给每个角色/环节配独立最优 agent**。

---

## 2. safety_auto_research 现状 vs 论文（代码级事实）

| 论文机制 | 本项目现状（已落地） | 差距 |
|---|---|---|
| Manager 角色 | `IterationRouter` + `layer_09` meta-loop + `ClosedLoopOrchestrator` | 有，但「可信任务状态」是散装（goal 字符串 + `audit_input` + `ResearchStateStore` 五件套），不是单一 `S` 对象 |
| Executor fresh-context / 子任务契约 | `run_capability` 每次新建 `StageRun`+executor；inner loop 不跨 subtask 累积；但整轮 inner 仍是一个大任务，无显式 bounded contract | 缺「每子任务契约（acceptance/boundary/prior-evidence）」 |
| Auditor 只读 + 独立验证 | `layer_11_external_audit` 读策展 `audit_input`（不含 inner 叙事）；确定性约束（primary/eval_is_real/heldout_consistency/claims_supported） | 已有 completion 类；**缺 integrity 类（mutation/provenance 检测）与显式 state-update findings** |
| 状态只由验证事实更新 | `audit_input` 策展化；隔离不变量①②③ 已固化（test_dual_loop.ContextSeparationTest） | 已较好；但 `S` 未被 Manager 显式读写 |
| AgentAdapter / 每角色后端 | `AgentHarness` ABC（`LocalAgentHarness`/`RemoteAgentHarness`）+ `AdapterRegistry`；但**只有一个 harness 实例服务于整个 inner loop** | **无「manager/executor/auditor 分别绑定不同后端/模型」的机制** |
| 每角色预算 | Phase2 三维预算（秒/调用/成本）但**角色间无差异化预算** | 缺 per-role 预算（exec 长、mgr/aud 短） |
| 失败续跑 | 有 compact/pruned 保留、rollback_id | 可强化：从「最近审计态」而非「当前 goal 拼串」恢复 |
| 隔离不变量 | `assert_inner_capability_allowed` + `assert_operator_inner_only` + `validate_route` 三道守卫 | ✅ 已完善，升级可直接复用，**不要重构** |

**结论**：本平台架构与论文高度同构，升级不需要推翻重来，而是**把隐式 MEA 显式化 + 加上「每环节最优 agent」分配层**。这正是「合理」之处。

---

## 3. 升级总架构：把双循环/进化循环包进 MEA

新增一个 **MEA 主控层**，位于现有 `run_dual_loop` / `run_evolutionary_loop` 之上（或重构其外壳），职责是：

1. 把研究目标 `𝒯` 拆解为**有界子任务契约**序列（文献→假设→设计→实现→评测→审计→决策→进化→综合）。
2. 维护单一**可信任务状态 `TaskState`**（一等公民，落盘）。
3. 每轮按 `RoleAgentRegistry` 取出**该环节最优 agent/后端**实例化 Executor/Auditor/Manager。
4. 每轮 fresh-context 跑 Executor（带 contract），只读 Auditor 验证，Manager 用 verified findings 更新 `TaskState`。
5. 失败/阻塞 → 写回 `TaskState` 的 gaps，下轮 fresh executor 聚焦。

```
                        ┌──────────────────────────────────────────┐
                        │   MEA 主控 (orchestrator.run_mea_loop)      │
                        │  维护 TaskState S（落盘，唯一真相源）        │
                        └───────────────┬────────────────────────────┘
        ┌─────────────── 读 S / 写契约 c ─┴──── 读 v(verified) 更新 S ─┐
        ▼                                                              ▼
 ┌─────────────┐  c_i   ┌──────────────────────┐  o_i   ┌──────────────────────┐
 │  Manager    │───────▶│  Executor (fresh ctx) │──────▶│  Auditor (read-only)  │
 │ 强推理模型   │        │  每环节最优 agent      │        │  独立模型 + 确定性检查 │
 │ + 冻结 verifier│      │  budget-bounded       │        │  completion/integrity/ │
 └─────────────┘        └──────────────────────┘        │  state-update findings │
        ▲                                                └───────────┬──────────┘
        └──────────────────── v_i（verified facts）─────────────────┘
                  RoleAgentRegistry：role/subtask → backend+model+budget
```

---

## 4. 模块级设计（落文件）

### 4.1 显式任务状态 `TaskState`（新增 `control_plane/task_state.py`）
把「散装状态」收敛成一个对象，Manager 读写、Executor 只读契约引用、Auditor 生产 findings。

```python
class StateRecord(BaseModel):
    kind: Literal["requirement", "artifact", "fact"]
    key: str                      # 稳定标识，如 "hypo#3" / "metric.heldout_acc"
    status: Literal["completed", "pending", "blocked", "untrusted"]
    content: dict                 # 结构化值（指标、路径、结论…）
    evidence_refs: list[str]      # 指向审计 v_i 的引用（仅 verified 才填）
    updated_by_audit: str | None  # audit run_id，None 表示尚未被验证

class TaskState(BaseModel):
    run_id: str
    objective: str
    records: dict[str, StateRecord]
    open_gaps: list[str]          # 下轮 executor 应聚焦的缺失证据/未满足项
    audit_log: list[str]          # V_i 索引（仅持久化审计证据，不持久化 executor 轨迹）

    def apply_verdict(self, v: AuditVerdict) -> None:
        # 只消费 auditor 的 verified findings；executor 声明被忽略
        ...
    def next_subtask_contract(self) -> SubtaskContract:
        # Manager 据此产出 c_i：取一个 pending/blocked 记录，构造 acceptance/boundary/prior-evidence
        ...
```

- 持久化进 `ResearchStateStore`（并入第六件套 `task_state`，复用 `store_tree.py` 的 SQLite/内存后端）。
- **隔离不变量复用**：`audit_input` 仍只含策展结果；`TaskState` 的 `open_gaps` 只注入 inner executor/goal，**绝不进 `audit_input`**。

### 4.2 每环节最优 agent：`RoleAgentRegistry` + `AgentAdapter`（新增 `execution_plane/agents/`）
把论文的 AgentAdapter 一般化为「角色→后端」可插拔层，是**用户核心诉求的落点**。

```python
# execution_plane/agents/adapter.py
class RoleAgentAdapter(ABC):
    role: str
    @abstractmethod
    def run_contract(self, contract: SubtaskContract, budget: RoleBudget) -> ExecOutput: ...
    # Auditor 专用：
    def audit(self, contract, exec_output, env_snapshot) -> AuditVerdict: ...

# 具体后端（复用现有 RemoteAgentHarness 的 transport 接缝）
class ClaudeCodeAdapter(RoleAgentAdapter): ...   # 编码类子任务
class CodexAdapter(RoleAgentAdapter): ...         # 编码/agent 类子任务
class WorkBuddyAdapter(RoleAgentAdapter): ...     # 通用 agent（本研究已接入）
class LLMApiAdapter(RoleAgentAdapter): ...        # Manager/审计推理（OpenAI 兼容，见 OPENMLE_API_MODEL）
class DeterministicAdapter(RoleAgentAdapter): ... # 评测/指标——故意不用 LLM，保证可复现
```

```python
# execution_plane/agents/registry.py
class RoleAgentRegistry:
    def __init__(self, spec: dict):  # 从 YAML 加载
        self._map = spec["roles"]
    def resolve(self, role: str, subtask_type: str | None = None) -> RoleAgentAdapter:
        # 支持「role 级默认 + subtask_type 级覆盖」——例如 executor 默认编码 agent，
        # 但 literature_search 子任务覆盖为检索 agent
        ...
    def budget_for(self, role: str) -> RoleBudget: ...
```

配置示例 `config/role_agents.yaml`：

```yaml
roles:
  manager:            { backend: llm_api, model: claude-opus-4.7, budget_seconds: 300 }
  executor_default:   { backend: claude_code, model: auto, budget_seconds: 1800 }
  executor_overrides:
    literature_search: { backend: workbuddy, model: web-research, budget_seconds: 1200 }
    hypothesis_gen:    { backend: llm_api, model: claude-opus-4.7, budget_seconds: 900 }
    experiment_code:   { backend: codex, model: auto, budget_seconds: 1800 }
  evaluator:          { backend: deterministic, model: none, budget_seconds: 300 }
  auditor:            { backend: llm_api, model: gpt-class-independent, budget_seconds: 300,
                        require_different_from: executor }   # ★硬约束：审计与执行必须异模型
  meta_decider:       { backend: llm_api, model: claude-opus-4.7, budget_seconds: 300 }
  self_evolver:       { backend: llm_api, model: cautious, budget_seconds: 300 }
  synthesizer:        { backend: workbuddy, model: writing, budget_seconds: 600 }
```

> **关键设计点**：`auditor.require_different_from: executor` 把论文「只读 + 独立」从工程上钉死——即使 executor 用 Codex/Claude Code，auditor 也必须走另一条 LLM-API 通道（如 GPT 类或 Qwen 类），从根上切断自确认。

### 4.3 MEA 主循环（改 `execution_plane/orchestrator.py`）
新增 `run_mea_loop`，外壳驱动现有双循环/进化循环作为「Executor 的一个子任务族」，但加上 TaskState 管理与角色 agent 路由：

```python
def run_mea_loop(self, run_id, role_spec: str = "config/role_agents.yaml",
                 max_rounds: int = 25):
    reg = RoleAgentRegistry.load(role_spec)
    state = self.svc.load_task_state(run_id) or TaskState.from_objective(run_id, objective)
    for i in range(max_rounds):
        contract = state.next_subtask_contract()           # Manager 产出 c_i
        # —— Executor（该环节最优 agent，fresh context）——
        exec_agent = reg.resolve("executor", contract.subtask_type)
        out = exec_agent.run_contract(contract, reg.budget_for("executor"))
        # —— Auditor（独立后端，只读）——
        aud_agent = reg.resolve("auditor")
        verdict = aud_agent.audit(contract, out, self._env_snapshot())
        # —— Manager 更新可信状态（只消费 verified）——
        state.apply_verdict(verdict)
        self.svc.save_task_state(state)
        if state.all_requirements_met():
            return WorkflowStatus.exited_converged
    return WorkflowStatus.exited_budget
```

- 现有 `run_dual_loop` / `run_evolutionary_loop` / `run_program_evolutionary_loop` **保留为 Executor 子任务实现**，由 contract 触发；不复用则不改其内部分离逻辑。
- `run_capability` 增加可选 `agent=RoleAgentAdapter` 参数，使每个 capability 可指定后端（复用 `AgentHarness` 的 transport 接缝）。

### 4.4 强化审计层（改 `execution_plane/capabilities/audit_executor.py`）
在现有 completion 类约束之外补两类 findings，对齐论文：

- **integrity findings**：检查本次 executor 是否动了受保护 artifact（`data/`、历史 `research_records`、其他 run 的产物）；检测 provenance（指标是否来自真实 `eval_is_real`，是否来自被改过的代码）；任何 mutation → `violation`，该报告不能支撑 completed。
- **state-update findings**：把 completion 结果翻译成 `TaskState` 的 `StateRecord` 更新提案（key/status/evidence），由 Manager 提交。
- 审计 prompt 继续**只吃策展输入**（`answer` + `prior_audits` + metrics），不读 executor 轨迹——隔离不变量①保持。

### 4.5 失败续跑 / 恢复（改 orchestrator + service）
- 中断/崩溃后从 `TaskState`（最近一次审计态）重建，而不是从 goal 拼串或失败轨迹。
- `open_gaps` 由 Auditor 的 incomplete/blocked findings 自动填充 → 下轮 contract 直接聚焦。
- 复用 `BackgroundTaskManager` 的崩溃恢复与 `EvolutionArchive` 的 `run_id` 标记。

### 4.6 角色预算（改 `run_mea_loop` 预算分发）
- 沿用 Phase2 三维预算框架，但按 role 差异化：executor 长（1800s，论文值）、mgr/aud/decider 短（300s）。
- `RoleBudget` 携带 `max_seconds / max_cost / max_tokens`，超预算该角色 episode 强制终止（保护整体 budget）。

---

## 5. ★ 每个研究环节的最优 agent 分配表（核心交付）

把自主研究拆成离散环节，逐一指定**当前最优 agent/后端/模型**，并说明为什么（避免自确认、发挥各后端所长）。

| # | 研究环节 | 对应现有模块 | 最优 agent / 后端 | 模型建议 | 预算 | 关键理由 / 隔离 |
|---|---|---|---|---|---|---|
| R0 | **目标拆解 + 状态维护 (Manager)** | `IterationRouter`+`layer_09`+新增 `TaskState` | `LLMApiAdapter`（强推理） | Claude Opus 4.7 / GPT-5 类 | 300s | 长程规划+策略，需强推理；决策经 `validate_route` 守卫 |
| R1 | **文献 / SOTA 调研** | `infrastructure/01_literature_search` | `WorkBuddyAdapter`（web+检索）或检索 agent | 带 web 工具的 agent | 1200s | 需要联网/检索/embedding，编码 agent 不擅长 |
| R2 | **假设生成 / idea** | `hypo_tree` 节点 | `LLMApiAdapter`（创造+严谨） | Opus/GPT 类 | 900s | 需要发散+可证伪性判断 |
| R3 | **实验设计 / 协议** | `infrastructure/02_study_design`（规划） | `LLMApiAdapter`（规划） | Opus 类 | 600s | 需要把假设转成可评测协议 |
| R4 | **实现 / 写代码** | inner loop `kaggle_eval` / open-goal | `ClaudeCodeAdapter` 或 `CodexAdapter` | 编码专用（auto） | 1800s | fresh-context 每子任务，避免长上下文漂移 |
| R5 | **评测 / 指标计算** | `eval_runner` / `kaggle_eval` metrics | **`DeterministicAdapter`（无 LLM）** | 无（纯计算） | 300s | 指标必须可复现、零幻觉；LLM-judge 仅作可选叠加且 fail-soft |
| R6 | **外部审计 (Auditor)** | `layer_11_external_audit` | **`LLMApiAdapter`（独立模型）** | **必须 ≠ R4 后端**（如 GPT/Qwen 类） | 300s | ★防自确认：审计与执行异模型+确定性约束+只读 |
| R7 | **元决策 / 路由** | `IterationRouter.propose_route` | `LLMApiAdapter`（强推理） | Opus 类 | 300s | 与 R0 可同模型；输出经 `validate_route` 守卫 |
| R8 | **自进化补丁提案** | `layer_09` meta-loop | `LLMApiAdapter`（谨慎） | 谨慎型 LLM | 300s | 必须过 `_FROZEN_KEYS` 冻结 verifier + 可证伪预测 + 真回滚 |
| R9 | **进化搜索编排** | `run_evolutionary_loop` / 岛模型 | 确定性编排 + `LLMOperatorBackend`/`TemplateOperatorBackend` | 算子后端可配 | 按代 | 种群管理确定性；Improve/Debug/Crossover 算子可用 LLM，每岛可不同 |
| R10 | **报告综合** | 最终报告生成 | `WorkBuddyAdapter`（写作） | 写作型 agent | 600s | 综合已验证 findings 成稿，不引入新事实 |

**选择/路由原则（写进 `RoleAgentRegistry` 的解析逻辑）：**
1. **审计 ≠ 执行**（硬约束）：R6 后端/模型必须不同于 R4，从架构层消除自确认。
2. **评测去 LLM 化**：R5 默认确定性，LLM-judge 仅可选叠加且 fail-soft（沿用现有 `LLM_JUDGE_URL` 机制）。
3. **编码归编码 agent、推理归推理模型、检索归检索 agent**：扬长避短，而非一个通用大模型通吃。
4. **强模型省轮次、弱模型费轮次**（论文 token 分析）：meta/决策用强模型减少 audit–replan 往返；执行可用编码专用模型。
5. **可学习化（未来）**：`leaderboard()` 已按环节记录指标，未来可由 meta-loop 反推「某环节用哪 agent 收益最高」，动态更新 `role_agents.yaml`（不破坏冻结 verifier）。

---

## 6. 分阶段落地路线（复用现有接缝，不破坏隔离）

- **P1 — 显式 TaskState + MEA 外壳（本次核心）**
  - 新增 `control_plane/task_state.py`（`StateRecord`/`TaskState`/`AuditVerdict`）+ 并入 `ResearchStateStore` 第六件套。
  - `orchestrator.run_mea_loop` 外壳；`run_capability` 增 `agent=` 参数。
  - `audit_executor` 补 integrity/state-update findings。
  - 测试：新增 `tests/test_mea_state.py`（TaskState 只吃 verified、open_gaps 恢复、隔离不变量回归）。

- **P2 — RoleAgentRegistry + AgentAdapter（用户核心诉求）**
  - 新增 `execution_plane/agents/{adapter,registry}.py`；`config/role_agents.yaml`。
  - `ClaudeCodeAdapter`/`CodexAdapter`/`WorkBuddyAdapter` 复用 `RemoteAgentHarness` 的 transport；`LLMApiAdapter` 复用 `openmle_integration` 的 `ApiLLMOperatorBackend` 接缝；`DeterministicAdapter` 直连 `eval_runner`。
  - 硬约束 `auditor.require_different_from: executor` 在 `registry.resolve` 中校验。
  - 测试：`tests/test_role_agents.py`（每个 role 解析到正确后端、审计异模型校验、预算下发）。

- **P3 — 每环节最优 agent 接线**
  - 把 R0–R10 逐一接到 `run_mea_loop`：inner loop 经 `role_agents.yaml` 选编码 agent；layer_11 走独立 LLM-API；layer_09 走谨慎 LLM；文献/综合走 WorkBuddy。
  - 前端 `frontend/` 增加「每环节当前 agent」可视化（复用 RunDashboard）。

- **P4 — 量化验证「更快更好」**
  - 在 titanic/OpenRSI 基准上跑「原双循环」vs「MEA+每环节最优 agent」对照；指标：收敛轮数↓、held-out 指标↑、token/成本↓、审计拒真率（自确认次数）↓。
  - 复用 `eval_runner` + `leaderboard()` 出对照报告。

**明确的非目标（四期仍不做）**：不碰模型权重；不引入 MCTS/AFlow 工作流图搜索；不让 inner agent 改 harness 自身代码（与权限隔离冲突）。

---

## 7. 风险与验证

- **风险1（隔离被破坏）**：每环节接不同 agent 时，若审计 agent 误读了 executor 轨迹会 reintroduce 自确认。→ 缓解：`audit_input` 仍只含策展结果；`require_different_from` 在 registry 层强制；保留 `ContextSeparationTest` 回归。
- **风险2（状态漂移）**：TaskState 若被 executor 直接写会退化成论文批评的「context 内自评」。→ 缓解：`StateRecord.evidence_refs` 必须指向审计 `v_i`，`apply_verdict` 拒绝无 evidence 的 completed。
- **风险3（成本）**：每环节独立 agent 可能增加调用。→ 缓解：评测/进化编排走确定性（R5/R9）；强模型只用在省轮次的 meta/决策（R0/R7）。
- **如何衡量成功**：对照实验（P4）看三件事——①收敛更快（轮数/小时↓）；②结果更优（held-out/leaderboard↑）；③更稳（审计拒真、回滚次数↓，状态可恢复）。

---

## 8. 关键接口草图（可直接进入实现）

```python
# control_plane/task_state.py
class AuditVerdict(BaseModel):
    completion: Literal["complete", "incomplete", "blocked"]
    integrity: Literal["clean", "suspect", "violation"]
    state_updates: list[StateRecord]      # 待 Manager 提交的更新提案
    evidence_refs: list[str]
    rationale: str

# execution_plane/agents/adapter.py
@dataclass
class RoleBudget:
    max_seconds: int = 300
    max_cost: float | None = None
    max_tokens: int | None = None

@dataclass
class SubtaskContract:
    subtask_type: str          # literature_search / hypothesis_gen / experiment_code / ...
    goal: str
    acceptance_criteria: list[str]
    boundary_constraints: list[str]
    prior_evidence_refs: list[str]

# execution_plane/orchestrator.py
def run_mea_loop(self, run_id: str, role_spec: str, max_rounds: int = 25) -> WorkflowStatus: ...
def run_capability(self, run_id, capability_id, params=None, agent: "RoleAgentAdapter | None" = None): ...
```

> 注：`AgentHarness` / `RemoteAgentHarness` / `AdapterRegistry` / `OperatorBackend` / `LLM_JUDGE_URL` / `validate_route` / 三道隔离守卫 均为**已存在的接缝**，本方案只在其上叠加，不重构隔离逻辑。
