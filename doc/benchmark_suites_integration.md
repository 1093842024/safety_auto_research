# 外部基准套件集成：ScienceAgentBench + MLE-bench

> 依据 Lilian Weng《Harness Engineering for Self-Improvement》(2026-07-04) 附录 "Some useful benchmarks"，
> 将两个自动研究评测基准以「套件（suite）」形式集成进 `benchmark_tasks/`。

## 集成层次

| 层次 | 内容 | 位置 |
| --- | --- | --- |
| 目录条目 | 套件级 `BenchmarkTask`（tracked-only） | `benchmark_tasks/__init__.py`：`suite.science_agent_bench`、`suite.mle_bench` |
| 任务清单 | 全部子任务 manifest（JSON 打包入库） | `benchmark_tasks/suites/data/*.json` |
| 官方基准结果 | 论文/排行榜 baseline 表（结构化） | `suites/science_agent_bench.py`、`suites/mle_bench.py` 的 `BASELINES` |
| 数据获取 | 真实数据/评测代码的外部获取命令 | 各套件 `data_acquisition` 字段（不二次分发原始数据） |

## ScienceAgentBench（arXiv:2410.05080, ICLR 2025）

- **102 个任务**，取自 4 学科 44 篇同行评审论文：Bioinformatics 27 / Computational Chemistry 20 /
  Geographical Information Science 27 / Psychology & Cognitive Science 28
  （博客中概括为"数学、化学、生物学、地理学"，论文精确学科如上）。
- 覆盖数据处理、模型开发、数据分析、信息可视化；每任务目标输出为**自包含 Python 程序**。
- **清单**：102 条已从 HF `osunlp/ScienceAgentBench`（verified split, 2026-04-30）拉取入库，
  含 `task_inst / domain / subtask_categories / dataset_folder_tree / gold_program_name / eval_script_name` 等字段。
  可用 `python -m safety_auto_research.benchmark_tasks.suites.science_agent_bench --refresh-manifest` 刷新。
- **评测指标**：SR（成功率，3 次尝试）、VER（可执行率）、CodeBERTScore、API cost、GPT-4o 可视化 judge。
- **基准结果**（论文 Table 3，12 行已结构化入库）：
  - 最佳独立：Claude-3.5-Sonnet + self-debug **SR 32.4%**（带专家知识 34.3%）
  - o1-preview + self-debug **SR 42.2%**（>10× 成本）← 目录条目 reference
- **数据获取**：HF annotation sheet（公开）+ GitHub README 的 SharePoint zip
  （datasets/eval_programs/gold_programs/scoring_rubrics，解压密码 `scienceagentbench`，禁止再分发）。
- **污染缓解**：测试数据/标签相对公开源做过修改；verified split 修复假阴性。

## MLE-bench（arXiv:2410.07095, OpenAI, ICLR 2025）

- **75 个 Kaggle 离线 ML 工程竞赛**：low 22（=lite, 158 GB）/ medium 38 / high 15（全量 3.3 TB）。
- 测试训练模型、准备数据、跑实验、提交 CSV 给评分脚本；**Kaggle 公开排行榜为人工基准**。
- **清单**：75 条竞赛入库（split、lite 类别与数据大小、官方 Known-Issues 14 条 = 11 泄漏/缺陷 + 3 拥挤）。
- **核心指标**：`any_medal_percentage`（≥铜牌竞赛占比，≥3 seeds 取均值±SEM，按 split 分档）。
- **基准结果**（9 行已结构化入库）：
  - 论文头条（博客引用）：**o1-preview + AIDE = 16.9%**（维护中排行榜同设置 17.12±0.61）← 目录条目 baseline
  - 同期：AIDE+gpt-4o 8.63 / AIDE+claude-3.5 7.56 / OpenHands 4.89 / MLAB 1.60
  - 后续 SOTA 参考：R&D-Agent+o1 22.4（2025-05）、MLEvolve+Gemini-3-Pro 61.33（2026-02）、Famou-Agent 2.0 64.44
- **资源规模分析**：推荐 24h / 36 vCPU / 440 GB RAM / A10 24GB；性能随尝试次数与时长预算上升（论文 §3.3/3.4）。
- **污染分析**：论文验证 GPT-4o 对竞赛讨论的熟悉度与成绩无显著相关 + 代码抄袭检测；
  官方承认的 12 个泄漏/准备缺陷竞赛已逐条标进 manifest 的 `known_issue` 字段。
- **数据获取**：`pip install -e .`（git-lfs）+ `~/.kaggle/kaggle.json` + `mlebench prepare --lite|--all|-c <id>`；
  评分 `mlebench grade` / `grade-sample`。
- 与既有条目的关系：`mlevolve.mle_bench` 是本仓库的**运行 harness**（无数据）；`platform.titanic/spaceship`
  是双循环**可实跑**的同形态玩具级任务；`suite.mle_bench` 是官方基准的完整集成。

