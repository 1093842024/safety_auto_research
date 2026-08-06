# 暂缓项单独评审分析 — L1 / L2 / L3 / L5 / L7

> 配套文档：`doc/code_review_2026-08-05.md`（全量审查 + P0–P3 修复路线）。
> 本文对修复路线中**暂缓**的 5 个 Low 级项做单独评审：逐一定位现状代码、说清缺陷本质、
> 解释当初为何暂缓（语义变更 / 回归风险）、给出**可落地的修复方案 + 风险评级**，并给出处置建议。
> 本文只做分析，不改动代码；文末给出「建议本次可修 / 建议继续暂缓」的裁决矩阵。

---

## 0. 总览与风险裁决

| 项 | 位置 | 缺陷本质 | 修复风险 | 建议 |
|----|------|----------|----------|------|
| **L2** | `kaggle_eval_executor.py:236-237` | gate 用 `op` 与排行榜 `direction` 语义分叉；lower 任务默认 `op="ge"` 实际是反的 | **低** | ✅ 本次可修（显式 `op` 优先，否则由 `direction` 派生） |
| **L3** | `eval_runner.py:137-160` | LLM-judge 任一样本抛错即 `break` 并整体回退启发式，已判分样本被丢弃 | **低** | ✅ 本次可修（`break`→`continue`，部分成功仍出分） |
| **L7** | `eval_runner.py:100-127` | 部分 key 对齐失败时静默只取对上的子集，漏评样本且不报错 | **低（仅加告警）/ 中（行序回填）** | ✅ 本次可修（加对齐统计 + 告警，零语义变更） |
| **L1** | `service.py:320` (`_recompute_top3`) | top3 方向取 `recs[0]`，同任务混入不同方向时全错 | **中** | ⚠️ 可修（按任务规范方向排序，需查 `get_task`）；建议本次修 |
| **L5** | `adapter.py:135` + `_score_submission:256/283/294` | 目标列按 dtype 决定 `astype(int/float)`，预测**恒** `astype(int)`；非整数浮点目标被截断/错位 | **中–高** | ⏸️ 继续暂缓（仅需整数标签数据集；改比较语义有回归风险） |

结论：**L2 / L3 / L7 是低风险且纯收益，建议本轮直接修；L1 中等风险但设计清晰，建议本轮修；
L5 仅影响非整数标签场景，当前内置数据集均为整数标签，继续暂缓**。

---

## 1. L2 — kaggle_eval 门限用 `op` 而非 `direction`

### 现状（代码）
`execution_plane/capabilities/kaggle_eval_executor.py:208` 取 `obj = run.objective_snapshot`，
`236-237`：

```python
op = params.get("op") or obj.get("op", "ge")
passed = (primary <= threshold) if op == "le" else (primary >= threshold)
```

而排行榜侧 `control_plane/service.py:240` 用 `direction = obj.get("direction")`（"higher"/"lower"）排序。

### 缺陷本质
`op`（`le`/`ge`）与 `direction`（`lower`/`higher`）表达的是**同一件事**（阈值比较方向），
却由两个独立字段控制，且默认不一致：
- `op` 默认 `"ge"`（line 236）；
- 若任务指标是 `lower is better`（如 `eval_loss`，见 `benchmark_tasks/registry.py:261` 的
  `"direction": "lower"`），但 `op` 未显式设置，则 gate 用 `>=`，**方向完全反了**——好模型被判 FAIL。

注意 `obj` 里其实**已经有** `direction`（capture_run_record 写入的就是它），所以 kaggle_eval
完全有条件从 `direction` 推导 `op`，只是当前没这么做。

### 为何暂缓
当初把「统一 gate 与排行榜语义」视为语义变更：若某处故意让 `op` 与 `direction` 相反，强行派生会改行为。
但经复核，**没有合法场景需要二者相反**——相反只会是 bug。因此暂缓主因是「需确认无显式相反用法」，而非真实风险。

### 修复方案（低风险，向后兼容）
**显式 `op` 优先；缺失时由 `direction` 派生**：

```python
explicit_op = params.get("op") or obj.get("op")
if explicit_op in ("le", "ge"):
    op = explicit_op
else:
    direction = str(obj.get("direction") or "higher").strip().lower()
    op = "le" if direction == "lower" else "ge"
passed = (primary <= threshold) if op == "le" else (primary >= threshold)
```

