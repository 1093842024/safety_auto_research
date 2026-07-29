# 链路状态 & 决策日志 Schema（层⑨）

控制器的输入/输出契约。一条完整研究链路（①-⑩）的一轮运行对应一条 `CycleRecord`，
多轮组成 `history[]`。控制器读 `CycleRecord` 做诊断，写 `DecisionRecord` 决定下一步。

---

## 1. CycleRecord —— 一轮链路状态

```jsonc
{
  "cycle_id": "run-2026-07-17-r3",
  "round": 3,
  "goal_contract": {
    "objective": "在 SafeBench 上把越狱拦截率提升到 ≥ 0.90，不牺牲正常任务通过率(>0.95)",
    "primary_metric": "block_rate",
    "threshold": 0.90,
    "guard_metrics": { "benign_pass_rate": 0.95 },
    "max_rounds": 8,
    "compute_budget": { "gpu_hours": 48, "usd": 200 }
  },

  // 每一环的门控状态与产物指针
  "stages": {
    "1_literature":  { "gate": "pass", "artifact": "lit/rc-3/refs.json",       "metrics": { "coverage": 0.86 } },
    "2_idea":        { "gate": "pass", "artifact": "ideas/idea_card_07.md",     "metrics": { "novelty": 0.72, "verify": "supported" } },
    "3_design":      { "gate": "warn", "artifact": "design/exp_3.yaml",         "metrics": { "has_baseline": false, "has_ablation": true } },
    "4_code":        { "gate": "pass", "artifact": "code/rc-3/",                "metrics": { "tests_pass": 1.0 } },
    "5_execution":   { "gate": "pass", "artifact": "runs/rc-3/logs/",           "metrics": { "reproducible": true } },
    "6_evaluation":  { "gate": "fail", "artifact": "eval/rc-3/scores.json",     "metrics": { "block_rate": 0.71, "benign_pass_rate": 0.96, "n_seeds": 1 } },
    "7_analysis":    { "gate": "warn", "artifact": "analysis/rc-3/report.md",   "metrics": { "falsifiable": false } }
  },

  "trunk_score": 0.71,          // 主指标当前值
  "prev_trunk_score": 0.69,
  "timestamp": "2026-07-17T15:00:00+08:00"
}
```

**门控取值**：`pass`（达标）/ `warn`（可用但有隐患）/ `fail`（不达标，须处理）。

---

## 2. DecisionRecord —— 一轮决策输出（追加进 history[]）

```jsonc
{
  "round": 3,
  "trunk_score": 0.71,
  "delta_vs_prev": 0.02,
  "convergence_level": "warn",              // none | warn | paradigm_shift | stop
  "global_stagnant": false,

  "decision": "REVISIT",                    // REVISIT | EXIT_SUCCESS | EXIT_BUDGET | EXIT_CONVERGED
  "target_stage": 3,                        // 回退目标环节 1-7；EXIT 时 null
  "route_rule": "R3",                       // 命中 controller §2 路由表的哪条
  "earliest_failing_gate": 3,               // 反向定位到的最早失效环节
  "reason": "评测 block_rate=0.71 未达线；根因是实验设计缺强基线对照(has_baseline=false)，导致结论不可证伪。下游评测/分析失效随之解决。",

  "carried_lessons": [                       // 注入下一轮的经验
    "lesson://weak-baseline-2026-07",
    "skill://ablation-baseline-checklist"
  ],
  "exhausted_parents": [],                    // 被否证/穷尽、禁止再扩展的节点
  "hitl_required": false,                     // 是否需人审断点
  "timestamp": "2026-07-17T15:05:00+08:00"
}
```

---

## 3. 决策状态机（DecisionRecord.decision）

```
                 ┌────────────── 达标 & verdict∈{ready,almost} ─────────▶ EXIT_SUCCESS
                 │
 CycleRecord ──▶ 诊断 ──┼── 预算/轮数耗尽 ───────────────────────────────▶ EXIT_BUDGET
                 │
                 ├── 收敛 stop 且无新范式 ──────────────────────────────▶ EXIT_CONVERGED
                 │
                 └── 其余 ─▶ 定位最早失效门控 ─▶ REVISIT(target_stage∈1..7)
                                                    │
                                          携带 lessons，重启该环节新一轮
```

---

## 4. 与源码字段对应

| Schema 字段 | 上游来源 |
|-------------|----------|
| `convergence_level` | Arbor `ConvergenceSignal.level`（warn/paradigm_shift/stop） |
| `exhausted_parents` | Arbor `ConvergenceDetector._find_exhausted_parents()` |
| `global_stagnant` | MLEvolve `conditions.is_globally_stagnant()` |
| `carried_lessons` | MetaClaw `lesson_to_skill.py` 产物；`stage_skill_map.py` 决定注入位置 |
| `hitl_required` | Arbor `hitl.py` + 本层安全规则（高危能力提升强制人审） |

> 安全场景下，`stages.*.artifact` 中若含攻击 payload，只存脱敏指纹/引用，不落明文。
