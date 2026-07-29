# Idea 生成与评估校验 Agent 规范（安全 Auto-Research）

> 本目录沉淀「idea 生成与评估校验」基础设施层中的 Agent 角色定义。
> 配套 skill 见 `../skills/`，数据集见 `../datasets_tasks/`，评测方式见 `../eval_methods/`。

## 1. 角色总览

| Agent 角色 | 职责 | 对应 skill / 源码 | 关键约束 |
|-----------|------|------------------|---------|
| **Idea Generator（生成）** | 从研究问题产出可辩护 idea card | `idea_spark` / `idea-creator` / `idea-discovery` | 每声明须 track 到检索记录 |
| **Novelty Checker（新颖性）** | 先验艺术碰撞、scoop 判定 | `scoop_check` / `novelty-check` | 5 级 overlap verdict |
| **Cross-Model Reviewer（跨模型审稿）** | 质量门裁决，独立于生成模型 | `auto-review-loop` / `research-review` | **Executor ≠ Reviewer 模型家族** |
| **Claim Auditor（声明审计）** | 声明可证性 / 引用 / 实验审计 | `paper-claim-audit` / `citation-audit` / `experiment-audit` / `result-to-claim` | 6 状态机 + 依赖链 |
| **Integrity Forensics（诚信取证）** | 抄袭 / 捏造 / 图像篡改取证 | `integrity-forensics` | 独立取证线程 |
| **Seed Analyzer（Claudini）** | 给定研究目标，分析 baseline 并下轮改进 | Claudini Auto-Research 循环 | Pareto 前沿进化 |

## 2. 核心机制：跨模型对抗审（最共识的质量保障）

> 来源：ARIS。这是所有层中"最具共识也最深刻"的机制——**不能让同一模型既当运动员又当裁判**。

- **模型家族隔离**：Executor（Claude / Codex 等）执行，Reviewer 必须用不同家族（如 GPT-5.5 xhigh）。
- **独立性保证**：Reviewer 只接收**文件路径**而非摘要；每次调用使用**新线程**，杜绝上下文污染。
- **自动审稿轮次**：4 轮自动审稿，分数从 5.0/10 → 7.5/10。
- **Type-A / B Gate 分类**：
  - Type-A（执行门）：同模型自判即可（如"代码是否编译通过"）。
  - Type-B（质量门）：**必须跨模型审查**（如"实验设计是否合理"）。

## 3. Claim 层状态机（PROVE / JUDGE）

> 来源：ARIS v0.4.18+。每个 claim 走 6 状态，由跨模型 jury 裁决，而非单模型自评。

```
drafted → unproven → sound-modulo-imports → verified
                         └────────────────→ refuted
                         └────────────────→ retracted
```

- **依赖链追踪**：claim 可声明 `depends_on` / `refutes` / `uses` 关系（与 `research-wiki` 的 claims 实体打通）。
- **安全适配**：漏洞 claim / 防御 claim 同样进入此状态机；`refuted` 的攻击 claim 应沉淀为"已知无效方向"，避免重复探索。

## 4. Claudini Seed Analyzer（算法自动发现模式）

> 来源：Claudini。适合"有明确 benchmark 的安全算法设计"任务（攻击算法、防御策略、优化器）。

循环：`Seed`（研究目标，如"用 <1e15 FLOPs 攻破 Qwen2.5-7B"）
→ `Analyze`（研究 baseline）→ `Design`（新 TokenOptimizer）→ `Implement`（Python 类）
→ `Benchmark`（标准配置）→ `Commit`（自动提交）→ `Repeat`。
多编码 Agent（Claude/Kimi/Codex/GLM）独立运行，共享 Benchmark 基础设施，Pareto 前沿可视化。

## 5. 与上下游接口

```
文献检索层 ──(Citation Gate)──> Idea Generator ──(Novelty Check)──> Claim Auditor
                                                    │
                              Cross-Model Reviewer ─┘ (Type-B 质量门)
                                                    │
                                      评估与基准层 ──(评测分数回灌)──> Seed Analyzer
```