- 显式 `op`（params 或 obj）仍生效 → **不破坏现有用法**；
- 仅当未显式设置时按 `direction` 推导 → 修复 lower 任务默认反门限；
- gate 与排行榜 `direction` 自此一致。

### 回归/语义风险
**低**。唯一变化：lower 任务的默认门限从「反的」变「对的」。建议加 1 个单测：
`kaggle_eval` 在 `direction=lower`、未给 `op` 时，`gate_passed` 当 `primary <= threshold`。
无显式 `op` 的 higher 任务行为不变。

---

## 2. L3 — LLM-judge 中途失败丢弃已成功判分样本

### 现状（代码）
`control_plane/eval_runner.py:137-160`：

```python
judged: list[dict] = []
judge_error: str | None = None
for out, ref in paired[:50]:
    try:
        judged.append(call_llm_judge(prompt="", reference=ref, prediction=out, url=judge_url))
    except LLMJudgeError as exc:
        judge_error = str(exc)
        break                                          # ← 一错全弃
if judged and judge_error is None:                     # ← 部分成功也被拒
    n = len(judged)
    avg = sum(j["score"] for j in judged) / n
    return {... "n": n ...}                            # LLM 平均分
# fall through to heuristic（启发式兜底，已判分的 judged 被丢弃）
```

### 缺陷本质
第 1 条就失败 → `judged` 空 → 回退启发式（合理）。但**第 3 条失败（前 2 条已成功）** →
`break` 后 `judge_error is not None` → `judged and judge_error is None` 为 False → 整体回退启发式，
**前 2 条已判分样本被白白丢弃**。这是「部分成功当全失败」的信息损失。

### 为何暂缓
当初标记为语义变更，因为「部分 LLM 分 + 部分启发式」是否混合会影响分数。但文档建议是
**部分成功时仍用已判分样本算平均分**（不混入启发式），这其实是更忠实于 LLM 判分的做法，并非破坏语义。

### 修复方案（低风险）
`break` → `continue`，且仅当 `judged` 为空才回退启发式；部分失败时把丢弃数写入 `details` 供透明：

```python
for out, ref in paired[:50]:
    try:
        judged.append(call_llm_judge(prompt="", reference=ref, prediction=out, url=judge_url))
    except LLMJudgeError as exc:
        judge_error = str(exc)
        continue                                       # ← 跳过坏样本，保留已判分
if judged:
    n = len(judged)
    avg = sum(j["score"] for j in judged) / n
    return {
        "metric_name": metric, "direction": "higher", "score": round(avg, 6),
        "n": n,
        "details": {
            "n": n, "judge": "llm", "judge_url": judge_url,
            "partial_failed": max(len(paired) - n, 0),   # 透明：多少条没判成
            "judge_error_sample": judge_error,
            "rationales": [j["rationale"] for j in judged[:5]],
            "evidence_refs": [j["evidence_refs"] for j in judged[:5]],
        },
    }
# 全失败时再回退启发式（逻辑不变）
```

### 回归/语义风险
**低**。仅改变「部分 LLM 失败」这一窄路径的结果：从「全回退启发式」变为「用已成功样本出分」。
对全成功 / 全失败两条路径零影响。建议单测：3 条中第 2 条抛 `LLMJudgeError` 时，返回 `n=2` 的 LLM 平均分而非启发式。

---

## 3. L7 — 评测集与预测集对齐回退仅按行序且只在完全未对齐时

### 现状（代码）
`control_plane/eval_runner.py:100-127`：

```python
ref_by_prompt = {_norm(prompt): answer for ... in refs if prompt}   # 全量建索引
paired = []
if ref_by_prompt:
    for pr in preds:
        p = _norm(pred_prompt)
        if p in ref_by_prompt:
            paired.append((out, ref_by_prompt[p]))                 # 只收命中的
# 部分命中时：paired 非空但不全，下面直接用，未命中样本被静默丢弃
if not paired and len(refs) == len(preds):
    paired = list(zip(refs, preds))                                # 全未命中才行序兜底
if not paired:
    raise ValueError("无法对齐...")
```

