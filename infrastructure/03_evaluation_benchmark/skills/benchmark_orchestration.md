---
name: benchmark-orchestration
description: >-
  安全 Auto-Research 的评估与基准编排 runbook：如何用 AutoLab Harbor 沙箱跑标准化任务评测，
  以及如何用 Claudini Bench/Leaderboard 做对抗算法（攻击/防御）的 Pareto 前沿评测。
  当需要"衡量 agent 或算法在安全任务上的真实水平"时触发。
---

# Benchmark Orchestration（评估与基准编排）

把"想法/算法"放进可复现的沙箱，用锚定评分得到可比较的数字。本 runbook 串联
`../agents/autolab_harness/` 与 `../agents/claudini_eval/` 两套已沉淀框架。

## A. 路径一：AutoLab 式标准化任务评测（通用能力 / 系统优化）

适用：模型开发、系统优化、CUDA、puzzle/算法类安全任务（如 `safety_router`、`grpo_multisource`）。

1. **准备任务**：复制 `../datasets_tasks/autolab_tasks/<task>/` 结构
   （`task.toml` + `instruction.md` + `environment/` + `tests/`）到你的运行区。
2. **起沙箱**：`uv sync && bash harbor_patch.sh`（启用 GPU、延长等待）。
3. **跑评测**：运行 `autolab_harness/main.py`，agent 在容器内执行，`allow_internet=false`。
4. **读分**：verifier 产出 metric，按 log-scaled 锚定（baseline→reference）归一化到 [0,1]。
5. **入榜**：把 normalized score 汇入内部 Leaderboard，对比 baseline / reference / 其他 agent。

## B. 路径二：Claudini 式对抗算法评测（攻击/防御 Pareto 前沿）

适用：有明确 benchmark 的安全算法设计——攻击算法、防御策略、优化器、对齐方法。

1. **实现方法**：继承 `claudini_eval/base.py` 的 `TokenOptimizer`，实现 `setup → step` + FLOP 计数。
   子类化即被 `methods/registry.py` 自动发现，零配置注册。
2. **选配置**：从 `claudini_eval/configs/`（15 个预设）挑 YAML，或用 CLI 覆盖参数。
3. **跑 Bench**：`python claudini_eval/run_bench.py --config <yaml>`。
4. **看排名**：`python claudini_eval/leaderboard.py` 按 track×model 分组，输出 ASR ↔ Compute Pareto 前沿。

## C. 路径三：Idea 质量评测（研究想法阶段）

适用：在写代码前判断 idea 是否值得做。

- 用 `../datasets_tasks/iclr2026_oral_problem_seeds_formal_100.jsonl` 作种子集。
- 跑 idea 质量三轴（A/B/C，0–100）与 scoop-check（5 级 overlap）。
- 落地：见 `02_idea_generation_eval/eval_methods/`。

## 质量红线

- 评测必须在隔离沙箱（Harbor / Docker）内进行，禁止联网外泄。
- 任务须含 `harbor-canary` GUID，禁止进入训练语料。
- 红队/攻击评测目标细节不外传；ASR 须同时报防御侧安全性降级。
