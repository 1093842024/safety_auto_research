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
