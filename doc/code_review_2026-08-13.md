# 代码审查 2026-08-13 — OSS 基准目录接线 + 沙箱 runner 批次

> 基线：`6b7d95c`（bench mark）之后至本次提交的全部未提交改动。
> 测试：328 passed, 0 failed, 15 subtests passed（`numpydoc` 缺失导致收集崩溃，已补装到 managed venv）。
> 类型检查：frontend `tsc --noEmit` 0 errors（前端无本轮改动）。

## 审查范围
- `benchmark_tasks/__init__.py`（MODIFIED）：基准目录重构，新增 `data_local` 字段；`_availability()` 对 `data_local` 任务返回可 launch；尾部 `from ._oss_wired_extra import build_extra_tasks` + `_TASKS.extend(...)`。
- `benchmark_tasks/_oss_wired_extra.py`（NEW, 473 行）：`build_extra_tasks(BenchmarkTask, _oss)`，含 26 个样板优先 OSS 任务；末尾 `exec()` 读取 `experiments/oss_validation/sab/sab_19_task_blocks.py`（13 个 SAB 块）。
- 8 个沙箱 runner：`run_safety_router` / `run_claudini_tmeoa` / `run_sab` / `run_autolab` / `run_arbor_algotune_knn` / `run_arc_bench` / `run_ara_understanding` / `run_autoclaude_trigger_eval` / `run_audio_cls`（MODIFIED）。
- 验证/构建脚本：`verify_oss_sandbox_e2e.py`、`verify_classification_sandbox_e2e.py`、`_prep_kaggle_presets.py`、`_verify_tierA_B.py`、`sandbox.full.Dockerfile`。
- 依赖源模块：`experiments/oss_validation/{claudini_tmeoa,tmeoa,sab,arbor,safety_router}/`。

## 发现与处理

### 🔴 Critical
无。

### ⚠️ Important
| # | 文件 | 问题 | 处理 |
|---|------|------|------|
| I1 | `benchmark_tasks/_oss_wired_extra.py:429-470` | 导入时 `exec()` 读取 `experiments/oss_validation/sab/sab_19_task_blocks.py`；该文件此前未提交（因 `experiments/` 含 2.2G `.venvs`），新克隆会静默丢失 13 个 SAB 任务 | **化解**：本轮提交 `sab_19_task_blocks.py` 等源模块并 gitignore `.venvs`；`if os.path.exists` 守卫保证缺失时优雅降级不崩溃 |
| I2 | `run_claudini_tmeoa_sandbox.py` / `run_sab_sandbox.py` | 依赖 `experiments/oss_validation/{claudini_tmeoa,tmeoa,sab}` 模块，同样未提交 | 同 I1，源模块随本次提交 |

### 💡 Minor
| # | 文件 | 问题 |
|---|------|------|
| M1 | `run_claudini_tmeoa_sandbox.py:199` | `isolation["data_readonly"]` 硬编码 `True`（未探针），与其余 runner 不一致；该任务数据按构造只读，低风险 |
| M2 | `run_audio_cls_sandbox.py` | `torch` 由 try 内移到模块级无条件 import；沙箱镜像恒含 torch，但本地无 torch 时导入即失败（沙箱专用脚本，可接受） |

## 验证（已执行）
- `python -c` 加载 catalog：`extra=26`、`total _TASKS=36`、`sab=14`（13 块 + 内联 #92）、无重复 `task_id`、`data_local=28` → 与 `SAB_EXCLUDED`（6 个因库版本漂移）一致。
- 安全扫描（grep）：无 `api_key`/`secret`/`shell=True`/`os.system`/内网 URL；`exec_module` 为运行 first-party SAB 评分脚本（设计内）。
- 凭据：`victim.py` 经 `tmeoa.client` 从环境变量读取密钥，结果 JSON 不含任何密钥；预算按 query 计数且受 `run_attack` 约束。
- 指标一致性：runner 输出 `metrics` 的数值字段与 catalog 声明 `eval_metric`/`gates` 匹配（核对 safety_router/grpo_multisource/flash_attention/sab）。

## 提交范围决策
- **提交**：catalog 代码 + 8 个 runner + 4 个验证脚本 + Dockerfile + `doc/` 审查纪要 + `experiments/oss_validation/` 源模块（`claudini_tmeoa/`、`tmeoa/`、`arbor/`、`safety_router/`、`sab/sab_19_task_blocks.py`、`sab/sab_task_index.json`、报告与结果 JSON）。
- **排除（gitignore）**：`**/.venvs/`（2.2G 嵌套虚拟环境）、`data/kaggle/**/result_*.json`（运行产物）、`data/kaggle/F3_*REPORT*.md`、`scripts/*.whl`（3MB 二进制）、`experiments/oss_validation/sab/task_*`（330M 可再生物化数据/数据集，SAB runner 缺失时诚实 `env_blocked`）。
- 未提交 `data/oss/`（3.5M 物化数据，使 `data_local` 任务可运行，纳入提交）。

## 结论
SHIP。无阻塞性缺陷；结构性耦合经提交范围设计已化解，新克隆可自洽加载完整目录。
