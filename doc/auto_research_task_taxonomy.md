# auto-research 任务分类与差异化 harness 设计

> 面向问题：算法模型的 auto-research 是否需要把「探索型」与「飞轮型」单独成两类任务，并各配独立的启动方式与 harness。
> 状态：方案已确认；**Phase 1（B 飞轮型最小闭环）已实现**（2026-09-08，见 §八）。Phase 2（A 探索型）待启动。配套可视化见会话内两张架构图（任务谱系 / 飞轮数据闭环）。

---

## 一、结论

**需要分，而且本质上是三类——现有平台是中间那一类。** 三者构成同一个算法模型从无到有、再到持续进化的完整研发（R&D）生命周期：

| 阶段 | 定位 | 输入 | 产出 |
|---|---|---|---|
| **A 探索型（Discovery）** | 从零到方案 | 只有问题描述（可选种子约束） | 最优算法/模型方案 + 训练/评测数据集 + 评估方案 |
| **C 调优型（Tuning）** | 给定任务优化指标 | 任务 + 数据集 + 评估方案（已给定） | 部署级模型 |
| **B 飞轮型（Flywheel）** | 方案冻结，数据飞轮 | 任务 + 数据集 + 评估 + 冻结方案 | 持续改进的模型 |

现有平台（`safety_auto_research`）**已经完整实现了 C 调优型**（双循环 + 外环审计 + 元循环 + 进化搜索）。用户要的 A、B 正好是 C 上游与下游的两个能力缺口。

---

## 二、为什么必须分（而非共用一个 harness）

三类任务在四个维度上存在**语义级冲突**，塞进同一个 harness 会产生互相矛盾的行为：

| 维度 | A 探索型 | C 调优型（现有） | B 飞轮型 |
|---|---|---|---|
| **目标函数** | 找到更优方案（recall 上限） | 优化声明的单一指标 | 多目标：badcase↑ **且** 原始集不退化 |
| **数据流** | 自建后冻结（静态） | 静态 | **动态**（badcase 持续流入） |
| **搜索空间 / 冻结约束** | 算法×模型×数据，全开放 | 超参×代码 | 仅数据×训练，**模型方案冻结** |
| **终止条件** | 候选收敛 / 预算耗尽 | 预算 / 审计 accept | 事件驱动，无固定终止 |

关键冲突举例：A 的核心动作是「尝试新架构」，B 的硬约束是「冻结架构」——这是**相反的指令**。若用同一 harness，要么 agent 在飞轮阶段乱换架构（破坏回归护栏），要么探索阶段被冻结约束锁死（找不到新方案）。

**设计原则**：复用同一「搜索外壳」（内循环 → 外环审计 → 路由 → 元循环，这套壳对三类任务都适用），差异放在**任务语义层**（目标函数、数据流、冻结键集合、终止条件、agent 工具集）。

---

## 三、现有平台能力盘点（调研证据，file:line 已核对）

### 已具备（可复用）

| 能力 | 位置 |
|---|---|
| 任务注册 8 类 task_type | `benchmark_tasks/registry.py:139`（tabular/text/image/audio 分类 + llm_sft/rl/opd + embedding） |
| 脚本化内循环（sklearn 调参，模型族固定 gbm/hgb/rf/logreg） | `execution_plane/capabilities/kaggle_eval_executor.py:195-208` |
| agent 内循环（写 sklearn 代码 + Docker 沙箱跑训） | `execution_plane/orchestrator.py:433`（`dispatch_open_goal`）→ `capabilities/sandbox_executor.py:266` |
| config 岛模型进化（超参空间 fe/model/cv_folds） | `orchestrator.py:1133`、`control_plane/evolution.py:51-55` |
| 程序级岛模型进化 OpenRSI（代码空间，算子 Draft/Improve/Debug/Crossover） | `orchestrator.py:1481`、`openmle_integration/operators.py:41` |
| 外环审计 layer_11（目标拆约束 + AREX Accept/Refine/Restart） | `capabilities/audit_executor.py:265-274` |
| 元循环 layer_09（改过程不改工件，有界 param patch + 可证伪预测） | `capabilities/self_evolution_executor.py:45-49,213` |
| 迭代路由 IterationRouter | `execution_plane/decision/router.py:82` |
| 经验生命周期 / 失败挖掘 / playbook | `experience_bank.query`（内循环回放）、`failure_miner.mine_failure_modes`、`control_plane/playbook.py` |
| held-out 一致性 + LLM-judge + 方向感知排行榜 | `kaggle_eval_executor.py:141-152`、`eval_runner.py:87,189`、`control_plane/service.py:506` |

