"""Recursive-improvement meta-loop executor (layer 09, now real).

This is the *outer* loop's second half (AREX / Bilevel Autoresearch): it improves the
research **process**, not the artifact. Concretely it:

  1. extracts the *mechanism carrier* from the run's event/stage trace — which
     capabilities were invoked, in what order, and the best real eval metric;
  2. proposes an ``ImprovementProposal`` (a patch to the routing bias / prompt hint /
     experience bank) that would make the *next* inner loop more effective;
  3. validates the proposal against the **frozen verifier** (Karpathy principle): any
     proposal that touches the eval threshold or data path is rejected and *reverted*;
  4. commits the accepted mechanism to a ``StrategyArchive`` (with a rollback id) and
     emits an ``ImprovementAppliedEvent`` so the meta-loop is auditable and revertible.

The proposal is deliberately conservative: it never degrades the held-out verifier and
only nudges *how* the agent searches. A fuller system would replay a held-out split to
measure the gain; here the validation is a safety/isolation gate plus a proxy metric.
"""

from __future__ import annotations

import os
from typing import Any

from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.events import ImprovementAppliedEvent
from ...platform_contracts.objects import ImprovementProposal
from ...platform_contracts.objects import StageRun
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK
from ...control_plane.store_tree import StrategyArchive


# Keys a proposal may NEVER touch (the held-out verifier is frozen, per Karpathy).
_FROZEN_KEYS = ("threshold", "data_dir", "data_path", "eval_suite", "gold", "target_threshold")


def _extract_mechanism(sdk: PlatformSDK, run_id: str) -> dict[str, Any]:
    """Read the trace and summarize *how* the research was conducted."""

    events = sdk.load_object(f"events:{run_id}")
    stages = sdk.service.list_stage_runs(run_id)
    capability_calls = [
        s.stage_code for s in stages if getattr(s, "executor_family", None) == "capability"
    ]
    eval_events = [e for e in events if e.get("event_type") == "eval_completed"]
    best_acc = max(
        [float(e.get("metrics", {}).get("accuracy", 0.0)) for e in eval_events], default=0.0
    )
    return {
        "capability_calls": capability_calls,
        "best_accuracy": round(best_acc, 4),
        "n_capability_calls": len(capability_calls),
        "has_real_eval": any("kaggle" in str(e.get("report_ref", "")) for e in eval_events),
    }


def _validate_against_freeze(patch: dict[str, Any]) -> tuple[bool, str]:
    """Reject any proposal that would weaken/retarget the frozen verifier."""

    blob = " ".join(str(k).lower() for k in patch.keys())
    for key in _FROZEN_KEYS:
        if key in blob:
            return False, f"proposal touches frozen verifier key '{key}'"
    return True, "ok"


class SelfEvolutionExecutor(StageExecutor):
    """Recursive-improvement meta-loop: improve the search *process* (not the artifact)."""

    stage_codes = ("layer_09_self_iterative_evolution", "self_iterative_evolution", "self_evolution")

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        mechanism = _extract_mechanism(sdk, stage_run.run_id)
        improvement_id = f"imp-{stage_run.stage_run_id}"
        rollback_id = f"rb-{stage_run.stage_run_id}"

        best_cap = next((c for c in reversed(mechanism["capability_calls"])), "kaggle_eval")
        patch: dict[str, Any] = {
            "routing_bias": {best_cap: 0.15},
            "prompt_hint": (
                f"Prefer verifying with {best_cap} before claiming success "
                f"(best cv={mechanism['best_accuracy']})."
            ),
        }
        if params.get("inject_frozen_violation"):
            # Test hook: a proposal that tries to weaken the verifier (must be reverted).
            patch["eval_threshold"] = 0.5

        ok, reason = _validate_against_freeze(patch)
        metrics_before = {"best_accuracy": mechanism["best_accuracy"]}

        proposal = ImprovementProposal(
            improvement_id=improvement_id,
            target_mechanism="routing_bias",
            title="bias search toward the best-performing capability",
            description=(
                f"Inner loop used {mechanism['n_capability_calls']} capability calls; "
                f"best real eval accuracy={mechanism['best_accuracy']}. Nudge the next "
                f"inner loop to prefer {best_cap} early."
            ),
            patch=patch,
            rationale=reason if ok else f"rejected: {reason}",
            expected_effect="faster convergence / fewer wasted capability calls",
            rollback_id=rollback_id,
            proposed_by="self_evolution",
        )

        # Persist the mechanism snapshot so it can be rolled back later.
        archive = StrategyArchive(params.get("strategy_db"))
        archive.commit({"mechanism": mechanism, "patch": patch, "accepted": ok})

        if not ok:
            # validate-and-revert: the degrading proposal is recorded but reverted.
            event = ImprovementAppliedEvent(
                run_id=stage_run.run_id,
                improvement_id=improvement_id,
                target_mechanism="routing_bias",
                proposal_ref=f"improvement://{stage_run.stage_run_id}",
                rollback_id=rollback_id,
                validated_heldout=False,
                metrics_before=metrics_before,
                metrics_after=metrics_before,
                reverted=True,
            )
            sdk.emit_event(event)
            return ExecResult(
                final_status=StageStatus.FAILED,
                gate_result=GateResult.FAILED,
                event=event,
                output_refs=[],
                detail=f"self-evolution REVERTED: {reason}",
            )

        metrics_after = dict(metrics_before)  # proxy: process validated, not metric-optimized
        event = ImprovementAppliedEvent(
            run_id=stage_run.run_id,
            improvement_id=improvement_id,
            target_mechanism="routing_bias",
            proposal_ref=f"improvement://{stage_run.stage_run_id}",
            rollback_id=rollback_id,
            validated_heldout=mechanism["has_real_eval"],
            metrics_before=metrics_before,
            metrics_after=metrics_after,
            reverted=False,
        )
        sdk.emit_event(event)
        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": "improvement_report",
                "uri": f"improvement://{stage_run.stage_run_id}",
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={"target_mechanism": "routing_bias", "rollback_id": rollback_id},
        )
        sdk.record_metric(
            stage_run.run_id, "improvement.accepted", 1.0, tags={"mechanism": "routing_bias"}
        )
        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=(
                f"self-evolution applied (mechanism=routing_bias, rollback={rollback_id}); "
                f"best cv={mechanism['best_accuracy']}"
            ),
        )
