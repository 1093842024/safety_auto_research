# Robustness Metrics — 鲁棒性与防遗忘评测（层⑩）

> 量化"对抗生成"是否真的提升了模型鲁棒性、且未遗忘旧能力。对应 `adv_redteam / adv_analysis / adv_antiforgetting`。
> 与层③ 评估基准（锚定/log-scaled）互补：层③ 测"达标"，本层测"抗打 + 不忘"。

## 一、鲁棒性指标

| 指标 | 定义 | 目标 |
|------|------|------|
| **ASR** (Attack Success Rate) | 对抗攻击绕过数 / 总攻击数 | 越低越好（<10% 强对抗） |
| **Adv-Acc** (对抗准确率) | 1 − ASR（按攻击族/能力维分桶） | 越高越好 |
| **Min Perturb Radius ρ\*** | 白盒下使模型出错的最小扰动半径 | 越大越鲁棒 |
| **Boundary Density** | 单位半径内失败点密度 | 监控用（高=易近边界生成） |
| **Coverage** | taxonomy 中被有效覆盖的攻击族占比 | =100% 为佳 |

## 二、防遗忘指标（核心新增）

| 指标 | 定义 | 目标 |
|------|------|------|
| **Retention Rate** | 重训后各维 acc / 重训前各维 acc | ≥ `retention_floor`（如 0.95） |
| **Forgetting Rate** | `max(0, prev_acc[dim] − curr_acc[dim])` | 越低越好（≈0） |
| **Replay Effectiveness** | 含回放 vs 不含回放的 retention 差 | 正增益 |
| **Knowledge Distillation Gap** | 新模型 vs 旧模型软标签 KL | 越小越保知 |

## 三、生成质量指标

| 指标 | 定义 | 目标 |
|------|------|------|
| **Diversity** | 生成样本的 embedding 距离 / 攻击族覆盖 | 高（R_diversity 约束） |
| **Validity** | 真能骗过当前模型且语义有效的占比 | 高（QC 通过率） |
| **Transferability** | 红队攻击跨模型家族的 ASR 保持 | >80%（原 doc RL-Jailbreak） |

## 四、评估协议

1. **鲁棒性评估**：红队生成 N=1000 轮对抗样本 → 蓝队审核 → 仲裁 → 报 ASR/Adv-Acc/覆盖。
2. **保留率评估**：在"已掌握基准集"（benign_sensitive_set + eval_gold）上测重训前后 acc，算 retention/forgetting。
3. **消融**：有/无回放缓冲、有/无 EWC，对比 forgetting_rate 与 adv_acc。
4. **置信区间**：bootstrap 1000 次报 95% CI（复用层③ 协议）。

## 五、与层⑨ 联动

- `retention[dim] < retention_floor` 或 `forgetting_rate[dim] > ε` → 层⑨ R10 路由要求提高回放权重 / 回 ⑦ 重训。
- `ASR` 较上轮显著上升且属高危 → HITL 断点（层⑨ 安全适配）。

## 六、报告模板（摘要）

```json
{
  "adv_acc_overall": 0.82,
  "asr_by_family": {"role_play": 0.69, "pgd": 0.91, "ood": 0.78},
  "retention": {"tool_use_safety": 0.96, "response_safety": 0.98},
  "forgetting_rate": {"helpfulness": 0.02},
  "diversity": 0.74,
  "replay_effectiveness": 0.11
}
```
