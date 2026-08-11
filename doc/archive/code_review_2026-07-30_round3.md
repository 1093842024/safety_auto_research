# 全项目代码审查报告（Round 3）

> 范围：safety_auto_research 全项目（双循环/进化循环/agent 协议/control_plane/前端契约）
> 基线：git HEAD=8797258 + 未提交改动（Harness 工程一/二/三期 + 基准套件 + SSE/对比视图）
> 方式：只读审查（本轮不改代码）；三条隔离不变量为判定基准
> 日期：2026-07-30

---

## 🚨 Critical（5 项：立即修）

### C1. agent_inner 模式下外审计系统性致盲——双循环实质不可能 Accept
- 位置：`execution_plane/orchestrator.py`（inner_metrics 提取）、`agent/harness.py:330-336`、`agent/transport.py:377-383`
- 问题：内循环指标只从 `inner_event.metrics` 取；CLI 协议要求 agent 终答 `"event":null`，
  而 `AgentStageResult.metrics` 存在却在 `run_stage` 构建 `ExecResult` 时被丢弃。
  → `audit_input.result_metrics={}`、real_eval=False → confidence≈0.34 → **永远 refine 到预算耗尽**，
  且每轮把噪声蒸馏进 playbook/experience，污染跨 run 学习。
- 触发：`agent_inner=True` + 任意真实 CLI agent。
- 修复：`inner_event is None` 时回退扫描本 run 最近的 `eval_completed` 事件填充审计输入
  （或把 `AgentStageResult.metrics` 映射进 audit_input）。

### C2. CLI transport 绕过 run_capability 禁止集——隔离不变量②失效（已人工复核确认）
- 位置：`execution_plane/agent/transport.py:334-340`（`_exec_tool` 直接调 `_capability_runner`）；
  对照 `agent/harness.py:241-245`（SubprocessTransport 走 `_tool_handler` 有检查）
- 问题：Codex/Claude Code 路径的 agent 可以 `run_capability("layer_11_external_audit")` 自审、
  或调 `layer_09` 自改策略——正是不变量②要防的自确认。
- 修复：`_exec_tool` 的 run_capability 分支先过 `assert_inner_capability_allowed(cid)`。

### C3. pending 策略补丁 × collab 调整/restart 时序交错——假判决 + 回滚冲掉人工调整
- 位置：`orchestrator.py` 补丁应用（self_evo 后）→ collab `outer_complete` 暂停（restart 时
  `inner_params = orig`，但 `pending_strategy` 仍挂）
- 问题：(a) 人工 restart 后下轮用未打补丁配置却给补丁记 verified/rolled_back；
  (b) `_apply_collab_adjustments` 覆盖补丁键，验证测的不是补丁；
  (c) 回滚时恢复 pre_patch 会把人工刚调的 model/fe 冲掉。
- 修复：collab restart/调整前先撤销 pending（恢复 pre_patch + expire），再应用人工调整。

### C4. transport 解析失败/工具轮耗尽时伪造 "succeeded + gate passed"
- 位置：`agent/transport.py:313,395-415`（`_fallback` 对 run_stage 返回 succeeded/passed）
- 问题：agent 打满 8 轮工具循环或输出非 JSON 时，一个可能毫无产出的 turn 被记为成功
  （决策落库、步骤入册）。
- 修复：fallback 返回 failed + GateResult.FAILED，上层显式终止或重试。

### C5. budget 计数在 agent 路径失真
- 位置：`orchestrator.py`（`_cap_calls += 1` 只在 inner/audit/self_evo 三处调用点）
- 问题：agent 内循环一整轮只 +1，agent 内部经 run_capability 工具实际发起的 N 次调用
  完全不计——`max_capability_calls`/`max_cost` 在 agent 模式被任意放大。
- 修复：计数挂进 `run_capability` 本体（每次真实调用 +1）。

---

## ⚠️ Important（13 项：分批修）

### 元循环/验证语义
- **I1. 策略验证基线取"历史最优"而非"补丁前配置"**（`self_evolution_executor.py:65-67`）
  → refine_hook 切差配置后，有益补丁也 delta<0 被误回滚。修复：提案时记录补丁前最近一次
  inner_acc 作为 baseline（由 orchestrator 在 apply 处注入）。
- **I2. audit_restart 不重置 inner_params**（`orchestrator.py` restart 支路只重置 goal）
  → 与 collab restart 语义不一致；refine_hook 修改、补丁、rejected 全保留。修复：重置为 orig。
- **I3. 异常路径不过期 pending 策略、stage 永久挂 RUNNING**（循环体无 try/finally）
  → 执行器抛错时 `pending_verification` 永久悬挂。修复：try/finally 统一 expire + stage 转 FAILED。
- **I7. 进化循环末代 champion 顶替全局最优**（正常出口返回末代冠军，预算出口用
  `archive.best(run_id)`，不一致）。修复：统一 `archive.best(run_id)`。
