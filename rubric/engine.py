"""RubricEngine — the single entry point for the rubric stage.

Two operations, one shared implementation, used by both integration points:

* :meth:`RubricEngine.review`   — audit a task's *declared* evaluation standard
  (registration-time feedback: ``POST /benchmark-tasks/validate``);
* :meth:`RubricEngine.induce`   — produce the frozen executable rubric for a run
  (runtime: ``layer_12_rubric_induction`` before the dual loop starts).

Dual-track LLM policy (matches the platform's existing convention, cf.
``control_plane/llm_judge.py`` and ``audit_executor``):

* the **deterministic rule engine is the default** — no network, reproducible, so
  tests and offline deployments behave identically;
* an LLM refinement pass runs only when explicitly enabled (``RUBRIC_LLM=1``), and
  **any** failure (transport, bad JSON, schema violation, empty output) silently falls
  back to the rule-engine rubric. The rubric stage must never break a research run.

The LLM may only ADD task-specific criteria and enrich text; it can never delete a
rule-engine criterion, weaken a programmatic ``check``, or lower a threshold. That
asymmetry is enforced in :func:`_merge_llm_criteria`, so a hallucinating model cannot
relax the grading standard.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from ..platform_contracts.objects import ExecutableRubric
from ..platform_contracts.objects import RubricCriterion
from ..platform_contracts.objects import RubricReview
from .review import review_standard
from .spec import TaskSpec
from .synthesize import synthesize_rubric

logger = logging.getLogger(__name__)

# Criterion ids reserved for the deterministic engine — the LLM must not reuse them.
_LLM_ID_PREFIX = "L"
_MAX_LLM_CRITERIA = 6

_SYSTEM_PROMPT = """你是科研评估标准审查专家。你的唯一任务是为一个自动研究任务补充「任务专属」的可执行评分标准条目。

硬性规则：
1. 只输出 JSON，不要任何解释文字或 markdown 代码块标记。
2. 只能新增条目，不得修改或删除已有条目。
3. 每个条目必须可被证据核验；禁止「充分分析」「深入讨论」这类无法核验的表述。
4. 通过条件必须以「方法与证据的正确性」定义，而不是「指标必须提升」或「假设必须成立」。阴性结论在方法正确时同样合格。
5. 不得引入任务未提供的数据、标签、工具或参考值。
6. 已有条目已覆盖：主指标测得、主指标达标、相对基线改进、评测真实性、泛化性差距、重复测量、结论证据支撑。不要重复这些。

