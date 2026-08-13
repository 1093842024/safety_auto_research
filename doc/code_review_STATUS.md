# safety_auto_research 代码审查与修复状态（活文档）

> ⚠️ **本文件是代码审查的权威活文档**。2026-08-07 文档树重构时，将原先分散的 `code_review_2026-08-05.md` + `review_deferred_L_items_2026-08-05.md` + `code_review_2026-08-06.md` 三份合并于此，并把更早的 `code_review_2026-07-30_round3.md`、`code_review_2026-08-04.md` 移入 `doc/archive/`。
> **维护规则**：后续每一轮审查**只更新本文件**（追加/修订条目与修复状态），不再新建带日期的审查文件；若必须保留历史快照，移入 `doc/archive/`。这样可避免审查文档无限膨胀。

**日期**：2026-08-05（末次全量审查）；附录 A/B 为 08-06 补充
**范围**：`safety_auto_research/` 整个项目（后端 Python + 前端 React/TS/Vite）
**方法**：结构普查 + 三个并行深度探索代理（后端架构 / 后端核心逻辑 / 前端）定位缺陷，再由主代理对全部 Critical/High 及关键 Medium 项**逐条回读源码核实**（file:line 已核对）。
**说明**：本报告在 2026-08-04 审查（已修复 P1-1~4 + P2×12）基础上做新一轮全量审查。标注「✅已核实」的条目为主代理直接读源码确认；其余来自探索代理，结论与代码一致。

---

## 0. 严重度汇总

| 严重度 | 数量 | 关键项 |
|---|---|---|
| 🔴 Critical | 1 | C1 模型代码以宿主全权限+全密钥环境变量执行（RCE/泄露） |
| 🟠 High | 4 | 缺陷1 隔离契约在 HTTP 面被打破；缺陷2 四个 run 端点同步阻塞+不可取消+状态不收尾；缺陷3 累积状态存储无锁并发崩溃；H1 多进程持久化数据损坏 |
| 🟡 Medium | 9 | M1 排行榜排序反了；M2 NaN/inf 无防护；M3 fe=bool 崩溃；M4 reward 把 0.0 当 1.0；M5 adapter 忽略 fe；M6 zip bomb；缺陷4/5/6/7 |
| 🟢 Low | 8 | 缺陷8/9/10/11；L1~L7 |

**结论**：架构设计（双循环 + 元循环 + 岛模型进化 + 审计隔离）整体合理，隔离不变量在 agent 路径下基本成立；但 **安全面有一条致命链（C1）**，且**隔离契约在 HTTP 工具面（缺陷1）、并发状态（缺陷3）、长任务生命周期（缺陷2）三处被打破或缺失**，必须在生产化前修复。前端与后端 API 路由**完全对应**、主响应结构一致，但存在「高影响功能缺口（无取消运行）」「契约漂移对用户静默」「无数据层/超时」「错误处理不一致」等可优化项。

---

## 1. 后端 — 安全与隔离（Critical / High）

### C1 🔴 模型（LLM 算子）生成代码以宿主全权限、含全部密钥的环境变量执行（无沙箱）
- **位置**：`openmle_integration/interpreter.py:49-72`（`run()`），调用链 `adapter.py:150-165` `_step_code` → `OpenMLETaskAdapter.step_task`。
- **问题**：`env = dict(os.environ)` 把宿主**全部**环境变量（含 `LLM_JUDGE_URL`、`OPENAI_API_KEY`、DB 路径、token 等）传给子进程；程序来自 LLM 算子（`draft/improve/debug/crossover_program`）生成的**不可信代码**，用 `subprocess.run` 直接执行，仅有 600s 超时，**无容器/chroot/seccomp、无网络隔离、无内存/CPU/子进程数限制、cwd 虽为临时目录但程序可用绝对路径读写任意文件**。
- **影响**：任意代码执行（RCE）；一行 `requests.post(..., json=dict(os.environ))` 即可外泄全部密钥；宿主被破坏。这是全平台最危险的攻击链——任何能影响算子 prompt 的输入（被污染的评测数据集、恶意任务描述）都可能驱动模型写出攻击代码。
- **✅已核实**。
- **修复**：在受限环境执行（gVisor/Firecracker/容器 + 只读根 + 独立网络命名空间）；**不要**传真实 `os.environ`，仅传最小必需变量（如 `OPENMLE_WORKDIR`、必要的 `PYTHONPATH`）；加资源上限 + 文件系统挂载裁剪。即便短期无法上容器，也至少应 `env = {k: v for k,v in os.environ.items() if k in ALLOWED}` 并清空所有 `*_KEY`/`*_TOKEN`。

### 缺陷1 🟠 隔离不变量(2)在 HTTP `/capabilities/{id}/run` 端点未被强制
- **位置**：`control_plane/api.py:470-485`（`run_capability` 端点）；`execution_plane/orchestrator.py:224-299`（`run_capability` 本体）。
- **问题**：该端点直接 `orchestrator.run_capability(run_id, capability_id, req.params)`，二者**均无** `assert_inner_capability_allowed` 调用。而真正的守卫 `assert_inner_capability_allowed`（`execution_plane/agent/harness.py:83-91`）只被 `RemoteAgentHarness._tool_handler` 调用。**`run_capability` 本体解析任意 capability 即执行**（orchestrator:244-286），对 `layer_11_external_audit` / `layer_09_self_iterative_evolution` 无任何拦截。
- **影响**：任何客户端 `POST /workflow-runs/{run_id}/capabilities/layer_11_external_audit/run` 都能以内循环“能力”身份运行外审计/自进化，让内循环“评估/改写自己的结果”，**直接违反文档不变量(2)**，破坏审计独立性。`tests/test_dual_loop.py::ContextSeparationTest` 只覆盖 harness 路径，**HTTP 面无测试**，该回归不会被发现。
- **✅已核实**（含守卫本体与其调用点）。
- **修复**：在 `api.py:475` 端点与 `orchestrator.run_capability` 入口处加入 `from ..execution_plane.agent.harness import assert_inner_capability_allowed; assert_inner_capability_allowed(capability_id)`；并补一个 HTTP 面的回归测试。

### 缺陷2 🟠 四个“运行”端点同步阻塞请求线程，不传 cancel_event、不收尾 run 状态
- **位置**：`control_plane/api.py:492-496`（`run_closed_loop`）、`510-540`（`run_dual_loop_endpoint`）、`547-576`（`run_evolution_endpoint`）、`597-631`（`run_mea_endpoint`）。
- **问题**：四个端点都在请求处理函数里**同步**调用 `orchestrator.run_*_loop(...)` 并直接 `return`，既未用后台线程模板（`launch_benchmark_task` 用的 `_spawn_bg_thread`/`_drive`，见 `api.py:1125-1161`、`1414-1448`），也**未把 `cancel_event=_run_cancel_events.get(run.run_id)` 传入循环**，且**从不调用 `svc.set_run_status(...)`**。
- **影响**：① 整段研究在 HTTP worker 线程内阻塞，单 worker 部署下饿死其它请求、客户端长时间挂起/超时；② 通过 `POST /.../cancel` 取消这些端点启动的 run **完全无效**（cancel 仅设置 `_run_cancel_events`，循环从不读取）；③ 循环正常返回后 WorkflowRun 始终停在 `RUNNING`，UI 永远显示“运行中”，且 `capture_run_record` 不触发（对照后台 `_drive` 在 `api.py:1142` 收尾）。
- **✅已核实**。
- **修复**：统一改为 `_spawn_bg_thread` + `_drive` 模式，传入 `cancel_event` 并在 `finally` 中 `set_run_status(...)` + `expire_pending_strategies(...)`。

### 缺陷3 🟠 累积状态存储无锁，多 run 并发时数据竞争 / 崩溃
- **位置**：`control_plane/store_tree.py`（`HypoTreeStore`/`ExperienceBank`/`StrategyArchive`/`PlaybookStore` 均无锁——grep `threading.Lock|RLock|self._lock` 在该文件**零命中**）；`control_plane/evolution.py:475-537`（`EvolutionArchive._cands` 普通 dict 无锁）。
- **问题**：`create_app`（`api.py:137-156`）只创建一个共享 `ResearchStateStore`，所有并发 run 的后台线程共享它，但这些存储只用普通内存 dict，**完全没有加锁**。`run_dual_loop` 每轮 `observe_hypothesis`(写 `_nodes`)、`compact_research_state`(迭代 `_nodes.values()`)、`register_experience`(写 `_entries` 并迭代去重)、`strategy_archive.commit/update`，以及 `run_evolutionary_loop` 写共享 `state_store.evolution`。
- **影响**：两个 run 并发时，一个迭代 `list(...)` 另一个插入 → `RuntimeError: dictionary changed size during iteration` 整条 run 崩溃；即使不崩溃，去重/读改写非原子会丢更新。`Repository`(`store.py`) 虽有全局锁，但保护的集合与累积状态是两套独立对象，后者裸露。
- **✅已核实**（store_tree 无锁已 grep 确认）。
- **修复**：给每个累积存储加 `threading.Lock`（或按 `run_id` 分片锁）；`EvolutionArchive._cands` 同样加锁。

### H1 🟠 持久化仅进程内锁，多进程/多 worker 部署下数据损坏与整体丢失
- **位置**：`control_plane/store.py:51,109-131,141-280`；`benchmark_tasks/registry.py:778-800`（`_LOCK` 同为模块级 `threading.Lock`）。
- **问题**：仓库与自定义任务存储都用**每进程** `threading.Lock`，各自把整个 JSON 整体 `os.replace` 落盘。若以多进程方式运行（`uvicorn --workers N` 或起多实例），多进程各自持一份内存状态，并发写同文件会**互相覆盖（lost update）**；更糟的是某进程写入中途被另一进程截断 → JSON 损坏 → 下次启动 `_load` 把损坏文件移入 `*.corrupt.*` 并**清空启动（全部记录丢失）**。
- **影响**：研究记录（排行榜）、工作流运行、自定义任务注册等数据在真实部署下可被静默破坏/整体丢失。
- **修复**：加跨进程文件锁（`fcntl.flock` / `portalocker`），或改用 SQLite（自带文件级锁，且支持增量写入，同时解决缺陷4/M7 的性能问题）。

---

## 2. 后端 — 并发、状态与持久化（Medium / High）

### 缺陷4 🟡 `Repository` 每次写入整体重写整个 JSON（持锁做重 IO）
- **位置**：`control_plane/store.py:109-131`（`_persist`），被 `append_event`/`record_metric`/`put_stage_run`/`put_artifact` 每次调用，全程持 `self._lock`。
- **影响**：长 run 产生成百上千事件/指标，每次都全量序列化+替换落盘，持锁做重 IO；多 run 共享一把锁后写入被串行化，吞吐被严重拖慢，且放大 H1 的覆盖窗口。
- **修复**：事件/指标改为批量/异步落盘或独立文件；或迁移 SQLite 增量写。

