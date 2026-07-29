# 安全 Auto-Research 基础设施层

> 基于《AI_Research_Infrastructure_Report.md》（10 个开源 AI Auto-Research 项目深度分析），
> 把各研究环节的核心资产统一梳理并沉淀到本目录，作为安全 auto-research 的可复用基础设施。
> 现共沉淀为 **10 层**（文献检索 / idea 生成与评估校验 / 评估与基准 / 实验设计 / 数据评估与清洗 /
> 代码开发 / 实验执行 / 结果分析与经验生成 / 自迭代进化 / 数据生成对抗）。
>
> 日期：2026-07-17

## 总览：十层 × 四类资产

| 基础设施层 | Skill（可复用 playbook） | Agent（角色/源码） | 数据集任务 | 评测方式 |
|-----------|------------------------|-------------------|-----------|---------|
| **① 文献检索** | `paper_search`(6源)、`research-lit`、`arxiv`、`semantic-scholar`、`openalex`、`deepxiv`、`prior-art-search`、`exa-search`、`alphaxiv`、`research-wiki`、`wiki-enrich`、`comm-lit-review` | `arbor_search_agent`（源码） | 7 连接器矩阵 + 时间窗口 + 去重规则 | 召回/去重/相关性/可溯源 四维 |
| **② idea 生成与评估校验** | 生成：`idea_spark`、`idea-creator`、`idea-discovery`、`novelty-check`、`scoop_check`；校验：`paper-claim-audit`、`claims-drafting`、`result-to-claim`、`citation-audit`、`experiment-audit`、`integrity-forensics`、`auto-review-loop`、`research-review` | Cross-Model Reviewer、Claim Auditor（6 状态机）、Seed Analyzer（Claudini） | 15 ideation pattern + 31 子模式 + 100 ICLR 种子 | idea 质量三轴、PROVE/JUDGE、5 级碰撞、多维审计 |
| **④ 实验设计** | `experiment-bridge`、`research-refine`、`research-pipeline` | Arbor Coordinator(假设树)、`stat_research_agent`(7 统计角色)、MLEvolve Planner/Evolution | 设计模板(22阶段/统计/假设树) | 设计质量四维(可证伪/消融/基线/可复现) |
| **⑤ 数据评估与清洗** | `data_eval_clean`(★runbook)、`ray-data`(分布式清洗/ETL) | 数据评估筛选(引用 `data_select_ifeval`)、多源配比(引用 `grpo_multisource`)、泄露检测(引用层⑧ `data_leakage_agent`) | 数据质量清单 / 标签 schema / 新老配比配置（引用层③ AutoLab 两任务） | 数据质量(覆盖/去重/标签噪声/泄露)、标签一致性、配比有效性(遗忘门控) |
| **⑥ 代码开发** | `code_development`(runbook) | MLEvolve Code(draft/improve/evolution/code_review/debug+coder)、`code_searcher` | 代码脚手架/质量基线 | 代码质量(可运行/测试/规范/无泄露) |
| **⑦ 实验执行** | `execution_runbook`(runbook) | MLEvolve Engine(MCGS)、ResearchClaw 22阶段、Arbor Executor、NanoResearch 执行 | 计算预算(GPU规格/FLOPs) | 可复现/隔离/canary 防污染 |
| **⑧ 结果分析与经验生成** | `result_analysis`(runbook) | MLEvolve Analysis(result_parse/fusion/aggregation/data_leakage+Memory)、Arbor Backpropagate | 记忆 schema/分析模板 | 分析质量 + 经验复用(记忆命中/迁移增益) |
| **③ 评估与基准**（capstone gate，位于 ⑧ 之后） | `benchmark_orchestration`（runbook） | AutoLab Harness(Harbor)、Claudini Bench/Leaderboard、`arc_bench_agent`、`mle_evolve_eval`(11 角色) | AutoLab **全量 36** 任务(4大类)+ ARC-Bench(55主题)+ 100 ICLR 种子 | 锚定评分、log-scaled、Leaderboard、canary 防作弊 |
| **⑨ 自迭代进化**（控制平面，闭合 ①-⑩） | `iteration_controller`(★决策 playbook)、`auto-review-loop`、`a-evolve` | `arbor_convergence`(收敛/升级/剪枝)、`metaclaw_bridge`(经验转技能+PRM门控)、`mle_evolve_conditions`(停滞检测) | `CycleRecord`+`DecisionRecord` schema + 诊断→环节路由表(R1-R10) | 退出准则(AND)、升级阈值、决策质量、自进化有效性(重试率-24.8%/精炼-40%) |
| **⑩ 数据生成对抗**（红队回环，持续硬化） | `adv_redteam`(★漏洞测试/红队)、`adv_analysis`(失败归因)、`adv_generation`(对抗生成)、`adv_antiforgetting`(防遗忘护栏) | `vulnerability_probe_agent`(红队探针)、`failure_analyst_agent`、`adversarial_generator_agent`、`antiforgetting_guardian_agent`(回放守护) | `attack_taxonomy`(攻击族)、`replay_buffer_schema`(经验回放)、`redteam_targets_template`(探针模板) | 鲁棒性(ASR/Adv-Acc/边界密度/覆盖)、防遗忘(保留率/遗忘率/回放增益)、生成质量(多样性/有效性/迁移性) |

