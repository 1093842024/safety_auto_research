# 本地基准数据 / 代码获取说明（benchmark_tasks/suites/data）

本目录存放从上游仓库拉取到**本地**的两个评测基准（ScienceAgentBench、MLE-bench）的
代码与数据，供后续研究使用。所有内容均 **不纳入 git 跟踪**（已加入 `.gitignore`），
属于本地研究资产。

## 目录结构

```
benchmark_tasks/suites/data/
├── vendor/
│   ├── ScienceAgentBench/        # SAB 官方代码仓库（MIT，已克隆 main 分支）
│   └── mle-bench/                # MLE-bench 官方代码仓库（已克隆 main 分支）
├── science_agent_bench_hf/       # SAB HuggingFace 数据集（verified split，可再分发）
│   ├── ScienceAgentBench.csv           # 102 个任务的完整输入（task_inst 等 12 列）
│   ├── verified-00000-of-00001.parquet # 同上的 parquet 格式
│   └── README.md
├── mle_bench_competitions.json   # 75 个竞赛清单（low/medium/high + lite 标注，JSON 格式）
├── science_agent_bench_tasks.json# 102 个任务的清单（从 HF verified split 拉取）
├── mle_bench_data/               # 已下载+准备的竞赛数据（gitignore，见下方 MLE-bench 节）
│   └── <competition_id>/{raw,prepared/public,prepared/private}
├── acquire_mle_bench_lite.py     # 批量下载+准备脚本（lite ∩ <100MB ∩ 非已知问题）
├── mle_bench_acquire_report.json # 下载报告（9/9 prepared）
├── mle_bench_rule_acceptance_checklist.md  # 竞赛规则接受与下载记录
├── sab_eval_crosscheck.json      # SAB 评测文件交叉核对明细
├── sab_eval_syntax_check.json    # SAB eval 脚本语法/接口校验明细
└── DATA_ACQUISITION.md           # 本文件
```

> 注：上游 Git 仓库的拉取方式——`github.com` 直连被本机代理以 502 拒绝，
> 因此 SAB 用 `codeload.github.com` tarball、`mle-bench` 用经 `gh` token 的
> GitHub Contents API 逐文件重建（见下方“获取方式”）。大文件/数据走专用通道（见下）。

---

## 1. ScienceAgentBench（已本地化）

### 已拉取
- **代码仓库**（`vendor/ScienceAgentBench/`，MIT）：含 `agent.py`、`run_eval.py`、
  `evaluation/harness`、`calculate_metrics.py`、`compute_scores.py` 等评估与运行代码。
- **任务输入数据集**（`science_agent_bench_hf/`）：来自 HuggingFace `osunlp/ScienceAgentBench`
  的 **verified split**（2026-04-30 发布的修正版，缓解评估假阴性）。102 个任务，
  学科分布 Bioinformatics 27 / GIS 27 / Psychology&CogSci 28 / Computational Chemistry 20。
  字段含 `task_inst`、`domain_knowledge`、`dataset_folder_tree`、`dataset_preview`、
  `gold_program_name`、`eval_script_name` 等——即“运行 agent 所需的全部输入”，可再分发。

### 完整评测数据（已下载 + 解压 + 验证完成，2026-07-31）

- **现状**：用户已将评测数据落到 `vendor/ScienceAgentBench/benchmark/benchmark_verified.zip`
  （1.7GB）。该 zip **整体加密**（955 个条目中 845 个真实数据文件带传统 ZipCrypto 加密标志，
  仅 110 个目录占位符为明文），与官方 GitHub/HF 的明文发布形态不同——属于该下载源/打包时
  额外加密。用户用密码解压成功后，内部顶层目录恰为 `benchmark/`，出现嵌套
  `benchmark/benchmark/`，已**拍平**为官方布局：
  - `benchmark/datasets/` 414 文件 / 3.8G（76 个任务数据子目录）
  - `benchmark/gold_programs/` 103 文件
  - `benchmark/eval_programs/` 224 文件（110 顶层 eval 脚本 + `gold_results/` 参考输出）
  - `benchmark/scoring_rubrics/` 102 文件（gated LLM-judge 资源，确定性评测不依赖）
  - 上游许可：*"Please DO NOT redistribute the unzipped data files online."* 仅限本地使用。
