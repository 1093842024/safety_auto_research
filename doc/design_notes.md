# 设计笔记（活文档）

> 本文件是 `safety_auto_research` 各功能设计的**单一权威**文档。按特性分节，每节标注状态。
> **维护约定**：新设计直接在此文件追加/更新分节，**不再为单个特性新建独立 design_*.md**
> （避免文档膨胀，类比 `code_review_STATUS.md`）。旧独立设计文档已并入本文件并删除。
> 最后整理：2026-08-07。

---

## §1 控制平面拆分与依赖注入（A1 + A3，附 F1 复核）

**状态**：✅ 已落地（2026-08-07 第五轮）。对应 `product_ux_backlog.md` 的 A1 / A3；F1 复核为 stale。

### 问题
重构前 `control_plane/api.py` 为 **1975 行单文件**：`create_app()` 内联 57 个路由 handler（52 path）+ 4 个私有 helper；依赖经闭包共享，不可见/不可替换/不可单测。

### 不变量（零回归红线）
| 目标 | 约束 |
|------|------|
| 按领域拆分路由 | **HTTP 表面零变化**：path/method/response_model/status_code/summary/tags 全不变 |
| 引入 DI | `create_app(service: ControlPlaneService | None = None) -> FastAPI` **签名不变**（9 个测试文件依赖） |
| 零逻辑回归 | handler 函数体**逐字搬运** |

### 方案
- **A3 `control_plane/deps.py`**：`ControlPlaneDeps` 聚合 `svc`/`state_store`/`orchestrator`/`run_cancel_events`/`shutdown_event`/`progress_bus` + 3 个 helper（`make_agent_orchestrator`/`build_run_ctx`/`spawn_full_experiment`）；`build_deps(service)` 复刻原装配顺序（含 `AGENT_COMMAND`→agent harness）。模块级 `_shutdown_event`/`_bg_threads`/`_run_cancel_events` + `atexit` 优雅关闭，语义与原 `api.py` 一致。
- **A1 `control_plane/routers/`**（7 个）：workflow_runs / loops / evolution / observability / benchmarks / research_records / experiments。每个导出 `build_<domain>_router(deps)`。**本地别名绑定**是关键——handler 体内引用名在 builder 作用域重绑同名局部变量，源码一字不改。
- **`api.py` → 73 行薄装配器**：仅 `create_app` + `@app.on_event("shutdown")` + 7 个 router builder。

### 验证
1. **OpenAPI 逐字比对**：重构前后 `openapi()["paths"]` 的 52 条 path 与 `components` **完全相等**。
2. **`tests/test_control_plane_structure.py`**（9 用例 + 15 subtests）：冻结 52 条 path×method 表；`api.py` 不得再出现 `@app.`、行数 <160；routers 不得反向 import `api`；`app.state.deps` 是 `ControlPlaneDeps` 且注入 service 被采用。
3. **全量回归 287 例全绿**（25 测试文件）。

### 已知差异（非功能）
FastAPI 0.139 的 `include_router` 保留惰性 `_IncludedRouter` 节点，路由比对须读 `app.openapi()["paths"]`（已改 `test_agent_protocol.py`）。

### 后续可选
`benchmarks.launch_benchmark_task`（247 行）可再下沉；`@app.on_event` 迁 lifespan。

### 附：F1 复核（stale）
SSE 实时推送**早已完整落地**——`progress_bus.py` 跨线程 `ProgressBus` 单例 + `GET /workflow-runs/{run_id}/stream`（`text/event-stream`）+ 5 处 `progress_callback=_progress_bus.emit` + 前端 `useSSE.ts`/`RunDashboard.tsx`。标记 `✅ 已修（stale）`，不重复实现。

---

## §2 审计追问交互协议（F6）

**状态**：✅ 已落地（2026-08-07 第四轮）。对应 `product_ux_backlog.md` 的 F6。

### 问题
外层审计只产一次性结论，unresolved claims 只能靠系统自动 refine/restart，人类无法对**单条 constraint** 定向追问。

