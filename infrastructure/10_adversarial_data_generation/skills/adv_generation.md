---
name: adv-generation
description: >
  数据生成对抗层⑩的对抗样本生成 playbook。基于 adv_analysis 的生成优先级队列，
  从失败簇批量产出对抗训练数据：扰动式(近边界难例)、合成式(LLM 变体/AIR 异构分组)、
  课程式(优先近边界, CHASE)、难度感知采样。产出去重、自动标注、质量过滤后的 adversarial_dataset，
  交付 ⑤ 数据评估清洗。触发词："生成对抗样本"、"造难例"、"hard negative"、"对抗训练数据"。
allowed-tools: Read, Grep, Glob, Write, Edit, Bash
---

# Adv-Generation — 对抗样本生成 Playbook（层⑩）

> 输入：`adv_analysis` 的 `gen_queue` + 失败簇 + 当前模型。输出：`adversarial_dataset`（待 ⑤ 清洗）。

## 1. 四种生成策略

| 策略 | 做法 | 来源 | 适用队列 |
|------|------|------|---------|
| **扰动式 perturbation** | 在失败点沿梯度推到决策边界附近，取 ρ*×[0.8,1.2] 难例 | PGD/FGSM | white-box / 小半径 |
| **合成式 synthesis** | LLM 以失败 case 为种子生成语义等价变体；用 AIR 异构 prompt 分组保多样性 | AIR, Self-Play | black-box / 长尾 |
| **课程式 curriculum** | 按 `boundary_density` 优先近边界难例，由易到难排课 | CHASE | 全部（默认） |
| **难度感知 sampling** | 选"当前模型置信度 0.3–0.7"的边界样本，避开已稳过/全错的极端 | 经验回放 | 迭代中后期 |

## 2. 执行流程

```
STEP 1  读 gen_queue，逐桶取生成策略
STEP 2  生成候选对抗样本（每桶 N 个，N 由预算与 priority 决定）
STEP 3  质量过滤（关键三关）：
        · 有效性：必须真能骗过当前 target（否则丢弃，非对抗）
        · 合法性：语义有效、非乱码、保留原始意图（R_intent 校验）
        · 安全：不含明文危险 payload（指纹化），过层③ canary
STEP 4  去重：embedding 近邻去重（复用层⑤ ray-data / 层② 去重规则）
STEP 5  自动标注：arbiter(层③/RL-MAAMS 仲裁) + rubric 打 gold label 与风险类别
STEP 6  产出 adversarial_dataset（带 attack_family / dim / difficulty / fingerprint 元信息）
```

## 3. 数据形态（与 ⑤ 对齐）

每条样本字段对齐层⑤ `label_schema_template` + 本层 `replay_buffer_schema`：

```json
{
  "id": "adv_55021",
  "text": "<对抗样本，危险 payload 已指纹化>",
  "gold_label": "unsafe",
  "risk_category": "tool_use_safety",
  "attack_family": "multi_step",
  "difficulty": 0.71,
  "fingerprint": "fp:sha256:ab12...",
  "source_stage": 10
}
```

## 4. 多样性与覆盖约束

- **多样性**：生成分布须覆盖 gen_queue 中各攻击族，避免只刷单一族（R_diversity 约束）。
- **覆盖补齐**：对 `coverage_hole`（ASR>0 未覆盖族）强制生成配额。
- **合法边界**：纯随机噪声/无语义样本不得入集（会被 ⑤ 当作噪声清洗掉）。

## 5. 输出契约

- `adversarial_dataset/`：本批对抗样本（字段如上）
- `gen_report`：`{generated: 1200, passed_qc: 980, by_family: {...}, coverage: [...]}`
- 一并交给 `adv_antiforgetting` 取回放缓冲，再整体交接 ⑤。