## API（control_plane）

- `GET /benchmark-suites` — 套件列表
- `GET /benchmark-suites/{suite_id}` — 详情（指标定义、数据获取、资源/污染分析）
- `GET /benchmark-suites/{suite_id}/tasks?domain=&split=&category=&limit=&offset=` — 子任务清单过滤
- `GET /benchmark-suites/{suite_id}/baselines` — 官方基准结果（含 headline）
- `/benchmark-tasks` 目录中同时可见 `suite.science_agent_bench`、`suite.mle_bench` 两条套件级条目。

## 测试

`tests/test_benchmark_suites.py`：18 项（manifest 完整性 102/75、学科与 split 分布、headline 数字
42.2/32.4/16.9/17.12、污染标记、目录集成、4+ 个 API 端点、已知问题剔除策略）。全量回归 **132 passed**。

## 本地数据落地（2026-07-30）

两个基准的**代码仓库与可再分发数据已拉取到本地** `benchmark_tasks/suites/data/`，供后续研究使用
（已加入 `.gitignore`，不纳入 git 跟踪）：

- `vendor/ScienceAgentBench/` — SAB 官方代码仓库（MIT）main 分支，含 `agent.py`、`run_eval.py`、
  `evaluation/harness`、`calculate_metrics.py` 等评估/运行代码。
- `vendor/mle-bench/` — MLE-bench 官方代码仓库 main 分支，含 `mlebench/` 包、`run_agent.py`、
  `experiments/splits/*.txt`、`agents/` 脚手架等（因本机代理拒绝直连 `github.com`，改用经 `gh` token 的
  GitHub Contents API 逐文件重建）。
- `science_agent_bench_hf/` — SAB HuggingFace 数据集 verified split（102 任务输入，CSV+parquet，可再分发）。
- `*.json` — 任务/竞赛 manifest（已结构化入库）。

**未能自动获取（需手动 / 凭证）**：
- SAB 完整评测数据（真实数据集 + eval/gold 程序）：密码保护 SharePoint zip，密码 `scienceagentbench`，
  上游标注**禁止再分发**；需人工登录下载并解压到 `vendor/ScienceAgentBench/benchmark/`。
- MLE-bench 竞赛数据（~3.3 TB 全量 / lite 较小）：需 `~/.kaggle/kaggle.json` + `mlebench prepare [lite]`；
  已用正确的 API token 下载 **lite ∩ <100MB ∩ 非已知问题 共 9 个**（约 3.2GB，落 `mle_bench_data/`），详见 DATA_ACQUISITION.md。

获取细节与命令见 `benchmark_tasks/suites/data/DATA_ACQUISITION.md`。

## MLE-bench Known-Issues 剔除策略

官方 README "Known Issues" 共列 **14 个**竞赛问题，经分拣分为两类：

- **11 个「标签/测试集泄漏 或 准备脚本缺陷」**——按 Weng 博客/官方标准应**剔除**：
  `tensorflow2-question-answering`、`tensorflow-speech-recognition-challenge`、`icecube-neutrinos-in-deep-ice`、
  `ranzcr-clip-catheter-line-classification`、`dog-breed-identification`、`champs-scalar-coupling`、
  `multi-modal-gesture-recognition`、`smartphone-decimeter-2022`、`hubmap-kidney-segmentation`、
  `random-acts-of-pizza`、外加 `invasive-species-monitoring`（但后者不在 75-split 目录内，无需处理）。
  这 11 个中 **10 个**已进入我们的 75 竞赛清单，已在 `mle_bench_competitions.json` 标记
  `excluded: true` + `exclusion_reason`。
- **3 个「排行榜拥挤/指标噪声」**——`tabular-playground-series-dec-2021`、`tabular-playground-series-may-2022`、
  `jigsaw-toxic-comment-classification-challenge`，**保留但**标记 `noisy_leaderboard: true`（非泄漏/缺陷，仅指标区分度低）。

**效果**：剔除后可用竞赛 = **65**（low 19 / medium 32 / high 14），`official_task_count` 仍记 75。
API `GET /benchmark-suites/mle_bench` 返回 `task_count=65 / excluded_task_count=10 / excluded_task_ids=[...]`；
`GET /benchmark-suites/mle_bench/tasks` **默认隐藏**被剔除项，`include_excluded=true` 可看全部 75。

> 注：竞赛数据已按要求下载——仅 **lite ∩ 数据量 <100MB ∩ 非已知问题**共 9 个（约 3.2GB，落 `mle_bench_data/`），
> 跳过了上述被剔除的 10 个竞赛，以及更大的 lite 竞赛（siim-isic 116GB、aptos 10.22GB 等）。早期误用的
> `KGAT_` Git 令牌（API 401）已替换为正确的 `username+key` 型 API token；Kaggle 要求逐竞赛在网站上手动
> 接受规则后才能下载。详情见 `benchmark_tasks/suites/data/DATA_ACQUISITION.md`。
