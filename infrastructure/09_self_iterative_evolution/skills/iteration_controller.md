---
name: iteration-controller
description: >
  自迭代进化控制器。对一轮已完成的完整研究链路（①文献检索→②Idea&校验→④实验设计→
  ⑤数据评估清洗→⑥代码开发→⑦实验执行→⑧结果分析与经验生成→③评估与基准(capstone)→
  ⑩数据生成对抗(红队回环, 经 R10 调度)）
  做综合复盘，判定应当 (a) 达标退出，还是 (b) 回退到 ①-⑩ 中某一个环节重新迭代。
  给出目标环节、回退理由、本轮须携带的经验（lessons/skills），并写入决策日志。
  触发词："下一轮怎么办"、"要不要再迭代"、"回到哪一步"、"是否收敛/退出"、"self-iterate"。
allowed-tools: Read, Grep, Glob, Write, Edit, Bash
---

# Iteration Controller — 自迭代进化决策 Playbook

> 这是安全 auto-research 闭环的"大脑"。它不产出研究成果，而是**决定下一步走向**：
> 达标退出，或精准回退到 ①-⑩ 的某一环重新迭代，并把上一轮经验注入下一轮。

本 playbook 综合三套上游机制（均已在同层 `agents/` 沉淀源码）：

| 机制 | 来源 | 职责 |
|------|------|------|
| **收敛检测 / 升级链** | Arbor `arbor_convergence`（`convergence.py`） | score velocity + plateau → warn / paradigm_shift / stop 三级信号 |
| **经验转技能 / 自进化** | AutoResearchClaw `metaclaw_bridge` + `a-evolve` skill | Solve→Observe→Evolve→Gate→Reload：把失败模式沉淀为可复用 skill |
| **停滞检测** | MLEvolve `mle_evolve_conditions` | 分支停滞 / 全局停滞 / 触发融合 |

> 环节编号采用本基础设施的统一分层（见顶层 `README.md`）：
> ①文献检索 / ②Idea&校验 / ③评估与基准(capstone) / ④实验设计 / ⑤数据评估清洗 /
> ⑥代码开发 / ⑦实验执行 / ⑧结果分析与经验生成 / ⑨自迭代进化(本层) / ⑩数据生成对抗(红队回环)。
> 回退/调度目标落在 ①-⑩（③ 是末端评测门控，其分数用于判定；⑩ 是红队回环，由 R10 调度产出对抗数据后回 ⑤）。

---

## 0. 输入契约

控制器每轮结束时读取一份**链路状态记录**（schema 见
`../datasets_tasks/iteration_decision_schema.md`），至少包含：

- 每一环 ①-⑩ 的产物指针（论文集、idea 卡、实验设计、清洗数据集、代码、运行日志、评测分数、分析报告、对抗数据集、回放缓冲）
- 每一环的**门控状态**（pass / warn / fail）与关键指标
- 目标契约 `goal_contract`：目标指标 + 阈值 + 最大迭代轮数 + 计算预算
- 历史轨迹 `history[]`：过去各轮的 trunk score / 决策 / 目标环节

---

## 1. 决策主流程（Diagnose → Route）

```
STEP A  归一化本轮信号 ── 收集 ①-⑩ 各环门控 + 分数 + 与上轮 delta
STEP B  达标判定 ────────  满足 goal_contract? ── 是 ─▶ EXIT(success)
STEP C  预算/上限判定 ───  超 max_rounds 或 预算耗尽? ── 是 ─▶ EXIT(budget)
STEP D  收敛/停滞判定 ───  Arbor stop 信号 或 全局停滞? ── 是 ─▶ 见 §3 升级链
STEP E  根因定位 ────────  找到最早失效环节（earliest failing gate）
STEP F  路由 ───────────  按 §2 路由表回退到目标环节，携带 lessons
STEP G  记账 ───────────  写决策日志 + 由 metaclaw_bridge 把经验转 skill
```

**关键原则——回退到"最早失效环节"而非最后环节**：一个链路的分数低，
根因往往在上游。评测分低（③）可能是实验设计缺基线（④）或 idea 本身弱（②）。
控制器必须沿 ⑧→①方向反向定位**第一个不达标的门控**，从那里重启，避免在
下游反复打补丁（这正是 MetaClaw "重试率 -24.8%、精炼循环 -40%" 的来源）。

---

## 2. 诊断 → 目标环节 路由表