### 决策
- 新增 `AuditFollowupEvent`（`event_type="audit_followup"`，字段：`run_id`/`audit_id`/`constraint_id`/`question`/`clarification`/`prior_status`/`new_status`/`new_score`/`response`/`confidence`/`recommendation`/`resolved`）。
- `AuditReport` 增可选 `followups: list[AuditFollowupEvent]`（向后兼容，原报告不可变，仅追加）。
- `POST /workflow-runs/{run_id}/audits/{audit_id}/followup`（可选 `GET .../followups`）：复用 `audit_executor.evaluate_constraint` 单约束重评 helper，用原 `audit_input` + `clarification` 重算该约束 score/status，重聚 confidence 与 recommendation，发 `AuditFollowupEvent`。
- **保持外层独立性**：`clarification` 仅作 judge 的 evidence 入参，与 `audit_input` 同源，遵守双循环隔离不变量。
- 前端 `AuditBoard.tsx` 每条 `status!=verified` 约束渲染内联追问表单 + followup 历史药丸；`client.ts` 加 `followupAudit()`。

### 明确不在 v1
流式/异步化、真实 LLM-judge 评 clarification、直接改 `IterationRouter`、追问权限工作流。

### 验证
`tests/test_audit_followup.py` 8 例全绿（澄清→verified、全部清零→accept、404/400、原报告不可变）。

---

## §3 无 Docker 环境下的 agent 研究隔离（F3，关联 F2）

**状态**：✅ 已实现并端到端验证（2026-08-10）。对应 `product_ux_backlog.md` 的 F3 / F2。本轮补两件事：① 前端 launch 流程加隔离提示 UI（用户可在控制台明确看到「该任务将以容器隔离方式执行」）；② F2 数据瓶颈突破——`text_classification` 作为首个能真跑的非 kaggle 模态，用真实 Stanford SST2 数据在沙箱内跑通（f1=0.8116，隔离三项全过）。

### 问题澄清
- **F3 空跑真因**：非「无外部 Agent」——agent 集成层（`RemoteAgentHarness` + `CodexTransport`/`ClaudeCodeTransport` + `SubprocessTransport`）早已就绪，本机 `codex`(0.133.0)/`claude` 均在，`deps.build_deps` 设 `AGENT_COMMAND` 即自动接入。真因是 16 个 agent-mode 任务原 `harbor run`/`arbor benchmark` 评估器被剥离后只剩纯文本 prompt，本地**无可执行 evaluator**。
- **F2 广度**是 F3 派生结果：F3 真执行后 16 个任务即可跑（kaggle 类本就有本地数据；其余取决于数据集可得性）。

### PoC 实测（macOS 26.5.1 / Tahoe, arm64，`scripts/agent_sandbox_poc.sh`）
| 隔离原语 | 实测 | 证据 |
|----------|------|------|
| `sandbox-exec`（Seatbelt） | ❌ 死 | `sandbox_apply: Operation not permitted`（内置 profile 也被拒；`-D` 参数绑定报 `unbound variable`） |
| `uv` | ✅ 可用 | hermetic venv <1s |
| `sudo -u nobody` / `chroot` | ❌ 被挡 | 本环境 `sudo` 返回 `Operation not permitted` |
| `podman` | ⚠️ 未初始化 | 二进制在，但无 machine（需 Linux VM） |

**结论**：本机无轻量现成 syscall 沙箱；`uv` 仅依赖隔离。但 2026-08-10 用户通过 **Colima + Docker CLI** 在本机装好了 Docker（方案 A，零改习惯），于是 F3 改为**本地 Docker 硬隔离**。

### 决策（更新：2026-08-10 用户装好 Colima/Docker）
1. **本地用 Docker 硬隔离**（替代原「软隔离」决策）：研究代码在一次性 Linux 容器内执行，数据集只读挂载、网络默认关闭、dropped caps + no-new-privileges + 资源限额。
2. **不做 podman 硬隔离**（维持不变）。
3. **远程 Docker 接入方式待定**：本机已有 Colima 满足本地硬隔离；若需更大算力/数据集，再走远程 `docker context`/SSH——接入方式仍待定，但已非 F3 阻塞项。

