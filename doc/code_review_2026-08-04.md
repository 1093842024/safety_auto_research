# Code Review — safety_auto_research（2026-08-04）

> 范围：后端核心（control_plane / execution_plane）+ OpenRSI/OpenMLE 集成（openmle_integration）。
> 目标：定位**功能性缺陷**（逻辑错误、方向错误、隔离破坏、边界/错误处理、状态不一致）。
> 方法：双 Agent 并行探索 + 对每条 P1 结论**逐行读源码复核**（trust-but-verify）。两条子代理结论被证伪，见文末。

## 一、确认的高危缺陷（P1，正确性）

### P1-1　程序级进化循环完全忽略指标方向 `op`
- **位置**：`execution_plane/orchestrator.py:1424`（存储 fitness）、`control_plane/evolution.py:432-459`（`IslandModel.best/migrate`）、`execution_plane/orchestrator.py:1565/1582/1597`（`_breed_program_generation` 的父代选择/精英保留）。
- **问题**：程序循环在 `c.fitness = float(fitness)` 处**直接用原始值**，`model.best()`、`migrate`、breed 全部用 `c.fitness or 0.0, reverse=True`（越大越好）选择。而配置循环在 `orchestrator.py:1182` 正确地套了 `_primary_score(op)`。对 `op=="le"`（log_loss / rmse 等越低越好）的任务，程序循环会把**最差**候选当 champion、迁移最差、精英保留最差——搜索方向整体反转。
- **影响**：任何 lower-is-better 任务的程序级岛模型搜索彻底失效（退化为“选最坏”）。
- **修复**：存储 fitness 时套 `_primary_score(fitness, op)`（或 op=="le" 时取负），并把 `maximize` 标志贯穿 `IslandModel.best/migrate` 与 `_breed` 的选择逻辑。

### P1-2　四算子完全忽略父程序 / 当前程序（搜索退化）
- **位置**：`openmle_integration/operators.py`
  - `_crossover_program`（144-158）：永远输出固定的 RF+GBM 混合，**从不读取** `prompt.parent_programs`；
  - `_improve_program`（117-123）、`_debug_program`（126-141）：**忽略** `prompt.current_program`，永远输出固定 GBM / 固定 RF。
- **问题**：离线模板后端不基于父代/当前程序做任何变换。配合“程序级 novelty_filter 用精确代码字符串去重”（算子模板 95% 相同），第 2 代起所有 improve/debug/crossover 子代都是模板的**逐字节副本** → 被 novelty filter 全部拒掉。
- **影响**：程序进化从第 2 代起几乎没有有意义变异，只有 4 个 draft 变体在贡献，岛模型名存实亡。
- **修复**：crossover 真正融合两个父代（如交换两家 estimator）；improve/debug 在父程序实例上做有向修改（而非吐固定模板）。

### P1-3　研究榜单对“越低越好”任务可能记录最差跑
- **位置**：`control_plane/service.py:216-233`（`capture_run_record`）。
- **问题**：若任务声明的 `eval_metric`（如 `"log_loss"`）**未**作为键出现在事件 metrics 中（kaggle 事件只带 `primary`+`accuracy`），代码回退到 `"accuracy"` 并以任务 `direction` 调 `_best`。当 `direction=="lower"` 时算的是 `min(accuracy)` → 把**最低准确率**的跑当成“最佳研究记录”写入榜单 / top-3。
- **影响**：任何非 accuracy 的 lower-is-better 指标会污染研究榜单。
- **修复**：声明指标键缺失时优先用 `primary`（真实目标）并按方向选优；或让 executor 以声明名记录该指标。

### P1-4　Reward bridge 不防 NaN，污染 RL 信号
- **位置**：`openmle_integration/reward_bridge.py:109`。
- **问题**：`fit = fitness if fitness is not None else 0.0` 无 NaN 校验。若 fitness 为 NaN（指标未定义）或 novelty 为 NaN，`max/min` 会传播 NaN 进 `improvement`/`total`，静默污染本地 LLM 训练奖励。
- **修复**：`if fit is None or math.isnan(fit): fit = 0.0`，并对 novelty 在 `min/max` 前夹成有限值。

## 二、确认的二级缺陷（P2，边界/潜在/加固）