### 缺陷5 🟡 `resolve_collaboration` 在 `resolve_approval` 抛异常时会死锁后台循环线程
- **位置**：`control_plane/api.py:1597-1631`；对照 `execution_plane/orchestrator.py:1784-1822`（`_collab_pause`）。
- **问题**：`resolve_collaboration` 先 `with lock:` 写入 `adjustments/rejected`，再 `svc.resolve_approval(...)`；若 `resolve_approval` 抛 `ConflictError`（如 run 当前不在 WAITING_APPROVAL），异常在 `api.py:1623` 被 `_translate` 重抛，第二段 `with lock: pauses[run_id].get("evt").set()` 永远不执行。后台 `_collab_pause` 轮询只在 `evt.set()` 或 run 变 FAILED/CANCELLED 时返回，无总超时。
- **影响**：后台线程**永久阻塞**在协作暂停点，run 卡死无法恢复。
- **修复**：把 `evt.set()` 移入 `finally`，`resolve_approval` 失败时主动将 run 置 FAILED。

### 缺陷6 🟡 `ProgressBus` 非线程安全（async 循环线程 vs 后台线程并发改 set/dict）
- **位置**：`control_plane/progress_bus.py:30-119`（`_queues`/`_conn_counts` 普通 dict，`self._queues[run_id]` 普通 set）。
- **影响**：订阅者接入/断开（循环线程改 set）与后台 `emit`→`_enqueue` 迭代同一 set 并发时，可能抛 `RuntimeError: Set changed size during iteration`；偶发 SSE 报错或漏发进度。
- **修复**：对 `_queues`/`_conn_counts` 加锁，或对 set 迭代用快照 `list(...)`。

### 缺陷7 🟡 `run_program_evolutionary_loop`（程序级岛模型 / OpenRSI）在 API 层完全未接线
- **位置**：`execution_plane/orchestrator.py:1440` 定义；`grep run_program_evolutionary_loop control_plane/api.py` **零命中**；仅 `tests/test_openmle_phase_bc.py:238` 测试调用。
- **影响**：架构文档/README 称为核心特性的“程序级进化搜索（OpenRSI）”在生产后端/前端**完全不可达**——仅测试可达。属孤儿路径：功能看似存在、用户却无法触发；其依赖的共享 `state_store.evolution`（缺陷3）也仅测试覆盖。
- **✅已核实**（grep api.py 零命中）。
- **修复**：补后台化 HTTP 端点（复用 `_drive` 模板），或显式标注为测试专用并移除文档误导。

### 缺陷8 🟢 同步运行端点异常时不调 `expire_pending_strategies`，待验证元循环提案泄漏
- **位置**：`control_plane/api.py:530/562/626/494` 无 `try/finally`；对照后台 `_drive`（`api.py:1154-1159`）有 `finally: expire_pending_strategies`。
- **影响**：`strategy_archive` 残留 `pending_verification` 条目，污染后续 run，`pending()` 语义失真。

### 缺陷9 🟢 `run_dual_loop` 在 `max_outer_iters <= 0` 时引用未绑定变量 `inner_acc`
- **位置**：`execution_plane/orchestrator.py:1087`（`_emit("finished", reason="budget", accuracy=inner_acc)`）；`inner_acc` 仅在 `while outer < max_outer_iters` 体内定义（`:752`）。
- **影响**：边界参数下抛 `UnboundLocalError`。默认 3 不触发，但属防御性缺陷。

### 缺陷10 🟢 `_run_cancel_events` 字典只增不删（轻微内存泄漏）
- **位置**：`control_plane/api.py:56,246`（`setdefault` 创建并永久保留每 run_id 的 `Event`）。
- **影响**：进程长期运行后该 dict 单调增长。

### 缺陷11 🟢 `run_dual_loop` 中 `run_self_evolution` 的 `except NotFoundError` 过窄
- **位置**：`execution_plane/orchestrator.py:1027`（仅 `except NotFoundError: pass`）；`run_capability` 在 `:286-293` 捕获执行异常后 `raise` 透传。
- **影响**：一旦接入真实 layer_09 执行器且其执行期抛运行时异常，不会被 `except NotFoundError` 捕获，会冒泡使整个双循环崩溃（预期是“layer_09 是 stub 则优雅跳过”）。

---

## 3. 后端 — 核心逻辑正确性（Medium / Low）

### M1 🟡 排行榜 `list_research_records` 排序方向反了——top3（最优）被排到最后
- **位置**：`control_plane/service.py:409-412`：
  ```python
  def _key(r):
      rev = r.get("direction","higher") != "lower"
      return (r.get("is_top3", False), -r.get("score",0.0) if rev else r.get("score",0.0))
  return sorted(recs, key=_key, reverse=False)  # 注释意图：top3 在前
  ```
- **问题**：升序排序时 `is_top3` 为 `True(1) > False(0)`，故 top3 记录排在非 top3 **之后**。实际结果是「所有非 top3 按分排好，再是所有 top3 按分排好」——最优记录落在列表底部。注释声明的意图（top3 优先）与实现相反。
- **影响**：`GET /research-records`（按任务列出）返回列表以最差条目开头、最优条目结尾，严重误导。注意全局 `leaderboard()`（service.py:414-434）的 `is_better` 逻辑是正确的，此 bug 仅在该按任务列表。
- **✅已核实**。
- **修复**：元组首位改为 `not r.get("is_top3", False)`，或整体 `reverse=True`。

### M2 🟡 分数中的 NaN/±inf 全程未防护（捕获、排序、上榜）
- **位置**：`control_plane/service.py:246-275`（`_best_dir` 用 `max/min` 对含 NaN 列表行为未定义）、`:285`（`round(float(target),6)` 可得到 `nan`/`inf`）、`:415-434`（`leaderboard()` 仅 `if r.get("score") is None` 跳过，不拦 NaN/inf）。
- **影响**：`inf` 可霸榜、`nan` 使排序位置未定义、记录无法正确排序；含 NaN 的指标一旦被记录即污染榜单。
- **修复**：捕获与入榜前用 `math.isfinite` 过滤/兜底。

### M3 🟡 协作调整传入 `fe` 为 bool 时，kaggle_eval 执行器崩溃
- **位置**：`execution_plane/orchestrator.py:1833-1834`（`inner_params["fe"] = adj["fe"]`，未做类型校验）→ `execution_plane/capabilities/kaggle_eval_executor.py:111`：
  ```python
  fe = (params.get("fe") or "basic").lower()   # 若 fe=True → True.lower() → AttributeError
  ```
- **影响**：一次合法的「开启 rich 特征」协作调整会让评估直接 `AttributeError` 失败（被 `orchestrator.py:287` 捕获标 FAILED）。
- **✅已核实**（kaggle_eval_executor.py:111）。
- **修复**：执行器内统一 `str(params.get("fe") or "basic").lower()`，并在 `_apply_collab_adjustments` 对 `fe` 字符串化。

### M4 🟡 `reward_population` 把 `novelty=0.0`（精确重复）当成 `1.0`
- **位置**：`openmle_integration/reward_bridge.py:178`：`novelty=getattr(c, "novelty", 1.0) or 1.0`。
- **问题**：候选为精确重复（`novelty=0.0`）时，`0.0 or 1.0` 因 `0.0` 为假值回退 `1.0`，使本应最低多样性的重复程序获满额多样性奖励。
- **修复**：`nov = getattr(c,"novelty",None); nov = nov if nov is not None else 1.0`（仅对 `None` 兜底，保留 `0.0`）。

### M5 🟡 `OpenMLETaskAdapter._build_preprocessor` 完全忽略 `fe` 参数
- **位置**：`openmle_integration/adapter.py:312-327`（签名有 `fe="basic"`，函数体从未使用）。
- **影响**：程序级进化走 dict/config 路径（`_step_config`）时「rich」特征工程是**空操作**，始终等价于 basic；而真正的 rich 特征只在 `kaggle_eval_executor.py:324-341` 实现，两者行为不一致，实验结果不可比。

### M6 🟡 数据集上传仅限原始字节、解压后无上限（zip bomb DoS）
- **位置**：`control_plane/api.py:848-894`（`upload_dataset` 仅查 `len(content) > 512MB`，随后 `zf.extractall(dest_dir)` 对解压后总大小无上限；非 zip 任意类型也原样写入 `data/custom_uploads/`）。zip-slip 已被 `target.startswith(realpath(dest_dir)+sep)` 拦住。
- **修复**：解压时累计写入字节/文件数上限，超限中止并清理；按白名单限制可上传扩展名。

### M7 🟡 每次写操作整体重写整库（放大 H1 竞态）
- **位置**：`control_plane/store.py:109-127,141-280`（`put_workflow_run`/`record_metric`/`append_event`/`put_research_record` 等每次都 `_persist()` 全量序列化）。
- **修复**：同缺陷4。

### L1 🟢 `_recompute_top3` 方向只取首条记录
- **位置**：`control_plane/service.py:296-304`（`direction = recs[0].get("direction","higher")`）。若同一任务混入不同方向，`recs[0]` 方向用于全部 top3 计算，导致错误；建议在写入时强制同任务方向一致。

### L2 🟢 kaggle_eval 门限用 `op` 而非 `direction`
- **位置**：`kaggle_eval_executor.py:233`（`passed = (primary<=threshold) if op=="le" else (primary>=threshold)`），而 `capture_run_record` 用 `direction`。建议让 `op` 由 `direction` 推导（`lower -> le`），避免两处语义分叉。

### L3 🟢 LLM-judge 中途失败丢弃已成功判分样本
- **位置**：`control_plane/eval_runner.py:139-160`（某条抛 `LLMJudgeError` 即 `break` 并整体回退启发式，前面已判分样本被丢弃）。建议部分成功时仍用已判分样本算平均分。

### L4 🟢 空文本 embedding 全零向量导致永不判重
- **位置**：`control_plane/evolution.py:89-98`（`_embed`）+`:101-102`（`_cosine`）。空配置归一化后 `_embed` 返回全零向量，与任意向量余弦为 0，`sim>=0.92` 永不成立 → 空配置永不被视为重复。

### L5 🟢 `eval_truth` 浮点目标列类型处理
- **位置**：`openmle_integration/adapter.py:135`（`eval_df[target].astype(int if df[target].dtype != float else float)`）。含非整数浮点目标时预测被 `astype(int)` 比较会错位（当前内置数据集为整数标签，属边缘）。

### L6 🟢 注册/删除自定义任务假设 `task_id` 键必存在
- **位置**：`benchmark_tasks/registry.py:780`（`existing = {it["task_id"] for it in items}`）。若 `custom_tasks.json` 某条缺 `task_id`（手动改坏）会抛 `KeyError`，建议用 `it.get("task_id")`。

