# 基础设施层 ③：评估与基准（Evaluation & Benchmark）

> 统一梳理「评估与基准」层的 **skill / agent / 数据集任务 / 评测方式**。
> 来源：《AI_Research_Infrastructure_Report.md》Layer 5（评估与基准）、AutoLab（Harbor 沙箱）、
> Claudini（Bench/Leaderboard）、MLE-bench、ARC-Bench、IdeaSpark Evaluation。

## 1. 定位

没有标准化基准就无法衡量 AI 自主研究 / 安全算法的真实水平。本层提供容器化评测框架、
锚定评分体系与 Leaderboard，把"想法/算法"变成可比较的数字。

## 2. Skill（已梳理至 `skills/`）

- `benchmark_orchestration.md` — 评估编排 runbook：AutoLab 式标准化任务评测 + Claudini 式对抗算法
  Pareto 评测 + Idea 质量评测 三条路径，含质量红线（隔离沙箱 / canary / 红队受控）。

## 3. Agent / 框架（已拷贝至 `agents/`）

- `autolab_harness/` — AutoLab 评测框架：`main.py`（入口）、`harbor_patch.sh`（沙箱补丁）、
  `pyproject.toml`、`README.md`。标准化任务结构 + Harbor 隔离 + log-scaled 锚定评分 + 轨迹回放。
- `claudini_eval/` — Claudini 对抗评测：`run_bench.py`（Bench CLI）、`leaderboard.py`（排名）、
  `base.py`（TokenOptimizer 抽象）、`registry.py`（自动注册）、`configs/`（15 个 YAML 预设）。
- `arc_bench_agent/` — ARC-Bench 跨域评测 agent（`AutoResearchClaw/researchclaw/agents/benchmark_agent`）：
  `orchestrator.py`（编排）、`acquirer.py`（论文获取）、`surveyor.py`（调研）、`selector.py`（选题）、
  `validator.py`（校验），用于开放式自主科研评测。
- `mle_evolve_eval/` — MLEvolve 评测 agent（MLE-bench #1 系统，65.3% 奖牌率 / 12h）：`run.py` 入口 + 11 个角色
  （`evolution_agent` / `improve_agent` / `fusion_agent` / `debug_agent` / `code_review_agent` / `draft_agent` /
  `data_leakage_agent` / `result_parse_agent` / `aggregation_agent` + `planner` / `coder` / `memory` 子模块）+ `engine`（MCGS）。
- `agents/README.md` — 角色总览 + 运行方式 + 与其他层接口 + 安全适配要点。

## 4. 数据集任务（已拷贝至 `datasets_tasks/`）

- `autolab_tasks/` — **AutoLab 全量 36 个开放挑战**（已排除 >3MB 二进制权重/数据集，保留 task.toml/instruction.md/tests/solution/environment）。标准化任务结构 + Harbor 沙箱 + Log-scaled 评分。4 大类：

  - **Model Development (7)**：grpo_multisource、scaling_law、llm_online_serving、data_select_ifeval、flux2_klein_lora、moving_mnist_world_model、multilingual_ocr
  - **System Optimization (15)**：adaptive_compression、agent_tool_routing、bm25_search_go、concurrent_kv_wal、discover_sorting、flash_attention、gaussian_blur、hash_join、radix_sort、regex_engine、resnet_bit_flip、safety_router、sstable_compaction_rs、stack_machine_golf、toy_isa_opt
  - **Puzzle and Challenge (10)**：adversarial_splay、aes128_ctr、fredkin_sort_network、levenshtein_distance、sha256_throughput、smallest_game_player、vliw_scheduler、z_order_range_scan、bvh_raytracer、fft_rust
  - **CUDA (4)**：huffman_canonical_decode_cuda、icp_correspondence_step_cuda、msm_pippenger_bls12_381_cuda、ntt_butterfly_cuda

  其中安全相关代表：`safety_router`（模型压缩+安全路由）、`grpo_multisource`（RL 安全对齐）、`adversarial_splay`（对抗鲁棒性）、`data_select_ifeval`（指令遵循安全）、`agent_tool_routing`（安全 agent）。

- `arc_bench/` — **ARC-Bench 数据集**（AutoResearchClaw，55 主题开放式自主科研 benchmark）：`config/`（ML/physics/quantum/biology/statistics 五领域注册表 + topics.yaml）、`baseline/`（FrameworkAdapter 适配）、`scripts/`（含 prompts/ 严格审计 prompt）。数据由 HuggingFace `AIMING-Lab-UNC/ARC-Bench` 加载。

- `iclr2026_oral_problem_seeds_formal_100.jsonl` — 100 个 ICLR-2026-Oral 问题种子（IdeaSpark 评测）。

- **外部基准（资产已在 agents/ 沉淀，数据由 HuggingFace 引用）**：
  - **MLE-bench**（75 Kaggle 式 ML 竞赛，Low/Medium/High 三级）—— 评测 agent 见 `agents/mle_evolve_eval/`（MLEvolve #1，65.3% 奖牌率/12h）；数据集 `openai/mle-bench`。
  - **Claudini Benchmark**（jailbreak + prompt injection 对抗）—— 见 `agents/claudini_eval/`。
  - 评分与排名细节见 `eval_methods/scoring.md` 第 3 节。

## 5. 评测方式（已梳理至 `eval_methods/`）

- `scoring.md` — ① 锚定评分（baseline→reference 线性/对数插值）② Log-scaled 评分（AutoLab，含 safety_router 实例）
  ③ 排名与 Leaderboard（AutoLab/Claudini/MLE-bench/ARC-Bench/IdeaSpark）④ 防作弊与合规（harbor-canary / 隔离 / 红队受控）
  ⑤ 安全任务示例 ⑥ 自建安全基准建议。

## 6. 安全领域适配

- 红队 benchmark 须受控环境运行，目标细节不外传。
- ASR 评测须同时覆盖攻击成功率与防御侧安全性降级。
- 自建安全基准优先复用 AutoLab `task.toml` 模板 + Harbor 沙箱，保证可复现与防作弊。