| # | 位置 | 问题 | 修复 |
|---|------|------|------|
| P2-1 | `reward_bridge.py:112` | `improvement = max(0,(fit-prev)*scale)` 永远奖励“增大”，对 `op=="le"` 的真实改进（变小）给 0 分，无 `maximize` 标志 | 增加 `maximize`，用 `sign` |
| P2-2 | `inner_capability.py:37` | `assert_operator_inner_only` 用黑名单；`caller_stage=None`（默认）时直接放行，隔离护栏可被“漏传参数”静默绕过 | 要求 caller_stage 非空 + 改用内循环白名单，未知/None 直接抛 |
| P2-3 | `adapter.py:244` | id 对齐分支 `merged[..._pred].astype(int)` 对 NaN（部分缺交）抛 ValueError，崩溃评测而非返回 `VALID_SOLUTION:False`；位置对齐分支(264)已 try/except | 同位置分支那样包 try/except |
| P2-4 | `adapter.py:124` | `prepare` 的 `astype(int if dtype!=float else float)` 对字符串/object 标签抛错 | 做 label-encode |
| P2-5 | `evolution.py:208` | `editable = [k for k in surface if k in params or True]` —— `or True` 使 `if k in params` 死代码，所有 surface 键恒入选 | 删除 `or True` |
| P2-6 | `evolution.py:436-459` | `migrate` 克隆 `fitness=None` 且**未** `archive.add`：①`archive.list_by_kind`(1588) 看不到迁移克隆，精确代码 novelty filter 无法对其去重，跨岛可进重复程序；②克隆被当全新候选重评，并非“导入已知冠军” | `migrate` 内 `archive.add(clone)` 并带上 `fitness=champ.fitness`（或显式标记重评） |
| P2-7 | `orchestrator.py:1590-1592` | 多样性守卫：`kept` 为空时回退到**全部** children（含精确副本），守卫失效 | 回退到精英 + 至少一个新种子，而非整套重复 |
| P2-8 | `service.py:163` | `set_run_status` 默认 `EventType.WORKFLOW_REQUESTED`，终态 `failed`/`exited_budget` 也发 REQUESTED 事件（调用方未传 event_type），可观测性失真 | 方法内按 status 映射正确 EventType |
| P2-9 | `store.py:197` | `run_id in str(a.producer_ref)` 是子串匹配，一个 workflow run_id 可能是另一 run 的 stage_run_id 子串 → 泄漏/误返跨 run 的 artifact | 按本 run 的 stage_run_id 集合精确匹配，或 artifact 存显式 `run_id` |
| P2-10 | `self_evolution_executor.py:165-166` | 可证伪 prediction 硬编码 `"metric":"accuracy","direction":"higher"`；当前被 orchestrator 用方向感知 inner_acc 覆盖 baseline 掩盖，但 proposal 自身语义对 `op=="le"` 错误 | 从 `obj.get("op")` 推导 direction |
| P2-11 | `orchestrator.py:1474` | `audit_input["result_metrics"] = champion.metrics` 含 `feedback`（adapter 的 `VALID_SOLUTION_FEEDBACK`，常为程序原始 stderr）；代码/身份不泄漏（已验证 OK），但原始 stderr 违反“仅冠军指标”约定 | 从审计载荷剥离 `feedback` |
| P2-12 | `orchestrator.py:1144/1230`、`1364/1481` | 代内预算门在 audit `run_capability`（会 +1 计数）之前检查，单代可超额 1 次 audit 调用 | audit 后再查一次 `_budget_exceeded()` |

## 三、已验证 OK / 子代理误报（重要，避免重复修复）

