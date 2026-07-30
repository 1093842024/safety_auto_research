# 自主研究进化方案差距分析与升级规划

> 对照文献：Lilian Weng《Harness Engineering for Self-Improvement》(2026-07-04)
> 分析对象：safety_auto_research 双循环自主研究平台（代码级事实核查）
> 日期：2026-07-30

---

## 一、文章核心框架速览

Weng 的中心论点：**递归自我改进（RSI）的近期路径不是模型重写权重，而是改进 harness**——
即围绕基座模型、编排「prompt/工具/子 agent/控制流/记忆/工作流」的系统层。优化对象的
meta 层级不断上移：instruction → 结构化上下文 → 工作流 → harness 代码 → 优化器代码。

对我们的平台最相关的四个支柱：

1. **上下文工程**：ACE（上下文=持续演化的 playbook，Generator/Reflector/Curator 三组件，
   条目化增量合并、去重、防简洁性偏置）；MCE（机制与内容分离的双层优化）。
2. **自我改进 harness**：Self-Harness 的 propose → evaluate → accept 闭环（弱点挖掘 →
   有界提案 → held-in/held-out 双重回归验证）；AHE 的三大可观测性（组件/经验/决策），
   每次编辑都是带预测的**可证伪声明**。
3. **进化搜索**：AlphaEvolve/DGM 的种群+适应度比例父代选择+新颖性拒绝采样；警示多样性坍塌。
4. **七大瓶颈**：弱评估器、上下文/记忆生命周期、负结果、多样性坍塌、reward hacking、
   长期成功、人的角色。

---

## 二、本项目现状（代码级事实）

平台已有**双循环架构**：内循环（kaggle_eval / agent open goal）→ 外审计
（layer_11，constraint-wise，策展隔离输入）→ router Accept/Refine/Restart →
meta-loop（layer_09，冻结 verifier + 策略归档）。累积态有 HypoTreeStore /
ExperienceBank / StrategyArchive 三件套，全部持久化（JSON + SQLite）。

**已经做对、与文章最佳实践一致的地方**（应保留并固化）：

| 文章原则 | 本项目实现 |
|---|---|
| 评估器/权限控制必须位于进化循环之外 | 外审计只读策展输入（`audit_input`），禁读内循环叙事；内循环 agent 的 `run_capability` 禁止集={layer_11, layer_09}（`harness.py`） |
| 冻结 verifier（Karpathy 原则） | layer_09 `_FROZEN_KEYS` 校验，触碰 threshold/data 的提案被拒绝（`self_evolution_executor.py`） |
| 文件系统作为持久记忆 | 全部状态落盘（control_plane_store.json + research_state.db） |
| 失败分支不删除（stepping stones） | HypoTree pruned 节点保留进 compact |
| 人的角色：上移而非移出 | HITL 协作暂停（step_confirm/outer_confirm）+ 审批门 |

---

## 三、差距分析（按严重度排序）

### S1. 策略进化回路「空转」——最严重
layer_09 的 patch（`routing_bias`/`prompt_hint`）**在全库无任何消费方**（grep 为 0）；
`StrategyArchive.get/rollback` 无任何调用方；`metrics_after = metrics_before`（代码注释自承
"proxy: process validated, not metric-optimized"）；`expected_effect` 是固定文案；
`validated_heldout` 只是布尔转发。
**即：propose-evaluate-accept 闭环只有 propose 和 record，没有 apply、evaluate、accept/rollback。**
对照 Self-Harness/AHE，这正是「自我改进 harness」的核心，我们目前是名义上的。

### S2. 上下文工程为零（对照 ACE/MCE）
- 无条目化 playbook、无 Reflector/Curator、无去重/计数合并；
- `compact_research_state()` 的返回值在 orchestrator 中**被直接丢弃**（orchestrator.py L569-574）；
- 跨轮记忆只靠 goal 字符串拼接 + `rejected_candidates` 透传；
- CLI agent 每轮把完整 conversation JSON 全量重放（transport.py），跨 outer iteration 不延续。

### S3. 记忆系统「写读断裂」
- ExperienceBank **只有 agent 主动调 `register_experience` 才会写入**——orchestrator/executor
  无任何自动写入；`query_experiences` 同样只有工具入口，**无自动注入**到后续 prompt/参数；
- LessonCard 由 LessonExecutor 频率统计生成，无反思提炼、无去重、无衰减；复用侧只有一条
  router 路由规则，内容不进 prompt。

### S4. 失败处理浅、负结果无消费方
- 无失败模式聚类/根因分析（`root_cause|failure_mode|cluster` grep 为 0）；LessonExecutor
  只是单 run 内 `Counter.most_common(1)`；