- **验证结果**（详见同目录 `SAB_EVAL_VERIFICATION.md` 及 `sab_eval_crosscheck.json` / `sab_eval_syntax_check.json`）：
  1. **结构一致性**：102 个 verified 任务的 `gold_program_name` / `eval_script_name` 在对应目录
     **0 缺失**；gold 103 / eval 224 / rubric 102 全部就位。
  2. **语法与接口**：110 个顶层 eval 脚本 `py_compile` 全部通过，且每个都定义 `eval()` 并返回
     `(int, str)` 二元组（符合 `run_eval.py` / `compute_scores.py` 调用契约）。
  3. **端到端冒烟测试**：选依赖最轻的任务 #92（`h_importances`，仅 numpy+json）——gold 程序运行成功
     产出 `pred_results/jnmf_h_importances.json`，`eval_h_importances.eval()` 对比参考输出返回
     `(1, 'N/A')` 即 **success=1**。**评测流程正确**。
- **限制（不影响流程正确性）**：仅 1 个轻量任务端到端跑通；其余 101 个 gold/eval 多依赖重科学栈
  （rdkit / torch / scipy / scikit-image / geopandas 等），需官方 `conda env sci-agent-eval` 才能
  全量运行。`scoring_rubrics/` 仅用于 LLM-judge 模式。
- 完成上述后，即可按官方 README 跑 `bash run_evaluation.sh`（或 `run_eval.py` / `compute_scores.py`）等评测流程。

---

## 2. MLE-bench（已本地化代码；竞赛数据需 Kaggle）

### 已拉取
- **代码仓库**（`vendor/mle-bench/`）：含 `mlebench/` 包、`run_agent.py`、`pyproject.toml`、
  `experiments/splits/{low,medium,high}.txt`（即 75 竞赛的三个复杂度 split）、
  `agents/`（aide / dummy 等脚手架）等。
- **竞赛清单**（`mle_bench_competitions.json`）：75 个竞赛，含 complexity_split、是否 lite、
  类别、数据集大小，以及官方 Known-Issues 标注。已知问题共 14 个：其中 **11 个为标签/测试集泄漏
  或准备脚本缺陷**（按 Lilian Weng 博客/官方 README 的剔除标准），另有 **3 个仅“排行榜拥挤/指标噪声”**
  （`tabular-playground-series-dec-2021`、`tabular-playground-series-may-2022`、
  `jigsaw-toxic-comment-classification-challenge`，不属于泄漏/缺陷）。
  - **剔除策略（已落地）**：11 个泄漏/缺陷竞赛中，10 个在我们 75 竞赛目录内，已在清单中标记
    `excluded: true` + `exclusion_reason`；`invasive-species-monitoring` 不在 75-split 目录中，
    故无需要剔除项。3 个拥挤排行榜竞赛**保留**但标记 `noisy_leaderboard: true`。
    剔除后可用竞赛 = **65**（low 19 / medium 32 / high 14）。API `/benchmark-suites/mle_bench/tasks`
    默认隐藏被剔除项，`include_excluded=true` 可看全部 75。

### 竞赛数据已下载（lite ∩ <100MB ∩ 非已知问题，共 9 个，2026-07-30 完成）

- **现状**：用户提供正确的 `~/.kaggle/kaggle.json`（`username+key` 型 API token，区别于早期
  误用的 `KGAT_` Kaggle Git 令牌——后者 API 鉴权 401 不可用）。Kaggle API 鉴权通过，9 个目标竞赛
  全部已在 kaggle.com 用该账号**手动接受竞赛规则**后下载并 `prepare` 成功。
  > Kaggle 不提供「接受规则」的 API，必须人工在网站上逐一点「I Accept」，否则 API 返回
  > "You must accept the competition rules before downloading"。
