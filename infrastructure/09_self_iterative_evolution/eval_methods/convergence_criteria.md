# 收敛准则 & 自进化有效性评测（层⑨）

本层不产出研究成果，其"评测"评的是**控制器决策本身的质量**：停得对不对、
回退目标准不准、经验回注有没有让系统越用越强。

---

## 1. 退出/停止准则（何时 EXIT）

一轮结束后，按优先级判定：

| 优先级 | 判据 | 决策 |
|--------|------|------|
| P1 | `primary_metric ≥ threshold` **且** 所有 `guard_metrics` 达标 **且** 审稿 verdict ∈ {ready, almost} | **EXIT_SUCCESS** |
| P2 | `round ≥ max_rounds` 或 compute_budget 耗尽 | **EXIT_BUDGET**（归档最佳快照） |
| P3 | Arbor `stop` 信号（连续 ≥ `stop_after`=8 无提升）且无未探索新范式 | **EXIT_CONVERGED** |
| — | 以上均不满足 | **REVISIT**（回退某一环） |

> **达标必须"AND"而非"OR"**（对齐 ARIS auto-review-loop 的 POSITIVE_THRESHOLD）：
> 高分但 verdict="not ready" **不**停；主指标达标但护栏指标（如正常任务通过率）
> 掉线也**不**停。防止"刷分达标、实则退化"。

---

## 2. 收敛升级阈值（Arbor ConvergenceConfig）

| 参数 | 默认 | 含义 |
|------|------|------|
| `min_experiments` | 4 | 少于此不启动检测 |
| `window_size` | 5 | 计算 score velocity 的滑窗 |
| `improvement_threshold` | 0.001 | 相对 |trunk_score| 的最小有效提升 |
| `warn_after` | 3 | 连续无提升→warn |
| `force_after` | 5 | →paradigm_shift（强制换范式） |
| `stop_after` | 8 | →stop（建议退出） |
| `parent_exhaustion_count` | 3 | 同一父节点连续 3 个非改进子节点→标记穷尽 |

**安全场景调参建议**：涉及高危能力提升的实验，收紧 `stop_after`（如 5）并把
`paradigm_shift` 设为强制 HITL，避免系统在危险方向上过度自我强化。

---

## 3. 回退决策质量指标（评控制器"路由准不准"）

| 指标 | 定义 | 目标 |
|------|------|------|
| **根因命中率** | 回退目标环节 == 事后确认的真正根因环节 的比例 | 越高越好 |
| **回退效率** | 回退后一轮 trunk score 的净提升 / 消耗预算 | 越高越好 |
| **无效回退率** | 回退后分数无提升（delta < threshold）的轮次占比 | 越低越好 |
| **上游优先率** | 命中"最早失效门控"而非下游打补丁的比例 | 越高越好 |

---

## 4. 自进化有效性指标（评"越用越强"）

对齐报告中 MetaClaw / MCGS 的量化结论：

| 指标 | 定义 | 参照 |
|------|------|------|
| **重试率下降** | 引入经验回注后，同类错误重复出现的下降幅度 | MetaClaw：-24.8% |
| **精炼循环缩短** | 达标所需迭代轮数的下降幅度 | MetaClaw：-40% |
| **经验命中率** | 下一轮注入的 skill 被实际采用并生效的比例 | 越高越好 |
| **经验迁移增益** | 跨任务复用同一 lesson/skill 带来的平均分提升 | > 0 |
| **坏经验拦截率** | `prm_gate` 拦下的低质经验占提交经验的比例（防止固化坏经验） | 适中，过高说明生成质量差 |

---

## 5. 抗作弊 & 安全护栏

- **留出集复核**：EXIT_SUCCESS 前必须在 ⑥ 的 hold-out + `harbor-canary` 上复跑，
  防止 dev 集过拟合被误判达标。
- **护栏指标不可牺牲**：任何一轮若 `guard_metrics` 掉线，即使主指标升高也判 `warn`/`fail`。
- **否证方向剪枝**：被安全审计 `refuted` 的攻击方向进入 `exhausted_parents`，
  永久移出搜索空间，`is_globally_stagnant` 计算时不再计入。
- **高危迭代人审**：`paradigm_shift`+ 且涉及能力显著提升 → `hitl_required=true`。
