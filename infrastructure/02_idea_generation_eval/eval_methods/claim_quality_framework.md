# Idea 与声明质量评估框架

> 统一「idea 生成与评估校验」层的评测方式。覆盖：idea 质量三轴、声明状态机、先验碰撞、
> 多维度审计、可实现性审计。各方法的落地 skill 见 `../skills/`，种子集见 `../datasets_tasks/`。

## 1. Idea 质量三轴（idea_quality，0–100）

来源：已沉淀 `eval_methods/idea_quality/SKILL.md`（含 evals.json + 3 个 sample_ideas）。

| 轴 | 评估内容 | 关键子判断 |
|----|---------|-----------|
| **A — 问题定位** | gap 是否真实、重要、非平凡、仍开放 | 防止"挑软目标"刷分 |
| **B — 方法质量** | 方法本身好坏 | depth（新机制 vs 微调）· soundness（逻辑自洽）· feasibility（可构建） |
| **C — 问题契合** | 方法是否真解决该 gap | 防止"方法好但答错问题" |

- **评分公式**：`overall = round(100 * (A + B + C - 3) / 12)`
- **判定带**：`strong ≥ 67` · `borderline 34–66` · `weak < 34`
- **A/C 硬门**：若 A≤2 或 C≤2，即使分数高也不能判 `strong`（改方法可救，但错问题不行）。
- **双轨**：单 idea 走绝对轨；两 idea 比较走 pairwise 轨（以相对判断为准，盲评来源）。
- **反偏规则**：每个分数必须引用 idea 原文证据；评 substance 而非长度/流畅；始终停留在 idea 阶段（不得臆造实验结果）。

## 2. 声明状态机（PROVE / JUDGE，6 状态）

```
drafted → unproven → sound-modulo-imports → verified
                         └────────────────→ refuted
                         └────────────────→ retracted
```

- 跨模型 jury 裁决，禁止单模型自评；支持 `depends_on` / `refutes` / `uses` 依赖链。
- 落地：`../skills/paper-claim-audit`、`../skills/claims-drafting`、`../skills/result-to-claim`。

## 3. 先验碰撞（scoop-check / novelty-check，5 级 overlap）

- 把 novelty 拆为 4 轴（问题框定 / 核心机制 / 关键洞察 / 应用域），构造 3 类查询
  （Original-Problem / Broad-Domain / Method-Signature）经 `paper-search` 检索去重。
- 输出按轴的 overlap verdict（5 级），并给出最近先验工作，形成可辩护的"delta 声明"。
- 落地：`../skills/scoop_check`、`../skills/novelty-check`。

## 4. 多维度审计清单

| 审计 | 检查什么 | 落地 skill |
|------|---------|-----------|
| Citation 审计 | 引用是否准确、可追溯 | `citation-audit` |
| Experiment 审计 | 实验是否可复现 | `experiment-audit` |
| Claim 审计 | 声明是否有证据支持 | `paper-claim-audit` |
| Coherence 审计 | 数据流形式化 + 数值 dry-run + 退化探测 | `idea_spark` 2.3 Coherence Gate |
| Falsification 审计 | 最小实验 + 结果指标方向 + 负载变量 + 阴性对照 | `idea_spark` Phase 3.2 / 4.1.5 |
| Integrity 取证 | 抄袭 / 捏造 / 图像篡改 | `integrity-forensics` |

## 5. IdeaSpark 质量校验链（端到端）

1. **Citation Gate**（确定性）：sub_pattern 引用必须来自真实读过的 overview.md → `validate --phase2`。
2. **Coherence Gate**（执行验证而非审查）：4 种 trace 动作（形式化数据流 / 数值 dry-run / 退化探测 / claim→step 映射）。
3. **5 项审计**（Phase 3.2）：gap_closure_reject / recipe_application / anti_pattern / paper_pointed_threat / falsification_structure。
4. **可实现性审计**（Phase 4.1.5）： sceptical-engineer 把每步改写为可构建 spec（中英文）。

## 6. 安全领域评估要点

- 攻击 idea 须额外评估**可复现性**与**合规风险**（红队评估须受控）。
- `refuted` 的攻击声明沉淀为"已知无效方向"，进入知识库避免重复探索。
- 质量门（Type-B）对安全研究**强制跨模型**：攻击方设计的攻击，须由不同家族模型红队评估。