### 实现（`scripts/`）
- `scripts/sandbox.Dockerfile`：最小研究镜像（`python:3.11-slim` + scikit-learn/pandas/numpy/scipy），运行时 `--network none` 纯离线。
- `scripts/build_agent_sandbox.sh`：硬隔离执行器。检测 Docker 不可用或 `AGENT_SANDBOX_DISABLE=1` 时**自动降级为软隔离**（host 直跑 + 同 env，打印警告）；否则 `docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --memory/--cpus/--pids-limit`，把 `AGENT_DATA_DIR→/data:ro`、`AGENT_REPO_DIR→/repo:ro`、`AGENT_SCRATCH_DIR→/scratch:rw` 挂入，研究命令在容器内执行。
- `scripts/sandbox_examples/run_kaggle_eval_sandbox.py`：容器内独立 Kaggle 评测（**忠实复刻** `kaggle_eval_executor` 的特征工程 + GBM + 5-fold CV + 指标 + gate），读 `/data` 写 `/scratch/result_<preset>.json`，并写入**隔离证明**（数据挂载只读、网络不可达）。
- `scripts/run_f3_sandbox_demo.py`：宿主编排——建 `ControlPlaneService`/`PlatformSDK` → 为每个任务建 `StageRun` → 调用 `build_agent_sandbox.sh` 在容器内跑评测 → 读回 `result.json` → **emit 真实 `EvalCompletedEvent`**（即此前推迟的「结果回写」，闭环接上）。

### 推广：`run_capability` 的 agent 模式（2026-08-10 落地）
单任务 demo 已推广为 **agent 能力的常驻沙箱路径**，不再只是独立脚本：
- `execution_plane/capabilities/sandbox_executor.py`：`SandboxResearchExecutor`——capability 驱动的沙箱执行器，两种模式：
  * **kaggle / tabular**：`params` 带 `preset`（或 `custom` + `target`/`data_subdir`/`threshold`/`eval_metric`/`op`），构建命令跑 `run_kaggle_eval_sandbox.py`，读出 `result_<preset>.json` → 重建真实 `EvalCompletedEvent`。把原「空跑」的表格类基准任务接成容器内真跑。
  * **通用 `research_cmd`**：`params["research_cmd"]`（容器内路径 `/repo/...`、`/data/...`）放进同一沙箱跑，读回 `result.json`。这是 **16 个非原生 tracked-only 任务**的接入路径——有可运行脚本+数据就同路径真跑，没有则**诚实失败**（不再静默空跑）。
  * **文本分类（`text_classification`）**：`cap_id == "text_cls_sandbox"`（或 `params["task_type"]=="text_classification"` / `preset=="text_cls"`）时构建命令跑 `scripts/sandbox_examples/run_text_cls_sandbox.py`，`data_dir` 默认 `data/benchmark`、挂载 `/data/{data_subdir}`（如 `text_cls_demo`）。这是 F2 数据瓶颈突破的首个落地模态——在仅含 sklearn 的沙箱镜像内即可真跑（详见「F2 数据瓶颈突破」）。
- `run_capability` 路由：在 `execution_plane/orchestrator.py` 中，当能力可沙箱化（`params["sandbox"] is True` 或 `AGENT_SANDBOX=1` 且能力在 `SANDBOX_CAPABILITY_IDS` = `{kaggle_eval, kaggle_eval_sandbox, run_research_sandbox, text_cls_sandbox}`）时，直接走 `SandboxResearchExecutor`（并注入 `_capability_id` 供 `_plan` 选分支），研究命令在容器内执行、宿主读 `result.json` 回写事件。Agent（codex/claude）仍在宿主，仅研究制品被隔离。
- 新增 3 个 non-infra 能力（不计入 10 个基础设施层）：`kaggle_eval_sandbox`、`run_research_sandbox`、`text_cls_sandbox`（文本分类专用，见「F2 数据瓶颈突破」），`/agent/protocol` 现暴露 15 个能力。
- `launch_benchmark_task`（`control_plane/routers/benchmarks.py`）：agent 模式下的任务在 `inner_agent_config` 注入沙箱提示——表格/kaggle 任务走 `kaggle_eval` 沙箱路由；非原生任务把 `task.run_command` 作为 `research_cmd` + `run_research_sandbox` 能力交给 agent，使其在容器内尝试真跑。