## 目录结构

```
infrastructure/
├── README.md                         # 本文件（十层统一总览）
├── 01_literature_search/             # 层① 文献检索
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # 12 个检索 skill（含 paper_search 脚本）
│   ├── agents/                       # arbor_search_agent + Agent 规范
│   ├── datasets_tasks/               # 数据源连接器清单
│   └── eval_methods/                 # 检索质量四维评估
├── 02_idea_generation_eval/          # 层② idea 生成与评估校验
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # 13 个生成/校验 skill（含 idea_spark 完整包）
│   ├── agents/                       # 跨模型审稿/声明审计/Seed 规范
│   ├── datasets_tasks/               # 15 pattern + 31 子模式 + 100 ICLR 种子
│   └── eval_methods/                 # idea_quality + claim_quality_framework
├── 04_experiment_design/             # 层④ 实验设计
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # experiment-bridge / research-refine / research-pipeline
│   ├── agents/                       # arbor_coordinator + stat_research_agent + mle_evolve_design
│   ├── datasets_tasks/               # 设计模板（22阶段/统计/假设树）
│   └── eval_methods/                 # 实验设计质量四维
├── 05_data_evaluation_cleaning/      # 层⑤ 数据评估与清洗（实验执行前关键环节）
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # data_eval_clean(★) + ray-data(分布式清洗/ETL)
│   ├── agents/                       # 数据centric 重训控制器（引用 data_select_ifeval/grpo_multisource/data_leakage）
│   ├── datasets_tasks/               # 数据质量清单 / 标签 schema / 新老配比配置
│   └── eval_methods/                 # 数据质量 / 标签一致性 / 配比有效性(遗忘门控)
├── 06_code_development/              # 层⑥ 代码开发
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # code_development runbook
│   ├── agents/                       # mle_evolve_code + code_searcher
│   ├── datasets_tasks/               # 代码脚手架/质量基线
│   └── eval_methods/                 # 代码质量门控
├── 07_experiment_execution/          # 层⑦ 实验执行
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # execution_runbook
│   ├── agents/                       # mle_evolve_engine + researchclaw_pipeline + arbor_executor + nanoresearch_execution
│   ├── datasets_tasks/               # 计算预算(GPU/FLOPs)
│   └── eval_methods/                 # 可复现/隔离/canary
├── 08_result_analysis_experience/    # 层⑧ 结果分析与经验生成
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # result_analysis runbook
│   ├── agents/                       # mle_evolve_analysis(memory) + arbor_backpropagate
│   ├── datasets_tasks/               # 记忆 schema / 分析模板
│   └── eval_methods/                 # 分析质量 + 经验复用
├── 03_evaluation_benchmark/          # 层③ 评估与基准（capstone gate，位于⑧之后）
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # benchmark_orchestration runbook
│   ├── agents/                       # autolab_harness + claudini_eval + arc_bench_agent + mle_evolve_eval
│   ├── datasets_tasks/               # AutoLab 36 任务 + ARC-Bench(55主题) + 100 ICLR 种子
│   └── eval_methods/                 # 锚定/log-scaled 评分 + Leaderboard
├── 09_self_iterative_evolution/      # 层⑨ 自迭代进化（控制平面，闭合 ①-⑨）
│   ├── README.md                     # 层内统一梳理
│   ├── skills/                       # iteration_controller(★) + auto-review-loop + a-evolve
│   ├── agents/                       # arbor_convergence + metaclaw_bridge + mle_evolve_conditions
│   ├── datasets_tasks/               # 链路状态+决策日志 schema（含诊断→环节路由表 R1-R10）
│   └── eval_methods/                 # 退出/停止准则 + 升级阈值 + 决策质量 + 自进化有效性
└── 10_adversarial_data_generation/   # 层⑩ 数据生成对抗（红队回环/持续硬化）
    ├── README.md                     # 层内统一梳理
    ├── skills/                       # adv_redteam★ + adv_analysis + adv_generation + adv_antiforgetting
    ├── agents/                       # vulnerability_probe/failure_analyst/adversarial_generator/antiforgetting_guardian
    ├── datasets_tasks/               # attack_taxonomy + replay_buffer_schema + redteam_targets_template
    └── eval_methods/                 # robustness_metrics（ASR/保留率/遗忘率/多样性）
```