- **I8. 指标方向/键缺陷**：自定义主指标非 accuracy 时 `inner_acc=0` → 回滚风暴 + 假失败经验；
  进化 fitness 对 log_loss 取 `max()` 选出**最差**候选；审计 cv 键缺失时 heldout 恒 verified。
  修复：按 eval_metric/op 定向读取并统一 higher-is-better。

### 审计/循环语义
- **I4. 审计 Restart 支路是死代码**（`recoverable = has_eval` 策展路径恒 True → restart 永不触发；
  AREX 三态退化为 Accept/Refine 两态）。修复：recoverable 综合连续失败轮次判定。
- **I5. round-0 审计经 objective 看到内循环 system prompt**（inner_agent_config 折入 goal，
  `audit_input["objective"]=goal`）——轻度违反不变量①注释承诺。修复：objective 一律用 base_goal。
- **I6. 未识别决策静默空转**（循环只认 EXIT_SUCCESS/audit_refine/audit_restart；CLI decide
  解析失败产生的 CONTINUE 会让 goal/参数不变地空转到预算耗尽）。修复：未识别按 refine 或
  failed 处理 + record_metric 暴露。
- **I9. kaggle 小数据/字符串标签崩溃链**：单样本类时 StratifiedKFold(2) 仍炸；字符串标签
  `to_numeric(coerce)`→NaN→`int(nan)` 崩；len<50 静默跳过 heldout 无标记；frac>0.5 无护栏。
- **I10. collab restart 丢弃同批人工 adjustments**（先 apply 再 restart 重置）。

### SSE / 前端契约（主代理复核发现）
- **F1. SSE 进度事件从后台线程发出即丢**（`progress_bus.py:47-54`）：bg 线程无 running loop，
  回退路径 `asyncio.all_tasks()` 本身也要求运行中的 loop → RuntimeError → 被 `_emit` 的
  try/except 吞掉。连接握手正常，但 inner_done/audit_done/finished 永远到不了前端。
  修复：`create_app` 启动时（lifespan/首请求）捕获主 loop 引用存入 bus，emit 直接使用。
- **F2. 一/二/三期与基准套件能力前端全盲**：`client.ts` 无 /playbook、/strategies、
  /evolution（GET+POST）、/benchmark-suites 任何绑定 → 新能力 UI 不可见、不可操作。
- **F3. reproduce 死胡同**：前端全库无 start-run 调用；reproduce 只建 REQUESTED run，
  UI 上永远无法启动 → 复现功能断链。修复：Leaderboard 复现后给「启动」入口
  （POST /workflow-runs/{id}/start 或 launch）。

---

## 💡 Minor（14 项：清扫批）

- **M1** 补丁验证轮同时叠加新 playbook/experience 注入，delta 归因不纯净（记录注入版本即可）。
- **M2** harness 工具调用用 `getattr(self.sdk, tool)`，任意 SDK 公开方法（如 mark_pruned）都可被
  agent 调起——应改显式白名单查表（`harness.py:260-263`）。
- **M3** 进化回放硬编码 EvalCompletedEvent；缓冲事件的 `report_ref`（`kaggle-eval://evo-<cand>`）
  不改写，审计/假设树的 evidence_ref 指向不存在的 stage；恢复回放非幂等。
- **M4** `evolution.py:71` 用内建 `hash()`（PYTHONHASHSEED 随机化）→ 跨进程不可复现，
  与 seed=42 承诺矛盾。改 md5/sha1 稳定哈希。
- **M5** `ExperienceEntry` 无 run_id 字段——跨 run 是设计意图，但不满足不变量③字面要求
  （无法按 run 审计/清除）。可加 `source_run_id` 可选字段。
- **M6** `api.py:1062` 对 `collaboration_aborted` 调 `set_run_status` → WorkflowStatus 无此枚举
  → ValueError → 外层 except 误标 failed 且 `capture_run_record` 被跳过（人工中止的 run
  永远进不了榜单）。
- **M7** `validate_route` 对 HITL_REQUIRED 无 target 约束；未知 REVISIT target 只 warn 不 reject。
- **M8** collab 暂停无限阻塞（人工永不 resolve 则线程挂死）；暂停时长不计预算。
- **M9** 审计信号三重自锚定（prior_audits + objective 文本 + rejected_candidates 同源），
  非泄漏但建议收敛为单一通道。
- **M10** 进化循环代内无预算检查（一代最多超 population_size 次）、不支持 cancel_event。
- **M11** SubprocessTransport stdout 读无超时、trace seq 恒 0。
- **P1** progress_bus 每 run 单队列：第二个订阅者会**顶掉**第一个的队列（前者永远收不到事件）。
- **P2** store.py 每次变更全量重写 JSON（写放大；run/event 量大后是性能隐患，可追加式 events 段）。
- **F4** useSSE 的 `ProgressEvent.kind` 联合类型缺 `generation_done`/`budget_exceeded` 等新 kind。

---

## 复核确认的良好现状（不改变）

- 隔离不变量①在 scripted 路径严格成立（playbook/experience 只进 inner，测试固化）；
  进化循环 BufferedSDK 只读直通 + 主线程串行回放的并发模型正确（store.py 读写持锁）。
