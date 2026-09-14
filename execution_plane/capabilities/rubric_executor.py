"""Rubric-induction capability — the run's grading contract (layer 12).

This is the *evaluation-standard* stage of the research loop. It runs ONCE per run,
BEFORE any inner-loop attempt, and answers "按什么标准判定这次研究成功":

* the task did **not** declare a usable evaluation standard -> a task-specific,
  scientific, machine-checkable rubric is **synthesized**;
* the task **did** declare one -> the standard is **reviewed** along three dimensions
  (准确性 / 完整性 / 科学性), its defects are reported, and it is normalized into the
  same executable rubric shape (so a weak-but-present standard is strengthened rather
  than blindly trusted).

Isolation invariants this executor is bound by
----------------------------------------------
1. **Control-plane only.** ``layer_12_rubric_induction`` is in
   ``OUTER_LOOP_RESERVED_CAPS``, so an inner-loop agent can never invoke it: an
   optimizing agent must not be able to (re)write the standard it is graded against.
2. **Produced before results, frozen after.** The rubric is built from the task
   definition only — never from measured metrics — so no post-hoc standard fitting is
   possible. ``integrity_hash`` pins the content and ``frozen=True`` marks it final.
3. **Read-only downstream.** The orchestrator hands the rubric to the inner loop as an
   execution contract (AutoSciRub's key mechanism) and to ``layer_11`` as its
   constraint set. Neither may mutate it.

Determinism: the default path is the pure rule engine (no network). ``RUBRIC_LLM=1``
enables an append-only LLM refinement pass that degrades silently on any failure.
"""

from __future__ import annotations

import logging
from typing import Any

from ...platform_contracts.enums import ArtifactType
from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.events import RubricSynthesizedEvent
from ...platform_contracts.objects import ExecutableRubric
from ...platform_contracts.objects import StageRun
from ...rubric import RubricEngine
from ...rubric import TaskSpec
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK

logger = logging.getLogger(__name__)

# Below this overall review score the task's declared standard is considered too weak
# to grade a research run against, and the stage gate FAILS — a loud signal that the
# task definition needs fixing (the run still proceeds with the synthesized rubric,
# which is strictly stronger than the broken standard).
REVIEW_GATE_THRESHOLD = 0.6


def build_task_spec(params: dict[str, Any], objective_snapshot: dict[str, Any]) -> TaskSpec:
    """Resolve the rubric engine's input from whatever the caller supplied.

    Precedence: an explicit ``task_spec`` in params (already a normalized dict), then a
    ``(task_type, values)`` registration pair, then the run's ``objective_snapshot``.
    """

    explicit = params.get("task_spec")
    if isinstance(explicit, dict) and explicit:
        spec = TaskSpec.from_benchmark_dict(explicit)
    elif params.get("task_type") and isinstance(params.get("values"), dict):
        spec = TaskSpec.from_registration(str(params["task_type"]), dict(params["values"]))
    else:
        spec = TaskSpec.from_objective_snapshot(objective_snapshot or {})
    # The EFFECTIVE inner-loop configuration (threshold / cv_folds actually in force for
    # this run) may differ from — or be absent in — the objective snapshot. Folding it in
    # keeps the gate and repeated-measurement criteria *evaluable*; without it they
    # degrade to blocked criteria and the rubric cannot check the run's real gate.
    eff = params.get("effective_config")
    if isinstance(eff, dict):
        for key, value in eff.items():
            if value is not None:
                spec.type_config.setdefault(key, value)
    return spec


