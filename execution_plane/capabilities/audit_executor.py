"""External audit capability — the OUTER loop's independent verifier (layer 11).

Implements AREX-style constraint-wise audit of an inner-loop answer ``r = (y, E, s)``:

  * the objective is decomposed into checkable constraints;
  * each constraint is verified / partial / conflict / missing using **programmatic
    checks first** (real eval metrics, data-integrity signals) and an LLM-judge proxy
    for argument-quality / claim-support (injected via ``judge`` — defaults to a
    deterministic stand-in so the platform is reproducible without a network);
  * it emits an ``AuditReport`` object + a real ``AuditCompletedEvent`` carrying the audit
    confidence ``s`` and recoverability ``v`` that drive Accept / Refine / Restart.

The auditor runs with an *independent* confidence from the inner loop, breaking
self-confirmation. For the demo the judge is deterministic; a production deployment
passes a ``judge`` that calls a separate model in a separate context.
"""

from __future__ import annotations

import os
from typing import Any
from typing import Callable

from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.events import AuditCompletedEvent
from ...platform_contracts.objects import AuditReport
from ...platform_contracts.objects import ExecutableRubric
from ...platform_contracts.objects import StageRun
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK


# Tunable thresholds (AREX decision law).
ACCEPT_THRESHOLD = 0.8
RESTART_FLOOR = 0.4


def _default_judge(answer: str, claim: str, evidence: str) -> float:
    """Deterministic stand-in for an LLM-judge (no network; reproducible).

    A real deployment injects a ``judge`` callable that queries a *separate* model
    in a *separate* context. The signature is identical so the swap is a one-liner.
    """

    a = (answer or "").lower()
    e = (evidence or "").lower()
    c = (claim or "").lower()
    # Heuristic: the claim's key tokens should be visible in the evidence or answer.
    tokens = [t for t in c.replace(":", " ").split() if len(t) > 3]
    if not tokens:
        return 0.5
    hits = sum(1 for t in tokens if t in e or t in a)
    return round(min(1.0, 0.35 + 0.65 * hits / len(tokens)), 3)


def _status_from_score(score: float) -> str:
    if score >= 0.7:
        return "verified"
    if score >= 0.4:
        return "partial"
    return "missing"


def _resolve_rubric(params: dict[str, Any], audit_input: dict[str, Any]) -> ExecutableRubric | None:
    """Load the run's frozen executable rubric from the curated audit input.

    Accepts either an already-constructed :class:`ExecutableRubric` or its serialized
    dict, from ``params["rubric"]`` or ``audit_input["rubric"]``. A malformed rubric is
    ignored (the audit falls back to its built-in constraints) rather than crashing the
    outer loop — the audit must always produce a verdict.
    """

    raw = params.get("rubric") or (audit_input or {}).get("rubric")
    if raw is None:
        return None
    if isinstance(raw, ExecutableRubric):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return ExecutableRubric.model_validate(raw)
    except Exception:  # noqa: BLE001 - a bad rubric must not break the audit
        return None


def evaluate_constraint(
    claim: str,
    judge: Callable[[str, str, str], float],
    answer: str,
    evidence: str,
) -> tuple[float, str]:
    """Re-score a single claim/constraint with the (deterministic) judge.

    Shared by the F6 follow-up endpoint: a researcher supplies a *clarification*
    that addresses one audit constraint, and this recomputes that constraint's
    score/status without re-running the full outer audit. Mirrors the claim-support
    scoring path inside :meth:`AuditExecutor.execute`.

    Returns ``(score, status)`` where ``status`` is one of
    ``verified`` / ``partial`` / ``missing`` (see :func:`_status_from_score`).
    """

    score = float(judge(answer, claim, evidence))
    return score, _status_from_score(score)