输出 JSON 格式：
{"criteria": [{"requirement": "...", "dimension": "correctness|generalization|rigor|integrity|reporting", "satisfaction_condition": "...", "priority": "high|medium|low", "required_analysis": ["..."], "metrics": ["..."], "comparisons": ["..."], "rationale": "为什么这个任务需要这一条"}]}
"""


def llm_enabled() -> bool:
    """True when the optional LLM refinement pass should be attempted."""

    return os.environ.get("RUBRIC_LLM") == "1"


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from an LLM reply (handles fenced blocks)."""

    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = text[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _build_user_prompt(spec: TaskSpec, base: ExecutableRubric) -> str:
    return json.dumps(
        {
            "任务": {
                "task_id": spec.task_id,
                "name": spec.name,
                "task_type": spec.task_type,
                "modality": spec.modality,
                "研究目标": spec.objective,
                "数据说明": spec.dataset_desc,
                "评估方法": spec.eval_method,
            },
            "已声明评估标准": spec.provided_standard(),
            "标准审查结论": {
                "是否已提供标准": base.review.standard_provided if base.review else False,
                "准确性": base.review.accuracy if base.review else None,
                "完整性": base.review.completeness if base.review else None,
                "科学性": base.review.scientificity if base.review else None,
                "缺陷": [
                    {"严重度": f.severity, "维度": f.dimension, "问题": f.message}
                    for f in (base.review.findings if base.review else [])
                ],
            },
            "已有条目": [
                {"id": c.criterion_id, "要求": c.requirement, "维度": c.dimension}
                for c in base.criteria
            ],
            "请补充": f"最多 {_MAX_LLM_CRITERIA} 条该任务专属的、上面未覆盖的评分条目。",
        },
        ensure_ascii=False,
    )


def _merge_llm_criteria(base: ExecutableRubric, data: dict[str, Any]) -> int:
    """Append validated LLM criteria to *base*. Returns how many were accepted.

    Safety asymmetry (deliberate): LLM criteria are append-only, get their own
    ``L*`` id namespace, always carry ``check={"kind": "judge"}`` (they can never
    claim a programmatic check they did not define), and are capped at ``medium``
    weight so a hallucinated criterion cannot dominate the verdict.
    """

    raw = data.get("criteria")
    if not isinstance(raw, list):
        return 0
    existing_ids = {c.criterion_id for c in base.criteria}
    existing_reqs = {c.requirement.strip() for c in base.criteria}
    goal_ids = [g.goal_id for g in base.goals] or ["G1"]
    accepted = 0
    for item in raw:
        if accepted >= _MAX_LLM_CRITERIA:
            break
        if not isinstance(item, dict):
            continue
        req = str(item.get("requirement") or "").strip()
        sat = str(item.get("satisfaction_condition") or "").strip()
        if len(req) < 8 or len(sat) < 4 or req in existing_reqs:
            continue
        dim = str(item.get("dimension") or "correctness")
        if dim not in ("correctness", "generalization", "rigor", "integrity", "reporting"):
            dim = "correctness"
        pri = str(item.get("priority") or "medium")
        if pri not in ("high", "medium", "low"):
            pri = "medium"
        if pri == "high":
            pri = "medium"  # LLM criteria never outrank the deterministic core
        cid = f"{_LLM_ID_PREFIX}{accepted + 1}"
        while cid in existing_ids:
            cid += "x"
        existing_ids.add(cid)
        existing_reqs.add(req)
        base.criteria.append(
            RubricCriterion(
                criterion_id=cid,
                goal_ids=[goal_ids[0]],
                requirement=req,
                dimension=dim,
                metrics=[str(x) for x in (item.get("metrics") or [])][:6],
                required_analysis=[str(x) for x in (item.get("required_analysis") or [])][:6],
                comparisons=[str(x) for x in (item.get("comparisons") or [])][:6],
                satisfaction_condition=sat,
                priority=pri,
                weight=1.0,
                check={"kind": "judge"},
                provenance={"literature": [str(item.get("rationale") or "LLM 补充")]},
            )
        )
        accepted += 1
    return accepted


class RubricEngine:
    """Deterministic rubric induction + standard review, with an optional LLM pass."""

    def __init__(self, use_llm: bool | None = None, llm_client: Any | None = None) -> None:
        self.use_llm = llm_enabled() if use_llm is None else bool(use_llm)
        self._llm_client = llm_client

    # ------------------------------------------------------------------ review
    def review(self, spec: TaskSpec) -> RubricReview:
        """Audit a declared evaluation standard (accuracy / completeness / scientificity).

        Purely deterministic — it runs inline on registration-form validation, so it
        must never do I/O.
        """

        return review_standard(spec)

    # ------------------------------------------------------------------ induce
    def induce(self, spec: TaskSpec) -> ExecutableRubric:
        """Produce the frozen executable rubric for *spec*.

        Handles both required cases:

        * no usable standard declared -> synthesize one (``source="synthesized"``);
        * standard declared           -> review it, then normalize/extend it into the
          same executable shape (``source="reviewed"``).
        """

        review = self.review(spec)
        rubric = synthesize_rubric(spec, review)
        if not self.use_llm:
            return rubric
        try:
            added = self._llm_refine(spec, rubric)
        except Exception as exc:  # never let the LLM break the rubric stage
            logger.warning("rubric LLM refinement failed, using rule engine only: %s", exc)
            return rubric
        if added:
            rubric.generator = "llm+rule_engine"
            # Re-freeze: the hash must cover the merged criteria.
            from .synthesize import _rubric_hash  # local import: internal helper

            content = rubric.model_dump(mode="json", exclude={"rubric_id", "integrity_hash"})
            h = _rubric_hash(content)
            rubric.integrity_hash = h
            rubric.rubric_id = f"rb-{h[:12]}"
        return rubric

    # ------------------------------------------------------------------ private
    def _llm_refine(self, spec: TaskSpec, rubric: ExecutableRubric) -> int:
        """Ask the LLM for extra task-specific criteria; returns how many were merged."""

        client = self._llm_client
        if client is None:
            from ..execution_plane.llm.client import LLMClient

            client = LLMClient()
        from ..execution_plane.llm.client import ChatMessage

        result = client.chat(
            [
                ChatMessage("system", _SYSTEM_PROMPT),
                ChatMessage("user", _build_user_prompt(spec, rubric)),
            ],
            temperature=0.0,
            max_tokens=1600,
        )
        data = _extract_json(getattr(result, "text", "") or "")
        if not data:
            logger.warning("rubric LLM returned no parseable JSON; using rule engine only")
            return 0
        return _merge_llm_criteria(rubric, data)


# Module-level convenience wrappers (the API layer wants one-liners).
def review_task_standard(spec: TaskSpec) -> RubricReview:
    return RubricEngine(use_llm=False).review(spec)


def induce_rubric(spec: TaskSpec, use_llm: bool | None = None) -> ExecutableRubric:
    return RubricEngine(use_llm=use_llm).induce(spec)