| # | 诊断信号（来自哪一环的门控/指标） | 回退目标 | 携带经验 |
|---|-----------------------------------|----------|----------|
| R1 | ⑧分析发现**文献覆盖不足 / 遗漏强基线**、被审出"已被前人做过" | **① 文献检索** | 缺口关键词、遗漏的 SOTA 论文 |
| R2 | ②校验 `refuted`/`scooped`，或 novelty 分低、idea 与证据矛盾 | **② Idea & 校验** | 被否证的 claim、碰撞命中、novelty 短板 |
| R3 | ⑦执行 / ⑧分析暴露**缺消融/缺对照/指标选错**，或结论不可证伪 | **④ 实验设计** | 缺失的 baseline/ablation、错误指标 |
| R4 | ⑦执行报 **代码 bug / 测试失败 / API 误用 / 实现与设计不符** | **⑥ 代码开发** | 错误类别、失败测试、debug 补丁 |
| R5 | ⑦执行 **超时 / OOM / 环境不可复现 / 数据管道错** | **⑦ 实验执行** | 资源规格、隔离/复现修复 |
| R6 | **数据问题**：badcase 高噪、标签错误/歧义、新老配比致遗忘旧能力、泄露命中 | **⑤ 数据评估清洗** | 数据质量报告、标签 schema、配比配置 |
| R7 | ③评测**达线但欠稳健**（方差大、少 seed、疑似过拟合 dev） | **③ 评估与基准** | 补 seed、留出集验证、抗污染检查 |
| R8 | 分数在**同一 approach family 内 plateau**（Arbor paradigm_shift） | **② 或 ④**（换范式） | 已穷尽父节点（禁止再扩展）、换架构/方法 |
| R9 | 全局停滞但**存在未探索的多样候选** | **④/⑦**（并行探索 + 融合） | 触发 branch fusion、集成多样解 |
| R10 | ③评测暴露**鲁棒性缺口**（对抗 ASR 高 / 边界 case 多）/ 检测到**灾难性遗忘**（旧能力回退）/ 或主动持续硬化需求 | **⑩ 数据生成对抗** →（产出对抗数据+回放缓冲+保留率约束）→ **⑤ 数据评估清洗** | 漏洞分类、对抗集、retention_floor、回放样本；⑩ 完成后经层⑨ 决定继续硬化或 EXIT |
| EXIT-S | 达标：指标 ≥ 阈值 **且** 审稿 verdict∈{ready,almost} | — 退出（成功） | 归档 idea 卡 + 漏洞模式库 |
| EXIT-B | 预算/轮数耗尽，未达标 | — 退出（预算） | 记录最佳快照 + 未竟方向 |
| EXIT-C | 收敛 stop 信号且无新范式可试 | — 退出（收敛） | Pareto 前沿 + 经验回传 |

> 单轮**只回退到一个目标环节**（最早失效者）。若并列多处失效，取链路最上游那个；
> 下游失效大概率随上游修复而消解。仅当上游全绿时才处理下游的稳健性问题（R7）。
> **数据驱动重训范式**：若 badcase 回流后根因定位在数据（R6），直接回 ⑤ 重做清洗/标注/配比，
> 再交 ⑥/⑦ 重训，无需回到 ①②④。
> **对抗驱动持续提升范式（⑩）**：达标后不立即 EXIT，经 R10 调度进入 ⑩ 主动找茬；⑩ 产出对抗数据+回放缓冲，
> 交 ⑤→⑥→⑦→③ 形成"对抗→重训"循环，并在 ⑦ 注入经验回放/EWC 防遗忘。⑩ 主场景为"已优化模型 +（可选）回流 badcase"。

---

## 3. 收敛升级链（来自 Arbor `convergence.py`）

连续 N 轮 trunk score 无实质提升时，`ConvergenceDetector` 逐级升级——控制器据此
决定"继续换方向 vs 退出"：

| 连续无提升轮数 | 信号 level | 控制器动作 |
|----------------|-----------|-----------|
| ≥ `warn_after`(3) | `warn` | 提示接近上限，优先 R8/R9（换 approach family / 集成），仍可迭代 |
| ≥ `force_after`(5) | `paradigm_shift` | **强制换范式**：下一个 idea 必须用不同方法族；禁止扩展已穷尽父节点；无新方向则走向 finalize |
| ≥ `stop_after`(8) | `stop` | **建议退出**：集成最优多样候选 → 跑留出集 → 归档报告；仅当有"从未探索的真正新方向"才可 override |

`suggested_actions` 与 `exhausted_parents`（禁止再扩展的节点）直接来自
`convergence.py` 的 `_get_suggestions()` / `_find_exhausted_parents()`。

---

## 4. 经验生成与回注（Solve→Observe→Evolve→Gate→Reload）

退出前 / 每次回退时，调用 `a-evolve` skill 与 `metaclaw_bridge`：

1. **Observe**：把本轮失败/欠优按 `{error_category, root_cause, frequency, severity}` 结构化。
2. **Evolve**：频次 ≥ 3 的复现模式 → 由 `lesson_to_skill.py` 生成一条可复用 skill。
3. **Gate**：`prm_gate.py`（PRM/LLM-as-judge）对新经验/新产物质量打分，低质不入库。
4. **Reload**：下一轮在对应环节由 `stage_skill_map.py` 按 `task_type` 注入 top-k 经验 skill。

这一步让系统"越用越强"，而不仅是"重跑一次"。

---

## 5. 安全 auto-research 适配要点

- **红队目标脱敏**：决策日志与经验库中，攻击目标/危险 payload 只存指纹/引用，不落明文。
- **`refuted` 剪枝**：被安全审计否证的攻击方向进入 `exhausted_parents`，禁止下一轮再扩展。
- **防作弊闭环**：退出前必须通过 ③ 的留出集 + `harbor-canary` 复核，防止 dev 集过拟合被误判达标。
- **人审断点**：涉及高危能力提升的迭代（如显著提升越狱成功率），`paradigm_shift` 及以上强制 HITL 确认后才继续。
- **数据合规（R6）**：回 ⑤ 时强制 PII 脱敏、安全标签人工复核、配比遗忘门控，防"修 badcase 而遗忘旧安全能力"。

---

## 6. 输出契约

控制器每轮输出一条**决策记录**（追加进 `history[]`，schema 见 datasets_tasks）：

```json
{
  "round": 3,
  "trunk_score": 0.71,
  "delta_vs_prev": 0.02,
  "convergence_level": "warn",
  "decision": "REVISIT",          // REVISIT | EXIT_SUCCESS | EXIT_BUDGET | EXIT_CONVERGED
  "target_stage": 10,              // 回退/调度目标环节编号 (①-⑩)，EXIT 时为 null；例：10=数据生成对抗, 5=数据评估清洗
  "route_rule": "R6",
  "reason": "重训后 badcase 验证集仍高噪，根因在标签错误与配比遗忘旧能力。",
  "carried_lessons": ["lesson://label-noise-2026-07", "skill://mixing-retention-gate"],
  "exhausted_parents": ["node_12", "node_15"]
}
```
