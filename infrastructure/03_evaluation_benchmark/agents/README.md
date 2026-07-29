# 评估与基准 Agent 规范（安全 Auto-Research）

> 本目录沉淀「评估与基准」基础设施层的 Agent / 框架入口源码与角色定义。
> 任务数据见 `../datasets_tasks/`，评分方式见 `../eval_methods/scoring.md`。

## 1. 角色总览

| Agent / 框架 | 职责 | 已沉淀源码 | 关键能力 |
|-------------|------|-----------|---------|
| **AutoLab Harness（Verifier）** | 容器化沙箱内运行 agent、隔离评测 | `autolab_harness/`（main.py, harbor_patch.sh, pyproject.toml, README.md） | Harbor 沙箱、log-scaled 锚定评分、轨迹回放 |
| **Claudini Bench CLI** | 统一对抗评测入口（YAML 预设 + CLI 覆盖） | `claudini_eval/`（run_bench.py, leaderboard.py, base.py, registry.py, configs/） | TokenOptimizer 抽象、15 预设、Pareto 排名 |
| **Claudini Leaderboard** | 按 track×model 自动排名 | `claudini_eval/leaderboard.py` | ASR ↔ Compute Pareto 前沿 |
| **ARC-Bench Agent** | 跨域开放式自主科研评测编排 | `arc_bench_agent/`（orchestrator, acquirer, surveyor, selector, validator） | 55 主题、五领域（ML/physics/quantum/biology/statistics）统一评测 |
| **MLEvolve Eval** | MLE-bench #1 系统（65.3% 奖牌率/12h）评测 agent | `mle_evolve_eval/`（run.py + 11 角色 + engine/MCGS） | 75 Kaggle 任务、Low/Med/High 三级、Monte Carlo Graph Search |

## 2. AutoLab Harness（容器化评测）

- **运行方式**：`uv sync` → `bash harbor_patch.sh`（启用 GPU + 延长等待）→ 跑 `main.py`。
- **标准化任务结构**：`task.toml` + `instruction.md` + `environment/`(Dockerfile) + `tests/`(verify.py) + `solution/`(reference)。
- **隔离**：agent 在 Harbor 容器内运行，`allow_internet=false`，防外泄/作弊。
- **评分**：log-scaled 锚定（baseline→reference），clip 到 [0,1]；含 `harbor-canary` GUID 防训练污染。
- **已沉淀任务**：`../datasets_tasks/autolab_tasks/` 含 **AutoLab 全量 36 个任务**（4 大类：Model Development 7 / System Optimization 15 / Puzzle&Challenge 10 / CUDA 4）。安全相关代表：safety_router、grpo_multisource、adversarial_splay、data_select_ifeval、agent_tool_routing。

## 3. Claudini Bench / Leaderboard（对抗算法评测）

- **统一抽象 `TokenOptimizer`**（`base.py`）：`setup → step` 循环 + FLOP 计数器，使不同攻击方法可统一评测。
- **自动注册**（`methods/registry.py`）：子类化即被发现，零配置方法管理。
- **Bench CLI**（`run_bench.py`）：YAML 预设配置 + CLI 覆盖，标准配置下评估。
- **15 个预设 YAML**（`configs/`）：覆盖 jailbreaking（随机目标 + safeguard）与 prompt injection（8B/70B）。
- **Leaderboard**（`leaderboard.py`）：按 track × model 分组，输出 ASR ↔ Compute 的 Pareto 前沿。

### 评测配置示例（来自 configs/）
```yaml
# injection_8b.yaml（节选）
target_model: "xxx-8b"
eval:
  method: prompt_injection
  safeguard: enabled
budget:
  max_flops: 1e15
```

## 4. 与其他层的接口

```
idea 生成与评估校验层 ──(候选 idea / 攻击方法)──> Claudini Bench (自动评估)
                                                   │
评估与基准层 ──(分数 / Pareto 前沿)──> 回灌 idea 层 (Seed Analyzer 下一轮)
              ──(normalized score)──> AutoLab Leaderboard (跨任务排名)
```

## 5. 安全领域适配要点

- 红队 benchmark 须在受控环境运行，目标细节不外传。
- ASR 评测须同时覆盖攻击成功率与安全性降级（防御侧）。
- 自建安全基准优先复用 AutoLab `task.toml` 模板 + Harbor 沙箱，保证可复现与防作弊。