- **落盘**：`benchmark_tasks/suites/data/mle_bench_data/<competition_id>/`，每竞赛含
  `raw/`（原始 zip）、`prepared/public/`（训练/测试/提交样例）、`prepared/private/`（隐藏测试标签）。
  总计约 **3.2GB**。
- **已下载清单（实测 prepared 体积）**：
  - aerial-cactus-identification — 87M
  - denoising-dirty-documents — 217M
  - detecting-insults-in-social-commentary — 3.1M
  - jigsaw-toxic-comment-classification-challenge — 133M（*超原 100MB 估算）
  - leaf-classification — 29M
  - nomad2018-predict-transparent-conductors — 15M
  - spooky-author-identification — 3.2M
  - text-normalization-challenge-english-language — 344M（*超原 100MB 估算）
  - text-normalization-challenge-russian-language — 517M（*超原 100MB 估算）
  - *注：jigsaw / text-en / text-ru 实测超过原 manifest `dataset_size_gb` 的 100MB 阈值（原估算只计了
    部分/原始 zip）。manifest 中这 3 项已修正为 0.133 / 0.344 / 0.517。如需严格 <100MB，删除对应
    `mle_bench_data/<id>` 目录即可。
- **执行方式**：`benchmark_tasks/suites/data/acquire_mle_bench_lite.py`（逐竞赛容错、校验失败回退
  `skip_verification`、规则未接受记 URL）；报告 `mle_bench_acquire_report.json`（9/9 prepared）。
  重跑命令与未下载的更大 lite 竞赛清单见 `mle_bench_rule_acceptance_checklist.md`。
- **未下载的更大 lite 竞赛**（按 <100MB 约束跳过，如需可放宽阈值或单独指定）：
  aptos2019(10.22G)、dogs-vs-cats-redux(0.85G)、histopathologic(7.76G)、mlsp-2013-birds(0.585G)、
  new-york-city-taxi(5.7G)、plant-pathology-2020(0.8G)、siim-isic(116.16G)、
  tabular-dec-2021(0.7G)、tabular-may-2022(0.57G)、whale-challenge(0.293G)；ranzcr-clip 既超 100MB 又被剔除。
- **重要坑**：`mle_bench_competitions.json` 扩展名是 `.json`，套件用 `json.load` 读取，**必须保持
  JSON 格式**；切勿用 `yaml.safe_dump` 改写（曾因此导致 7 个测试挂掉，已修复为 json.dump）。
- 因数据巨大，全量 3.3 TB 下载**禁止**，仅准备小体积竞赛；`mle_bench_data/` 已加入 `.gitignore`。
- 评测/打分：`python run_agent.py ...` → `mlebench grade` / `grade-sample`。

---

## 资源规模与污染分析（已结构化进套件模块）
- **资源规模**：论文中 o1-preview+AIDE 最佳设置使用 24h 时限、36 vCPU、单张 A10 GPU。
- **污染分析 / 剔除策略**：MLE-bench 官方 Known-Issues 共 14 个。其中 11 个存在标签/测试集泄漏
  或准备脚本缺陷（如 `random-acts-of-pizza` 标签泄漏、`dog-breed-identification` 测试集来自公开
  标注语料、`tensorflow2-question-answering` grade.py 校验失败等），已在 `mle_bench_competitions.json`
  标记 `excluded: true` 并逐条写明 `exclusion_reason`，复现时**默认剔除**。另有 3 个仅“排行榜拥挤”
  的竞赛标记 `noisy_leaderboard: true`（保留）。官方 fix 计划在 MLE-bench v2。

## 再分发与合规
- SAB 任务输入（HF verified split）与两仓库代码可自由用于研究；**SAB 完整评测 zip 与
  MLE-bench 竞赛数据受各自许可约束，仅限本地使用、不得再分发**。
- 本目录整体已加入 `.gitignore`，不会误提交到项目仓库。
