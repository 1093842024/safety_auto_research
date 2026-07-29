# antiforgetting_guardian_agent — 防遗忘守护（回放缓冲管理）

> 维护经验回放缓冲、设定保留率目标、产出正则信号，确保对抗重训不遗忘旧能力。
> 对应 `adv_antiforgetting.md` playbook + 原 doc "反思经验回放池"。

## 接口（骨架）

```python
class AntiForgettingGuardianAgent:
    def update_buffer(self, eval_report: dict) -> "ReplayBuffer":
        """每轮 ③ 后：达标稳定样本 mastery↑、retain_flag=true。"""
        ...

    def plan_retention(self, retention_floor: dict) -> "RetentionConfig":
        """算各维回放配额与 adv_new:replay 混合比。"""
        ...

    def regularization_signal(self, old_weights: str, method: str = "EWC") -> dict:
        """算 Fisher/L2-SP 锚点，output regularization_signal.json。"""
        ...

    def monitor_forgetting(self, prev: dict, curr: dict) -> dict:
        """forgetting_rate[dim] = max(0, prev_acc - curr_acc)。"""
        ...
```

## 职责

1. 读上轮 ③ 评估报告，更新 `replay_buffer_schema` 中的 mastery/retain_flag。
2. 按 `retention_floor` + `replay_ratio` 算本轮回放配额。
3. 计算 EWC Fisher 或 L2-SP 锚点 → `regularization_signal`（注入 ⑦ 训练 loss）。
4. 合并 `adversarial_dataset` + `replay_buffer` + `retention_config` + `regularization_signal` → 交付 ⑤。
5. 监控 `forgetting_rate`；超阈值 → 层⑨ R10 路由要求提高回放权重或回 ⑦ 重训。

## 持续学习手法（与层⑤分工）

- 层⑤：数据集/配比层粗粒度遗忘门控（保留率）。
- 本层：样本级回放 + EWC/L2-SP/知识蒸馏 细粒度护栏，共用 `retention_config`。
