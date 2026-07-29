"""Lesson executor — experience -> reinjection (layer 08). Emits ``LessonPromotedEvent``.

Reads the run's event log + decisions, extracts the dominant failure / vulnerability
pattern, and promotes it into a platform ``LessonCard`` via ``sdk.register_lesson``.
The emitted ``LessonPromotedEvent`` is the reinjection signal that makes the next
round "know" what was learned — closing the loop.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.objects import StageRun
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK


def _as_dict(obj: Any) -> dict[str, Any]:
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


class LessonExecutor(StageExecutor):
    stage_codes = ("08", "08_result_analysis_experience", "lesson", "experience", "result_analysis")

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        raw_events = sdk.load_object(f"events:{stage_run.run_id}")
        raw_decisions = sdk.load_object(f"decisions:{stage_run.run_id}")
        # events come back as dicts; decisions as pydantic objects — normalize both.
        events = [_as_dict(e) for e in raw_events]
        decisions = [_as_dict(d) for d in raw_decisions]

        vuln_patterns: list[str] = []
        reason_codes: list[str] = []
        for e in events:
            if e.get("event_type") == "attack_completed":
                vuln_patterns.extend(e.get("vulnerability_patterns", []))
            if e.get("event_type") == "eval_completed" and not e.get("gate_passed"):
                reason_codes.append("eval_gate_failed")
        for d in decisions:
            reason_codes.extend(d.get("reason_codes", []))

        counter = Counter(reason_codes + vuln_patterns)
        if counter:
            pattern, freq = counter.most_common(1)[0]
            pattern_type = "vulnerability" if pattern in vuln_patterns else "decision_reason"
        else:
            pattern = params.get("pattern", "generic_improvement")
            pattern_type = "generic"
            freq = 1

        scope = params.get("scope", "adversarial_hardening")
        applicable_stages = params.get(
            "applicable_stages",
            ["10_adversarial_data_generation", "05_data_evaluation_cleaning"],
        )
        # Higher frequency of the same pattern -> higher confidence / PRM score.
        confidence = round(min(1.0, 0.6 + 0.1 * freq), 4)
        prm_score = round(min(1.0, 0.5 + 0.12 * freq), 4)
        payload_ref = f"lesson-payload://{stage_run.stage_run_id}"

        event = sdk.register_lesson(
            {
                "scope": scope,
                "source_run_id": stage_run.run_id,
                "pattern_type": pattern_type,
                "applicable_stages": applicable_stages,
                "confidence": confidence,
                "prm_score": prm_score,
                "payload_ref": payload_ref,
            }
        )

        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED,
            event=event,
            output_refs=[event.lesson_id],
            detail=f"promoted lesson {event.lesson_id} pattern={pattern} freq={freq}",
        )
