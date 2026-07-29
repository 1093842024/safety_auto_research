# 层⑩ 数据生成对抗（Adversarial Data Generation）

> **定位**：自动红队 + 对抗样本工厂 + 防遗忘护栏。它在"模型已成型"之后（来源为 ⑦ 实验执行的产出，
> 或线上已优化模型），主动扮演**攻击者**，把研究闭环从"被动达标"升级为"主动硬化"：
>
> **测试模型漏洞 → 分析失败 → 生成对抗数据 → 维护经验回放缓冲（防遗忘）→ 交付 ⑤ 数据评估清洗**
>
> 产出"对抗数据集 + 回放样本 + 保留率约束"，进入"对抗 → 清洗 → 重训"的持续提升闭环，
> 让模型在持续暴露缺陷的同时**不遗忘过去已掌握的知识**。
>
> **可复用资产（上游来源）**：
> - `safety_auto_research/doc/Adversarial_safety_multi_Agent.md`（核心参考）：MAGIC/CHASE 红蓝对抗 RL、
>   Self-Play 反思经验回放（防遗忘）、奖励工程、经验回放池、Agent 接口、技术栈。
> - `safety_auto_research/doc/AI_Research_for_safety.md`：跨模型对抗审、攻防协同进化、ARA 风险知识对象。
> - 跨层引用：层② `cross-model-reviewer`（跨模型家族强制）、层③ 评估基准（鲁棒性锚点）、
>   层⑤ 数据配比（含遗忘门控）、层⑦ 训练执行（重训注入回放/EWC）、层⑨ 路由（R10）。

## 本层四类资产

| 资产 | 内容 |
|------|------|
| **Skill** | `adv_redteam`（★漏洞测试/红队 playbook：多策略攻击 + GRPO 红队训练）、`adv_analysis`（失败聚类与归因）、`adv_generation`（对抗样本生成：扰动/合成/课程式）、`adv_antiforgetting`（防遗忘护栏：回放缓冲 / EWC / 保留率门控） |
| **Agent** | `vulnerability_probe_agent`（红队/漏洞探针，Attack Agent）、`failure_analyst_agent`（失败分析师）、`adversarial_generator_agent`（对抗生成器）、`antiforgetting_guardian_agent`（回放缓冲守护，防遗忘） |
| **数据集任务** | `attack_taxonomy`（攻击类型分类体系）、`replay_buffer_schema`（经验回放缓冲 schema，防遗忘核心）、`redteam_targets_template`（红队目标/探针模板） |
| **评测方式** | `robustness_metrics`：对抗准确率 / ASR / 遗忘率 / 保留率 / 多样性 / 覆盖度 / 边界密度 |

## 主流程（对抗 → 清洗 → 重训 持续提升闭环）

```text
[已优化模型 / ⑦ 实验执行产出模型]
        │
        ▼
  ① 漏洞测试(红队探针): 按 attack_taxonomy 多策略攻击
        │   · 黑盒：提示重写(角色扮演/虚构框架/说服/多步诱导/混淆编码/翻译)
        │   · 白盒：PGD/FGSM 梯度扰动（模型权重可访问时）
        │   · OOD / 分布偏移 / 覆盖引导 fuzz
        │   记录：ASR、漏检 case、失败模式指纹
        ▼
  ② 失败分析: 聚类失败 case；按 taxonomy 归因(攻击族/能力维度/根因)
        │   量化：鲁棒性缺口、各能力维度边界密度
        ▼
  ③ 对抗生成: 从失败簇生成新对抗样本
        │   · 扰动式(近边界难例) / 合成式(LLM 变体, AIR 异构 prompt 分组)
        │   · 课程式(优先近边界, CHASE) / 难度感知采样
        │   · 去重 / 自动标注(arbiter+rubric) / 质量过滤(必须真能骗过当前模型且语义有效)
        ▼
  ④ 防遗忘护栏: 构建回放缓冲(已掌握 case)；设定各能力维度保留率目标
        │   产出：正则/EWC 重要性信号 + replay_buffer + retention_config
        ▼
  ⑤ 交接: 对抗数据集 + 回放缓冲 + 保留率约束
        │        └──────────────▶ ⑤ 数据评估清洗(清洗/标签/配比含回放)
        ▼
  ⑤ → ⑥ 代码(训练代码) → ⑦ 实验执行(重训, 含经验回放/EWC)
        → ③ 评估(鲁棒性 + 保留率) → ⑨ 自迭代(继续硬化 / EXIT)
```

## 与"两种研究范式"的关系

- **范式 A / B（标准研究链路 / 数据驱动重训）**：层⑩ 作为**持续硬化回路**叠加在 ③ 之后——
  模型达标后不立即 EXIT，而是经层⑨ 调度进入 ⑩ 主动找茬，暴露的缺陷驱动新一轮 ⑤→⑥→⑦→③。
- **范式 C（对抗驱动持续提升，本层主场景）**：一个已优化成型的模型 +（可选）线上回流 badcase →
  **直接进入层⑩**（红队攻击 + 对抗生成 + 防遗忘护栏）→ ⑤ 数据评估清洗 → ⑥ 代码 → ⑦ 重训 →
  ③ 鲁棒性评估 → ⑨ 自迭代。这是"自动提高算法鲁棒性与对抗性"的默认入口，跳过 ①②④（除非根因在架构/策略）。

## 安全领域适配要点

1. **红队受控**：攻击须在隔离沙箱（Harbor/Docker）运行；目标细节/危险 payload 只存指纹/引用，不落明文；复用层③ `harbor-canary` 防训练污染。
2. **跨模型强制**：红队（攻击者）与被攻击的蓝队模型须为**不同模型家族**（复用层② `cross-model-reviewer`），破除"同模型自审"盲区。
3. **防遗忘优先**：任何对抗重训必须带回放缓冲 + 保留率门控，防止"修了漏洞却遗忘旧安全能力"；复用层⑤ 配比遗忘门控。
4. **经验沉淀**：成功攻击策略 → 漏洞模式库（ARA 风险知识对象）；失败/已否证攻击 → `exhausted_parents`（层⑨ 剪枝，禁止再扩展）。
5. **人审断点**：显著提升越狱/绕过成功率的迭代，在 `paradigm_shift` 及以上强制 HITL 确认后才继续（见层⑨ 安全适配）。

## 与其他层接口

- **上游**：⑦ 实验执行（被攻击模型权重）、③ 评估与基准（鲁棒性基线/锚点）、线上已优化模型（范式 C 入口）；可选线上回流 badcase。
- **下游**：⑤ 数据评估清洗（本层产出 `adversarial_dataset` + `replay_buffer` + `retention_config` 作为其输入）。
- **回注**：⑧ 结果分析若发现"对抗集上仍漏检 / 旧能力回退"，经层⑨ R10 路由回本层加深攻击或调高回放权重。
- **跨层引用**（避免重复拷贝）：
  - 层② `skills/cross-model-reviewer`（或 `research-review`）—— 跨模型家族强制
  - 层③ `datasets_tasks/`（AutoLab / MLE 基准）—— 鲁棒性评测锚点
  - 层⑤ `skills/data_eval_clean` + `datasets_tasks/mixing_ratio_config` —— 清洗/配比（含回放）
  - 层⑦ `skills/execution_runbook` —— 重训（经验回放 / EWC 注入）
  - 层⑨ `skills/iteration_controller` —— R10 路由（鲁棒性/遗忘 → ⑩ → ⑤）
