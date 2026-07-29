# 层⑨ 自迭代进化 (Self-Iterative Evolution)

> **闭环的"大脑"。** 一轮完整研究链路（①文献→②Idea&校验→④实验设计→⑤数据评估清洗→
> ⑥代码开发→⑦实验执行→⑧结果分析与经验生成→③评估与基准(capstone)）跑完后，本层综合复盘，判定：
> **达标退出**，还是**精准回退到 ①-⑩ 中某一环重新迭代**，并把上一轮经验注入下一轮，
> 让系统"越用越强"。

对应报告"自进化层 (Self-Evolution)"：MetaClaw（重试率 -24.8%、精炼循环 -40%）、
MCGS 记忆系统、Arbor 收敛检测 + 反向传播。

---

## 目录

```
09_self_iterative_evolution/
├── README.md                       # 本文件
├── skills/
│   ├── iteration_controller.md     # ★核心：诊断→回退到 1-8 某环 or 退出 的决策 playbook
│   ├── auto-review-loop/           # ARIS：review→fix→re-review 直到通过/上限（停止准则）
│   └── a-evolve/                   # AutoResearchClaw：Solve→Observe→Evolve→Gate→Reload
├── agents/
│   ├── README.md                   # 三引擎协同说明
│   ├── arbor_convergence/          # 收敛检测/升级链/穷尽剪枝/断点（convergence.py 等）
│   ├── metaclaw_bridge/            # 经验转技能 + PRM 门控 + 阶段技能映射
│   └── mle_evolve_conditions/      # 分支停滞/全局停滞/触发融合
├── datasets_tasks/
│   └── iteration_decision_schema.md # 链路状态 CycleRecord + 决策 DecisionRecord schema
└── eval_methods/
    └── convergence_criteria.md      # 退出/停止准则 + 升级阈值 + 决策质量 + 自进化有效性指标
```

---

## 统一梳理（skill / agent / 数据集 / 评测）

### Skill（可复用 playbook）
| Skill | 来源 | 职责 |
|-------|------|------|
| `iteration_controller` | 本层原创（综合三引擎） | 主决策流程 + 诊断→环节路由表（R1-R10 + 三种 EXIT） |
| `auto-review-loop` | ARIS | 自主评审循环，POSITIVE_THRESHOLD 停止准则（score≥6 AND verdict∈{ready,almost}） |
| `a-evolve` | AutoResearchClaw | 5 步自进化：收集证据→诊断根因→提出变异→质量门控→下轮重载 |

### Agent（决策引擎源码）
| Agent | 来源 | 职责 |
|-------|------|------|
| `arbor_convergence` | Arbor Coordinator | score velocity + plateau → warn/paradigm_shift/stop；`exhausted_parents` 剪枝 |
| `metaclaw_bridge` | AutoResearchClaw | `lesson_to_skill` 经验转技能、`prm_gate` 质量门控、`stage_skill_map` 阶段注入 |
| `mle_evolve_conditions` | MLEvolve | 分支/全局停滞检测、分支融合触发、下一节点选择 |

### 数据集任务
- `iteration_decision_schema.md`：`CycleRecord`（各执行环门控 + 目标契约 + 轨迹）与
  `DecisionRecord`（决策/目标环节/理由/携带经验），含决策状态机与源码字段对应。

### 评测方式
- **退出准则**：P1 达标(AND) / P2 预算 / P3 收敛。
- **决策质量**：根因命中率、回退效率、无效回退率、上游优先率。
- **自进化有效性**：重试率下降、精炼循环缩短、经验命中率/迁移增益、坏经验拦截率。

---

## 核心决策原则

1. **回退到"最早失效环节"而非最后环节**——沿 ⑧→① 反向定位第一个不达标门控，
   从那里重启，避免下游反复打补丁（MetaClaw 收益来源）。
2. **达标用 AND 不用 OR**——主指标、护栏指标、审稿 verdict 三者同时满足才 EXIT_SUCCESS。
3. **收敛升级链**——连续无提升 3/5/8 轮分别触发 warn / 强制换范式 / 建议退出。
4. **经验闭环**——每次回退/退出都把 lessons 经 PRM 门控转成 skill，下轮按阶段注入。

## 安全 auto-research 适配
- 决策日志/经验库对攻击目标脱敏（只存指纹）。
- 被安全审计 `refuted` 的攻击方向进入 `exhausted_parents`，永久剪枝。
- EXIT_SUCCESS 前强制 hold-out + `harbor-canary` 复核，防过拟合刷分。
- `paradigm_shift`+ 且涉及高危能力提升 → 强制人审断点（HITL）。

## 与全链路的关系
本层是 ①-⑧ 执行环之外（⑨ 控制平面 + ⑩ 红队回环）的**控制平面**：读取各层门控与分数（尤其 ⑦ 的 trunk score、⑧ 的分析），
输出"回退目标 + 携带经验"或"退出信号"。它把八个执行环节缝合成一个**自迭代闭环**：

```
①→②→④→⑤→⑥→⑦→⑧→③(评估基准, capstone)→⑨(本层)
                                           │
                          ┌────────────────┴─────────────────┐
             REVISIT(回到 1-8 任一环)              EXIT(达标/预算/收敛)
                  │
         携带经验重启该环 ─────────────▶ 回到对应环节，进入下一轮
```
