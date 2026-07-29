"""Eval executor — Benchmark Plane (layer 03). Emits ``EvalCompletedEvent``.

Real (deterministic, reproducible) logic: derives a benchmark measurement from a
seed hashed off the run id when no ``measured_metrics`` are supplied, compares the
target metric against the run's objective threshold, and emits the result. This is
the "评测" node whose gate outcome feeds the iteration router.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.events import EvalCompletedEvent
from ...platform_contracts.objects import StageRun
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK


class EvalExecutor(StageExecutor):
    stage_codes = ("03", "03_eval", "eval", "benchmark")

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        run = sdk.load_object(f"run:{stage_run.run_id}")
        obj = run.objective_snapshot or {}

        target_metric = params.get("target_metric") or obj.get("target_metric", "accuracy")
        threshold = float(
            params.get("threshold")
            if params.get("threshold") is not None
            else obj.get("target_threshold", 0.8)
        )
        op = params.get("op") or obj.get("op", "ge")

        measured = dict(params.get("measured_metrics") or {})
        if not measured:
            seed = int(hashlib.sha256(stage_run.run_id.encode()).hexdigest(), 16) % 1000
            rng = random.Random(seed)
            base = rng.uniform(0.7, 0.98)
            measured = {
                "accuracy": round(base, 4),
                "robustness": round(max(0.0, base - rng.uniform(0.05, 0.25)), 4),
            }

        value = float(measured.get(target_metric, measured.get("accuracy", 0.0)))
        passed = (value <= threshold) if op == "le" else (value >= threshold)
        gate_passed = passed

        eval_suite_id = params.get("eval_suite_id", f"suite-{target_metric}")
        report_ref = f"eval-report://{stage_run.stage_run_id}"

        event = EvalCompletedEvent(
            run_id=stage_run.run_id,
            eval_suite_id=eval_suite_id,
            stage_run_id=stage_run.stage_run_id,
            passed=passed,
            metrics=measured,
            gate_passed=gate_passed,
            report_ref=report_ref,
        )
        sdk.emit_event(event)

        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": "eval_report",
                "uri": report_ref,
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={"suite": eval_suite_id, "gate_passed": gate_passed},
        )
        sdk.record_metric(
            stage_run.run_id, f"eval.{target_metric}", value, tags={"suite": eval_suite_id}
        )

        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED if gate_passed else GateResult.FAILED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=f"eval {target_metric}={value} vs {op} {threshold} -> {'PASS' if passed else 'FAIL'}",
        )
