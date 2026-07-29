# 层⑨ 自迭代进化 — Agents（决策引擎源码）

本层的 agent 均为"控制器"角色：不产出研究成果，而是**监测轨迹、判定收敛、
决定回退目标、把经验转化为可复用技能**。三套引擎分工互补。

| Agent | 角色 | 关键文件 | 对应决策职责 |
|-------|------|----------|-------------|
| **arbor_convergence** | 收敛检测与升级 | `convergence.py`、`idea_tree.py`、`checkpoint.py`、`config.py` | score velocity + plateau → warn/paradigm_shift/stop；`exhausted_parents` 剪枝；断点续跑 |
| **metaclaw_bridge** | 经验→技能自进化 | `lesson_to_skill.py`、`prm_gate.py`、`stage_skill_map.py`、`skill_feedback.py`、`session.py` | 把本轮 lessons 生成 skill；PRM 质量门控；按阶段注入 top-k 经验 |
| **mle_evolve_conditions** | 停滞/终止判定 | `conditions.py`、`node_selection.py` | 分支停滞 / 全局停滞 / 触发融合；下一节点选择 |

## 三引擎如何协同（对应 `../skills/iteration_controller.md` 主流程）

```
每轮结束
  │
  ├─ arbor_convergence.on_experiment_complete()  → 收敛信号 (warn/paradigm_shift/stop)
  ├─ mle_evolve_conditions.is_globally_stagnant() → 是否全局停滞
  │        │
  │        └─ 二者共同决定：继续迭代 / 换范式 / 退出（见 controller §3）
  │
  └─ 若继续迭代：
         a-evolve (Observe) → metaclaw_bridge.lesson_to_skill (Evolve)
                            → prm_gate (Gate) → stage_skill_map (Reload 下一轮)
```

## 关键实现要点

- **Arbor `ConvergenceConfig`** 阈值：`warn_after=3 / force_after=5 / stop_after=8`、
  `parent_exhaustion_count=3`、`improvement_threshold=0.001`、`window_size=5`。
  可按安全场景在 `config.py` 调整（如高危能力迭代收紧 `stop_after`）。
- **MetaClaw `prm_gate`** 使用 LLM-as-judge，对关键阶段产物打分，低质经验不入库，
  避免"把坏经验固化成 skill"。
- **MLEvolve `conditions`**：`is_branch_stagnant`（分支内连续 3 次无提升）触发换分支；
  `is_globally_stagnant`（窗口内全局无提升）触发退出/融合；`should_trigger_branch_fusion`
  在时间窗内融合多样解。

## 与其他层接口

- **上游**：读取 ①-⑩ 各层的门控状态与分数（尤其 ③ 评估与基准的 trunk score、⑧ 的分析报告、⑩ 的鲁棒性/遗忘率）。
- **下游**：产出"回退目标环节 + 携带经验"，驱动对应层（①-⑩）重启一轮；或输出退出信号归档。
- **经验回流**：生成的 skill 写回各层 `skills/`，形成"越用越强"的自进化。

## 安全适配

- 决策日志/经验库对攻击目标脱敏（只存指纹）。
- 被安全审计 `refuted` 的攻击方向进入 `exhausted_parents`，永久禁止再扩展。
- `paradigm_shift` 及以上级别，若涉及高危能力提升，强制人审断点（HITL）后再继续。