### 缺陷本质
- **全命中 / 全未命中**：行为正确。
- **部分命中（最常见于 key 有细微差异：大小写、空格、截断）**：`paired` 只含命中的子集，
  未命中的预测与参考被**静默排除**，最终分数只基于部分样本，且**无任何告警/计数**。
  若 1000 条里只命中 3 条，你拿到一个「3 条上的高分」却以为评了全部——典型的静默错误。

### 为何暂缓
文档建议「部分对齐失败时明确告警/统计」。当初暂缓是因为若进一步「行序回填未命中部分」（混合两种对齐），
会改变配对语义、引入错配风险，属于语义变更。但**只加告警/计数本身不改变任何评分结果**。

### 修复方案
**方案 A（推荐，零语义变更，风险=无）**：部分命中时把对齐统计写进 `details`，并打日志告警。

```python
if ref_by_prompt:
    for pr in preds:
        p = _norm(pred_prompt)
        if p in ref_by_prompt:
            paired.append((out, ref_by_prompt[p]))
# 新增：部分对齐的可观测性
if 0 < len(paired) < len(preds):
    dropped = len(preds) - len(paired)
    logger.warning(
        "eval alignment partial: matched=%d, preds=%d, refs=%d, dropped=%d "
        "(key mismatch? falling back to matched subset only)",
        len(paired), len(preds), len(refs), dropped,
    )
```

并在返回 `details` 中加 `"alignment": {"matched": ..., "preds": ..., "refs": ..., "dropped": ...}`。

**方案 B（更高鲁棒性，但中风险）**：对未命中的预测/参考用行序兜底补齐。风险：当 key 部分有效、
部分无效时，行序兜底可能把不对的样本强行配对，产生错配。除非能证明「未命中部分本就该行序对齐」，
否则不建议。

### 回归/语义风险
- 方案 A：**无**（仅增加告警与统计字段）。
- 方案 B：中（改变部分命中场景的配对）。
建议只做 A；B 留作后续按需设计。单测：构造 3 条预测中 1 条 key 不匹配 → 断言 `details.alignment.dropped == 1` 且分数仍算出。

---

## 4. L1 — `_recompute_top3` 方向只取首条记录

### 现状（代码）
`control_plane/service.py:315-329`：

```python
def _recompute_top3(self, task_id: str) -> None:
    recs = self._repo.list_research_records(task_id)
    if not recs:
        return
    direction = recs[0].get("direction", "higher")          # ← 用首条决定全表方向
    def _sort_key(r):
        s = r.get("score", 0.0)
        return (0 if math.isfinite(s) else 1, -s if direction != "lower" else s)
    ordered = sorted(recs, key=_sort_key, reverse=False)
    for i, r in enumerate(ordered):
        self._repo.update_research_record(r["record_id"], is_top3=(i < 3))
```

而 `capture_run_record`（line 303）把每条记录的 `direction` 写自 `obj.get("direction")`。

### 缺陷本质
排行榜排序方向依赖**任意一条记录**（`recs[0]`）。若同一 `task_id` 下混入不同 `direction`
（例如任务指标方向被改过、或 tracked-only 任务跨多次运行方向不一致、或 `objective_snapshot.direction`
与任务规范方向偶发不符），则所有 top3 都用 `recs[0]` 的方向排——可能把「越低越好」误当「越高越好」，
top3 完全排反。

### 为何暂缓
文档建议「写入时强制同任务方向一致」——这会改变**写入语义**（冲突时丢弃/改写记录），且需要
迁移既有混方向数据，属于语义变更 + 数据风险，故暂缓。

### 修复方案（中等风险，但设计清晰）
**不依赖任意记录，改按「任务规范方向」排序**（读取即修复，不改写入）：

```python
def _canonical_direction(self, task_id: str) -> str | None:
    # 任务目录里任务有规范 direction（benchmark_tasks/registry.py:884 已存）
    try:
        from safety_auto_research.benchmark_tasks import get_task
        t = get_task(task_id)
        if isinstance(t, dict) and t.get("direction"):
            return str(t["direction"]).strip().lower()
    except Exception:
        pass
    return None

def _recompute_top3(self, task_id: str) -> None:
    recs = self._repo.list_research_records(task_id)
    if not recs:
        return
    # 规范方向优先；查不到（非注册任务）时回退到首条，保持历史行为
    direction = self._canonical_direction(task_id) or recs[0].get("direction", "higher")
    ...
```