- `rejected_candidates` 存进了审计报告和 compact，但**没有任何代码消费它**——下一轮内循环
  的 prompt/参数里看不到「哪些方向已被否决」，文章说的「从失败中学习是削减搜索空间的最佳
  方式」没有落地。

### S5. 评估器弱且维度单一
- 外审计 judge 与 eval_runner judge 均为 token 重叠启发式（`_token_f1`）；
- `EvalExecutor` 在无 measured_metrics 时用 run_id 哈希生成伪随机指标（mock）；
- kaggle_eval 只有 train.csv 上的 CV，**无 held-out 独立验证**；
- 预算控制只有 `max_outer_iters`，无 token/时间/成本维度。

### S6. 无并行假设搜索 / 无多样性机制
双循环完全串行；HypoTree 有 `branch` 字段但全用 "main"；无种群、无新颖性度量——
文章的「多样性坍塌」风险在我们这里没有防护，也没有利用并行加速。

### S7. 决策可观测性缺失（对照 AHE 第三支柱）
没有「编辑 + 预测 + 事后验证」的可证伪声明机制；每次 refine/策略变更没有记录
「预期修复什么、预期影响多大、事后是否兑现」，无法回答「进化到底有没有用」。

---

## 四、升级规划（三期）

### 第一期：让进化回路真正闭环（本次落地）
目标：补齐 S1/S2/S4/S7 的核心机制，全部纯 Python 实现，不破坏三条隔离不变量。

1. **ACE 式 Playbook（上下文工程 0→1）**
   - 新增 `control_plane/playbook.py`：`PlaybookStore`（SQLite 持久化，并入
     `ResearchStateStore` 第四件套），条目化 bullet（id/section/content/helpful/harmful 计数），
     **确定性去重合并**（规范化文本键，重复条目只加计数不新增——ACE 防坍塌设计）；
   - 内置规则版 **Reflector**：每轮从内循环指标 + 审计裁决 + 被拒候选 + 失败模式中提炼
     候选条目，Curator 做增量合并；
   - **自动注入内循环**：scripted 模式写入 `inner_params["playbook_context"]`，agent 模式
     追加进 goal；**绝不进入 audit_input**（隔离不变量①）。

2. **失败模式挖掘 + 负结果消费（S4）**
   - 新增 `control_plane/failure_miner.py`：从事件日志聚类失败模式（gate 失败、eval 未过、
     审计被拒候选、unresolved claims），计数+证据引用；
   - 失败模式 → playbook 负向条目；**`rejected_candidates` 首次获得消费方**：注入下一轮
     内循环参数与 goal（agent 模式）。

3. **策略 propose → apply → evaluate → accept/rollback 闭环（S1/S7）**
   - layer_09 提案升级：**可编辑面白名单**（AHE 组件可观测性思想）——只允许
     `{fe, model, cv_folds}` 的具体参数补丁 + 必须携带**可证伪预测**（目标指标/方向/最小增益）；
     冻结 verifier 校验保持不变；
   - `StrategyArchive` 条目带 `run_id`（不变量③），新增 `pending()/update()/rollback()` 真语义；
   - orchestrator 应用被接受的参数补丁到下一轮内循环，**下一轮内循环评测后立即验证**：
     实际增益 < 预测且回归超容差 → 真回滚（恢复参数快照 + 条目标记 rolled_back）；
     兑现 → 标记 verified。每次编辑都是可审计的可证伪声明。

### 第二期：评估器强化 + 经验生命周期（S3/S5）——✅ 已实施（2026-07-30）
- **kaggle_eval held-out split** ✅：`heldout_frac=0.15` 分层切分（`heldout_seed` 可配），
  CV 仍喂内循环，held-out 一次性打分进事件（`heldout_accuracy`/`generalization_gap`），
  gate 阈值保持冻结；外审计新增 `heldout_consistency` 确定性约束（gap≤0.03 verified /
  ≤0.05 → 0.7 / ≤0.08 → 0.4 / 以上 → 0.1 conflict）——过拟合直接拉低审计置信度。
- **真实 LLM judge** ✅：新增 `control_plane/llm_judge.py`（POST JSON，要求返回
  `score/rationale/evidence_refs`——ScientistOne 式证据链；任何失败抛 `LLMJudgeError`
  由调用方确定性回退）；`eval_runner.llm_judge_eval` 接真实路径（每对一次调用、上限 50
  条、回退记 `fallback_reason`）；外审计 judge 经 `LLM_AUDIT_JUDGE=1` 可选 LLM 化
  （默认确定性不变）。