- store.py 持久化为锁内 tmp+os.replace 原子写；损坏文件自动备份恢复。
- task_manager 的 error 任务不会被永久跳过（is_done 只认 ok），崩溃恢复语义正确。
- useSSE 的 EventSource 生命周期管理（卸载/runId 变更 close）正确。

## 建议修复批次（待确认后执行）

| 批次 | 内容 | 项 |
|---|---|---|
| 批 1 | 🚨 隔离与护栏 | C2、C4、C5、C1、C3 |
| 批 2 | ⚠️ 元循环/验证语义 | I1、I2+I10、I3、I7、I8 |
| 批 3 | ⚠️ 审计语义 + SSE + 前端补全 | I4、I5、I6、I9、F1、F2、F3 |
| 批 4 | 💡 清扫 | 全部 Minor |

每批完成后跑全量测试（基线 130 passed）+ 按需 tsc。

---

## 修复执行结果（2026-07-30 当日完成，批 1→4 全部落地）

**批 1（🚨）✅**
- C2：`transport.py::_exec_tool` 的 run_capability 分支接入 `assert_inner_capability_allowed`（CLI 路径与 harness 路径护栏一致；验证：layer_11 被拒、kaggle_eval 正常）。
- C4：`_fallback` run_stage 改返 `failed/failed`，不再伪造成功。
- C5：能力调用计数移入 `run_capability` 本体（`_cap_call_counts[run_id]`），agent 内部调用逐次计数；`dispatch_open_goal` 与进化候选回放同样计数。
- C1：`inner_event=None` 时回退扫描本 run 最近 `eval_completed` 事件填充审计 metrics/report_ref/gate。
- C3：新增 `_rescind_pending()`，三处 collab 人工调整前先撤销未验证补丁（恢复 pre_patch + `expired_superseded`）。

**批 2（⚠️ 元循环）✅**
- I1：补丁验证基线改为"应用时上一轮 inner_acc"（不再取历史最优）。
- I2：audit restart 真重置 `inner_params = orig`；I10：collab restart 后在 orig 上重放 adjustments。
- I3：`run_capability` 执行器异常时 stage 转 FAILED 再抛出；api 两处 `_drive` 增加 finally → `expire_pending_strategies(run_id)`（新增公开方法）。
- I7：进化循环出口统一返回 `archive.best(run_id)`（全局最优）。
- I8：新增 `_primary_score()`（accuracy→primary 回退 + op=le 取负），双循环/进化适应度/验证统一方向；审计 heldout_consistency 要求 cv 键存在。

**批 3（⚠️ 审计+SSE+前端）✅**
- I4：`recoverable` 引入连续 REFINE streak（≥3 → False），restart 支路可达（验证：3 连 refine 后 RESTART）。
- I5：`audit_input["objective"]` 一律用 `base_goal`（内循环指令不再进审计）。
- I6：未识别决策按 refine 兜底（改 goal + refine_hook + `loop.unrecognized_decision` 指标）。
- I9：标签先 dropna、非数值标签 factorize（iris/breast_cancer 可跑，验证 iris acc=0.9686）；`_min_class<2` 明确报错；heldout_frac clamp 到 [0,0.5]；跳过 heldout 时 metrics 显式标记 `heldout_skipped`。
- F1：`ProgressBus.set_loop()` + SSE 端点捕获主 loop（后台线程进度事件不再被吞）。
- F2：`client.ts` 新增 playbook/strategies/evolution/benchmark-suites 绑定；RunDashboard 新增「进化观察」子 Tab（新组件 `EvolutionPanel.tsx`：playbook/策略生命周期/进化种群三表）。
- F3：`POST /research-records/{id}/reproduce?autostart=true`（复用 `_build_run_ctx`+`_spawn_full_experiment`，平台任务即刻重跑；tracked-only 建档为 REQUESTED 并注明）；Leaderboard 新增「复现并启动」按钮。

**批 4（💡）✅（M7/M8/M11/P2 判定暂缓，理由见下）**
- M1 验证指标带 playbook 条数标签；M2 harness 工具调用先过 AGENT_TOOL_NAMES 白名单；
- M3 回放重写 report_ref/uri 到真实 stage；M4 哈希改 blake2b（跨进程稳定）；
- M5 ExperienceEntry 增 `source_run_id`（旧数据兼容），自动蒸馏带 run_id；
- M6 `_map_summary_status()`（collaboration_aborted→cancelled 等），两处 `_drive` 不再 ValueError；
- M10 进化循环支持 cancel_event + 代内逐候选预算检查；
- P1 progress_bus 改为每 run 订阅者集合（不再互相顶掉）；F4 useSSE kinds 补全。
- 暂缓：M7（validate_route 收紧需契约讨论）、M8（collab 超时策略需产品决定）、
  M11（SubprocessTransport 读超时需 select 改造）、P2（JSON 写放大属架构取舍）。

**验证**：全量 **132 passed** + 前端 **tsc 0 errors** + 定点冒烟（C2/C4/I4/I9 全部行为符合预期）。