### 缺失（正是 A、B 两类任务的缺口）

| 缺口 | 证据 |
|---|---|
| 文献/技术调研层 `layer_01_literature_research` 是 **stub**（executor=None，无 web/arXiv 检索） | `capabilities/registry.py:23-31` |
| 数据评估/清洗层 `layer_05_data_evaluation_cleaning` 是 **stub**（无采集/合成/标注/清洗实现） | `capabilities/registry.py:59-67` |
| badcase 回流仅有枚举 `badcase_retrain`，**无任何执行/控制代码** | `platform_contracts/enums.py:40`、`frontend/src/contracts.ts:46` |
| 无自动标注 / hard-negative mining / 伪标签 / 数据回放训练 | 全仓 grep 无 `auto_label`/`hard_negative`/`pseudo_label` |
| 程序进化算子限定 sklearn 模板（`TemplateOperatorBackend` 离线只产 sklearn 变体） | `openmle_integration/operators.py:121-196` |

**结论**：C 调优型已完整；A 需要补「数据集构建 + 开放域算法调研」两层；B 需要补「badcase 收集 → 自动标注 → 回放重训 → 回归门」的飞轮模块。

---

## 四、建模建议：用 `run_type`/`research_phase`，而非新增 `task_type`

**关键设计决策**：A→C→B 是**同一个任务的三个生命阶段**，不是三个不同的任务。同一个「涉政内容检测」任务会先 discovery（找方案）→ tuning（精调）→ flywheel（持续改进），任务对象在三阶段间流转，数据集/方案/评估方案作为**资产在阶段间传递**。

因此建议扩展已有的 `RunType`（`platform_contracts/enums.py`），新增两个取值，而不是在 `TASK_TYPE_SPECS` 里再加 task_type：

```
RunType.DISCOVERY   # A 探索型
RunType.TUNING      # C 调优型（现有 STANDARD_RESEARCH 的语义）
RunType.FLYWHEEL    # B 飞轮型
```

每个 phase 挂各自的 harness + 启动端点 + 冻结键集合 + 工具集。资产链式传递：A 产出（方案 + 数据集 + 评估）→ C 的输入 → C 产出（冻结方案 + 部署模型）→ B 的输入。

---

## 五、差异化启动方式与 harness

### A 探索型 — 「发现环」Discovery Loop

**启动方式**：只给「问题描述 + 可选的种子数据/约束 + 预算」，无需预置数据集。前端表单从「必填 dataset_path」改为「可选种子 + 目标指标 + 资源上限」。

**新增能力层**（三层落地）：
1. **数据集构建层**（layer_05 从 stub → 真实）：数据采集（公开源/爬虫/API）→ 合成（模板/LLM 生成）→ 自动标注（弱监督 + 规则 + LLM-judge）→ 清洗去重。产出即 A 的「训练/评测数据集」资产。
2. **文献检索层**（layer_01 从 stub → 真实）：web search + arXiv + GitHub 检索，产出「候选算法/模型方案的先验清单」。
3. **开放域多方案探索**：复用 `run_program_evolutionary_loop` 的程序级岛模型，但把算子后端从「sklearn 模板」扩展为「开放域算法生成」（LLM 生成任意 ML 框架代码，如 torch/hf/xgboost/lightgbm，每个候选方案 = 一个 program island）。

**harness 特点**：
- **两阶段 funnel**：cheap 阶段（小数据/短训练）海量筛候选方案 → full 阶段（完整数据）精验 top-N，省算力。
- **文献引导 prior**：检索结果给候选方案加权，优先探索「文献报道有效」的路径。
- **审计防假阳性**：复用 layer_11 的 `heldout_consistency` + `claims_supported`，强化为「方案是否可复现」（防止 agent 声称 SOTA 但不可复现）。

### B 飞轮型 — 「数据飞轮环」Flywheel Loop

**启动方式**：任务 + 数据集 + 评估方案 + **冻结方案**（从 A/C 产出继承）。前端提供「冻结架构 + 配置 badcase 阈值」的表单。

**新增能力层**（四步闭环，见飞轮图）：
1. **badcase 收集层**：线上评测失败样本、红队攻击样本、用户反馈回流，统一入库（带来源标签）。
2. **自动标注层**：模型 ensemble 置信度 + 规则 → 高置信度自动标；低置信度走 LLM-judge 仲裁，再低的送人工抽检。
3. **回放重训层**：hard-negative mining + `badcase : 原始样本` 自适应配比 + 增量微调（**冻结架构，不换模型方案**）。
4. **回归门（不退化护栏）**：每次迭代在原始评测集上 gate 必须通过（≥ 上次的阈值），否则拒绝新模型、回退。