### 前端 launch 流程隔离提示（2026-08-10 落地）
让用户在**控制台明确看到「该任务将以容器隔离方式执行」**，把后端隔离信号透传到前端 UI：
- **后端信号（`sandbox_isolation`，4 级）**：`benchmark_tasks/__init__.py` 的 `_sandbox_isolation(t)` 与 `control_plane/routers/benchmarks.py` 的 `launch_benchmark_task` 均输出该字段：
  * `none` —— 平台原生任务（`kaggle_eval` harness 或 `supported_by_platform`），无需沙箱；
  * `container-hard` —— agent 模式 + `AGENT_SANDBOX=1` + Docker 可用，一次性容器硬隔离（只读数据 + 无网络）；
  * `container-soft` —— agent 模式 + `AGENT_SANDBOX=1` 但 Docker 不可用，自动降级软隔离（host 直跑 + 警告）；
  * `host` —— agent 模式但 `AGENT_SANDBOX` 未设，宿主运行（无隔离）。
- **前端呈现**：`api/client.ts` 的 `BenchmarkTask` 接口新增 `sandbox_isolation?`；`BenchmarkCatalog.tsx` 与 `NewResearch.tsx` 加 `isoBadge()`（🐳 容器隔离 / 🛡️ 软隔离 / ⚠️ 无隔离 / 无徽标），卡片与详情页显示；`NewResearch` 的 step-2 确认视图在 launch 按钮前按 `container-hard`/`container-soft`/`host` 分别弹横幅，明确告知用户「该任务将以容器隔离方式执行 / 软隔离 / 宿主无隔离」。
- **实测**：后端 `/benchmark-tasks` 返回 22 任务中 17 个 agent 模式任务全 `container-hard`、5 个平台原生 `none`；UI 已在 http://localhost:5173 验证。

### 环境配置要点（本机启用 F3 必须）
- **Docker Hub 在本机/Colima 不可达**，需配 registry mirror：在 `~/.colima/default/colima.yaml` 的 `docker:` 块加 `registry-mirrors: [https://docker.m.daocloud.io]`，`colima stop && colima start` 生效（已验证 daocloud 镜像可达）。
- **`~/.docker/config.json` 含 `"credsStore":"desktop"`**（Docker Desktop 助手，未装）→ 所有 pull 报 `docker-credential-desktop not found`。已移除该行（已备份 `config.json.bak-*`），pull 即恢复正常。
- **Colima VM 只与 macOS 共享 `$HOME` 下路径**：scratch 目录必须放在 home 下（如 `~/.cache/agent_sandbox_scratch`），放 `/tmp`、`/var/folders` 的容器写盘会落到 VM 本地盘、宿主不可见。此坑已通过把默认 `AGENT_SCRATCH_DIR` 改为 `${HOME}/.cache/agent_sandbox_scratch` 规避。

### 验证（2026-08-10 实跑，`run_f3_sandbox_demo.py`）
| 任务 | CV 精度 | 门 | 数据只读 | 网络阻断 | EvalEvent |
|------|---------|----|----------|----------|-----------|
| titanic | 0.8257 | PASS (≥0.82) | ✅ | ✅ | ✅ 真实事件 |
| spaceship | 0.7965 | FAIL (<0.80) | ✅ | ✅ | ✅ 真实事件 |

- 隔离证明三项全 `[x]`：数据集只读挂载（写 `/data` 被拒）、`--network none` 下出站连接不可达、每次运行都回写真实 `EvalCompletedEvent`。
- 报告落地 `data/kaggle/F3_SANDBOX_REPORT.md`；镜像 `safety-research-sandbox:latest` 已构建并缓存。