### L7 🟢 评测集与预测集对齐回退仅按行序且只在完全未对齐时
- **位置**：`control_plane/eval_runner.py:118-122`。部分 key 对齐失败时只用对上的部分而非回退行序，可能漏评大量样本而不报错；建议部分对齐失败时明确告警/统计。

---

## 4. 不变量执行核对（对照文档 4 条隔离不变量）

| 不变量 | 代码实际 | 结论 |
|---|---|---|
| (1) 审计只读 curated input，绝不读内循环事件 | `run_dual_loop` 组装 `audit_input`（仅 objective/metrics/verdict/prior_audits，orchestrator:853-863）；`AuditExecutor` 忽略事件日志（audit_executor.py:98-128）；`test_outer_audit_ignores_inner_narrative` 通过 | **已执行**（唯一泄漏面是被缺陷1 的 HTTP 端点允许以 `answer=` 注入内循环叙述） |
| (2) inner `run_capability` 禁用集 {layer_11, layer_09}；算子走 `assert_operator_inner_only` 白名单 | 算子路径：evolution.py:300/342/347/381 均传 `caller_stage="inner_program_evolution"` 经校验 ✅；agent 路径：`RemoteAgentHarness._tool_handler` 调 `assert_inner_capability_allowed` ✅。**但** HTTP 端点 `api.py:475` 与 `run_capability` 本体无禁用集检查 ❌（缺陷1） | **部分执行——HTTP 工具面被破坏** |
| (3) 累积对象必须携带 `run_id` | `observe_hypothesis`/`compact_research_state`/`archive.add`/`register_experience`/`strategy_archive.commit` 均传 `run_id` | **已执行** |
| (4) playbook/experience 只注入内循环，绝不入 `audit_input` | `run_dual_loop` 把二者折入 `inner_params`/`dispatch_goal`（:711-725），`audit_input` 单独构建不含二者（:853-863） | **已执行** |

---

## 5. 前端 — 架构、功能与前后端匹配度

### 5.1 结构地图
- **路由/页面**：单页应用，`App.tsx` 用 `view` 状态机切换（非 React Router），6 个顶层视图 + RunDashboard 内 9 个子标签（总览/调试/审计/假设树/改进/进化/审批/Agent流水/事件）。
- **API 客户端**：`src/api/client.ts`（`apiGet`/`apiPost` 基于原生 `fetch(/api${path})`，**无超时、无 AbortController`**）；`src/api/useSSE.ts`（`EventSource` 订阅 `/api/workflow-runs/{id}/stream`，事件名与后端 `progress_bus.py` 一致）；`src/contracts.ts`（Zod schema 自动生成，勿手改）。
- **状态管理**：**无 React Query/Redux/Zustand**，全部 `useState`+`useEffect` 手动轮询（App 每 4s 拉 `getRuns`，各视图自拉）。
- **构建**：`vite.config.ts:10-15` `/api`→`http://127.0.0.1:8000`，**无硬编码绝对 URL**（已 grep 确认）。

### 5.2 前后端匹配度
- **(a) 前端调用但后端未暴露**：**无**。25 个 `client.ts` 函数全部命中 `api.py` 已注册路由（含 SSE `/stream`）。✅ 正向结论。
- **(b) 后端暴露但前端未使用（高/中影响功能缺口）**：
  - `POST /workflow-runs/{id}/cancel`（`api.py:238-249`）——**前端无任何“取消运行”入口**（grep `cancel` 仅命中模态框 `onCancel` 与状态文案“已取消”，无实际调用）。跑到一半的 run 无法从 UI 中止。**🔴 高影响缺口**。
  - `GET /experiences`（`api.py:686-691`）—— 跨 run 经验银行，前端未调用；`client.ts:114-121` 的 `ExperienceEntry` 接口是**死代码**（无 `getExperiences`）。
  - `POST /workflow-runs/{id}/evaluate`、`/report-metric`（`api.py:1244,1260`）—— tracked-only 任务的评测/上报，无 UI。
  - `GET /benchmark-suites/...`（`api.py:719-800`）—— 外部基准套件，纯未发布功能。
- **(c) 响应结构（response-shape）**：主数据形状前后端**一致**（审计/AuditBoard、进化候选/EvolutionCandidate、Playbook、假设树/HypothesisTree、compare_runs/CompareRuns、Agent 协议/NewResearch 逐项核对通过）。
  - **关键元问题**：`safeParse` 失败时不抛错、直接 `return data as T`（`client.ts:25`）——任何契约漂移**不会报错、只在 UI 静默显示 undefined/空白**。配合 `tsconfig.json`（`noUnusedLocals/Parameters:false`）与大量 `as`/`any`，**类型保护实效性很弱**，契约回归对用户不可见。**🟡 中（正确性隐患）**。

### 5.3 UX 与代码质量
- **错误处理不一致**：`apiGet/apiPost` 在非 2xx 抛错；但 `DualLoopLive/AuditBoard/HypothesisTree/ImprovementTimeline/EventStream/ApprovalConsole/EvolutionPanel` 仅 `console.warn`、UI 静默。后端宕机时这些面板显示“暂无数据”，**看上去像没数据而非后端错误**，误导用户。**🟡 中**。
- **无 fetch 超时 / AbortController**：后端挂起时请求永久挂起。**🟡 中**。
- **空值安全不一致（潜在崩溃）**：`NewResearch.tsx:43` `Object.keys(t.gates).length` 未守卫；而 `BenchmarkCatalog.tsx:42` 用了 `t.gates || {}`。若任一任务 `gates` 缺失，`NewResearch` 的 `metricPill` 会抛 `Cannot read properties of undefined`。**🟡 中**。
- **重复常量映射**：`client.ts:518-558` 是状态/裁决映射单一事实源，但 `DualLoopLive.tsx:54-73` **又本地复制一份**；日后在 `client.ts` 加新状态，`DualLoopLive` 不会体现 → 漂移隐患。**🟡 中**。
- **可访问性（a11y）**：表单 `<label>` 未用 `htmlFor`/`id` 关联（`ConfigForm`、`RegisterTask`、协作表单）；无 `aria-live` 区域，错误横幅与实时进度对辅助技术不可见。**🟡 中**。
- **类型弱化**：`RunDashboard.tsx:117-118` 把 `cfg`/`il` 声明为 `any`，绕过类型检查。**🟡 中**。
- **重复拉取**：App 每 4s `getRuns` + RunDashboard 每 3s `getRun`+`getEvents`，且 `sub==="live"` 时 `DualLoopLive` **再每 3s 拉一遍同一 run 的 `getRun`+`getEvents`**——同一数据被同时拉两次。**🟡 中（性能/冗余）**。
- **死代码**：`ExperienceEntry` 接口未使用；`tsconfig` 严格度低使死代码不被发现。**🟢 低**。
- **正面项**：`dangerouslySetInnerHTML` grep **零命中**；React keys 基本稳定（用 `run_id`/`audit_id`/`node_id`/`candidate_id`）；空状态占位符普遍良好；响应式 `@media` 折叠侧栏合理。

### 5.4 前端优化机会（具体可落地）
1. **引入数据层（React Query / SWR）**——统一缓存、去重、后台刷新、SSE 触发 `invalidateQueries`；一举消除“双拉 getRun/getEvents”与“无超时/无取消”。**🔴 高优先级**。
2. **加“取消运行”按钮**——RunDashboard 状态头为非终态时显示，调用 `POST /workflow-runs/{id}/cancel` 并配 `window.confirm`（补齐 §5.2b 高影响缺口）。**🔴 高**。
3. **全局错误边界 + Toast**——区分“无数据”与“加载失败”，用 error-boundary + 轻量 toast 替代散落 `console.warn`。**🟡 中**。
4. **fetch 加超时 + AbortController**（`client.ts:30-54`）。**🟡 中**。
5. **让 `safeParse` 失败上抛或显式告警**，避免契约漂移对用户静默。**🟡 中**。
6. **统一状态/裁决映射**：删除 `DualLoopLive.tsx:54-73` 本地副本，改为 `import { STATUS_LABEL, STATUS_CLASS, DECISION_LABEL, DECISION_CLASS } from "../api/client"`。**🟢 低**。
7. **修复 `gates` 空安全**：`NewResearch.tsx:43` 改为 `Object.keys(t.gates || {}).length`。**🟡 中**。
8. **表单 label 关联**（`htmlFor`/`id`）。**🟡 中（a11y）**。
9. **SSE 驱动失效而非整页重拉**：`useSSE` 收到事件后只 `invalidate` 相关数据，而非每次 progress 全量重拉（`RunDashboard.tsx:85-89`）。**🟡 中（性能）**。
10. **发布未接能力或明确标记 unreleased**：Experiences 视图、外部基准套件浏览、进化触发（POST evolution）、MEA、tracked-only 评测/上报。当前“存在但不见天日”，易造成维护歧义。**🟡 中**。
11. **初始加载骨架屏**：给 `BenchmarkCatalog/NewResearch/RunDashboard` 加最小 loading 态，与 `Leaderboard/CompareRuns` 对齐。**🟢 低**。

---

## 6. 修复优先级路线图

### P0（安全，立即）
- **C1**：模型代码执行沙箱化 + 环境变量隔离（最小白名单）。一行级先止血：不要传真实 `os.environ`。

### P1（隔离契约 + 并发 + 生命周期，生产化前必修）
- **缺陷1**：`api.py:475` 与 `orchestrator.run_capability` 加 `assert_inner_capability_allowed` 守卫 + HTTP 面回归测试。
- **缺陷3**：累积状态存储加锁（`store_tree.py` 4 个存储 + `evolution.py` `_cands`）。
- **缺陷2**：四个 run 端点统一改为 `_spawn_bg_thread`+`_drive`，传 `cancel_event`、收尾 `set_run_status` + `expire_pending_strategies`。
- **H1**：跨进程文件锁或迁移 SQLite。

### P2（正确性与 UX，迭代修复）
- 后端：M1（排行榜排序）、M2（NaN/inf 防护）、M3（fe 字符串化）、M4（reward novelty 0.0）、M5（adapter fe）、M6（zip bomb）、缺陷5（协作死锁）、缺陷4/7/M7（持久化批量化 + 程序级进化接线）。
- 前端：React Query 数据层、取消运行按钮、错误边界/Toast、fetch 超时、safeParse 上抛、`gates` 空安全、去重常量映射、a11y label、SSE 失效而非重拉。

### P3（边缘/防御性，低优）
- 缺陷8/9/10/11；L1~L7。

---