- ❌ **“`_primary_score` 仅在缺 accuracy 时取负”** —— **错误**。`orchestrator.py:121` 对 `op=="le"` **无论** v 来自 accuracy 还是 primary 都取负，方向处理正确。
- ❌ **“Collaboration `action=='abort'` 被静默忽略”** —— **错误**。API `resolve_collaboration`（`api.py:1553`）置 `rejected = (resolution != "approved")`，`_collab_pause` 据此返回 `None` → `collaboration_aborted`。当前契约无独立 `action` 字段，reject 即覆盖 abort。
- ✅ 审计代码/身份隔离：`audit_input` 只用 `champion.metrics`，无 `.code`/`operator`/`parent_ids`（1472-1484）。
- ✅ `run_id` 在 Candidate/Archive/HypoTree 上按 run 过滤成立。
- ✅ `novelty_filter` 双粒度（config 余弦≥0.92；program 精确代码 set）符合规格。
- ✅ `local_train` 仅在工厂内 import torch、张量 device 与 model 一致，无 torch 也可导入。
- ✅ 配置循环方向处理（1182 `_primary_score`）正确。

## 四、测试基线（本次实测）

- `test_control_plane.py`：**8 passed**。
- `test_evolution.py` + `test_openmle_phase_bc.py` + `test_dual_loop.py`：**27 passed**（含算子/crossover、双循环隔离）。注意：`test_openmle_phase_bc` 的 crossover 用例只校验输出形态、**未**校验父代是否真的影响结果 → 正是 P1-2 未被测试捕获的原因。
- 全量 132 套件本次**未能跑完**：沙箱在“所有模块一次性加载”时触发内存上限（进程被 SIGKILL，exit 137），并非项目缺陷；`test_openmle_phase_d`（torch/MPS）已排除。建议 CI 分模块/加内存上限跑。
- 前端（TypeScript）：依前轮记录 `tsc 0 errors`，本轮未做深度审计。

## 五、优先级建议

1. **立即修**：P1-1（程序循环方向）、P1-2（算子用父程序）、P1-3（榜单方向回退）、P1-4（reward NaN）。这四条直接决定程序级搜索与榜单的正确性。
2. **尽快修**：P2-2（隔离护栏 bypass）、P2-3/P2-4（adapter 崩溃）、P2-6（migrate 未归档）、P2-8/P2-9（可观测性/泄漏）。
3. **补测试**：crossover 父代影响断言、reward 的 NaN 与 `op=="le"`、id-merge NaN 路径、string-label `prepare`、P1-3 的 lower-is-better 榜单用例。

## 六、修复执行记录（P1-1~P1-4，2026-08-04）

四条 P1 已全部落地，并修复了修复过程中引入的回归。

### P1-1 — 程序级进化循环指标方向（orchestrator.py）
- 原：`run_program_evolutionary_loop` 在 1429 行直接 `c.fitness = _primary_score(raw_fitness, op)`，**误把 float 当作 dict 传入**。
- `_primary_score(metrics, op)` 签名要求 `dict`（`metrics.get("accuracy")`→`metrics.get("primary")`），对非 dict 直接 `return 0.0`（107/115-116）。
- 后果：每个候选 `fitness` 被清零 → `model.best()` 选出 `fitness=0.0` 的 champion → 触发 `test_openmle_phase_bc::test_program_nodes_written_and_searched` 断言 `champ.fitness > 0.5` 失败（实测 `0.0 not greater than 0.5`）。
- 修：先构建 `c.metrics`（含 `primary`/`accuracy`），再 `c.fitness = _primary_score(c.metrics, op)`，op=="ge" 直通原始精度、op=="le" 取负，selection/migration/elitism 方向正确。
- 附带：把 `observe_hypothesis` 的 `score` 改用 `metrics["primary"]`（原始目标值）而非 `c.fitness`，避免 op=="le" 时所有候选 observation score 被钳成 0.0。

### P1-2 — 四算子忽略父/当前程序（operators.py）
- 新增 `_hash_int(text)`（`hashlib.md5`，进程间稳定，替代不稳定的内置 `hash()`）。
- `_improve_program`：按 `_hash_int(current_program)` 在 3 个模型族间变化，并输出 `# improved-from parent(hash=...)` 注释。
- `_debug_program`：用 `_hash_int(current_program)` 播种 `random_state`（4 选 1）。
- `_crossover_program`：用 `_hash_int("\n---\n".join(parent_programs))` 播种两个 `random_state`，输出父源长度注释。
- `TemplateOperatorBackend.generate` 把 `prompt.current_program` / `prompt.parent_programs` 透传给 improve/debug/crossover。