## 跨层数据流（自迭代闭环）

```
文献检索 ① ──Citation Gate──> Idea 生成与评估校验 ② ──Cross-Model Review──┐
   ▲                              │  (PROVE/JUDGE 6状态)              │
   │  └──先验碰撞(scoop-check)────┘                                  │
   │                                                                  ▼
   │  实验设计 ④ ──设计质量四维──> 数据评估清洗 ⑤ ──数据质量门──> 代码开发 ⑥ ──代码质量门──> 实验执行 ⑦ ──可复现/隔离──> 结果分析与经验生成 ⑧
   │     ▲                     ▲                                     │                                          │
   │     │                     │                                     │                                          ▼
   │     │        badcase 回流 ┘                                     │                                评估与基准 ③（capstone gate）
   │     │        (数据驱动重训范式：跳过 ①②④，直接 ⑤→⑥→⑦)          │                                         │
   │     │                                                           │                                         ▼
   │     │                                           ┌──────────── 自迭代进化 ⑨（控制平面）───────────┐    │
   │     │                                           │  综合复盘全环门控 + 收敛/停滞信号              │    │
   │     └──────REVISIT 回退到 ①-⑩ 任一环（携带经验）──┤  诊断→路由表 R1-R10 定位"最早失效环节"         │    │
   └─────────────────────────────────────────────────┤                                             │    │
                                                       │  达标/预算/收敛 ─▶ EXIT（归档 + 经验入库）    │    │
                                                       └──────────────────────────────────────────────┘    │
                                                                                 │
   知识沉淀(Research Wiki / Idea Card / 漏洞模式库 / 全局记忆) <─────────────────────────────────────────┘
        经验回注：MetaClaw lesson→skill / Arbor Backpropagate / MLEvolve Memory（PRM 门控后按阶段注入下一轮）
```

### ⑩ 对抗驱动回路（叠加在 ③ 之后，红队回环）

```
[已优化模型 / ⑦ 产出] ──⑩ 数据生成对抗──▶ 漏洞测试 → 失败分析 → 对抗生成 → 防遗忘护栏
        │                                            │
        │                           对抗数据 + 回放缓冲 + 保留率约束
        │                                            ▼
        └──── ⑤ 数据评估清洗 → ⑥ 代码 → ⑦ 重训(经验回放/EWC) → ③ 鲁棒性评估 ─┐
                                                                              │
                                              层⑨ R10 调度：继续硬化(回⑩) 或 EXIT ─┘
```

**⑨ 的决策**：一轮 ①→⑧→③（及 ⑩ 红队回环）跑完后，层⑨读取全部门控与分数，判定
（a）**EXIT**——达标 / 预算耗尽 / 收敛无新范式；或（b）**REVISIT**——沿 ⑧→① 反向
定位第一个失效门控，回退到 ①-⑩ 中该环节重启一轮，并把上一轮经验（lessons/skills）
经 PRM 门控注入。这把八个执行环节缝合成"越用越强"的自进化闭环。

## 两种研究范式

- **范式 A（标准研究链路）**：①文献 → ②idea → ④实验设计 → ⑤数据评估清洗 → ⑥代码 → ⑦执行 → ③评估 → ⑧分析 → ⑨自迭代。
  层⑤ 在此作为实验执行前的数据准备与质量把关环节。
- **范式 B（数据驱动重训）**：已优化模型 + badcase 回流 → **直接进入层⑤**（数据评估/清洗/标注/配比）→ ⑥代码（训练代码）→ ⑦执行（重训）→ ③评估 → ⑧分析 → ⑨自迭代。
  层⑤ 在此是范式入口，跳过 ①②④（除非根因在架构/策略，再经层⑨ 路由回 ④/⑥）。
- **范式 C（对抗驱动持续提升，层⑩ 主场景）**：已优化模型 +（可选）线上回流 badcase → **直接进入层⑩**（红队攻击 + 对抗生成 + 防遗忘护栏）→ ⑤数据评估清洗 → ⑥代码 → ⑦重训（经验回放/EWC）→ ③鲁棒性评估 → ⑨自迭代（R10 调度：继续硬化或 EXIT）。
  层⑩ 在此是"自动提高鲁棒性与对抗性"的默认入口，跳过 ①②④（除非根因在架构/策略）。

## 安全领域适配要点（贯穿十层）