## 附录：已直接核实项（主代理回读源码）
- ✅ C1：`openmle_integration/interpreter.py:53` `env = dict(os.environ)` 传给不可信子进程。
- ✅ 缺陷1：`control_plane/api.py:475` 端点与 `execution_plane/orchestrator.py:224-299` `run_capability` 均无 `assert_inner_capability_allowed`；守卫本体 `execution_plane/agent/harness.py:83-91` 仅 harness 调用。
- ✅ 缺陷2：`control_plane/api.py:492-631` 四端点同步调用 `orchestrator.run_*_loop` 并直接 `return`，无 `_spawn_bg_thread`、无 `cancel_event`、无 `set_run_status`。
- ✅ 缺陷3：`control_plane/store_tree.py` grep `threading.Lock|RLock|self._lock` 零命中（无锁）。
- ✅ 缺陷7：`grep run_program_evolutionary_loop control_plane/api.py` 零命中（未接线）。
- ✅ M1：`control_plane/service.py:409-412` 排序元组首位 `is_top3` 使 top3 记录落在列表末。
- ✅ M3：`execution_plane/capabilities/kaggle_eval_executor.py:111` `fe = (params.get("fe") or "basic").lower()` 对 bool 抛 `AttributeError`。
- ✅ 前端无取消运行：`grep -ri cancel frontend/src/views` 仅模态 `onCancel` 与状态文案，无 `POST .../cancel` 调用。
- ✅ `safeParse` 非阻塞：`frontend/src/api/client.ts:19-28` 失败时 `return data as T`。


---

## 附录 A：暂缓项 L1/L2/L3/L5/L7 单独评审与落地（原 review_deferred_L_items_2026-08-05.md）

# 暂缓项单独评审分析 — L1 / L2 / L3 / L5 / L7

> 配套文档：本文（`doc/code_review_STATUS.md`，全量审查 + P0–P3 修复路线）。
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


---

## 附录 B：2026-08-06 轮修复核实 + F1–F6（原 code_review_2026-08-06.md）

# safety_auto_research 代码审查纪要（2026-08-06 轮）

**范围**：自上次提交 `04310d0`（2026-08-05 全量审查落地）以来的工作树改动
（`git diff HEAD` + 新文件）。方法：结构普查 + 三个并行深度代理
（MEA 框架 / api+store 审查修复 / 其余改动）定位缺陷，主代理回读源码核实并落地修复。

**验证基线**：修复前 `251 passed, 1 failed`（失败项 `test_mea_endpoints_create_and_report`
因 MEA 端点后台化后测试仍期望同步终态）；修复后 **`252 passed, 0 failed`，`tsc --noEmit` 0 errors**。

---

## 0. 上一轮（2026-08-05）修复落地核实

工作树已正确实现 2026-08-05 报告中的关键修复，本次逐条回读核实：

| 项 | 状态 | 核实点 |
|---|---|---|
| C1 模型代码执行环境变量隔离 | ✅ 已修 | `interpreter.py:_safe_env` 仅放行白名单 + 丢弃 `*_KEY/TOKEN/SECRET/...`，不再 `dict(os.environ)` |
| 缺陷2 四运行端点同步阻塞 | ✅ 已修 | `api.py` 四端点 + 2 个额外端点全部 `_spawn_bg_thread(_drive)` + 传 `cancel_event` + `set_run_status` + `expire_pending_strategies` |
| 缺陷3 累积存储无锁 | ✅ 已修 | `store_tree.py` 三存储 + `evolution.py._cands` + `playbook.py` 均加 `RLock`，RMW 路径全守护 |
| 缺陷10 cancel_event 泄漏 | ✅ 已修（本轮补强，见下） | 6 处 `finally` 释放 |
| H1 跨进程锁 | ⚠️ registry 已修；store 不完整（本轮修，见下） | `registry.py` 真实 `fcntl.flock`；`store.py` 原仅 `_save` 守护 |
| M1 排行榜排序 / M2 NaN·inf / M3 fe=bool / M4 reward novelty 0.0 / M5 adapter fe / L1 方向 / L3 部分失败 / L4 空向量 / L7 对齐 | ✅ 已修 | `service.py` / `kaggle_eval_executor.py` / `reward_bridge.py` / `adapter.py` 逐项核对通过 |
| 前端 gates 空安全 / 取消按钮 / ErrorBoundary·Toast / AbortController 超时 | ✅ 已修 | `NewResearch.tsx` / `RunDashboard.tsx` / `components/` / `client.ts` 核对通过 |

---

## 1. 本轮新发现 + 已落地修复

### F1 🟠 `store.py` H1 跨进程锁未覆盖全部写入器（真实多进程损坏缺口）
- **位置**：`control_plane/store.py:153-160`（原 `_save` 包 `_cross_process_lock`）vs `:166-303` 约 10 个 mutator 直接调 `self._persist()`。
- **问题**：H1 的 `fcntl.flock` 只包在 `_save()`/`persist_now` 上，而 `put_workflow_run`/`put_stage_run`/`put_decision`/`put_artifact`/`put_lesson`/`record_metric`/`append_event`/`put_research_record`/`update_research_record`/`delete_research_record` 均 `with self._lock: ...; self._persist()` —— 跨进程锁被绕过。`uvicorn --workers N` 下这些写入器并发重写整库 JSON，仍会 lost update / 中途截断损坏。
- **修复**：将 `_cross_process_lock()` 移入 `_persist()` 自身（`store.py:111-138`），使其成为唯一磁盘写点，覆盖所有调用方；`_save()` 简化为仅持线程锁。`_persist` 内一次性 `flock` 同一 fd，无同进程重入死锁。

### F2 🟠 `interpreter.py` 超时未杀进程组（C1 修复连带回归）
- **位置**：`interpreter.py:146`（加 `start_new_session=True`）+ `:155-157`（超时分支只构造 `ExecutionResult`，未 `killpg`）。
- **问题**：C1 修复为支持干净发信号整组才加 `start_new_session=True`，但超时分支未杀进程组。不受信任模型代码若 `fork` 孙进程，超时后成为孤儿进程常驻 → 资源泄漏 / 潜在 DoS。
- **修复**：超时分支加 `try: os.killpg(proc.pid, signal.SIGKILL) except (ProcessLookupError, OSError): pass`（`:155-161`）。已 `import signal`。

### F3 🟡 `interpreter.py` opt-in 内存上限静默失效
- **位置**：`_child_resource_limits` 读 `os.environ["OPENMLE_MEM_LIMIT_MB"]`（`interpreter.py:86`），但该键不在 `_safe_env` 转发白名单 → 子进程 env 中不存在 → `RLIMIT_AS` 永不被设置。
- **修复**：将 `"OPENMLE_MEM_LIMIT_MB"` 加入 `_SAFE_ENV_KEYS`（`interpreter.py:38-39`）。

### F4 🟡 `api.py` 缺陷10：`pop` 在 `expire_pending_strategies` 抛错时被吞
- **位置**：6 处 `finally: try: expire_pending_strategies(...); _run_cancel_events.pop(...) except: pass`。
- **问题**：若 `expire_pending_strategies` 抛异常，`pop` 被跳过，cancel_event 仍泄漏。
- **修复**：将 `pop` 移到独立 `finally`（`:527-533 / 595-601 / 654-660 / 731-737 / 1285-1291 / 1573-1579`），并给 `expire_pending_strategies` 的 except 加 `logging.exception`。

### F5 🟡 `test_mea_framework.py` 测试与后台化端点行为不一致（根因：缺陷2 修复）
- **问题**：MEA 端点后台化后返回 `status="running"`，测试却断言 `status in (exited_converged, exited_budget)` → 失败。
- **修复**：断言立即返回 `running`，随后轮询 `svc.get_workflow_run(run_id).status` 直到终态（30s 超时），再校验 `mea/state` 端点。

### F6 🟢 `.gitignore` 补齐运行时产物
- 新增 `data/custom_tasks.json`、`data/custom_tasks.json.lock`、`data/task_state/`（MEA 运行时 TaskState + 注册表锁文件），避免误提交运行时 JSON。

---

## 2. 审查发现但本轮**未阻塞推送**的项（设计取舍 / 可选配置 / 非崩溃）