class RubricInductionExecutor(StageExecutor):
    """Induce / review the task-specific executable scoring rubric (layer 12)."""

    stage_codes = ("layer_12_rubric_induction", "rubric_induction", "rubric")

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        run = sdk.load_object(f"run:{stage_run.run_id}")
        snapshot = getattr(run, "objective_snapshot", None) or {}
        spec = build_task_spec(params, snapshot)
        if not spec.task_id:
            spec.task_id = getattr(run, "target_id", "") or stage_run.run_id
        if params.get("objective"):
            spec.objective = str(params["objective"])

        use_llm = params.get("use_llm")
        engine = RubricEngine(
            use_llm=None if use_llm is None else bool(use_llm),
            llm_client=params.get("llm_client"),
        )
        rubric: ExecutableRubric = engine.induce(spec)
        review = rubric.review
        report_ref = f"rubric://{stage_run.stage_run_id}"

        blocking = len(review.blocking_findings()) if review else 0
        # Gate semantics: PASSED means "the run has a trustworthy grading contract".
        # A synthesized rubric passes (the platform supplied what the task lacked); a
        # *reviewed* standard whose audit is critically broken fails loudly.
        gate_passed = True
        gate_detail = ""
        if review is not None and review.standard_provided:
            if blocking:
                gate_passed = False
                gate_detail = (
                    f"任务已声明的评估标准存在 {blocking} 项严重缺陷，"
                    "已按审查结论生成更强的可执行标准并继续，但任务定义需要修正。"
                )
            elif review.overall < REVIEW_GATE_THRESHOLD:
                gate_passed = False
                gate_detail = (
                    f"任务已声明的评估标准审查总分 {review.overall:.2f} "
                    f"低于门限 {REVIEW_GATE_THRESHOLD}（{review.verdict}）。"
                )

        event = RubricSynthesizedEvent(
            run_id=stage_run.run_id,
            rubric_id=rubric.rubric_id,
            task_id=rubric.task_id,
            stage_run_id=stage_run.stage_run_id,
            source=rubric.source,
            generator=rubric.generator,
            criteria_count=len(rubric.criteria),
            standard_provided=bool(review.standard_provided) if review else False,
            review_overall=float(review.overall) if review else 0.0,
            review_verdict=str(review.verdict) if review else "acceptable",
            blocking_findings=blocking,
            integrity_hash=rubric.integrity_hash,
            report_ref=report_ref,
        )
        sdk.emit_event(event)

        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": ArtifactType.RUBRIC.value,
                "uri": report_ref,
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
                "integrity_hash": f"sha256:{rubric.integrity_hash[:16]}",
            },
            schema_version=rubric.schema_version,
            metadata={
                "rubric_id": rubric.rubric_id,
                "source": rubric.source,
                "generator": rubric.generator,
                "criteria_count": len(rubric.criteria),
                "review_overall": float(review.overall) if review else 0.0,
                "review_verdict": str(review.verdict) if review else "",
                # The full rubric travels in the artifact metadata so the run record is
                # self-contained (no side store needed to reconstruct the contract).
                "rubric": rubric.model_dump(mode="json"),
            },
        )
        if review is not None:
            sdk.record_metric(
                stage_run.run_id, "rubric.review_overall", float(review.overall),
                tags={"verdict": review.verdict, "source": rubric.source},
            )
            sdk.record_metric(
                stage_run.run_id, "rubric.criteria_count", float(len(rubric.criteria)),
                tags={"generator": rubric.generator},
            )

        action = "审查并规范化已提供标准" if rubric.source == "reviewed" else "生成任务专属评分标准"
        detail = (
            f"{action}: rubric={rubric.rubric_id} 条目={len(rubric.criteria)} "
            f"(可程序化判定 {sum(1 for c in rubric.criteria if (c.check or {}).get('kind') != 'judge')} 条) "
            f"| 审查 {review.verdict if review else 'n/a'} "
            f"总分={review.overall if review else 0:.2f} "
            f"(准确性 {review.accuracy if review else 0:.2f}/"
            f"完整性 {review.completeness if review else 0:.2f}/"
            f"科学性 {review.scientificity if review else 0:.2f})"
        )
        if gate_detail:
            detail = f"{detail} | {gate_detail}"

        return ExecResult(
            # The stage itself always *runs* successfully; the standard's quality is
            # reported through the gate, never by pretending the stage crashed.
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED if gate_passed else GateResult.FAILED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=detail,
            # The orchestrator needs the frozen rubric in-process (inner-loop contract +
            # layer_11 constraint set) within this same run.
            payload={"rubric": rubric.model_dump(mode="json")},
        )