- `benchmark_tasks.registry` **不 import `control_plane`**（已核实），故 service 单向 import 无循环依赖；
- `get_task` 对自定义/注册任务均返回含 `direction` 的 dict（registry.py:884）；
- **向后兼容**：查不到规范方向时回退 `recs[0]`，与现状一致；只有「存在规范方向且记录方向与之冲突」时才改变结果，而这恰恰是修复目标。

> 若日后要更进一步「写入时强制一致」（文档原建议），需配套数据迁移脚本（把历史混方向记录归一或标记），
> 不建议本轮做——读取侧修复已覆盖绝大多数正确性需求。

### 回归/语义风险
**中**。风险点：① 引入 `control_plane → benchmark_tasks` 的新依赖（已确认无环）；② 对「有规范方向但记录方向写错」的任务，top3 会翻转——这是正确的修复，但会改变这类任务的榜单结果（属预期改善）。
建议单测：`get_task` 返回 `lower` 的任务，注入一条 `direction="higher"` 的 record，断言 `_recompute_top3` 用 lower 方向排。

---

## 5. L5 — `eval_truth` 浮点目标列类型处理

### 现状（代码）
`openmle_integration/adapter.py:135`：

```python
"eval_truth": eval_df[target].astype(int if df[target].dtype != float else float).values,
```

其中 `df` 是**训练集** DataFrame，用它推断目标列 dtype 决定 `eval_truth` 的 cast。
而 `_score_submission`（226-300）里预测**恒**被 `astype(int)`（line 256 / 283），最终
`correct = int(np.sum(preds == truth))`（line 294）做**精确整数相等**比较。

### 缺陷本质
- 当前内置数据集标签均为**整数**（registry 里 `tabular_classification` 类任务），`df[target]` 为 int →
  `eval_truth` 为 int，`preds` 为 int → 精确相等正确。
- 隐患场景（边缘）：
  1. 目标含**非整数浮点值**（如 0.5/1.5 的浮点标签、或回归式标签）：训练列若是 float → `eval_truth` 保 float，
     但 `preds` 被**强制 `.astype(int)`** → `int(0.5)=0 != 0.5` → 全部判错，分数恒为 0。
  2. 训练列恰为整数 dtype、但真实应为 float（如全为整数值的连续量）：`eval_truth` 变 int，截断。
- 根因：用「列 dtype」而非「值是否真的整数」来决定 cast，且预测侧硬编码 int。

### 为何暂缓
修复需改**评分比较语义**（整数精确相等 → 浮点容差 `np.isclose`，或支持浮点标签的准确率/误差 metric），
这会同时影响当前整数标签数据集的行为（如容差边界），属语义变更；且当前数据集不触发该 bug。故暂缓。

### 修复方案（中–高风险，需产品决策）
**最小安全改进**：当目标列含非整数值时，保持 float 并用容差比较：

```python
truth_vals = eval_df[target].values
is_int_like = np.allclose(truth_vals.astype(float), truth_vals.astype(int).astype(float))
eval_truth = truth_vals.astype(int) if is_int_like else truth_vals.astype(float)
# _score_submission 中 preds 也相应：整数标签用 ==，浮点标签用 np.isclose(tol)
```

但这要求 `_score_submission` 的 `preds == truth` 分支按 `is_int_like` 选择 `==` 或 `np.isclose`，
**改变所有任务的比较路径** → 回归风险。

**更稳妥的处置**：保持现状（整数标签场景正确），把 L5 标记为
「**仅在接入非整数标签任务时才修**」的待办；或在任务配置里显式声明 `label_dtype: int|float`，
由配置驱动 cast，避免全局语义变更。

### 回归/语义风险
**中–高**。任何改动都会波及当前整数标签数据集的评分路径；且浮点容差阈值本身需要标定（否则 0.999≈1.0 误判）。
**建议继续暂缓**，除非有明确的非整数标签评测需求；届时按「配置驱动 dtype + 容差比较」单独设计。

---

## 6. 处置裁决矩阵（建议）