**harness 特点**：
- **事件驱动**：badcase 达到阈值自动触发一轮，无需人工启动。
- **回归门是硬约束**：复用了现有 audit 的 `heldout_consistency` 思路，但语义从「过拟合检查」变为「不退化护栏」。

---

## 六、优化改造方案（效率 / 效果 / 易用性 / 体验）

### A 探索型

| 维度 | 改造点 |
|---|---|
| **效率** | ① 两阶段 funnel（cheap 筛 → full 验）；② 算法指纹去重——把现有 program 精确去重扩展为「算法指纹」（框架+模型族+关键结构），避免重复探索同构方案；③ 复用现有并行种群评估（`run_program_evolutionary_loop` 已并行）扩展到多算法×多模型组合 |
| **效果** | ① 文献 prior 引导优先探索；② 审计层强化「可复现性」——heldout 一致性 + 拒绝不可复现的 SOTA 声称；③ 多方案保留（不只留 top-1，保留 top-k 供 B 阶段按场景选型） |
| **易用性** | 启动只需「问题描述 + 预算」；平台自动完成数据集构建 → 调研 → 探索 → 产出方案清单 |
| **体验** | 探索过程可视化：方案树（每候选方案的指标/来源/成本）、文献引用溯源、budget 消耗进度 |

### B 飞轮型

| 维度 | 改造点 |
|---|---|
| **效率** | ① 增量微调（冻结方案前提下只在新 badcase + 少量原始样本上微调，而非全量重训）；② 自动标注的置信度调度——高置信度自动、低置信度送 LLM/人工，降低标注成本 |
| **效果** | ① **回归门硬护栏**（核心）——原始集不退化是接受新模型的必要条件；② `badcase : 原始` 配比自适应（badcase 占比过高时加大原始样本回放防灾难遗忘）；③ badcase 去重与噪声过滤（标注置信度纳入样本权重） |
| **易用性** | 事件驱动：badcase 阈值触发自动跑一轮；人工只做低置信度抽检 + 接受/拒绝最终裁决 |
| **体验** | 飞轮可视化：badcase 覆盖率曲线、模型版本 diff、回归门通过/拒绝状态、每轮新增 badcase 的来源分布 |

---

## 七、落地路径（建议分阶段）

1. **Phase 1（优先 B 飞轮型）**：任务边界最清晰、价值立竿见影，且 `badcase_retrain` 枚举已预留、回归门可直接复用现有 `heldout_consistency` 审计思路。实现飞轮四步 + `RunType.FLYWHEEL` 端点。
2. **Phase 2（A 探索型）**：工作量最大，需把 `layer_01`（文献检索）与 `layer_05`（数据构建）两个 stub 落地，并扩展程序算子后端到开放域框架。
3. **贯穿项**：`RunType` 扩展 + 资产链式传递（A→C→B 的数据/方案/评估流转）+ 前端「探索/调优/飞轮」三态表单与可视化。

---

## 八、Phase 1 实现记录（2026-09-08）：B 飞轮型最小闭环

已按 §七.1 落地 B 飞轮型的最小闭环。**核心交付物是一个能力执行器**（单次飞轮迭代），通过现有的 `run_capability` 工具即可被 agent / 脚本调用，复用双循环的外环审计与元循环作为「接受/拒绝 + 回退」的上层驱动。

### 落地内容（file:line 已核对）

| 组件 | 位置 | 说明 |
|---|---|---|
| `BadcaseRetrainExecutor` | `execution_plane/capabilities/badcase_retrain_executor.py` | 一次飞轮迭代：①读已标注 badcase CSV（`badcase_path`）；②冻结架构下自适应 `badcase:original` 配比回放重训（`badcase_ratio`，默认 0.3，上限 0.9 防灾难遗忘）；③冻结原始评测集回归门（`regression_tol`，默认 0.0 = 不退化硬护栏） |
| 能力注册 | `execution_plane/capabilities/registry.py:125-138,257` | `badcase_retrain` 作为 extra（非 infra 层）能力注册，绑定真实执行器；`__init__.py` 导出 |
| 复用冻结方案 | `badcase_retrain_executor.py` → `KaggleEvalExecutor._build_xy` + `PRESETS` | 特征工程与 C 调优型共享，保证「冻结方案」可比 |
| 回归门方向 | `_regression_ok(baseline, retrained, tol, direction)` | 复用 `derive_gate_op` 的 higher/lower 方向约定；lower-is-better 时退化=指标上升超 tol |
| 事件契约 | `EvalCompletedEvent`（`passed = regression_passed AND badcase_improved`） | metrics 携带 `baseline_primary/retrained_primary/baseline_badcase_acc/retrained_badcase_acc/regression_passed/badcase_improved`，供外环驱动 accept/reject |

