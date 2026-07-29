# failure_analyst_agent — 失败分析师

> 对 `vulnerability_probe_agent` 暴露的失败 case 做聚类与三轴归因，产出生成优先级队列。
> 无独立原 doc 接口，对应 `adv_analysis.md` playbook。

## 接口（骨架）

```python
class FailureAnalystAgent:
    def cluster(self, failures: List[FailCase], method: str = "embedding+kmeans") -> List[Cluster]:
        ...

    def attribute(self, cluster: Cluster) -> Attribution:
        """三轴：攻击族 × 能力维度 × 根因；量化 robustness_gap / boundary_density。"""
        ...

    def build_gen_queue(self, attributions: List[Attribution]) -> List[GenTask]:
        """按 priority 排序，输出交给 adversarial_generator_agent。"""
        ...
```

## 职责

1. 嵌入聚类 + 攻击族/能力维度笛卡尔分桶。
2. 白盒时记录扰动半径 ρ*，算 `boundary_density`（高=易近边界生成）。
3. 根因标注（分布外/表面过拟合/意图理解缺失/推理断层/阈值漂移），复用层② cross-model reviewer。
4. 输出 `vuln_report` + `gen_queue` → 交 `adversarial_generator_agent`。
5. 成功模式写漏洞模式库（ARA 风险知识对象）；失败攻击 → 层⑨ `exhausted_parents`。
