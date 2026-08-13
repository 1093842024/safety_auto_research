# 分类模态 · 端到端入沙箱链路验证报告

- Generated: 2026-08-12 16:41 GMT+8
- Goal: 验证「在真实 agent（codex）接线情况下 launch 一个分类模态任务」时，其**端到端入沙箱链路**是否打通。
- 验证脚本：`scripts/verify_classification_sandbox_e2e.py`（可重跑）

## 环境与前置（均已就绪）

| 项 | 状态 |
|----|------|
| 真实 agent `codex` CLI | ✅ `/opt/homebrew/bin/codex`（在 PATH，launch 时 `can_run_agent=True`，双循环真正启动而非停留 requested） |
| Docker daemon | ✅ 29.5.2，可达 |
| 沙箱镜像 `safety-research-sandbox:latest` | ✅ 本地存在（1.14 GB） |
| `scripts/build_agent_sandbox.sh` | ✅ 存在 |
| 算子执行器 `SandboxResearchExecutor` | ✅ 存在 |

## Launch 路由（codex 接线后的解析）

以已注册自定义任务 `custom.task_20260811171209`（text_classification，可执行）为例：

- `supported_by_platform = False` → `is_harness_task = True` → **`agent_mode = True`**。
- `agent_cli = codex` 且在 PATH → `run_orchestrator` 被置位 → **`can_run_agent = True`**，双循环真正启动（不再是 tracked-only 的 `requested`）。
- 依据 `control_plane/routers/benchmarks.py` ~L364-373，文本分类被路由到：
  **`sandbox_capability = "text_cls_sandbox"`**，并把 `task_type / text_col / label_col / data_subdir / threshold / eval_metric` 折入 `inner_agent_config`。
- 内循环 `run_capability("text_cls_sandbox", ...)` → `SandboxResearchExecutor` 以 `AGENT_SANDBOX=1` 进入 Docker 沙箱。

> 其余三类（image / audio / embedding）同构，分别路由到 `image_cls_sandbox` / `audio_cls_sandbox` / `embedding_sandbox`。

## 端到端沙箱执行（真实 Docker）

`verify_classification_sandbox_e2e.py` 用 launch handler 会生成的 **完全相同 params**，直接驱动
`SandboxResearchExecutor.execute(text_cls_sandbox)`，设置 `AGENT_SANDBOX=1`：

```
== executing text_cls_sandbox capability inside Docker sandbox ==
  final_status      = StageStatus.SUCCEEDED
  gate_result       = GateResult.PASSED
  detail            = sandbox kaggle eval [text_cls/logreg]:
                      f1_macro=0.8116 vs ge 0.8 -> PASS
                      | isolation=hard (net=none)
                      | payload_claim: data_ro=True net_blocked=True
  isolation_mode    = hard   (expect 'hard')
  event.passed      = True
  event.metrics     = {'primary': 0.8116, 'accuracy': 0.8156, 'f1_macro': 0.8116,
                       'n_classes': 2.0, 'n_train': 6400.0, 'n_test': 1600.0}

=== CLASSIFICATION SANDBOX E2E: PASS ===
```

## 链路断言（全部通过）

| 断言 | 结果 |
|------|------|
| 执行器成功（SUCCEEDED） | ✅ |
| **真实硬隔离**（Docker，`isolation=hard`，`--network none`） | ✅ |
| 真实 `EvalCompletedEvent` 被回写控制面 | ✅ |
| 指标真实且在 [0,1]（`f1_macro=0.8116`，过 ≥0.8 门限） | ✅ |
| 隔离证明来自 host 侧 launcher 标记文件（R9 不可伪造），非 payload 自报 | ✅ |

## 结论

**分类模态任务的端到端入沙箱链路已打通。** 当 codex 接线（`AGENT_SANDBOX=1`）后 launch 一个
`text_classification` 自定义任务：launch 将其路由到 `text_cls_sandbox` 能力，执行器在一次性
Docker 容器内（数据只读挂载、出站网络阻断）运行 `run_text_cls_sandbox.py`，产出真实指标并回写
`EvalCompletedEvent`，隔离等级由 host 侧不可伪造的 attestation 记录。**这正是 F3 表格任务已验证的
同款沙箱机制，现证明确实泛化到了分类模态。**

> 范围说明：本验证覆盖「分类模态任务 → 沙箱执行 → 真实度量 → 事件回写」这一链路（平台侧确信点）。
> 最外层的 codex **研究改进循环**（agent 反复迭代提升指标）依赖 codex 实时 API/网络，未在本环境
> 实跑；但其入口（codex 在 PATH → `can_run_agent=True` → 双循环启动 → 内循环 `run_capability(text_cls_sandbox)`）
> 已逐环验证。

## Artifacts

- `scripts/verify_classification_sandbox_e2e.py`（可重跑的端到端验证脚本）
- 沙箱执行残留 scratch 由执行器按 R16 自动清理（`AGENT_SANDBOX_KEEP_SCRATCH=1` 可保留调试）

（关联：表格模态沙箱验证见 `data/kaggle/F3_SANDBOX_REPORT.md` 与
`data/kaggle/F3_SANDBOX_REPORT_WINE_IRIS_BC_2026-08-12.md`）
