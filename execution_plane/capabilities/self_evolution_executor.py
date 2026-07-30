"""Recursive-improvement meta-loop executor (layer 09, now real).

This is the *outer* loop's second half (AREX / Bilevel Autoresearch): it improves the
research **process**, not the artifact. Concretely it:

  1. extracts the *mechanism carrier* from the run's event/stage trace — which
     capabilities were invoked, in what order, and the best real eval metric;
  2. proposes an ``ImprovementProposal`` carrying a **bounded, concrete param patch**
     (Self-Harness-style: the editable surface is an explicit whitelist) plus a
     **falsifiable prediction** (AHE-style decision observability: metric / baseline /
     minimum expected delta);
  3. validates the proposal against the **frozen verifier** (Karpathy principle): any
     proposal that touches the eval threshold or data path is rejected and *reverted*;
  4. commits the accepted mechanism to the shared ``StrategyArchive`` (run-scoped,
     ``pending_verification`` status) so the orchestrator can *apply* the patch to the
     next inner loop and *verify* the prediction against the actual next metric —
     closing the propose → apply → evaluate → accept/rollback loop.

The proposal is deliberately conservative: it never degrades the held-out verifier and
only nudges *how* the agent searches (feature level / model family / cv folds).
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

# Editable surface (AHE-style component observability): the ONLY inner-loop params the
# meta-loop may patch, with their allowed value sets. Anything outside this surface is
# rejected — this keeps the proposal bounded and the action space explicitly traceable.
_EDITABLE_SURFACE: dict[str, tuple[Any, ...]] = {
    "fe": ("basic", "rich"),
    "model": ("gbm", "gbm-strong", "hgb", "rf", "logreg"),
    "cv_folds": (2, 3, 5, 8, 10),
}

# Post-hoc verification tolerance: a patch is falsified when the actual metric regresses
# more than this below the baseline recorded at proposal time.
VERIFY_TOLERANCE = 0.002


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

    def _walk_keys(d: dict[str, Any]) -> str:
        parts: list[str] = []
        for k, v in d.items():
            parts.append(str(k).lower())
            if isinstance(v, dict):
                parts.append(_walk_keys(v))
        return " ".join(parts)

    blob = _walk_keys(patch)
    for key in _FROZEN_KEYS:
        if key in blob:
            return False, f"proposal touches frozen verifier key '{key}'"
    return True, "ok"


def _validate_editable_surface(param_patch: dict[str, Any]) -> tuple[bool, str]:
    """Reject param patches outside the explicit editable surface (bounded proposal)."""

    for key, value in param_patch.items():
        allowed = _EDITABLE_SURFACE.get(key)
        if allowed is None:
            return False, f"param '{key}' is outside the editable surface {sorted(_EDITABLE_SURFACE)}"
        if value not in allowed:
            return False, f"value {value!r} for '{key}' not in allowed set {allowed}"
    return True, "ok"


def _propose_param_patch(inner_params: dict[str, Any]) -> tuple[dict[str, Any], float, str]:
    """Conservative deterministic policy: one bounded nudge at a time.

    Returns ``(param_patch, predicted_min_delta, rationale)``. An empty patch means the
    current configuration is already at the surface's strongest point (no-op proposal).
    """

    cur_fe = (inner_params.get("fe") or "basic").lower()
    cur_model = (inner_params.get("model") or "gbm").lower()
    cur_cv = int(inner_params.get("cv_folds", 5) or 5)
    if cur_fe == "basic":
        return {"fe": "rich"}, 0.003, "richer feature engineering usually lifts CV accuracy"
    if cur_model in ("gbm", "gradientboosting", "gb"):
        return {"model": "gbm-strong"}, 0.002, "HistGB defaults measured stronger on tabular presets"
    if cur_model in ("rf", "randomforest"):
        return {"model": "gbm"}, 0.002, "GBM usually edges out RF on these tabular presets"
    if cur_cv < 5:
        return {"cv_folds": 5}, 0.0, "more folds reduce CV variance (stability, not accuracy)"
    return {}, 0.0, "configuration already at the editable surface's strongest point"


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

        inner_params = dict(params.get("inner_params") or {})
        param_patch, min_delta, policy_note = _propose_param_patch(inner_params)

        best_cap = next((c for c in reversed(mechanism["capability_calls"])), "kaggle_eval")
        patch: dict[str, Any] = {
            "routing_bias": {best_cap: 0.15},
            "prompt_hint": (
                f"Prefer verifying with {best_cap} before claiming success "
                f"(best cv={mechanism['best_accuracy']})."
            ),
            "param_patch": param_patch,
        }
        if params.get("inject_frozen_violation"):
            # Test hook: a proposal that tries to weaken the verifier (must be reverted).
            patch["eval_threshold"] = 0.5

        ok, reason = _validate_against_freeze(patch)
        if ok and param_patch:
            ok, reason = _validate_editable_surface(param_patch)

        # Falsifiable prediction (AHE decision observability): what we expect this edit
        # to do, verified by the orchestrator against the NEXT inner-loop metric.
        prediction = {
            "metric": "accuracy",
            "direction": "higher",
            "baseline": mechanism["best_accuracy"],
            "min_delta": min_delta,
            "tolerance": VERIFY_TOLERANCE,
            "claim": (
                f"applying {param_patch} should not regress accuracy below "
                f"{mechanism['best_accuracy']} - {VERIFY_TOLERANCE}"
            ),
        }
        metrics_before = {"best_accuracy": mechanism["best_accuracy"]}

        proposal = ImprovementProposal(
            improvement_id=improvement_id,
            target_mechanism="routing_bias",
            title="bounded param nudge toward the best-performing configuration",
            description=(
                f"Inner loop used {mechanism['n_capability_calls']} capability calls; "
                f"best real eval accuracy={mechanism['best_accuracy']}. "
                f"Proposed param patch: {param_patch or '(no-op)'} — {policy_note}."
            ),
            patch=patch,
            rationale=reason if ok else f"rejected: {reason}",
            expected_effect=prediction["claim"],
            rollback_id=rollback_id,
            proposed_by="self_evolution",
        )

        # Persist the mechanism snapshot in the SHARED archive when the orchestrator
        # wired one (so it can apply + verify), else fall back to a standalone db.
        shared_store = getattr(sdk, "state_store", None)
        archive = (
            shared_store.strategy_archive
            if shared_store is not None
            else StrategyArchive(params.get("strategy_db"))
        )
        archive.commit(
            {
                "mechanism": mechanism,
                "patch": patch,
                "accepted": ok,
                "run_id": stage_run.run_id,
                "inner_param_patch": param_patch,
                "prediction": prediction,
                "status": "pending_verification" if (ok and param_patch) else (
                    "no_op" if ok else "rejected"
                ),
            }
        )

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

        metrics_after = dict(metrics_before)  # actual delta measured post-hoc by orchestrator
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
                f"self-evolution proposed param patch {param_patch or '(no-op)'} "
                f"(rollback={rollback_id}); best cv={mechanism['best_accuracy']}"
            ),
        )
