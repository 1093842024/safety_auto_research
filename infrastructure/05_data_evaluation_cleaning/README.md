# 层⑤ 数据评估与清洗（Data Evaluation & Cleaning）

> **定位**：实验执行前的**关键环节**——把"原始/回流数据"变成"可用于训练的高质量数据集"。
> 它是两种研究范式的共同枢纽：
> - **(A) 标准研究链路**：实验设计 ④ 之后、代码开发 ⑥ 之前，做数据准备与质量把关。
> - **(B) 数据驱动重训范式（本层主场景）**：一个**已优化成型的模型**回流一批 **badcase**，
>   直接进入本层做「数据评估 → 清洗 → 标签优化适配 → 新老数据配比」，再交给代码开发 ⑥（训练代码）
>   与实验执行 ⑦（重新训练 / 微调），形成"模型 → badcase → 数据迭代 → 重训提升"的数据闭环。
>
> 可复用资产：AutoLab `data_select_ifeval`（数据筛选评估）、`grpo_multisource`（多源配比+遗忘门控）、
> MLEvolve `data_leakage_agent`（泄露检测，见层⑧）、NanoResearch `ray-data`（分布式清洗/ETL）。

## 本层四类资产

| 资产 | 内容 |
|------|------|
| **Skill** | `ray-data`（分布式数据变换/清洗/去重/ETL，源码）、`data_eval_clean`（★本层 runbook：评估→清洗→标注→配比） |
| **Agent** | 数据评估筛选（引用 AutoLab `data_select_ifeval`）、多源配比（引用 AutoLab `grpo_multisource`）、泄露检测（引用层⑧ `data_leakage_agent`）、数据变换（NanoResearch `ray-data`） |
| **数据集任务** | 数据质量检查清单、标签 schema 模板（标签优化适配）、新老数据配比配置模板；并引用层③ AutoLab `data_select_ifeval` / `grpo_multisource` 作为可运行基准 |
| **评测方式** | 数据质量指标（覆盖率/去重率/标签噪声率/分布偏移/泄露率）、标签一致性（标注者间一致性）、配比有效性（遗忘门控/保留率） |

## 主流程（badcase 回流重训范式）

```text
[已优化模型] --(生产/评测回流)--> badcase 池
        │
        ▼
  ① 数据评估  : 统计 badcase 分布、错误类型、与训练集的偏移；量化现有数据质量短板
        │
        ▼
  ② 数据清洗  : 去重/去噪/格式归一；用 ray-data 做规模化 ETL；用 data_leakage_agent 查泄露
        │
        ▼
  ③ 标签优化适配: 设计/修订标签体系(taxonomy)；对 badcase 重新标注/纠偏；统一标签语义
        │
        ▼
  ④ 新老配比  : 设计 old:new 比例、回放(replay)、课程式(curriculum)；以遗忘门控约束保留率
        │
        ▼
  ⑤ 交接      : 产出清洗后数据集 + 配比配置 → 代码开发 ⑥(训练代码) → 实验执行 ⑦(重训)
        │
        ▼
  (实验执行 ⑦ / 结果分析 ⑧ 反馈数据问题) ──REVISIT──> 回到 ①-④ 任一子环节
```

## 安全领域适配要点

1. **敏感数据脱敏**：badcase 可能含生产隐私/违规样本，清洗前先做 PII 脱敏与合规过滤，禁止进入训练集明文。
2. **标签合规**：重标注的标签体系须避免引入歧视性/违规类别；安全相关标签（如有害内容分级）走人工复核。
3. **防泄露**：训练/验证/测试切分须隔离 group 与时间，复用层⑧ `data_leakage_agent` 做门控。
4. **配比护栏**：新 badcase 占比过高易灾难性遗忘旧能力，须以"遗忘门控"（参考 `grpo_multisource`）约束保留率。
5. **可溯源**：每条清洗/标注/配比决策记录来源与操作人（或 agent），支撑层⑨ 自迭代的经验回注。

## 与其他层接口

- **上游**：实验设计 ④（数据需求/假设）、文献检索 ①（标签体系参考既有工作）；数据驱动重训范式可**直接进入本层**（跳过 ①②④）。
- **下游**：代码开发 ⑥（本层产出 `dataset` + `mixing_config` 作为训练代码输入）、实验执行 ⑦（重训）。
- **回注**：结果分析 ⑧ 若发现"数据方差大/过拟合/分布偏移"，经层⑨ 路由回本层重做清洗或配比。
- **跨层引用**（避免重复拷贝）：
  - 层③ `datasets_tasks/autolab_tasks/data_select_ifeval/` — 数据筛选评估基准
  - 层③ `datasets_tasks/autolab_tasks/grpo_multisource/` — 多源配比 + 遗忘门控基准
  - 层⑧ `agents/mle_evolve_analysis/data_leakage_agent.py` — 泄露检测 agent