### 关键设计决策（与 §四 的偏离说明）

- **`RunType` 复用 `BADCASE_RETRAIN`（已存在的枚举值），未新增 `FLYWHEEL`**。`BADCASE_RETRAIN` 在 `enums.py:40` / `frontend/src/contracts.ts:46` / generated JSON 三处已完整接线，语义即「本次运行做坏例重训」。新增 `FLYWHEEL` 别名是纯外观项，留待前端三态表单时一并处理，避免本次改动触碰 generated 文件。
- **自动标注（§五.B 四步中的第 2 步）暂缓**。最小闭环假设 badcase CSV 已带标签（线上评测/红队/人工反馈回流）；自动标注的置信度调度属 Phase 1.5 增量。

### 测试（`tests/test_badcase_retrain.py`，6 例全绿）

- 能力注册与绑定断言（extra 非 infra）。
- happy path：真 hard-negative 回放重训 → 坏例召回提升 + 回归通过 → gate PASS。
- 拒绝路径：毒化（标签翻转）badcase → 原始集退化 → 回归门 FAIL → 拒绝新模型。
- `_regression_ok` 方向单测（higher/lower）。
- `test_capabilities.py::test_protocol_exposes_capabilities` 能力数 18→19。

> 本方案待确认后再进入代码实现（遵循先 plan/confirm 后 implement 的流程）。—— 已于 2026-09-08 完成 Phase 1。

---

## 九、Phase 1.5 实现记录（2026-09-08）：FLYWHEEL 端点 + 前端可视化 + e2e demo

### 落地内容

| 组件 | 位置 | 说明 |
|---|---|---|
| `RunType.FLYWHEEL` | `platform_contracts/enums.py` + 4 处 generated（`WorkflowRun.json` / `contracts.ts`×2 / 前端副本） | 标记飞轮型 run；generated 文件经 `export_typescript`/`export_schemas` **重生成**（顺带修复了 `audit_followup`/`node_kind`/`followups` 等既有漂移） |
| `BadcaseRetrainExecutor.collect_badcase` | `execution_plane/capabilities/badcase_retrain_executor.py` | 飞轮第 1 步「自动采集」：基线在 held-out 上的误判样本按**原始行**写回 badcase CSV（特征工程 round-trip 正确，titanic 的 Title 从 Name 重新提取） |
| `POST/GET /workflow-runs/{id}/flywheel` | `control_plane/routers/flywheel.py` | POST 同步跑一轮（collect→retrain→回归门）返回 before/after + ACCEPT/REJECT；GET 回放迭代历史 |
| 前端飞轮面板 | `frontend/src/views/FlywheelPanel.tsx` + `App.tsx` 侧栏「🔄 数据飞轮」 | 配置表单 + 本轮结果（坏例召回/回归指标/回归门裁决）+ 坏例覆盖率曲线 |
| e2e demo | `scripts/run_flywheel_demo.py` | 真实 Titanic 一轮飞轮，打印 baseline→retrained 与 ACCEPT/REJECT |

### 端到端验证（真实 Titanic，logreg）

| badcase_ratio | 坏例召回 | 回归 accuracy | 裁决 |
|---|---|---|---|
| 0.10 | 0.000 → 0.222 | 0.832 → **0.836**（通过） | ✅ ACCEPT |
| 0.15 | 0.000 → 0.378 | 0.832 → 0.813（退化） | ❌ REJECT |
| 0.20 | 0.000 → 0.489 | 0.832 → 0.772（退化） | ❌ REJECT |

回归门硬护栏被实证：badcase 配比过高会灾难性遗忘（原始集退化），飞轮正确拒绝新模型，只在 `ratio=0.10` 的甜点处接受。

### 测试

- `tests/test_flywheel.py`（4 例：collect round-trip / 端到端 / 历史回放 / 404）。
- `test_control_plane_structure.py` 路由表 `EXPECTED_ROUTES` + `ROUTER_BUILDERS` 收录 flywheel 两条路由。
- 受影响套件 62 passed + tsc 0 errors。