### F2 数据瓶颈突破（2026-08-10 落地）
沙箱只解决「能否安全跑」，不解决「数据从哪来」。本轮**让至少一个非 kaggle 任务真正在沙箱内跑起来**：
- **选模态**：16 个非原生任务中，`text_classification` 是唯一能在当前 sklearn-only 沙箱镜像内跑的模态（TF-IDF + 线性模型，无需 torch/GPU）；其余（image/audio/LLM/embedding）需 torch/GPU 或大模型权重，本机/容器内不可行。
- **真实数据获取（本机网络受限，踩坑后确定路径）**：`sklearn.datasets.fetch_20newsgroups` 被墙（403）、`datasets` 的 GLUE loader 因新版 hf_hub 失效、`fancyzhx/sms-spam-collection` 仓库名不存在、OpenML `fetch_openml` 解析 ARFF OOM（exit 137）。最终用 `curl` 直下 `stanfordnlp/sst2` 的 parquet（`https://huggingface.co/datasets/stanfordnlp/sst2/resolve/main/data/train-00000-of-00001.parquet`，OpenML/HF 直连可达）→ `pd.read_parquet` → 写 `data/benchmark/text_cls_demo/train.csv`（列 `text`/`label`，8000 行：positive 4438 / negative 3562）。生成脚本：`scripts/make_text_cls_demo_dataset.py`（支持 `--source {stanford_sst2,sms_spam,20newsgroups}`、`--limit`、`--out`）。
- **容器内评测**：`scripts/sandbox_examples/run_text_cls_sandbox.py`——容器内 TF-IDF + LogisticRegression/LinearSVC，含 `probe_readonly`/`probe_network_blocked` 隔离证明，输出 `result.json`，schema 仿 `run_kaggle_eval_sandbox.py`（`preset`/`model`/`eval_metric`/`threshold`/`op`/`passed`/`gate_passed`/`metrics`/`isolation`/`report_ref`）。
- **端到端真跑验证**：`run_capability("text_cls_sandbox", {sandbox:True, data_subdir:"text_cls_demo"})` → `eval_completed`，`primary(f1_macro)=0.8116`，`GATE_PASSED=True`（≥0.5），`DETAIL` 含 `isolation: data_ro=True net_blocked=True`，发出真实 `EvalCompletedEvent`。回归测试 `tests/test_capabilities.py::TextClsSandboxRunCapabilityTest` 用真实 SST2 demo 数据软降级实跑通过（断言 `primary > 0.5`、event 非空）。
- **现状边界**：text_classification 已真跑（SST2 f1=0.81）；image/audio/LLM/embedding 受 torch/GPU/大模型权重限制，沙箱仅接 `run_research_sandbox` 诚实失败路径，暂无可用镜像/数据。

### 残留风险
- 软隔离降级路径（无 Docker 时）仍不防不可信代码/外传——已知取舍，仅作无 Docker 时的便利边界。
- **F2 数据瓶颈（部分突破）**：16 任务多依赖 Harbor/Arbor 数据集，本机未必有；kaggle 类（titanic/spaceship/…）本机有数据已可真跑，`text_classification`（SST2）也已接 `text_cls_sandbox` 在沙箱内真跑（f1=0.8116）。其余 image/audio/LLM/embedding 模态受 torch/GPU/大模型权重限制，沙箱仅接 `run_research_sandbox` 诚实失败路径。

### 后续
- ✅ 已做：① 把 16 个 agent-mode 任务接成容器内真跑（表格类经 `kaggle_eval` 沙箱、非原生经 `research_cmd`、文本分类经 `text_cls_sandbox`，无脚本则诚实失败）；② 前端 launch 流程加隔离提示 UI（🐳 徽标 + step-2 横幅），透传后端 `sandbox_isolation` 4 级信号；③ F2 `text_classification` 真跑（真实 SST2 数据 + 沙箱隔离证明 + 真实 `EvalCompletedEvent`）。
- 更新 backlog：F3 标 ✅ 已修（本地 Docker 硬隔离 + run_capability 已推广 + 前端隔离提示 UI）；F2 标「部分解（kaggle 类 + text_classification 已真跑；其余模态受 torch/GPU/数据可得性限制）」。
- 远程 Docker 接入方式确定后补充 §3 决策 3 的实现方案。
