# 基础设施层 ②：Idea 生成与评估校验（Idea Generation & Evaluation）

> 统一梳理「idea 生成与评估校验」层的 **skill / agent / 数据集任务 / 评测方式**。
> 来源：《AI_Research_Infrastructure_Report.md》Layer 2（Idea生成）、Layer 3（质量保障：跨模型对抗审 / PROVE-JUDGE）、
> ResearchStudio-Idea（IdeaSpark + Scoop-Check）、ARIS（idea-creator/claim 审计）、Claudini（Seed 分析）。

## 1. 定位

从研究问题产出**可辩护、可溯源**的研究 idea，并通过跨模型对抗审 + 声明状态机 + 多维审计
保证质量——这是"不能让同一模型既当运动员又当裁判"的核心层。

## 2. Skill 清单（已拷贝至 `skills/`）

### 生成类
| Skill | 来源 | 功能 |
|-------|------|------|
| `idea_spark` | ResearchStudio | 5 阶段端到端 idea 生成（1947 篇语料接地 + 15 模式 + 质量校验链），含完整 scripts/references | 1.2M |
| `idea-creator` | ARIS | 头脑风暴式 idea 创造 | 28K |
| `idea-discovery` | ARIS | 研究空白发现 | 24K |
| `novelty-check` | ARIS | 新颖性检查 | 8K |
| `scoop_check` | ResearchStudio | 先验艺术碰撞、按轴 overlap verdict | 28K |

### 评估校验类（质量保障）
| Skill | 来源 | 功能 |
|-------|------|------|
| `paper-claim-audit` | ARIS | 声明审计 | 16K |
| `claims-drafting` | ARIS | 声明起草 | 12K |
| `result-to-claim` | ARIS | 结果→声明映射 | 20K |
| `citation-audit` | ARIS | 引用审计 | 32K |
| `experiment-audit` | ARIS | 实验可复现审计 | 12K |
| `integrity-forensics` | ARIS | 抄袭/捏造/图像篡改取证 | 16K |
| `auto-review-loop` | ARIS | 跨模型自动审稿循环 | 28K |
| `research-review` | ARIS | 同行评审 | 12K |

## 3. Agent（已梳理至 `agents/README.md`）

- **Idea Generator**（idea_spark/idea-creator）— 产出 idea card，每声明 track 到检索。
- **Novelty Checker**（scoop_check/novelty-check）— 先验碰撞。
- **Cross-Model Reviewer** — 核心机制：Executor ≠ Reviewer 模型家族；4 轮审稿 5.0→7.5；Type-A/B Gate。
- **Claim Auditor** — 6 状态机（PROVE/JUDGE）+ 依赖链。
- **Integrity Forensics** — 独立取证线程。
- **Seed Analyzer（Claudini）** — 研究目标 → baseline 分析 → 下轮改进，Pareto 前沿进化。

## 4. 数据集任务（已拷贝至 `datasets_tasks/`）

- `ideation_patterns/` — **15 种 ideation pattern** 卡片（overview + companion-combos + 15 个模式文件），
  操作性创新模式词汇表（reframe_as_solvable_object、assumption_audit_and_pivot、controlled_diagnostic_design …）。
- `ideation_sub_patterns/` — **31 种子模式** 词汇表（overview + C00–C05 代表卡片）。
- `iclr2026_oral_problem_seeds_formal_100.jsonl` — **100 个 ICLR-2026-Oral 问题种子**（IdeaSpark 评测用）。

## 5. 评测方式（已梳理至 `eval_methods/`）

- `idea_quality/`（已迁移）— 质量三轴 A/B/C（0–100）+ pairwise 盲评 + 反偏规则 + 3 个 sample_ideas + evals.json。
- `claim_quality_framework.md` — 统一框架：① idea 质量三轴 ② 声明状态机（PROVE/JUDGE）③ 先验碰撞 5 级
  ④ 多维度审计清单 ⑤ IdeaSpark 质量校验链（Citation/Coherence/5项审计/可实现性）⑥ 安全领域评估要点。

## 6. 安全领域适配

- 攻击 idea 须额外评估可复现性与合规风险（红队受控）。
- `refuted` 攻击声明沉淀为"已知无效方向"，避免重复探索。
- 质量门（Type-B）对安全研究**强制跨模型**：攻击方设计，须由不同家族模型红队评估。