- **ExperienceBank 自动写读 + 衰减去重** ✅：`add()` 按规范化 (kind, lesson) 去重合并
  （uses+1、confidence+0.05、刷新 recency），`query()` 按 `confidence×0.97^staleness`
  衰减排序；orchestrator 每轮自动蒸馏成功/失败经验，内循环 dispatch 自动注入 top-3
  （`experience_context` / goal 的 [EXPERIENCE] 段，同样绝不进 audit_input）。
  `ExperienceEntry` 契约增 `uses`/`seq` 字段（带默认值，旧数据兼容）。
- **多维预算硬停** ✅：`run_dual_loop(budget={max_seconds, max_capability_calls,
  max_cost, cost_per_call})`，亦读 `objective_snapshot.config.budget`；任一维度超额硬停
  为 `exited_budget` 并 `record_metric("budget.exceeded")`。
- 测试：`tests/test_phase2.py`（13 项）；全量 **104 passed**。
- 实测：titanic 3 轮 converge（revisit→revisit→accept），held-out 指标入事件，
  `heldout_consistency` 出现在审计约束中，经验自动沉淀并去重（uses 计数生效）。

### 第三期：并行进化搜索（S6）——✅ 已实施（2026-07-30）
- **种群式候选管理** ✅：`control_plane/evolution.py`——`Candidate`（params/fitness/
  novelty/generation/parent_id/offspring_count），`EvolutionArchive` 为第五件累积存储
  （SQLite 表 `evolution_candidates`，条目带 `run_id`，隔离不变量③）。
- **适应度比例父代选择** ✅：ShinkaEvolve 式 `select_parent`——权重 = 适应度排名分 /
  (1+后代数)，防止精英垄断采样（省样本探索）。
- **新颖性拒绝采样** ✅：无依赖哈希 embedding（bag-of-tokens 256 维 + 余弦），
  `novelty_filter` 在评估前拒绝相似度 ≥0.92 的近似重复候选（多样性坍塌防护），
  被拒候选同样归档（负记录）。
- **并行评估 + 子任务管理器** ✅：`control_plane/task_manager.py`——线程池执行、
  结果 JSON 原子落盘、崩溃恢复（已完成候选不重跑）；worker 用 BufferedSDK 跑真实
  executor（`n_jobs=1` 防超订），主线程串行回放成正式 StageRun/事件/artifact/指标。
- **多分支假设树** ✅：每个候选写入所属代分支（`gen{g}`）的假设节点——branch 字段
  首次承载真实结构。
- **每代冠军过冻结审计** ✅：审计输入仍为策展制（objective+指标+prior verdicts），
  Accept → `exited_converged`，否则繁殖下一代；预算三维硬停复用。
- **入口**：`ClosedLoopOrchestrator.run_evolutionary_loop`；端点
  `POST /workflow-runs/{id}/evolution` + `GET /workflow-runs/{id}/evolution`。
- 测试：`tests/test_evolution.py`（10 项，含审计不见候选叙事的隔离回归、预算硬停、
  任务管理器崩溃恢复）；全量 **130 passed**。

### 明确的非目标（三期仍不做）
- 不碰模型权重（SIA/联合优化证据尚不充分，且本机无 torch/GPU）；
- 不引入 MCTS/AFlow 式工作流图搜索（当前工作流形态稳定，收益/复杂度比低）；
- 不让内循环 agent 编辑 harness 自身代码（DGM 模式）——与我们的权限隔离设计冲突，
  且文章同样警示这会破坏抽象边界。

---

## 五、第一期落地清单（本次交付）

| # | 模块 | 变更 |
|---|---|---|
| 1 | `control_plane/playbook.py`（新增） | PlaybookEntry/PlaybookStore/规则 Reflector/确定性 Curator |
| 2 | `control_plane/failure_miner.py`（新增） | 失败模式聚类（事件日志 → mode 计数 + 证据） |
| 3 | `control_plane/store_tree.py` | ResearchStateStore 增第四件套 playbook；StrategyArchive 增 run_id/pending/update/rollback 真语义 |
| 4 | `execution_plane/capabilities/self_evolution_executor.py` | 可编辑面白名单 + 具体参数补丁 + 可证伪预测；优先用共享 state_store 归档 |
| 5 | `execution_plane/orchestrator.py` | playbook 反思/注入内循环；rejected_candidates 消费；策略补丁应用 + 事后验证 + 真回滚 |
| 6 | `tests/test_playbook.py`（新增） | 单元 + 双循环集成 + 隔离不变量回归 |
| 7 | `control_plane/api.py` | `GET /playbook` 只读端点（控制面板可观测） |

状态：✅ 已实施（见本节代码与 `tests/test_playbook.py`；全量测试通过）。