1. **对抗性**：检索覆盖攻击/防御/评测三套术语；攻击 idea 须额外评估可复现性与合规风险。
2. **红队受控**：评估与基准层红队 benchmark 须在隔离沙箱（Harbor/Docker）运行，目标细节不外传。
3. **跨模型强制**：质量门（Type-B）对安全研究强制 Executor ≠ Reviewer 模型家族。
4. **知识沉淀**：`refuted` 攻击声明沉淀为"已知无效方向"；合规来源打 `compliance` 标签。
5. **可溯源/合规**：所有检索记录查询串+时间戳，所有评测含 `harbor-canary` 防训练污染。
6. **数据合规（层⑤ 专项）**：badcase 清洗前先 PII 脱敏与合规过滤；安全分级标签强制人工复核；
   配比以遗忘门控约束保留率，防止"修 badcase 而遗忘旧安全能力"；训练/验证/测试 group+时间隔离。
7. **对抗受控（层⑩ 专项）**：红队攻击须隔离沙箱 + 跨模型家族强制（红队 ≠ 被攻击模型，复用层②）；
   危险 payload 仅存指纹/引用；显著提升越狱/绕过成功率的迭代强制 HITL（层⑨ 安全断点）；
   防遗忘护栏（经验回放 + 保留率门控 + EWC）确保"修漏洞而不遗忘旧安全能力"。

## 与原项目关系

- 原 `safety_auto_research/evaluation/paper_idea_eval/` 的 `idea_quality` 与 100 种子已**集中沉淀**到层②/层③，
  作为统一基础设施的评测资产（原目录保留，避免重复可继续引用）。
- 各层 skill/agent 均为从上游项目抽取的**核心有价值部分**（SKILL.md playbook + 关键源码 + 数据集/评测定义），去掉了冗余与二进制权重。
- 本轮新增 ④⑤⑥⑦⑧ 五层的上游来源：
  - **实验设计 ④** ← Arbor（Coordinator/假设树）、AutoResearchClaw（`stat_research_agent` 7 角色）、MLEvolve（Planner/Evolution）
  - **数据评估与清洗 ⑤** ← AutoLab（`data_select_ifeval` 数据筛选评估、`grpo_multisource` 多源配比+遗忘门控）、MLEvolve（`data_leakage_agent` 泄露检测）、NanoResearch（`ray-data` 分布式清洗/ETL）。支撑"已优化模型 + badcase 回流 → 数据迭代 → 重训"的数据驱动范式，是实验执行前的关键环节。
  - **代码开发 ⑥** ← MLEvolve（`draft/improve/evolution/code_review/debug` + `coder/`）、AutoResearchClaw（`code_searcher`）
  - **实验执行 ⑦** ← MLEvolve（`engine/` MCGS）、AutoResearchClaw（22 阶段流水线）、Arbor（Executor）、NanoResearch（GPU 执行+自愈）
  - **结果分析与经验生成 ⑧** ← MLEvolve（`result_parse/fusion/aggregation/data_leakage` + 全局记忆）、Arbor（Backpropagate/收敛）
- MLEvolve 的代码/执行/分析角色按其职能**分散沉淀**到 ④⑤⑥⑦⑧（避免与层③ `mle_evolve_eval` 全量副本重复），各层 README 注明跨层引用。
- 编号说明：层③「评估与基准」按原指令为第 3 个核心层，实际在流水线中是 **capstone gate**，位于 ⑧ 之后（见上图与总表）。
- **自迭代进化 ⑨** 的上游来源：Arbor（`convergence.py` 收敛检测 + `idea_tree`/`checkpoint`）、
  AutoResearchClaw（`metaclaw_bridge` 经验转技能 + PRM 门控 + `a-evolve` skill）、
  MLEvolve（`conditions.py` 停滞检测 + `node_selection`）。它是 ①-⑧ 执行环之外（⑨ 控制平面 + ⑩ 红队回环）的**控制平面**，
  只做决策与经验回注，不产出研究成果。为避免与 ⑧ Backpropagate 重复，⑨ 聚焦"跨环节回退路由 +
  退出判定 + 经验转技能"，⑧ 聚焦"单环节内的结果解析与记忆写入"。
- **数据生成对抗 ⑩** 的上游来源：安全域原始方案 `doc/Adversarial_safety_multi_Agent.md`
  （MAGIC/CHASE 红蓝对抗 RL、Self-Play 反思经验回放、奖励工程、经验回放池、Agent 接口）、
  `doc/AI_Research_for_safety.md`（跨模型对抗审、攻防协同进化、ARA 风险知识对象）。
  它是 ③ 之后的**红队回环**：自动暴露模型漏洞并生成对抗数据，经 ⑤→⑥→⑦→③ 驱动持续提升，
  并通过回放缓冲 / EWC 防遗忘。为避免与层⑤ 配比遗忘门控重复，⑩ 聚焦"样本级对抗生成 + 回放护栏"，
  层⑤ 聚焦"数据集级清洗 / 配比"。
