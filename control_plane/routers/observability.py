"""Hypothesis tree, audits (+ follow-ups), improvements, experiences, playbook and strategies.

Extracted verbatim from ``control_plane/api.py`` (backlog item A1: split the
1975-line monolith into per-domain routers). Handler bodies are unchanged; the
shared dependencies they used to close over are now bound from
:class:`~..deps.ControlPlaneDeps` as local aliases.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status

from ...platform_contracts.events import AuditFollowupEvent
from ..schemas import AuditFollowupRequest
from ..deps import ControlPlaneDeps
from ..deps import translate_exc as _translate


def build_observability_router(deps: ControlPlaneDeps) -> APIRouter:
    """Build the observability router bound to *deps*."""

    router = APIRouter()

    # --- local aliases: keep handler bodies byte-identical to the old closures ---
    svc = deps.svc
    state_store = deps.state_store

    @router.get(
        "/workflow-runs/{run_id}/hypo-tree",
        summary="Cumulative HypothesisTree snapshot (Arbor-style cross-round state)",
    )
    def hypo_tree(run_id: str) -> dict:
        try:
            svc.get_workflow_run(run_id)
            return state_store.hypo_tree.snapshot(run_id=run_id)
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual-loop observability: external audit events -----
    @router.get(
        "/workflow-runs/{run_id}/audit",
        summary="External-audit events (AuditCompletedEvent) for a run",
    )
    def list_audit(run_id: str) -> list[dict]:
        try:
            svc.get_workflow_run(run_id)
            return [e for e in svc.list_events(run_id) if e.get("event_type") == "audit_completed"]
        except Exception as exc:
            raise _translate(exc)

    # ----- Rubric stage observability: the run's frozen grading contract -----
    @router.get(
        "/workflow-runs/{run_id}/rubric",
        summary="The executable scoring rubric this run is graded against (layer_12), "
                "plus its standard review and per-criterion verdicts from the audits",
    )
    def get_run_rubric(run_id: str) -> dict:
        """Reconstruct the run's grading contract from its durable event log.

        Reads the ``rubric_synthesized`` event (what standard was established, when, and
        with which integrity hash) and joins it with the rubric criteria recorded on each
        ``audit_completed`` event, so the console can show *which* criteria were unmet in
        each iteration instead of only the aggregate confidence.

        Returns ``{"rubric": null}`` when the run predates / disabled the rubric stage —
        a run without a rubric is a valid legacy run, not an error.
        """

        try:
            run = svc.get_workflow_run(run_id)
        except Exception as exc:
            raise _translate(exc)

        events = svc.list_events(run_id)
        synth = next(
            (e for e in events if e.get("event_type") == "rubric_synthesized"), None
        )
        # Per-iteration criterion verdicts, in audit order.
        iterations: list[dict] = []
        for idx, e in enumerate(
            [ev for ev in events if ev.get("event_type") == "audit_completed"]
        ):
            crit = [
                c for c in (e.get("constraints") or []) if c.get("criterion_id")
            ]
            if not crit:
                continue
            iterations.append({
                "iteration": idx,
                "audit_id": e.get("audit_id"),
                "confidence": e.get("confidence"),
                "gate_passed": e.get("gate_passed"),
                "passed": sum(1 for c in crit if c.get("status") == "verified"),
                "failed": sum(1 for c in crit if c.get("status") != "verified"),
                "criteria": crit,
            })

        rubric: dict | None = None
        if synth is not None:
            # The full frozen rubric is deterministic in the task definition, so it can be
            # rebuilt exactly (same ids + integrity_hash) without a side store.
            try:
                from ...execution_plane.capabilities.rubric_executor import build_task_spec
                from ...rubric import RubricEngine

                spec = build_task_spec({}, run.objective_snapshot or {})
                if not spec.task_id:
                    spec.task_id = run.target_id or run_id
                rebuilt = RubricEngine(use_llm=False).induce(spec)
                rubric = rebuilt.model_dump(mode="json")
                # Honesty guard: if the recorded hash differs (e.g. the run used the LLM
                # pass, or the task definition changed since), say so instead of passing
                # the rebuild off as the authoritative contract.
                rubric["hash_matches_run"] = (
                    rebuilt.integrity_hash == synth.get("integrity_hash")
                )
            except Exception:
                rubric = None

        return {
            "run_id": run_id,
            "rubric": rubric,
            "event": synth,
            "iterations": iterations,
        }

    # ----- Dual-loop observability: audit follow-up events (F6 protocol) -----
    @router.get(
        "/workflow-runs/{run_id}/audits/{audit_id}/followups",
        summary="Follow-up events (AuditFollowupEvent) for a specific audit",
    )
    def list_audit_followups(run_id: str, audit_id: str) -> list[dict]:
        try:
            svc.get_workflow_run(run_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")
        return [
            e
            for e in svc.list_events(run_id)
            if e.get("event_type") == "audit_followup" and e.get("audit_id") == audit_id
        ]

    @router.post(
        "/workflow-runs/{run_id}/audits/{audit_id}/followup",
        summary="Researcher follow-up (clarification/question) on one audit constraint",
    )
    def audit_followup(run_id: str, audit_id: str, body: AuditFollowupRequest) -> dict:
        # 1) run must exist
        try:
            svc.get_workflow_run(run_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")

        # 2) locate the target audit event — the immutable ground truth of the dual loop
        target = next(
            (
                e
                for e in svc.list_events(run_id)
                if e.get("event_type") == "audit_completed" and e.get("audit_id") == audit_id
            ),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"audit {audit_id} not found for run {run_id}",
            )

        # 3) locate the constraint within that audit
        constraints = target.get("constraints", []) or []
        constraint = next((c for c in constraints if c.get("id") == body.constraint_id), None)
        if constraint is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"constraint '{body.constraint_id}' not found in audit {audit_id}",
            )

        prior_status = constraint.get("status", "missing")
        prior_score = float(constraint.get("score", 0.0))

        # 4) re-score the single constraint given the researcher's clarification.
        #    The follow-up is a *supplementary* event — it never mutates the original
        #    audit verdict, and v1 does NOT auto-feed the result back into IterationRouter.
        from ...execution_plane.capabilities.audit_executor import (
            _default_judge,
            evaluate_constraint,
        )

        if body.clarification:
            new_score, new_status = evaluate_constraint(
                claim=constraint.get("description", body.constraint_id),
                judge=_default_judge,
                answer=body.clarification,
                evidence=body.clarification,
            )
            resolved = new_status == "verified"
            recommendation = "accept" if resolved else "revisit"
            response = "researcher clarification re-scored constraint"
        else:
            # Question-only follow-up: no new evidence to re-score; record the ask.
            new_score, new_status = prior_score, prior_status
            resolved = False
            recommendation = "revisit"
            response = None

        event = AuditFollowupEvent(
            run_id=run_id,
            audit_id=audit_id,
            constraint_id=body.constraint_id,
            question=body.question,
            clarification=body.clarification,
            prior_status=prior_status,
            new_status=new_status,
            new_score=round(new_score, 4),
            response=response,
            confidence=round(new_score, 4),
            recommendation=recommendation,
            resolved=resolved,
        )
        from ...execution_plane.sdk import PlatformSDK

        sdk = PlatformSDK(svc, state_store=state_store)
        sdk.emit_event(event)
        return event.model_dump()

    # ----- Dual-loop observability: recursive-improvement events -----
    @router.get(
        "/workflow-runs/{run_id}/improvements",
        summary="Recursive-improvement events (ImprovementAppliedEvent) for a run",
    )
    def list_improvements(run_id: str) -> list[dict]:
        try:
            svc.get_workflow_run(run_id)
            return [e for e in svc.list_events(run_id) if e.get("event_type") == "improvement_applied"]
        except Exception as exc:
            raise _translate(exc)

    # ----- Dual-loop observability: cross-run ExperienceBank -----
    @router.get(
        "/experiences",
        summary="Cross-run ExperienceBank entries (training-free reinjection)",
    )
    def list_experiences() -> list[dict]:
        return [e.model_dump(mode="json") for e in state_store.experience_bank.list_all()]

    # ----- Dual-loop observability: ACE playbook (itemized evolving context) -----
    @router.get(
        "/playbook",
        summary="ACE playbook entries (itemized evolving context, per-task scope)",
    )
    def list_playbook(scope: str | None = None) -> list[dict]:
        from dataclasses import asdict

        return [asdict(e) for e in state_store.playbook.list_entries(scope)]

    # ----- Dual-loop observability: StrategyArchive (propose-apply-verify-rollback) -----
    @router.get(
        "/strategies",
        summary="Meta-loop strategy archive with verification/rollback status",
    )
    def list_strategies(run_id: str | None = None) -> list[dict]:
        return state_store.strategy_archive.list_all(run_id)

    # ----- Benchmark task catalog (mined from the OSS projects) -----

    return router
