# 层⑤ 数据评估与清洗 — Agents（数据centric 重训控制器）

> 本层不强制单一 agent 实现，而是把"数据评估→清洗→标注→配比"拆成可由既有 agent 扮演的角色，
> 并以一个**数据centric 重训控制器**（本目录 `data_centric_controller.md` 描述其决策逻辑）编排它们。

## 角色与可复用资产映射

| 角色 | 职责 | 复用资产（跨层引用，避免重复拷贝） |
|------|------|-----------------------------------|
| **数据评估筛选** | 从数据池筛选高质量样本、量化质量短板 | AutoLab `data_select_ifeval`（`datasets_tasks/autolab_tasks/data_select_ifeval/`）：50k 池选 5k 最大化 IFEval，含 `select_data.py` |
| **多源配比** | old/new 多源混合 + 遗忘门控 | AutoLab `grpo_multisource`（`datasets_tasks/autolab_tasks/grpo_multisource/`）：多源训练 + retention gate |
| **泄露检测** | 训练/验证/测试隔离、查泄露 | MLEvolve `data_leakage_agent.py`（层⑧ `agents/mle_evolve_analysis/data_leakage_agent.py`） |
| **数据变换/清洗** | 规模化去重/归一/ETL | NanoResearch `ray-data`（`skills/ray-data/`，分布式数据管线） |

## 数据centric 重训控制器（编排逻辑）

决策循环（区别于层⑨ 的跨环节路由，本控制器只在本层 ①-④ 子阶段内闭环，必要时上抛层⑨）：

```
badcase 回流
  → 评估(①): 分型 + 偏移 + 质量短板
       ├─ 根因在数据 ─▶ 清洗(②) → 标注(③) → 配比(④)
       └─ 根因非数据（架构/策略）─▶ 上抛层⑨，路由到 ④/⑥
  → 配比(④) 遗忘门控验证
       ├─ 通过 ─▶ 交接 ⑥/⑦（重训）
       └─ 失败 ─▶ 回 ④ 调高 old 占比 / 加 replay
  → 重训后 ⑦/⑧ 反馈数据问题 ─▶ 层⑨ 路由回本层对应子阶段
```

## 运行方式

1. 进入本层：标准链路从 ④ 之后进入；数据驱动重训范式从 badcase 回流直接进入。
2. 调用 `data_eval_clean` runbook（`skills/data_eval_clean.md`）走 ①-④。
3. 清洗用 `ray-data` 规模化处理；筛选/配比参考 AutoLab 两个任务的可运行实现。
4. 门控全部通过后，产出 `dataset` + `mixing_config` → 交出至 ⑥/⑦。
5. 若本层无法解决（根因在架构/训练策略），交层⑨ 决定回退到 ④/⑥。

## 安全适配

- 控制器在清洗前强制脱敏与合规过滤（见 `eval_methods/data_quality_metrics.md` 安全专项门控）。
- 配比遗忘门控防止"为修 badcase 而遗忘旧安全能力"。
- 所有数据操作记录可溯源字段，供层⑨ 经验回注与 PRM 门控。
