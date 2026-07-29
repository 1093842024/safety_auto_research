# 层⑩ Agents — 数据生成对抗角色

> 四个角色协同完成"漏洞测试 → 失败分析 → 对抗生成 → 防遗忘护栏"。
> 接口设计直接对应 `doc/Adversarial_safety_multi_Agent.md` 的 `RedTeamAgent / BlueTeamAgent / ArbiterAgent / RewardEngine`，
> 本层只取"攻击侧 + 分析侧 + 生成侧 + 防遗忘侧"，蓝队/仲裁/奖励属于被攻击模型与层③评估。

| Agent | 角色 | 对应原 doc 接口 | 上游 | 下游 |
|-------|------|----------------|------|------|
| `vulnerability_probe_agent` | 红队/漏洞探针（Attack Agent） | `RedTeamAgent.generate_adversarial` | ⑦模型 / ③基线 | adv_analysis |
| `failure_analyst_agent` | 失败分析师 | （聚类/归因，无独立原接口） | adv_redteam 失败池 | adv_generation |
| `adversarial_generator_agent` | 对抗生成器 | （生成 hard negative） | adv_analysis 队列 | adv_antiforgetting |
| `antiforgetting_guardian_agent` | 防遗忘守护（回放缓冲管理） | （replay buffer manager） | ③评估报告 | ⑤数据清洗 |

**协作时序**：probe → analyst → generator → guardian →（合并包）→ ⑤。
各 agent 源码骨架见对应 `.md`；完整 RL 训练器（GRPO 红队/蓝队/仲裁）参考 `doc/Adversarial_safety_multi_Agent.md` 第 5 节接口与第 7.4 节。
