# safety_auto_research 全量代码审查与前端评估报告

**日期**：2026-08-05
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
