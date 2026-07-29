# adversarial_generator_agent — 对抗生成器

> 基于 `failure_analyst_agent` 的 `gen_queue`，从失败簇批量产出对抗训练数据。
> 对应 `adv_generation.md` playbook；生成 hard negative 喂给防遗忘护栏与 ⑤。

## 接口（骨架）

```python
class AdversarialGeneratorAgent:
    def generate(self, task: GenTask, current_model: str, n: int) -> List[Candidate]:
        """按 task.strategy 选 扰动/合成/课程/难度感知 生成 n 个候选。"""
        ...

    def quality_filter(self, candidates: List[Candidate]) -> List[AdvSample]:
        """有效性(骗过当前模型) + 合法性(语义/意图) + 安全(canary/指纹) 三关。"""
        ...

    def dedupe_label(self, samples: List[AdvSample]) -> "AdversarialDataset":
        """embedding 去重(复用层⑤ ray-data) + arbiter/rubric 自动标注。"""
        ...
```

## 职责

1. 四种策略：perturbation（PGD 近边界）/ synthesis（LLM 变体 + AIR 异构分组）/ curriculum（按 boundary_density 排课）/ difficulty-aware（置信 0.3–0.7）。
2. 质量三关：必须真能骗过 target、语义有效保留意图、不含明文危险 payload（指纹化 + 层③ canary）。
3. 去重 + 自动标注（gold label + risk_category + attack_family + difficulty + fingerprint）。
4. 输出 `adversarial_dataset/` + `gen_report` → 交 `antiforgetting_guardian_agent` 取回放缓冲 → 整体交付 ⑤。