| 项 | 本文建议 | 改动量 | 回归风险 | 是否本轮修 |
|----|----------|--------|----------|------------|
| L2 | 显式 `op` 优先，否则由 `direction` 派生 | ~4 行 | 低 | ✅ 修 |
| L3 | `break`→`continue`，部分成功仍出分 + 透明统计 | ~8 行 | 低 | ✅ 修 |
| L7 | 加对齐统计 + 告警（方案 A，零语义变更） | ~10 行 | 无 | ✅ 修 |
| L1 | 按 `get_task` 规范方向排序（读取侧修复） | ~15 行 | 中 | ⚠️ 建议修（需单测） |
| L5 | 维持整数标签现状，标记为非整数标签需求驱动 | 0（或配置驱动重做） | 中–高 | ⏸️ 暂缓 |

**总评**：5 项中 3 项（L2/L3/L7）是低风险纯收益，应直接纳入本轮；L1 设计清晰、向后兼容，建议本轮一并修；
L5 因涉及全局评分语义、且当前数据集不触发，继续暂缓并转为「按需触发」的待办。

> 若确认按本裁决实施，建议在 `tests/` 补 4 个针对性单测（L2 门限方向 / L3 部分失败出分 / L7 部分对齐告警 / L1 规范方向排序），
> 落入现有 `test_fix_regression_*` 或对应模块测试文件，确保修复不被回退。

---

## 7. 修复落地记录（2026-08-06，按裁决矩阵实施 L1/L2/L3/L7）

### 裁决执行结果

| 项 | 状态 | 改动文件 | 单测 |
|----|------|----------|------|
| L2 | ✅ 已修 | `execution_plane/capabilities/kaggle_eval_executor.py`（新增 `derive_gate_op` 纯函数 + 调用点） | `L2GateOpTest`（显式 op 优先 / lower→le / higher→ge） |
| L3 | ✅ 已修 | `control_plane/eval_runner.py`（`break`→`continue` + 透明 `partial_failed`/`judge_error_sample`） | `L3PartialJudgeTest`（3 中第 2 条失败 → n=2 平均分 0.7，非回退启发式） |
| L7 | ✅ 已修 | `control_plane/eval_runner.py`（新增 `alignment` 统计 + `logger.warning` 告警，零语义变更） | `L7AlignmentTest`（3 预测中 1 key 不匹配 → `dropped==1` 仍可出分） |
| L1 | ✅ 已修 | `control_plane/service.py`（新增 `_canonical_direction`，按 `get_task` 规范方向排序，回退 `recs[0]`） | `L1DirectionTest`（规范 lower 翻转排名 / 未知任务回退首条） |
| L5 | ⏸️ 继续暂缓 | — | — |

### 关键设计点
- **L2** 抽成模块级 `derive_gate_op(params, obj)`，显式 `op`（`params` 或 `objective`）优先，否则由 `direction` 派生；既可被单测直接覆盖，也避免在内联重逻辑里反复引入分支。
- **L3** 仅改「部分失败」这一窄路径：已判分样本保留、坏样本 `continue`、失败计数写入 `details.partial_failed`；全成功 / 全失败两条路径行为完全不变。
- **L7** 纯可观测性增强：在 `paired` 建好后统计 `matched/dropped` 并 `logger.warning`；`alignment` 字典同时并入 LLM-judge 与启发式两条 `details` 返回，评分结果零变化。
- **L1** 读取侧修复、不改写入；`_canonical_direction` 复用 service 已 import 的 `get_task`（无新依赖、无循环依赖）；查不到规范方向时回退 `recs[0].direction`，与历史行为一致。

### 回归结果（从父目录分批跑，全绿）
- `tests/test_fix_regression_2026_08_05.py`：**18 passed**（新增 L1/L2/L3/L7 共 7 例）
- `tests/test_capabilities.py` + `tests/test_benchmark_registry.py`：**25 passed**
- `tests/test_evolution.py` + `tests/test_review_supplementary.py`：**22 passed**（80s）
- `tests/test_dual_loop.py`：**8 passed**
- 合计相关套件 **73 passed**；改动模块均 `py_compile` 通过。

> L5 维持暂缓：仅在接入非整数标签评测任务（回归/浮点标签）时再按「配置驱动 dtype + 容差比较」单独设计。