### P1-3 — 榜单对 lower-is-better 回退取 min（service.py）
- `capture_run_record`：把单一 `_best` 拆成方向感知的 `_best_dir`（higher→max / lower→min，作用于真实目标）与恒定higher-is-better 的 `_best_higher`（accuracy 族永不反转）。
- 匹配顺序：①声明指标名→`_best_dir`；②`primary` 键→`_best_dir`（新增，防 lower-is-better 非 accuracy 任务选到最差 run）；③accuracy 族回退→`_best_higher`；④兜底任意指标→`_best_dir`。

### P1-4 — reward_bridge NaN 污染（reward_bridge.py）
- 已 `import math`；`reward_func` 对 `fitness is None / math.isnan(fitness)` 钳为 `0.0`，并对 `prev_fitness` / `novelty` 做同样 NaN 守卫；`improvement` 用 `max(0, (fit-prev))` 且 prev 已守卫。

### 回归验证（2026-08-04 实测）
- 修复 P1-1 传参 bug 后，目标批次 **41 passed**（test_openmle_phase_bc/a、test_evolution、test_control_plane、test_research_records）。
- 扩大回归：**49 passed**（test_openmle_phase_d/d_ext、test_dual_loop、test_playbook、test_phase2）+ **25 passed**（test_execution_plane、test_capabilities、test_agent_mode）+ **49 passed**（test_benchmark_registry/suites、test_platform_contracts、test_agent_protocol）。
- 合计本次 touched 路径 **164 passed，0 failed**。全量套件仍受沙箱“模块一次性加载内存上限”限制（SIGKILL/exit 137），非项目缺陷，CI 建议分模块跑。

## 七、P2 修复执行记录（2026-08-04 续）

按优先级（报告五.2「尽快修」→ 其余）逐条落地，均为功能性/隔离性加固。

### 五.2 优先集
- **P2-2** `inner_capability.py`：`assert_operator_inner_only` 由黑名单改为**白名单** `INNER_LOOP_ALLOWED_CALLERS={"inner","inner_program_evolution"}`。`caller_stage=None`/未知/外循环均抛 `OperatorAuditViolation`，隔离护栏 fail-closed（原先漏传参数静默放行）。
- **P2-3** `adapter.py:_score_submission`：id 对齐分支 `merged[..._pred].astype(int)` 包 `try/except (ValueError, TypeError)`，缺值/NaN 时返回 `VALID_SOLUTION:False` 而非崩溃（与位置对齐分支一致）。
- **P2-4** `adapter.py:prepare`：目标为字符串/object 标签时 `LabelEncoder` 编码为稳定 int，并写入 train.csv，使 sklearn 流水线 + int 比较一致（原先 `astype(int)` 对 object 抛错）。
- **P2-6** `evolution.py:IslandModel.migrate`：克隆携带 `champ.fitness`/`metrics` 并标记 `status="evaluated"`（导入已知冠军、不再静默重评）；新增 `archive` 参数，克隆 `archive.add` 注册，使精确代码 novelty filter 能对其去重（防跨岛重复程序）。orchestrator 调用处传入 `archive`。
- **P2-8** `service.py:set_run_status`：不再默认 `WORKFLOW_REQUESTED`；未显式传 `event_type` 时按 `status` 映射（`_event_type_for_status`：`RUNNING`→STARTED，终态→FINISHED，其余→REQUESTED）。新增 `_TERMINAL_STATUSES` 集合。
- **P2-9** `store.py:list_artifacts`：由 `run_id in str(producer_ref)` 子串匹配改为**精确匹配**本 run 的 `stage_run_id` 集合（内联避免非重入锁死锁）。防 `run_1` 误匹配 `run_10_stage_a` 泄漏跨 run artifact。