class AuditExecutor(StageExecutor):
    """Constraint-wise external audit of an inner-loop answer."""

    stage_codes = ("layer_11_external_audit", "external_audit", "audit")

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        judge: Callable[[str, str, str], float] = params.get("judge") or _default_judge
        if params.get("judge") is None and os.environ.get("LLM_AUDIT_JUDGE") == "1":
            # Opt-in real LLM judge for claim support (Phase 2). Falls back to the
            # deterministic heuristic on any failure — the audit must always verdict.
            from ...control_plane.llm_judge import judge_url as _judge_url
            from ...control_plane.llm_judge import make_claim_judge

            if _judge_url():
                judge = make_claim_judge(_default_judge)

        run = sdk.load_object(f"run:{stage_run.run_id}")
        obj = run.objective_snapshot or {}
        objective = params.get("objective") or obj.get("goal") or obj.get("objective") or run.target_id

        # ---- OUTER-LOOP CONTEXT: prefer the control-plane-CURATED audit input ----
        # The orchestrator assembles this from the inner loop's *result* (metrics / verdict)
        # plus the objective and the outer loop's own prior verdicts. It deliberately
        # EXCLUDES the inner loop's experimental narrative (hypotheses explored, experiences
        # registered, lessons) so the auditor's judgment cannot be biased by *how* the answer
        # was produced. This is the mechanism that keeps the outer audit scientifically
        # independent and breaks self-confirmation.
        # (Legacy fallback: scan only eval_completed from the event log if no curated input.)
        audit_input = params.get("audit_input") or {}
        prior_audits = list(audit_input.get("prior_audits", [])) if audit_input else []
        if audit_input:
            last_metrics = dict(audit_input.get("result_metrics", {}) or {})
            eval_gate = bool(audit_input.get("result_gate_passed", False))
            report_ref = audit_input.get("result_report_ref")
            real_eval = bool(
                audit_input.get(
                    "result_real_eval", bool(report_ref and "kaggle" in str(report_ref or ""))
                )
            )
            has_eval = True
        else:
            events = sdk.load_object(f"events:{stage_run.run_id}")
            eval_events = [e for e in events if e.get("event_type") == "eval_completed"]
            last_eval = eval_events[-1] if eval_events else None
            last_metrics = (last_eval or {}).get("metrics", {})
            eval_gate = bool(last_eval.get("gate_passed")) if last_eval else False
            report_ref = last_eval.get("report_ref") if last_eval else None
            real_eval = bool(last_eval and "kaggle" in str(last_eval.get("report_ref", "")))
            has_eval = last_eval is not None

        # The auditor's "answer" is the inner-loop *result* only — plus, legitimately, the
        # outer loop's own prior verdicts (cross-round outer-loop context). It never ingests
        # inner-loop narrative text, so evaluation bias cannot enter through the prompt.
        answer = params.get("answer") or f"inner-loop result for {objective}"
        if prior_audits:
            trend = "; ".join(
                f"{a.get('recommendation')}@s={a.get('confidence')}" for a in prior_audits[-3:]
            )
            answer = f"{answer}\n[prior outer-loop audits: {trend}]"

        threshold = float(params.get("threshold", obj.get("audit_threshold", ACCEPT_THRESHOLD)))
        restart_floor = float(params.get("restart_floor", RESTART_FLOOR))

        # ---- decompose objective into checkable constraints ----
        constraints: list[dict[str, Any]] = []

        # (1) primary metric meets threshold — programmatic, highest weight
        if has_eval:
            m_acc = last_metrics.get("accuracy")
            ev_score = 1.0 if eval_gate else 0.3
            constraints.append({
                "id": "primary_metric",
                "description": f"primary metric meets threshold ({threshold})",
                "status": "verified" if eval_gate else "conflict",
                "evidence_ref": report_ref,
                "score": ev_score,
                "note": f"cv accuracy={m_acc}",
            })
        else:
            constraints.append({
                "id": "primary_metric",
                "description": "primary metric measured by a real eval",
                "status": "missing",
                "evidence_ref": None,
                "score": 0.0,
                "note": "no eval_completed event found",
            })

        # (2) evaluation is genuine, not mocked — programmatic
        constraints.append({
            "id": "eval_is_real",
            "description": "evaluation is a real (non-mocked) measurement",
            "status": "verified" if real_eval else "partial",
            "evidence_ref": report_ref,
            "score": 1.0 if real_eval else 0.4,
            "note": "report_ref indicates real kaggle/sklearn eval" if real_eval
            else "no real-eval signal in event log",
        })

        # (2b) held-out generalization check (Phase 2) — programmatic, deterministic.
        # The CV number feeds the inner loop; the held-out number is a one-shot,
        # never-optimized measurement. A large CV→held-out gap signals overfitting
        # (Weng: 'overly optimistic' failure mode / p-hacking guard).
        ho_acc = last_metrics.get("heldout_accuracy") if has_eval else None
        # I8 fix: also require a CV reference metric — without it the gap is
        # meaningless (a missing "accuracy" key must not silently verify).
        cv_ref = last_metrics.get("accuracy") if has_eval else None
        if isinstance(ho_acc, (int, float)) and isinstance(cv_ref, (int, float)):
            cv_acc = float(cv_ref)
            gap = round(cv_acc - float(ho_acc), 4)
            if gap <= 0.03:
                ho_score, ho_status = 1.0, "verified"
            elif gap <= 0.05:
                ho_score, ho_status = 0.7, "partial"
            elif gap <= 0.08:
                ho_score, ho_status = 0.4, "partial"
            else:
                ho_score, ho_status = 0.1, "conflict"
            constraints.append({
                "id": "heldout_consistency",
                "description": f"held-out generalization gap within tolerance (gap={gap})",
                "status": ho_status,
                "evidence_ref": report_ref,
                "score": ho_score,
                "note": f"cv={cv_acc} vs heldout={ho_acc}",
            })

        # (3) claims supported by evidence — LLM-judge proxy (sees ONLY the curated result)
        claim_support = judge(answer, "claims are supported by the reported evidence", str(last_metrics))
        constraints.append({
            "id": "claims_supported",
            "description": "inner-loop claims are supported by evidence",
            "status": _status_from_score(claim_support),
            "evidence_ref": report_ref,
            "score": claim_support,
            "note": f"judge(score)={claim_support}",
        })

        # (4) TASK-SPECIFIC EXECUTABLE RUBRIC (layer_12) — the run's grading contract.
        # The rubric was induced from the task DEFINITION before any result existed and
        # then frozen, so grading against it cannot be post-hoc standard fitting. Each
        # criterion is evaluated PROGRAMMATICALLY against the curated metrics wherever a
        # ``check`` spec exists; only criteria that genuinely have no machine check fall
        # back to the claim judge (and say so via ``evaluated_by``).
        rubric = _resolve_rubric(params, audit_input)
        rubric_summary: dict[str, Any] = {}
        if rubric is not None and rubric.criteria:
            from ...rubric.checks import evaluate_criteria as _eval_criteria
            from ...rubric.checks import summarize as _summarize_criteria

            rubric_ctx = {
                "metrics": dict(last_metrics or {}),
                "report_ref": report_ref,
                "real_eval": real_eval,
                "artifact_types": list(
                    (audit_input or {}).get("artifact_types", []) or []
                ),
                "config": dict((audit_input or {}).get("result_config", {}) or {}),
            }
            rubric_constraints = _eval_criteria(
                rubric.criteria, rubric_ctx, judge=judge, answer=answer
            )
            constraints.extend(rubric_constraints)
            rubric_summary = _summarize_criteria(rubric_constraints)
            rubric_summary["rubric_id"] = rubric.rubric_id
            rubric_summary["source"] = rubric.source

        # (5) any caller-supplied extra constraints (e.g. from the research objective)
        extra_constraints = list(params.get("constraints", []) or [])
        if audit_input:
            extra_constraints += list(audit_input.get("constraints", []) or [])
        for extra in extra_constraints:
            cs = judge(answer, str(extra), str(last_metrics))
            constraints.append({
                "id": f"extra:{len(constraints)}",
                "description": str(extra),
                "status": _status_from_score(cs),
                "evidence_ref": None,
                "score": cs,
                "note": f"judge(score)={cs}",
            })

        # ---- aggregate confidence s ----
        # The built-in ``primary_metric`` constraint keeps its x2 weight; rubric criteria
        # carry their own priority-scaled weight so a "high"-priority criterion (e.g. the
        # generalization-gap check) actually moves the verdict.
        #
        # Rubric criteria that could NOT be decided on evidence (``evaluable=False``: the
        # requirement is blocked by a task-definition gap, or the needed metric was never
        # reported) get weight 0. They remain listed as unmet in the criterion-level
        # report, but they must not depress the *research's* score — that gap belongs to
        # the task definition and is already reported by the rubric review and layer_12's
        # gate. Net effect: adding a rubric can only tighten a verdict when a real check
        # actually fails, never merely because data is absent.
        def _weight(c: dict[str, Any]) -> float:
            if c.get("id") == "primary_metric":
                return 2.0
            if c.get("criterion_id"):
                if not c.get("evaluable", True):
                    return 0.0
                scale = {"high": 2.0, "medium": 1.0, "low": 0.5}
                return float(c.get("weight", 1.0)) * scale.get(str(c.get("priority")), 1.0)
            return 1.0

        weights = [_weight(c) for c in constraints]
        total_w = sum(weights) or 1.0
        confidence = round(sum(c["score"] * w for c, w in zip(constraints, weights)) / total_w, 4)

        unresolved = [c["description"] for c in constraints if c["status"] != "verified"]
        rejected = list(params.get("rejected_candidates", []) or [])
        recoverable = has_eval  # can refine if we have a result to build on
        # I4 fix: make the Restart branch reachable — a trajectory that has already
        # consumed several consecutive REFINE verdicts without converging is no
        # longer recoverable (refining forever just burns budget on a dead end).
        if recoverable and prior_audits:
            streak = 0
            for a in reversed(prior_audits):
                if a.get("recommendation") in ("revisit", "refine"):
                    streak += 1
                else:
                    break
            if streak >= 3:
                recoverable = False

        # ---- HARD-FAILURE VETO: a decisive failed check blocks Accept ----
        # A single aggregate confidence can average a hard failure away: adding more
        # easy-to-pass constraints *dilutes* the one that actually matters (an 0.18
        # CV→held-out gap, or a primary metric below its gate). The rubric exists
        # precisely to report *which* criterion is unmet, so any decisive check that RAN
        # and FAILED (``conflict``) vetoes Accept regardless of the scalar:
        #
        #   * the built-in ``primary_metric`` constraint (the run's own gate), and
        #   * any HIGH-priority rubric criterion.
        #
        # ``missing`` / ``partial`` are NOT vetoes — an undecidable criterion must not
        # deadlock the loop (see checks.py rule 2); it is reported, not punished.
        rubric_veto = [
            c for c in constraints
            if c.get("status") == "conflict"
            and (
                c.get("id") == "primary_metric"
                or (c.get("criterion_id") and c.get("priority") == "high")
            )
        ]

        # ---- Accept / Refine / Restart (AREX decision law) ----
        if confidence >= threshold and not rubric_veto:
            recommendation = "accept"
            gate_passed = True
        elif not recoverable:
            recommendation = "restart"
            gate_passed = False
        else:
            recommendation = "refine"
            gate_passed = False
        if rubric_veto:
            veto_note = "硬性判定条目未通过: " + "; ".join(
                f"{c.get('criterion_id') or c.get('id')}({c.get('dimension') or 'gate'})"
                for c in rubric_veto[:4]
            )
            # Surface the veto as an unresolved claim so refine folds it into the goal.
            unresolved = [veto_note] + [u for u in unresolved if u != veto_note]

        audit_id = f"audit-{stage_run.stage_run_id}"
        report_ref = f"audit-report://{stage_run.stage_run_id}"
        report = AuditReport(
            audit_id=audit_id,
            audited_target=objective,
            constraints=constraints,
            unresolved_claims=unresolved,
            rejected_candidates=rejected,
            confidence=confidence,
            recoverable=recoverable,
            audit_confidence=0.9,
            recommendation=recommendation,
            report_ref=report_ref,
        )
        event = AuditCompletedEvent(
            run_id=stage_run.run_id,
            audit_id=audit_id,
            audited_target=objective,
            constraints=constraints,
            unresolved_claims=unresolved,
            rejected_candidates=rejected,
            confidence=confidence,
            recoverable=recoverable,
            audit_confidence=0.9,
            gate_passed=gate_passed,
            report_ref=report_ref,
        )
        sdk.emit_event(event)
        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": "audit_report",
                "uri": report_ref,
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={
                "recommendation": recommendation,
                "confidence": confidence,
                # Criterion-level verdict (AutoSciRub verification-report style): which
                # criteria passed / failed, not just the scalar.
                "rubric_summary": rubric_summary,
            },
        )
        sdk.record_metric(stage_run.run_id, "audit.confidence", confidence, tags={"recommendation": recommendation})
        if rubric_summary:
            sdk.record_metric(
                stage_run.run_id, "rubric.criteria_passed",
                float(rubric_summary.get("passed", 0)),
                tags={"rubric_id": str(rubric_summary.get("rubric_id", "")),
                      "failed": str(rubric_summary.get("failed", 0))},
            )

        rubric_bit = ""
        if rubric_summary:
            rubric_bit = (
                f" | rubric {rubric_summary.get('passed')}/"
                f"{rubric_summary.get('passed', 0) + rubric_summary.get('failed', 0)} 通过"
                f"（未通过: {','.join(rubric_summary.get('unmet_criterion_ids', [])) or '无'}）"
            )
        if rubric_veto:
            rubric_bit += f" | VETO: {len(rubric_veto)} 条硬性条目未通过"
        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED if gate_passed else GateResult.FAILED,
            event=event,
            output_refs=[artifact.artifact_id],
            detail=(
                f"external audit: confidence={confidence} -> {recommendation.upper()} "
                f"(unresolved={len(unresolved)}, recoverable={recoverable}){rubric_bit}"
            ),
        )
