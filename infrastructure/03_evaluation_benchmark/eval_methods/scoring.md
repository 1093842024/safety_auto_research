# 评估与基准：评分与排名方式

> 统一「评估与基准」层的评测约定。落地代码见 `../agents/`（AutoLab Harbor 评测框架、
> Claudini Bench/Leaderboard），任务数据见 `../datasets_tasks/`。

## 1. 锚定评分体系（Anchor Scoring）

避免天花板/地板效应：每条任务设 **baseline**（随机/零样本）与 **reference**（人类/最优），
score 在二者间线性或对数插值归一化到 [0,1]。

```
normalized = (score - baseline) / (reference - baseline)   # 线性
# 或 log-scaled（见下）
```

- baseline 选"最差合理解"，reference 选"SOTA / 人类专家解"。
- 未在 [baseline, reference] 区间的实测值 clip 到 [0,1]。

## 2. Log-scaled 评分（AutoLab）

> 来源：AutoLab。针对"加速比 / 参数量"等比尺度指标。

对每个任务以其优化指标（如 speedup、total_params）做 log 变换后锚定：

```
# 以越低越好的 metric 为例（如 total_params）
score = clamp( log(metric) 在 [log(baseline), log(reference)] 上的归一化, 0, 1 )
```

- 例（safety_router）：baseline=16641 参数（两层 MLP），reference=2081 参数（压缩 MLP），
  metric=`total_params`，direction=lower。
- 容器化 Harbor 沙箱保证可复现；agent 与 verifier 隔离运行。

## 3. 排名与 Leaderboard

| 来源 | 排名维度 | 入口 |
|------|---------|------|
| AutoLab | 按 task 的 normalized score；Live Leaderboard + 轨迹回放 | `autolab_harness/main.py` + https://autolab.moe |
| Claudini | 按 track × model 分组，ASR ↔ Compute 的 Pareto 前沿 | `claudini_eval/leaderboard.py` |
| MLE-bench | 奖牌率（Any-Medal / Gold），Low/Medium/High 三级 | MLEvolve 评估 |
| ARC-Bench | 55 主题开放式自主科研完成度 | AutoResearchClaw |
| IdeaSpark Eval | 100 个 ICLR-2026-Oral 种子；idea-quality 3 轴 0–100 + scoop-check 5 级 | `../datasets_tasks/iclr2026_oral_problem_seeds_formal_100.jsonl` |

## 4. 评分防作弊与合规

- **harbor-canary**：AutoLab 任务含 `harbor-canary` GUID，禁止进入训练语料。
- **隔离执行**：agent 在 Harbor 容器内运行，`allow_internet=false`，防外泄/作弊。
- **安全适配**：攻击成功率（ASR）评测须受控环境；红队 benchmark 不得外传目标细节。

## 5. 安全领域任务示例（已沉淀于 `../datasets_tasks/autolab_tasks/`）

| 任务 | domain | 优化目标 | 安全关联 |
|------|--------|---------|---------|
| `safety_router` | puzzle/ML | 最小化 total_params（安全路由分类器） | 模型压缩 + 安全路由 |
| `grpo_multisource` | model dev | GRPO 多源训练 | RL 安全对齐 |
| `adversarial_splay` | systems | 对抗性 splay 树优化 | 对抗鲁棒性 |
| `scaling_law` | model dev | 拟合 scaling law | 安全能力 scaling |
| `llm_online_serving` | systems | 在线服务吞吐 | 安全服务部署 |
| `data_select_ifeval` | data | 数据选择 (IFEval) | 指令遵循安全 |
| `agent_tool_routing` | agent | 工具路由 | 安全 agent |

## 6. 扩展：自建安全基准的建议

1. 用 AutoLab `task.toml` + `instruction.md` + `environment/`(Dockerfile) + `tests/`(verify.py) 模板。
2. 设 baseline / reference 双锚，metric + direction 写清。
3. 接入 Harbor 沙箱（`harbor_patch.sh` 启用 GPU + 长等待）。
4. 跑 `main.py` 产出 normalized score，汇入内部 Leaderboard。