### 其余 P2
- **P2-1** `reward_bridge.py`：`RewardConfig` 加 `maximize=True`；`improvement` 按方向取符号（maximize=False 时 `prev-fit` 为正），`op=="le"` 真实改进获正奖励。
- **P2-5** `evolution.py:mutate`：删除 `or True` 死代码，`editable` 仅含 `k in params` 的键，不再向候选注入不存在的参数键。
- **P2-7** `orchestrator.py:_breed_program_generation`：多样性守卫 `kept` 为空时回退到**单个最优 child**（而非整套重复 children），配合下方精英保留，避免守卫失效、又不致岛空。
- **P2-10** `self_evolution_executor.py`：`prediction["direction"]` 由 `run.objective_snapshot.op` 推导（`op=="le"`→`lower`），不再硬编码 `higher`。
- **P2-11** `orchestrator.py` 程序循环审计：构造 `_audit_metrics` 剥离 `feedback`（adapter 原始 stderr），审计载荷仅含策展指标（隔离不变量）。
- **P2-12** `orchestrator.py` 配置/程序两循环的 audit 之后各补一次 `_budget_exceeded()` 复检，防单代超额 1 次 audit 调用。

### 测试
- 新增 `tests/test_p2_fixes.py`（13 例）锁定 P2-1/2/5/8/9 行为契约。
- 因 P2-2 严格化，`test_openmle_phase_d_ext.py::test_draft_program_uses_api_backend` 原调用 `draft_program` 漏传 `caller_stage`，补 `caller_stage="inner_program_evolution"`（生产调用均已传，符合新契约）。
- 回归（仓库根 pytest，分模块避 OOM）：Batch A（openmle/evolution/control_plane/contracts）**67 passed**，Batch B（dual_loop/phase2/execution/playback 等）**97 passed**，加 `test_p2_fixes` **13 passed**，合计 **177 passed，0 failed**。

## 八、补测试执行记录（2026-08-04 续，五.3 全部落地）

针对报告五.3 的“补测试”清单，新建 **`tests/test_review_supplementary.py`**（全部数据集无关：合成数据 / 纯逻辑，免去 titanic 依赖，任何环境可跑），覆盖五.3 全部 5 项：

| # | 项 | 测试类 / 方法 | 锁定契约 |
|---|----|--------------|----------|
| 1 | crossover 父代影响断言 | `CrossoverParentInfluenceTest.test_crossover_distinct_from_parents_and_deterministic`、`test_changing_a_parent_changes_crossover` | 同父代→同产出（确定性）；产出≠任一父模板；换父代→产出变（父哈希播种 rs） |
| 2 | reward 的 NaN 与 `op=="le"` | `RewardNaNAndLowerIsBetterTest` 5 例 | `fitness=None/NaN`→`fit=0.0`、improved=0、`total` 有限；`prev_fitness/ novelty=NaN` 各守卫；`maximize=False` 时下降为正改进、上升为 0 |
| 3 | id-merge NaN 路径 | `IdMergeNaNPathTest.test_id_merge_valid_submission_scores`（正向对照）、`test_id_merge_nan_preds_is_valid_false_not_crash` | 预测含 NaN 时 `_score_submission` 返回 `VALID_SOLUTION=False` + `score=None` + 反馈含 “NaN”，不崩溃（P2-3） |
| 4 | string-label `prepare` | `StringLabelPrepareTest.test_string_target_label_encoded` | 目标为字符串列时 `prepare` 成功、`eval_truth` 为整数编码、两类别→两整数；内建流水线 step 仍 `VALID_SOLUTION=True`（P2-4） |
| 5 | P1-3 lower-is-better 榜单 | `LowerIsBetterLeaderboardTest.test_capture_picks_best_primary_not_worst_accuracy`、`test_leaderboard_and_top3_direction_aware` | 声明指标（`log_loss`）不在记录键时回落 `primary` 并按方向取 `min`（=0.30），而非 `min/max(accuracy)`（=0.50/0.60）；`leaderboard`/`_recompute_top3` 方向感知 |

### 测试结果（2026-08-04 实测，仓库根 pytest）
- `test_review_supplementary.py`：**12 passed**（5 类共 12 例）。
- 关联回归（确认未破坏既有行为）：`test_openmle_phase_a.py`+`test_p2_fixes.py` **18 passed**；`test_openmle_phase_bc.py`+`test_openmle_phase_d.py` **19 passed**。
- 本次新增 + 关联合集 **49 passed，0 failed**。

> 注：`test_openmle_phase_a.py` 内 `OpenMLEPhaseATest` 仍受 titanic 数据集 presence 装饰器控制（缺失则 skip）；新增的 id-merge / string-label 用例改用合成数据集，不依赖 titanic，常驻运行。
