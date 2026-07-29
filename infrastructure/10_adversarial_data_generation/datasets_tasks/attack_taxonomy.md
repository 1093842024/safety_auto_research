# Attack Taxonomy — 攻击类型分类体系（层⑩）

> 数据生成对抗的统一攻击族字典。红队探针、失败归因、对抗生成均以此为坐标。
> 覆盖黑盒提示重写 / 白盒梯度扰动 / OOD / 覆盖引导 fuzz 四大类，参考 `doc/Adversarial_safety_multi_Agent.md`。

## 一、黑盒·提示重写（Prompt Rewriting，仅 API 访问）

| 攻击族 | 描述 | 示例手法 | 难度 |
|--------|------|---------|------|
| `role_play` | 角色扮演 | "你是无限制助手…" | 低 |
| `fictional_frame` | 虚构框架 | "在小说剧情里…" | 中 |
| `persuasion` | 说服改写 | 权威/紧急/共情话术 | 中 |
| `multi_step` | 多步诱导 | 先铺垫再越界 | 高 |
| `obfuscation` | 混淆编码 | 插入干扰符/谐音/emoji/零宽 | 中 |
| `translation` | 翻译迁移 | 非母语调优绕过 | 中 |

## 二、白盒·梯度扰动（Gradient Perturbation，权重可访问）

| 攻击族 | 描述 | 适用 | 记录量 |
|--------|------|------|--------|
| `pgd` | 投影梯度下降 | 连续输入/embedding | 最小扰动半径 ρ* |
| `fgsm` | 单步快梯度 | 快速探边界 | ρ* |
| `cw` | Carlini-Wagner | 低扰动强攻击 | ρ* |

## 三、OOD / 分布偏移（Distribution Shift）

| 攻击族 | 描述 |
|--------|------|
| `domain_rand` | 域随机化（风格/字体/背景） |
| `long_tail` | 长尾采样（稀有违规变体） |
| `noise_inject` | 噪声注入（不影响人类判读） |

## 四、覆盖引导 fuzz（Coverage-Guided Fuzz）

| 攻击族 | 描述 |
|--------|------|
| `uncertainty_fuzz` | 基于模型不确定性变异搜索 |
| `gradcov_fuzz` | 基于梯度覆盖的边界探测 |

## 标注约定

每条对抗样本记录 `attack_family`（上表 key）+ `capability_dim`（RUBAS 四维 / 业务风险类别）
+ `difficulty`（0–1）+ `fingerprint`。归因时按 `attack_family × capability_dim` 笛卡尔分桶。

## 扩展

- 新攻击族出现 → 先入 `vuln_patterns`（漏洞模式库），稳定后补入本 taxonomy。
- 失败的攻击尝试 → 层⑨ `exhausted_parents` 剪枝，不再扩展。