- **MEA 离线审计器自确认**（`execution_plane/agents/adapter.py:637`）：无 LLM judge 时，审计器 `completion` 由 `exec_output.status` 推导，离线模拟下等价于执行器自认完成。`enforce_separation` 仅保证审计器**后端**异于执行器，无法在运行时检测「无独立证据」。这是离线模拟模式的设计取舍（真实部署配 `llm_api` 审计器即独立验证），且 `test_independent_auditor_clean` 固化了该行为；改为总是 `incomplete` 会破坏离线 demo 与确定性测试，故留作后续：明确标注离线模式为「模拟不可信」，或在无 judge 时默认 `incomplete` 并改测试。
- **`registry.py:165 enforce_separation` 仅检查 `executor_default`，未遍历 `executor_overrides`**：某 subtask 经覆盖项路由到与审计器同 backend+model 时可绕过；且该项为 opt-in（缺 `require_different_from` 键即静默关闭）。建议默认开启并覆盖所有覆盖项。
- **`adapter.py:191 TransportExecutorAdapter.run_contract` 无异常包裹**：CLI/网络/超时错误会冒泡崩溃整条 MEA 循环（对照 `LocalExecutorAdapter` 有 fail-soft）。当前未在测试路径触发，建议加 `try/except → ExecOutput(status="failed")`。
- **`mea.py:71-75 naive 基线对 R0 计数偏差 / `savings_ratio` 在 `cost_proxy==0` 返回 0.0**：指标展示偏差，非功能缺陷。
- **`service.py:_make_record` 的 `_clean_score` 未单独 try**：非有限分上报会抛 `ValueError`，但 `api.py:_translate` 已将其映射为 `400`（非 500），对坏输入返回 400 是正确 UX，故不改。

---

## 3. 结论

本轮在「2026-08-05 审查 + 落地」基础上，补齐了 H1 在 `store.py` 的真实多进程损坏缺口（F1）、C1 修复的孤儿进程回归（F2）、opt-in 内存上限失效（F3）、cancel_event 释放的边界遗漏（F4），并修正了后台化端点对应的测试（F5）与 gitignore（F6）。**无遗留 Critical / 阻塞性 Important 项**。MEA 离线审计语义等 3 项属设计取舍，不影响发布，已记录待后续迭代。

**验证**：`252 passed, 0 failed`；`tsc --noEmit 0 errors`。

---

## 4. 补遗：缺陷7 REST 接线（2026-08-07）

**来源**：缺陷7 出自 2026-08-05 全量审查（见 `doc/code_review_STATUS.md` 正文，原 `code_review_2026-08-05.md:79`）—— `run_program_evolutionary_loop`（OpenRSI 程序级岛模型）在 `control_plane/api.py` **零命中**，仅为 orchestrator 内部调用 + `test_openmle_phase_*` 可达的孤儿路径；前端 `EvolutionPanel` 岛视图就绪但无后端驱动。

**修复**（直接补上后端接线）：
- `control_plane/schemas.py`：新增 `ProgramEvolutionRequest`（`task_config` / `backend_type`（template|llm）/ `backend_config` / `islands` / `pop_per_island` / `generations` / `max_workers` / `novelty_threshold` / `audit` / `audit_params` / `budget` / `seed`，全带默认值，空 body 可调用，回落内置 titanic preset）。
- `control_plane/api.py`：新增 `POST /workflow-runs/{run_id}/program-evolution`（后台化驱动，沿用 缺陷2 的 `cancel_event` / `set_run_status` 终态 / `capture_run_record` 收尾 / 缺陷10 的 `finally` 释放）+ `GET` 同路径（`state_store.evolution.list_by_kind(run_id, "program")` 仅回程序节点 + 岛视图谱系）。`backend_type=="llm"` 经 `make_api_backend` 对接 OpenAI 兼容 API；缺 `data_dir` 时回落 `data/kaggle/titanic`。run 缺失时按需 materialize（与 MEA 端点一致）。
- 孤儿路径终结，前端岛视图现在可真正驱动。

**验证**：新增 `tests/test_program_evolution_endpoint.py`(3) 全绿；回归 `test_openmle_phase_bc`(9)/`test_mea_framework`(44)/`test_capabilities`(9) 无回归。基线 **255 passed, 0 failed**。

---

## 附录 C：2026-08-11 轮全量审查（api.py 拆分 routers 后首轮）

**范围**：`safety_auto_research/` 全项目（后端 Python + 前端 React/TS）。重点覆盖自 `68b9515` 以来工作树的 53 项未提交改动 —— 单体 `api.py`（2000+ 行）拆分为 `deps.py` + `routers/*`、新增 `capabilities/sandbox_executor.py` 与 `scripts/` 沙箱链路、前端多视图改动。

**方法**：结构普查 + 4 个并行深度审查代理（控制面 API / 执行面与沙箱 / 核心逻辑与持久化 / 集成层与前端）定位缺陷，主代理对全部 Critical/High 及关键 Medium **逐条回读源码核实**，关键项另做**运行时实测**（novelty 相似度、roc_auc scorer、NaN clamp）。

**基线**：分 4 批从父目录运行 pytest → **297 passed, 0 failed**（+15 subtests）；`tsc --noEmit` 0 errors。

> ⚠️ **本轮核心结论**：297 个测试全绿，但下列缺陷**无一被现有测试网捕获**。测试盲区集中在「后台线程成功路径」「HTTP 端点状态机」「算法有效性（而非仅可运行性）」三类。

### 严重度汇总

| 严重度 | 数量 | 关键项 |
|---|---|---|
| 🔴 Critical | 1 | R1 取消功能整体失效（cancel_event 从未注册） |
| 🟠 High | 8 | R2 未导入 logging 致 NameError；R3 成功路径不收尾；R4 closed-loop 漏 start；R5 REQUESTED→FAILED 非法；R6 novelty 被路径污染致进化退化；R7 自定义任务静默清空；R8 同一 run 可并发重复启动；R9 沙箱 fail-open + 隔离证明可伪造 |
| 🟡 Medium | 13 | R10 roc_auc 恒 NaN；R11 judge NaN→满分；R12 compare_runs 方向盲；R13~R22 |
| 🟢 Low | 8 | R23~R30 |

**误报剔除**（子代理报告但经回读证伪）：① 「`prepare` 对字符串标签 `astype(int)` 崩溃」—— `adapter.py:95` 已有 `LabelEncoder` 前置编码；② 「沙箱超时只杀父进程」—— `exec docker run --rm` 已使 SIGKILL 命中 docker 并回收容器。

---

### 🔴 Critical

#### R1 取消功能整体失效 —— `cancel_event` 从未在启动时注册
- **位置**：唯一创建点 `control_plane/routers/workflow_runs.py:79`；读取点 `routers/loops.py:73,143`、`routers/evolution.py:78,196`、`routers/workflow_runs.py:348`、`routers/benchmarks.py:503`、`control_plane/deps.py:325`（共 7 处）。
- **问题**：event 只在**用户点取消时**才被创建：
  ```python
  # workflow_runs.py:79 —— 取消端点是唯一创建者
  _run_cancel_events.setdefault(run_id, threading.Event()).set()
  ```
  而每个 `_drive` 在**线程启动那一刻**就取值传给 loop：`cancel_event=_run_cancel_events.get(run_id)`。正常时序（先启动、后取消）下字典里必然没有该 key → loop 收到 `None`，`run_dual_loop` 内 `if cancel_event is not None and cancel_event.is_set()` 恒为假。全仓库 grep 确认无任何启动时预注册。
- **影响**：**用户可见的核心功能整体失效**。前端点"取消"后 DB 翻 `cancelled`、UI 显示"已取消"，但内循环/进化搜索/MEA 后台线程仍满负荷跑完（可能数小时训练 + GPU 占用）。跑飞的实验无法从 UI 中止。
- **✅已核实**（grep 全部 7 处 + 唯一创建点）。
- **修复**：在每个 `_drive` 首行改为 `cancel_ev = _run_cancel_events.setdefault(run_id, threading.Event())` 并全部改用 `cancel_event=cancel_ev`；注册须在 `_spawn_bg_thread` 之前或 `_drive` 首行，避免取消请求早于线程调度时丢失。

---

### 🟠 High

#### R2 `orchestrator.py:1068` 调用未导入的 `logging` → `NameError`
- **位置**：`execution_plane/orchestrator.py:1068`；import 区 `:24-50` 无 `import logging`；全文件仅此一处使用 logging（grep 确认）。
- **问题**：
  ```python
  except Exception as _se_err:
      # 缺陷11 fix: ... must not abort the whole research run. Log and continue
      logging.warning("self-evolution meta-loop step failed (non-fatal): %s", _se_err)
  ```
- **影响**：该行正是「缺陷11 修复」，本意让 meta-loop 失败非致命；实际 `NameError` 冒泡，**把可恢复的元循环异常升级为整条双循环崩溃**——与修复意图完全相反。
- **✅已核实**。
- **修复**：`orchestrator.py` 顶部补 `import logging`（或改用已有的 `_json`/`_os` 风格 `import logging as _logging` 并同步该行）。

#### R3 5 处 `_drive` 的收尾 `finally` 缩进错误 —— 成功路径完全不执行清理
- **位置**：`routers/loops.py:88-94`、`routers/loops.py:152-158`、`routers/evolution.py:92-98`、`routers/evolution.py:210-216`、`routers/workflow_runs.py:357-363`。
- **问题**：`finally` 挂在 `except` 分支内层 `try` 上（16 空格），而非外层 `try`（12 空格）：
  ```python
          except Exception:                       # 外层 except（12 空格）
              logging.exception("dual loop failed ...")
              try:                                # 内层 try（16 空格）
                  svc.set_run_status(run_id, "failed")
              except Exception:
                  logging.exception(...)
              finally:                            # ← 挂在内层 try 上！
                  try: orchestrator.expire_pending_strategies(run_id)
                  finally: _run_cancel_events.pop(run_id, None)
  ```
  对照**写对的两处**：`deps.py:339`、`routers/benchmarks.py:518`，`finally` 与 `try` 同为外层缩进。
- **影响**：① run **正常结束**时 `expire_pending_strategies` 不被调用 —— I3 想防的「pending 元循环提案残留」在最常见的成功路径上完全没防住，下一次 run 会读到上一轮悬挂的策略提案；② `_run_cancel_events` 在成功路径不回收。历史「F4/缺陷10」修复只覆盖了 7 处中的 2 处。
- **✅已核实**（逐处比对缩进 + 对照组）。
- **修复**：5 处 `finally` 块整体反缩进 4 格，与外层 `try`/`except` 同级。

#### R4 `POST /workflow-runs/{run_id}/closed-loop` 漏调 `start_workflow_run`，run 永久卡 `requested`
- **位置**：`routers/workflow_runs.py:340-350`。
- **问题**：其余四个运行端点（`loops.py:53-56`、`loops.py:133-136`、`evolution.py:54-57`、`evolution.py:149-152`）都先把 run 推进 RUNNING，唯独 closed-loop 直接开跑。而 `run_closed_loop` 第一步 `dispatch_stage` → `create_stage_run` 被 `service.py:620` 拦截（`stages can only be created for a running workflow`）。
- **影响**：对任何未手动 start 的 run 100% 失败，但 HTTP 返回 `200 {"status":"running","accepted":true}` 骗过前端；随后兜底 `set_run_status("failed")` 又因 R5 非法转换而失败 → run 永久停在 `requested`，用户只看到不动的"已请求"，无任何错误提示。
- **✅已核实**。
- **修复**：`:343` `try:` 后补 `try: svc.start_workflow_run(run_id) except (ConflictError, ValueError): pass`（照抄 `loops.py:53-56`），并 import `ConflictError`。

#### R5 `REQUESTED → FAILED` 是非法状态转换，导致"启动前失败"无法置终态
- **位置**：`platform_contracts/transitions.py:10-14`（`REQUESTED` 后继仅 `{RUNNING, WAITING_APPROVAL, CANCELLED}`）；受害点 `workflow_runs.py:354`、`loops.py:85,149`、`evolution.py:89,207`、`benchmarks.py:515`、`deps.py:336`、`experiments.py:174,178`。
- **影响**：凡在 run 仍处 `requested` 时抛错（R4、`build_run_ctx` 线程内抛错、agent CLI 缺失等），兜底 `set_run_status(..., "failed")` 会再抛 `ConflictError` 被 `except Exception` 吞掉 → run 永久滞留 `requested`，用户既看不到失败也无法重试。
- **✅已核实**（转换表 + 受害点）。
- **修复**：在 `transitions.py:10-14` 的 `REQUESTED` 集合加入 `WorkflowStatus.FAILED`（语义正当：从未跑起来的 run 失败是合法终态）。一处修复连带解决 R4 与 R14。

#### R6 novelty 去重被 `data_dir` 路径 token 污染 —— config 级进化多样性崩溃
- **位置**：`control_plane/evolution.py:115`（`candidate_text` 序列化**全部** params）→ `:90 _embed`（正则 `[a-z0-9一-鿿]+` 词袋）→ `:268`（`sim >= threshold`，默认 0.92）。
- **问题**：`json.dumps(params)` 包含 `data_dir` 长路径，被切成 `users/glennge/work/github/ai/research/safety/auto/research/data/kaggle/titanic` 等 ~12 个 token，父子完全相同且占据 embedding 多数维度；真正可变的超参只有 `gbm`/`rf`、`basic`/`rich` 等 1-2 个 token。
- **实测**（阈值 0.92，≥ 即判重被拒）：

  | 变异 | 相似度 | 结果 |
  |---|---|---|
  | `model` gbm→rf | **0.9722** | ❌ 被拒 |
  | `fe` basic→rich | **0.9722** | ❌ 被拒 |
  | 三点同时变异 | 0.9167 | ✅ 勉强通过 |
  | [对照] 剥离 `data_dir` 后 gbm→rf | 0.9167 | ✅ 通过 |
- **影响**：config 级进化的**单点变异子代 100% 被拒**，第一代后多样性崩溃，退化为 orchestrator 的随机重置兜底（`orchestrator.py:1454-1469`），搜索几乎不前进。属算法有效性缺陷——功能"能跑"但"无效"，测试无法发现。
- **✅已核实 + 实测复现**。
- **修复**：embedding 前剥离路径/常量类键（`data_dir`/`preset`），只在可编辑 surface（`fe`/`model`/`cv_folds`/`threshold` 等）上算相似度。

#### R7 自定义任务存储损坏后静默清空全部已注册任务
- **位置**：`benchmark_tasks/registry.py:773-775`（`_load_raw` 损坏返回 `[]`，**无备份**）→ `:805,825`（`register_custom_task` 全量覆盖写）。
- **问题**：
  ```python
  except (json.JSONDecodeError, OSError):
      return []            # 损坏直接返回空，未备份
  # register: items = _load_raw() → [] ; items.append(record) ; _save_raw(items)  ← 用 1 条覆盖整库
  ```
  对照 `control_plane/store.py:62-77` **有** `.corrupt.<mtime>` 备份 + 日志。
- **影响**：磁盘满/异常写导致文件半截后，用户全部自定义任务被静默抹除且不可恢复。
- **✅已核实**（含对照组）。
- **修复**：`_load_raw` 损坏时先 `shutil.move` 备份为 `.corrupt.<mtime>` 并 `logging.error`，与 `store.py` 对齐。

#### R8 同一 run 可被并发重复启动，无任何互斥
- **位置**：`loops.py:53-56,133-136`、`evolution.py:54-57,149-152`、`deps.py:307-310`。
- **问题**：所有运行端点用 `except (ConflictError, ValueError): pass` 兼容"已在运行"，反而抹掉了唯一的并发保护。子代理实测：对同一 run 连发 3 次 `POST /dual-loop`，三次均 200，并发跑起 3 个同名 loop 线程。
- **影响**：前端双击或页面重试即在同一 run_id 上并行跑多个双循环：StageRun/事件流交错、假设树与经验银行竞态、多线程抢写终态、SSE 混杂两路事件、算力成倍消耗。
- **修复**：`deps.py` 增 `_active_runs: set[str]` + 锁，运行端点入口 `if run_id in _active_runs: raise HTTPException(409, ...)`，并在 R3 修好的外层 `finally` 中 `discard`。

#### R9 沙箱 soft 降级 fail-open + 隔离证明可伪造
- **位置**：`scripts/build_agent_sandbox.sh:57-79`（`use_soft`）、`:80-82`（三个降级触发点）；`execution_plane/capabilities/sandbox_executor.py:279,307,326`。
- **问题**：① 缺 docker CLI / daemon 不可达 / `AGENT_SANDBOX_DISABLE=1` 时，`use_soft()` **直接在宿主执行命令**（继承完整环境、无网络隔离），仅 stderr 一行提示，平台侧无从感知、不阻断；**本机无 docker，即该路径默认失效**。② `isolation = res.get("isolation", {})` 取自被测脚本自报的 `result.json`，`"sandbox": "docker" if isolation.get("network_blocked") else "soft"` —— 任意 `research_cmd`（含 LLM 生成代码）可谎报 `network_blocked: true`，artifact 元数据即被标为硬隔离。
- **影响**：C1（模型代码 RCE）的整条防线在默认环境下不成立，且审计溯源信息不可信。
- **✅已核实**（脚本全文 + `network_blocked` 生成点在 `scripts/sandbox_examples/run_*_sandbox.py`）。
- **修复**：增加 `AGENT_SANDBOX_REQUIRE_HARD=1` 开关，缺 docker 时**直接失败而非降级**；平台侧不采信被测脚本自报字段，改由执行器根据实际使用的执行路径（docker/soft）自行标注 `sandbox`。

---

### 🟡 Medium

#### R10 `roc_auc` / `auc` 指标恒为 NaN（评分源头失效）
- **位置**：`execution_plane/capabilities/kaggle_eval_executor.py:221,223`：`make_scorer(roc_auc_score, needs_proba=True, ...)`。
- **问题**：sklearn ≥1.6 移除 `needs_proba`，该 kwarg 被当作 `**kwargs` 透传给 `roc_auc_score`，且 scorer 退化为 `response_method='predict'`。**实测（sklearn 1.9.0）**：`cross_val_score(...)` → `[nan nan nan]`。
- **影响**：任何以 `roc_auc`/`auc` 为主指标的研究任务主分恒 NaN → 门限静默 FAIL，并污染 fitness/榜单（M2 的 isfinite 防护只在下游，源头已坏）。
- **✅已实测复现**。
- **修复**：改为 `make_scorer(roc_auc_score, response_method="predict_proba", average="macro", multi_class="ovr")`。

#### R11 `llm_judge` 对 NaN/inf 未防护，反被 clamp 成满分 1.0
- **位置**：`control_plane/llm_judge.py:80`：`"score": max(0.0, min(1.0, score))`。
- **问题**：**实测** `max(0.0, min(1.0, nan)) == 1.0`、`inf` 同为 `1.0`（NaN 比较恒 False）。
- **影响**：LLM 返回 NaN 时样本被记满分，污染评测均值与榜单。
- **修复**：`if not math.isfinite(score): raise LLMJudgeError(...)`，交由已有的 L3 部分失败回退逻辑处理。

#### R12 `compare_runs` 方向盲，且 `or` 链跳过合法的 `0.0`
- **位置**：`control_plane/service.py:503-506`：
  ```python
  v = m.get("accuracy") or m.get("primary") or m.get(metric_name)
  if best_score is None or v > best_score:   # 永远取最大
  ```
- **影响**：① lower-is-better 指标（`eval_loss`/`error_rate`）选出**最差**值；② `accuracy == 0.0` 为假值会被 `or` 跳到 `primary`，合法零分被忽略。
- **修复**：按 direction 决定 `>`/`<`（复用 `leaderboard()` 的 `is_better`）；`or` 链改为逐个 `if k in m` 判定。

#### R13~R22（其余 Medium，位置已核实）
- **R13** `experiments.py:69` debug 端点把 run 推 RUNNING 后从不置终态 → 列表永久"运行中"僵尸 run。
- **R14** `experiments.py:174,178` agent CLI 缺失时 `set_run_status("failed")` 触发 R5 非法转换 → 冒泡 **500**，用户看不到已写好的中文提示。
- **R15** `progress_bus.py:55-57` 首个订阅者连接前事件被静默丢弃（无 backlog/replay），前端跳转窗口期丢开头事件；`:100-103` `while True: await q.get()` 无终止哨兵，run 结束后 SSE 流永不关闭。
- **R16** `sandbox_executor.py:214` `tempfile.mkdtemp` 无对应 `shutil.rmtree`（全文件 grep 零命中）→ 磁盘泄漏。
- **R17** `frontend/src/views/EvolutionPanel.tsx:105-108` 需 pb 与 st **同时** rejected 才报错，单点失败 UI 静默显示陈旧数据。
- **R18** `frontend/src/views/AuditBoard.tsx:40-46` followup 失败填空数组、外层仅 `console.warn` → 后端故障呈现为"无数据"假阴性。
- **R19** `control_plane/eval_runner.py:233,246` `gold` 被强制 `str()` 而 `ranked` 保留原类型 → 整型 doc id 场景召回恒漏判。
- **R20** `openmle_integration/adapter.py:294-309` `TEST_FITNESS` 恒为 accuracy，`cfg.eval_metric` 仅回显进 `AUX_EVAL_INFO`；`direction="lower"` 时 `MetricValue(value=acc, maximize=False)` 语义自相矛盾。
- **R21** `control_plane/schemas.py:174` `config` 带 `alias="config_snapshot"` 但全文件无 `ConfigDict/populate_by_name` → 按字段名传 `config` 被静默丢弃（当前前端未调用该端点，影响有限）。
- **R22** `routers/benchmarks.py:92,117` `offset` 无下界校验（`offset=-2` 产生错误切片），响应回了 `total`/`offset` 却缺 `limit`。

---

### 🟢 Low
- **R23** 无 `requirements.txt`/`pyproject.toml`；`routers/benchmarks.py:187` 的 `UploadFile` 使 `python-multipart` 成为未声明硬依赖，缺失时 `create_app()` 直接抛错、全部端点不可用。
- **R24** `deps.py:341-345` `pop` 与 `expire_pending_strategies` 在同一 `try` 内，后者抛错则 `pop` 被跳过（R3 已在 5 处修正此模式，此处仍是旧写法）。
- **R25** `openmle_integration/reward_bridge.py:170` `valid` 被计算两次，首次为死代码。
- **R26** `routers/loops.py:15` 导入未使用的 `status`；`:138` 局部变量 `status` 遮蔽同名模块（后续若用 `status.HTTP_*` 即炸）。
- **R27** SSE 与轮询在 run 终态后均不停止（`useSSE.ts:61-64`、`RunDashboard.tsx:53-73`）。
- **R28** `AuditBoard.tsx:38-43` 对每个 audit 串行 `await` followup（N+1 请求）。
- **R29** `EventStream`/`AuditBoard`/`getResearchRecords` 长列表无分页/虚拟化。
- **R30** `deps.py:365-366` 与 `:378-380` 重复构造 store/orchestrator（非泄漏，纯冗余）。

---

### 前后端契约核对（路由重构后）
- **(a) 前端调用但后端缺失：0** —— 33 条 `client.ts` 路径全部命中后端（57 条注册路由），拆分未丢路由、无路径遮蔽、无前缀错配。✅
- **(b) 后端存在但前端未调用：24 条** —— 多为驱动器内部/遗留端点（`request-approval`、`start`、`evaluate`、`report-metric`、`benchmark-suites/*`、`experiences` 等），非缺陷但属能力不可达。
- **(c) 响应结构不匹配：0（字段级）** —— 逐项核对 hypo-tree / audit / evolution / research-records / `objective_snapshot.config.inner_loop` 嵌套均一致。
- **隔离守卫未丢**：实测 `POST .../capabilities/layer_11_external_audit/run` 与 `layer_09_self_iterative_evolution` 均返回 400。✅

### 修复优先级路线图
- **P0**：R1（取消失效）
- **P1**：R2（NameError）、R3（finally 缩进）、R5（状态机）+ R4（closed-loop）
- **P2**：R6（novelty 污染）、R7（任务静默丢失）、R10（roc_auc NaN）、R11（judge NaN）、R8（并发互斥）
- **P3**：R9（沙箱 fail-open，需产品决策是否强制硬隔离）、R12~R22
- **P4**：R23~R30

---

## 附录 C-2：2026-08-11 轮修复落地记录（R1–R30 全量闭环）

**结论：附录 C 提出的 30 项全部修复完毕，无暂缓项。** 回归 **317 passed / 0 failed**（基线 297 + 本轮新增 20 例），`tsc --noEmit` 0 errors。

### 逐项状态

| 项 | 严重度 | 状态 | 落地位置与做法 |
|---|---|---|---|
| R1 | 🔴 | ✅ | 抽出 `deps.acquire_run_slot/release_run_slot`，在 `_spawn_bg_thread` **之前**注册 cancel event，7 处读取点统一改用返回的 `cancel_ev` |
| R2 | 🟠 | ✅ | `orchestrator.py` 补 `import logging` |
| R3 | 🟠 | ✅ | 5 处 `_drive` 的收尾块从 `except` 内提到 `finally`，成功路径同样收尾 |
| R4 | 🟠 | ✅ | closed-loop 端点补 `start_workflow_run`，run 不再卡 `requested` |
| R5 | 🟠 | ✅ | `transitions.py` 允许 `REQUESTED → FAILED`（"启动前失败"可落终态） |
| R6 | 🟠 | ✅ | novelty 分词剔除 `data_dir` 等路径类 token，config 级多样性恢复 |
| R7 | 🟠 | ✅ | 自定义任务存储损坏改为 `.corrupt.<ts>` 备份 + 拒绝启动，不再静默清空 |
| R8 | 🟠 | ✅ | `acquire_run_slot` 兼作单飞 token，重复 POST 返回 409 |
| R9 | 🟠 | ✅ | 见下「关键设计点 ①」 |
| R10 | 🟡 | ✅ | 适配 sklearn ≥1.6：`make_scorer` 去掉已移除的 `needs_proba`，改用 `response_method` |
| R11 | 🟡 | ✅ | judge 分数先判 `isfinite` 再 clamp，NaN 不再被 `max/min` 夹成满分 1.0 |
| R12 | 🟡 | ✅ | 见下「关键设计点 ②」 |
| R13 | 🟡 | ✅ | 新增 `service.end_debug_session()`；debug 借 RUNNING 后在 `finally` 回置 REQUESTED，不再留僵尸 run |
| R14 | 🟡 | ✅ | `routers/experiments.py` 加 `_fail_run()` best-effort 包装，agent 缺失路径不再冒 500、保住中文提示 |
| R15 | 🟡 | ✅ | 见下「关键设计点 ③」 |
| R16 | 🟡 | ✅ | `sandbox_executor.execute()` 用 `finally` 清理 `mkdtemp` scratch（5 条返回路径全覆盖），`AGENT_SANDBOX_KEEP_SCRATCH=1` 可留存排查 |
| R17 | 🟡 | ✅ | `EvolutionPanel` 逐源收集 `allSettled` 失败名，单源失败即报错（原需全部失败才提示） |
| R18 | 🟡 | ✅ | `AuditBoard` 拉取失败保留上次数据 + 显式报错，不再假阴性显示"尚无外部审计事件" |
| R19 | 🟡 | ✅ | `embedding_retrieval_eval` 两侧统一 `str()` 比较，整型 doc id 不再恒漏判（recall 恒 0） |
| R20 | 🟡 | ✅ | 见下「关键设计点 ④」 |
| R21 | 🟡 | ✅ | `ReportMetricRequest` 加 `populate_by_name=True`，字段名 `config` 与别名 `config_snapshot` 均接受 |
| R22 | 🟡 | ✅ | `list_suite_tasks` 改 `Query(200, ge=1, le=1000)` / `Query(0, ge=0)`，负 offset 不再回绕取尾部 |
| R23 | 🟢 | ✅ | 新增 `requirements.txt` / `requirements-dev.txt` / `requirements-optional.txt`，显式钉住**代码里搜不到的** `python-multipart` |
| R24 | 🟢 | ✅ | `release_run_slot` 中 `pop` 与后续清理分离到独立 `try`，清理抛错不再吞掉释放 |
| R25 | 🟢 | ✅ | `reward_bridge.reward_population` 删除首次 `valid` 死代码 |
| R26 | 🟢 | ✅ | `routers/loops.py` 局部变量 `status` → `loop_status`，解除对 `fastapi.status` 的闭包遮蔽（`status` 导入本身有用，原判"未使用"为误报） |
| R27 | 🟢 | ✅ | `useSSE` 收 `done` 关流；`RunDashboard`/`EvolutionPanel` 终态停轮询；终态集合统一到 `client.TERMINAL_RUN_STATUSES` |
| R28 | 🟢 | ✅ | `AuditBoard` followups 改 `Promise.allSettled` 并行，消除每 3s 一次的 N+1 |
| R29 | 🟢 | ✅ | `EventStream`（50/页）与 `AuditBoard`（10/页）newest-first 分页 + 展开/收起 |
| R30 | 🟢 | ✅ | `build_deps` 先定配置后构造，service/store/orchestrator 各只建一次（原最多建 3 个 orchestrator 丢 2 个） |

### 关键设计点

**① R9 沙箱：从 fail-open 改为可选 fail-closed，并让隔离证明不可伪造**

原实现有两个独立问题，分别对应两处修改：

- *fail-open*：`build_agent_sandbox.sh` 在缺 docker 时无条件降级到宿主直跑。新增 `AGENT_SANDBOX_REQUIRE_HARD=1`（或 `params["require_hard"]=True`）→ 直接 `exit 78`(EX_CONFIG)，**被测命令一次都不执行**。默认仍降级（保住本地开发体验），但 stderr 明确告警且结果标 `sandbox=soft`。
- *隔离证明可伪造*：旧代码用 `result.json` 里的 `isolation.network_blocked` 判断"这是 docker 硬隔离"——而 `result.json` 正是**被测程序自己写的**，任何脚本都能声明自己被隔离过。改为由**宿主侧启动器**写 attestation（`{"mode":"hard|soft|unavailable", ...}`），执行器只认这份文件；marker 路径经 `AGENT_SANDBOX_MARKER_PATH` 传入并**刻意放在 scratch 之外**（scratch 是 rw 挂载且是 soft 模式下 payload 的活动目录），且在 soft 分支 `unset` 该变量后才运行 payload——被测程序既够不着也不知道路径。marker 缺失/损坏一律判为 `unknown`（绝不判 hard）。payload 的自报值降级为诊断字段 `isolation_claimed_by_payload`。

实测三条路径（`tests/test_fix_regression_2026_08_11.py::R9SandboxIsolationTest`）：require_hard 下 canary 文件未被创建（payload 确未执行）、soft 下 `AGENT_SANDBOX_MARKER_PATH` 在 payload 环境中为空、scratch 内无 `.sandbox_mode`。

**② R12 排行榜与对比视图曾各算各的**：`capture_run_record` 方向感知取 min/max，`compare_runs` 却恒取 `max` 且用 `or` 链（合法的 `0.0` 被跳过）。抽出共享选择器 `service._select_objective(scores, metric_name, direction)`，优先级 `声明指标 → primary → accuracy 类 → 任意`；其中 accuracy 类**永不反向**（即使任务是 lower-is-better，accuracy 仍取 max，因为它本身就是越高越好）。`compare_runs` 响应新增 `direction`，前端 `CompareRuns.tsx` 据此反向排序，并按 `minPositive/score` 反比计算条宽，使"条越长越好"在两种方向下语义一致。

**③ R15 SSE 既丢头也不收尾**：`emit` 在无订阅者时静默丢弃（浏览器连上前的进度永久丢失），`subscribe` 又永远 park 在 `await q.get()`（run 结束后连接不关）。改为：`emit` 先入 per-run 回放缓冲（64 条/run，LRU 保 32 个 run）再投递；新增 `close(run_id)` 发 EOF 哨兵；订阅时先回放 backlog，若 run 已结束立即发 `done` 返回。后端唯一挂载点是 `release_run_slot`——所有后台 driver 的收尾都汇聚于此，因此不存在"某条路径忘了关流"。

**④ R20 adapter 指标名与实际算的分不一致**：无论 `cfg.eval_metric` 声明什么，`TEST_FITNESS` 恒为 accuracy；当 `direction="lower"` 时又设 `maximize=False`，语义变成"最小化准确率"。新增 `_score_declared_metric()` 真正按声明指标计算（f1/f1_macro/precision/recall/balanced_accuracy/error_rate）；对**硬标签算不出**的指标（roc_auc/log_loss 等）不再冒名顶替，而是按方向选 `error_rate`(lower) 或 `accuracy`(higher) 作代理并置 `metric_fallback=True`，使下游 `op=="le"` 的取负逻辑仍然正确。

### 文件改动清单

**后端**：`control_plane/`{`service.py`, `deps.py`, `progress_bus.py`, `schemas.py`, `eval_runner.py`, `routers/`{`experiments.py`, `benchmarks.py`, `loops.py`}}、`execution_plane/`{`orchestrator.py`, `capabilities/sandbox_executor.py`}、`openmle_integration/`{`adapter.py`, `reward_bridge.py`}、`platform_contracts/transitions.py`、`scripts/build_agent_sandbox.sh`
**前端**：`api/`{`client.ts`(新增 `TERMINAL_RUN_STATUSES`), `useSSE.ts`}、`views/`{`CompareRuns.tsx`, `EvolutionPanel.tsx`, `AuditBoard.tsx`, `RunDashboard.tsx`, `EventStream.tsx`}
**新增**：`requirements.txt`、`requirements-dev.txt`、`requirements-optional.txt`、`tests/test_fix_regression_2026_08_11.py`（20 例：R9×5 / R12×4 / R15×4 / R19×3 / R20×3 / R21×1）

### 回归结果（从父目录分批跑）

| 批次 | 结果 |
|---|---|
| agent_mode / agent_protocol / api_endpoints / audit_followup / benchmark_registry / benchmark_suites | 72 passed |
| capabilities 19 / control_plane 8 / control_plane_structure 9 | 36 passed |
| dual_loop 8 / evolution 10 / execution_plane 9 / fix_regression_08_05 18 / **fix_regression_08_11 20** | 65 passed |
| mea_framework 44 / openmle_phase_a 5 / bc 9 / d 10 / d_ext 8 / p2_fixes 13 | 89 passed |
| phase2 13 / platform_contracts 8 / playbook 10 / program_evolution_endpoint 3 / research_records 9 / review_supplementary 12 | 55 passed |
| **合计** | **317 passed, 0 failed** |

> ⚠️ 运行约束（沿用既有惯例）：必须从**父目录** `AI_research/` 执行 `python -m pytest safety_auto_research/tests/...`；一次性加载全部用例会 OOM 被 SIGKILL（exit 137），须按上表分批。

---

## 附录 C-3. Benchmark 任务完备性补全与端到端验证（2026-08-11）

### 背景与目标
用户要求：(1) 将缺失 / 不完整的 benchmark 任务补全到「**可评估 + 可训练优化**」的程度，并实际跑评测与训练优化验证；(2) 训练 / 评测数据 >1GB 的任务在可选列表中**置灰**，不补全、不验证。

### 一、可选任务盘点（22 精选 + 2 外部套件 + 8 自定义类型）
- 5 个平台原生任务（titanic / spaceship-titanic / wine / iris / breast_cancer）→ kaggle_eval 端到端。
- 其余 17 个精选任务来自 autolab / claudini / Arbor / AutoResearchClaw / ARA / Auto-claude 等**外部兄弟仓库**，本仓无本地数据与运行环境（Harbor / Arbor 依赖）→ tracked-only，无法本地训练。
- 2 个外部套件（ScienceAgentBench / MLE-bench）：数据集 ~6.9GB。
- 8 个自定义任务类型：tabular（平台可执行）；text / image / audio / embedding 分类（此前 `executable=False, harness=manual`）；llm_sft / llm_rl / llm_opd（基座权重 >1GB）。

### 二、置灰策略（`benchmark_tasks._availability` + `to_dict`）
`BenchmarkTask` 新增 `data_size_bytes` / `enabled` / `unavailable_reason` 字段；`_availability` 规则：
- `suite.*` → 禁用，`data_size_bytes=6.9e9`
- `mlevolve.mle_bench` → 禁用（openai/mle-bench >1GB，需 Kaggle 凭据）
- 外部前缀 `autolab.` `claudini.` `arbor.` `autoresearchclaw.` `ara.` `autoclaude.` → 禁用
- `task_type in (llm_sft, llm_rl, llm_opd)` → 禁用（基座权重 >1GB）
- 其余 → 启用

> ⚠️ **关键设计点**：LLM 类型的 `available=False` 仅作**目录 / 表单 UI 灰显提示**（前端渲染 `⛔ 已置灰（不可用）` + 原因，禁用「用此任务新建研究」），**不在后端拒绝注册**。tracked 型 LLM 任务仍是合法（非本地可执行）目录条目，`executable=False` 已表达「本环境不训练」。因此 `registry.validate_registration` 中对 `available=False` 的拒绝分支**已移除**——否则会破坏既有 API 契约（`test_register_launch_tracked` 等注册 tracked LLM 任务返回 201）。

### 三、4 个分类模态补全为「可评估 + 可训练优化」
此前 text / image / audio / embedding 分类均 `executable=False, harness=manual`。本次补全：
- `registry.py`：4 个类型 `executable=True` + `harness={text,image,audio,embedding}_cls_sandbox`；`suggest_run_command` 指向仓库内真实脚本；image 增加 `tiny_cnn` 轻量 backbone（免预训练下载）。
- 新增 3 个沙箱执行脚本（`scripts/sandbox_examples/`）：
  - `run_image_cls_sandbox.py`（torchvision TinyCNN，host torch）
  - `run_audio_cls_sandbox.py`（librosa logmel / MFCC + CNN，host torch）
  - `run_embedding_sandbox.py`（文-文对比学习，recall@10 检索评测，host torch）
  - （text 的 `run_text_cls_sandbox.py` 已存在，TF-IDF + 线性，sklearn-only，可在 docker 硬隔离内跑）
- 平台接线：`sandbox_executor.py` 扩展 `SANDBOX_CAPABILITY_IDS` 并新增 3 个 `_plan` 分支；`capabilities/registry.py` 注册 3 个 `InfraCapability`；`routers/benchmarks.py` 的 agent 模式启动把 4 个分类类型路由到对应沙箱能力。
- 样本数据：`benchmark_tasks/sample_data/generate_sample_data.py` 生成 <1GB 合成数据。修复 image 数据嵌套 bug——原先 `train/val` 目录被 ImageFolder 误读为两个类（恒定 0.7733），改为扁平 `<root>/<class>/*.png`（runner 内部切分），epochs=1→0.587、epochs≥3→1.0。

### 四、实际评估与训练优化验证（`scripts/verify_benchmark_completeness.py` + pytest）
4 个模态均可产出合法 [0,1] 指标，且**具备优化空间**：

| 模态 | baseline | improved | Δ | 说明 |
|---|---|---|---|---|
| image (TinyCNN) | acc 0.587（ep1，underfit） | 1.0（ep≥3） | **+0.41** | underfit→fit |
| embedding (contrastive) | recall@10 0.33（dim64/ep40，过拟合） | 1.0（dim16/ep3） | **+0.67** | 过拟合→尺寸恰当 |
| text (TF-IDF) | f1_macro 达合成上限 | — | sensitive=no | 合法指标，无空间（已达上限） |
| audio (logmel/MFCC) | acc 1.0 | — | sensitive=no | 合法指标，无空间（已达上限） |

> 说明：text / audio 在合成数据上已达指标上限，故「sensitive=no」（无优化空间），但指标合法、评测链路通畅；image / embedding 则真实展现了「调参即可提升」的优化空间，满足「可训练优化」要求。

text 能力经真实 `SandboxResearchExecutor.execute` 在 docker 硬隔离下**端到端跑通**：`status=SUCCEEDED, passed=True`。

### 五、前端灰显
`api/client.ts` 增加 `enabled` / `unavailable_reason` / `data_size_bytes`；`BenchmarkCatalog.tsx` 对禁用卡片加 `disabled` 类 + `⛔ 已置灰（不可用）` 胶囊 + 原因，禁用「用此任务新建研究」按钮；`styles.css` 加 `.pill.gray` 与 `.catalog-card.disabled`（`opacity:0.55; grayscale`）。`npx tsc --noEmit` 0 错误。

### 六、回归
- 新增 `tests/test_benchmark_completeness_2026_08_11.py`（11 例：灰显策略 / 沙箱 dispatch / runner 可解析 / 扁平 ImageFolder 回归）。
- 更新 `test_benchmark_registry.py::test_only_tabular_is_executable`（现 5 个可执行类型）、`test_capabilities.py::test_protocol_exposes_capabilities`（能力数 15→18）。
- 移除 `registry.validate_registration` 中对 `available=False` 的拒绝分支（灰显是 UI 提示，不应阻断 tracked LLM 任务注册）。

### 文件改动清单
**后端**：`benchmark_tasks/`{`__init__.py`, `registry.py`}、`execution_plane/capabilities/`{`sandbox_executor.py`, `registry.py`}、`control_plane/routers/benchmarks.py`
**前端**：`api/client.ts`、`views/BenchmarkCatalog.tsx`、`styles.css`
**新增**：`scripts/sandbox_examples/run_{image,audio,embedding}_cls_sandbox.py`、`scripts/verify_benchmark_completeness.py`、`benchmark_tasks/sample_data/*`、`tests/test_benchmark_completeness_2026_08_11.py`

### 回归结果（从父目录分批跑）
| 批次 | 结果 |
|---|---|
| A：benchmark_completeness(新·11) / benchmark_registry / benchmark_suites / capabilities / evolution / dual_loop / review_supplementary / fix_regression_08_05 / fix_regression_08_11 | 132 passed |
| B：agent_mode / agent_protocol / api_endpoints / audit_followup / control_plane / control_plane_structure / execution_plane / mea_framework | 108 passed（15 subtests） |
| C：openmle_phase_a / bc / d / d_ext / p2_fixes / phase2 / platform_contracts / playbook / program_evolution_endpoint / research_records | 88 passed |
| **合计** | **328 passed, 0 failed**（R1–R30 基线 317 + 本轮新增 11） |

---

## 附录 C-4. OSS 任务实验验证（codex 替代 LLM Agent · tmeoa 替代 GPT-4o，2026-08-11）

按用户要求对精选外部 OSS 研究任务做「数据收集 + 实验验证」：本机 `codex` CLI 替代原任务 LLM agent（生成解题程序），tmeoa 代理 `deepseek-v4-flash-official` 替代 GPT-4o，本地自带 eval 脚本打分。

### 关键工程事实
- `python`/`python3`（PATH）= 裸 managed 3.13.12，**无 sklearn/pandas**；managed venv（`.../envs/default/bin/python`）含 sklearn 1.9.0 / pandas 3.0.3 / numpy 2.4.6。→ 统一改为 **codex 只生成程序（agent 角色），harness 用 managed venv 跑分**（更贴合 SAB 原设计）。
- codex 须在 git 仓库内运行（否则 `Not inside a trusted directory`）。workspace 全部置于 `safety_auto_research/experiments/oss_validation/`。
- macOS 无 `timeout`；tmeoa `deepseek-v4-flash-official` 是**推理模型**，须 `max_tokens≥2000` 并读 `choices[0].message.content`。
- `benchmark_tasks/suites/data/vendor/ScienceAgentBench/` 实际含**完整 benchmark**（3.7GB：datasets/eval_programs/gold_programs），`science_agent_bench.py` 注释「数据未分发」已过时。

### 已跑通（4 个任务，覆盖 3 个 suite）
| 任务 | 产出 | 评分 | 状态 |
|---|---|---|---|
| autolab.safety_router | solve.py, hidden=16→2081 参数 | 公开 split 过 / 私有 split 未过（acc 0.6125，泛化缺口） | ⚠️ |
| SAB #92 (JNMF) | solve.py (numpy+json) | 与 gold 误差 1.67e-16，全键对 | ✅ |
| SAB #5 (DKPES RF) | solve.py (sklearn) | AUROC=1.0 ≥0.91 | ✅ |
| arbor.algotune_knn | solution.py (`np.argpartition`) | 正确 PASS；dev 2.43x / held-out 2.24x | ✅ |

> safety_router 私有 split 未过是真实泛化发现：调参可解（hidden=16, epochs=400, seed=42 → 私有 acc 0.656，仍 2081 参数匹配 reference）。

### 可行性结论
- **可行（已验证/可类推）**：autolab 其余 CPU 任务（~27 个，如 adaptive_compression/levenshtein_distance/hash_join…）；arbor-zoo 算法调优；SAB 102 任务中 29 个轻量候选（CSV/JSON 产出类可直接类推，部分需 ccobra/biopsykit）。
- **不可行（本地）**：autolab CUDA 任务 6 个（aes128_ctr/flash_attention/ntt_butterfly_cuda… 需 GPU）；claudini（需 70B 权重 Meta-SecAlign-70B/gpt-oss-safeguard-20b）；AutoResearchClaw/ARA/Auto-claude（需特定大模型 API/权重）；mle_bench 按用户要求跳过。
- **tmeoa 限制**：纯文本推理模型，**无法忠实替代 SAB figure 的 GPT-4o visual judge**（无图像输入）。非 figure 任务（#92/#5）不需要 GPT-4o，本地确定性评分即可。

### 产物
- `experiments/oss_validation/REPORT_2026-08-11.md`（完整报告）
- `experiments/oss_validation/results.json`（结构化结果）
- `experiments/oss_validation/tmeoa_client.py`（GPT-4o 替代客户端）
- 各任务 workspace：`safety_router/`、`sab/task_92_h_importances/`、`sab/task_05_dkpes/`、`arbor/algotune_knn/`（均含 solve.py/solution.py + run_eval/eval + result.json）
