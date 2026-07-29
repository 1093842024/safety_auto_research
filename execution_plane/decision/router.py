"""Iteration & routing engine (spec §10.1.2 / layer 09 playbook).

Translates a completed-stage event into a *suggested* ``RouteDecision`` (the reference
policy, R1-R10). In the agent-driven design this is no longer the authoritative decider:

  - ``propose_route`` returns the *reference* decision (used as a suggestion / autopilot).
  - ``validate_route`` is the *safety guardrail*: it rejects any decision (agent-made or
    reference) that violates the canonical decision-record contract, so the agent can
    never commit an illegal transition.

The authoritative decision is made by the ``AgentHarness`` (see ``execution_plane.agent``);
this router supplies the suggestion and the validator.

Reference mapping (for ``propose_route``):
  - ``EvalCompletedEvent``    -> gate pass/fail -> proceed to hardening (R10) or exit / revisit benchmark (R7)
  - ``AttackCompletedEvent``  -> ASR + retention -> continue hardening (R10) / revisit data cleaning (R6) / exit
  - ``LessonPromotedEvent``   -> experience reinjected -> continue (or exit)
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ...platform_contracts.enums import DecisionType
from ...platform_contracts.enums import EventType
from ...platform_contracts.enums import RunType
from ...platform_contracts.events import AttackCompletedEvent
from ...platform_contracts.events import AuditCompletedEvent
from ...platform_contracts.events import BasePlatformEvent
from ...platform_contracts.events import EvalCompletedEvent
from ...platform_contracts.events import LessonPromotedEvent
from ...platform_contracts.objects import DecisionRecord
from ...platform_contracts.objects import WorkflowRun
from ...platform_contracts.transitions import validate_decision_record_contract

# Tuning thresholds for the routing rules.
ASR_CEILING = 0.15
RETENTION_FLOOR = 0.90

EVAL_STAGE = "03_eval"
ATTACK_STAGE = "10_adversarial_data_generation"
DATA_STAGE = "05_data_evaluation_cleaning"
LESSON_STAGE = "08_result_analysis_experience"
AUDIT_STAGE = "11_external_audit"
ORCH_STAGE = "00_agent_orchestration"

# Known stage codes across the ten infrastructure layers + the dual-loop extras.
# A REVISIT target must name one of these (soft check — custom stages may also be
# added by a program).
KNOWN_STAGE_CODES: frozenset[str] = frozenset(
    {
        "00_agent_orchestration",
        "01_literature_research",
        "02_idea_generation_evaluation",
        "03_eval",
        "03_eval_benchmark",
        "04_experiment_design",
        "05_data_evaluation_cleaning",
        "06_code_development",
        "07_experiment_execution",
        "08_result_analysis_experience",
        "09_self_iterative_evolution",
        "10_adversarial_data_generation",
        "11_external_audit",
    }
)


@dataclass
class RouteDecision:
    decision_type: DecisionType
    target_stage: str | None
    reason_codes: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    route_rule: str = ""
    summary: str = ""


class IterationRouter:
    """Reference policy + safety validator (no longer the sole decider)."""

    def propose_route(self, event: BasePlatformEvent, run: WorkflowRun) -> RouteDecision:
        """Reference / autopilot policy. Returns the *suggested* decision for an event.

        In agent mode this is only a suggestion handed to the ``AgentHarness``; the
        authoritative decision is the harness's, validated by ``validate_route``.
        """
        et = event.event_type
        if et == EventType.EVAL_COMPLETED:
            return self._decide_eval(event, run)
        if et == EventType.ATTACK_COMPLETED:
            return self._decide_attack(event, run)
        if et == EventType.AUDIT_COMPLETED:
            return self._decide_audit(event, run)
        if et == EventType.LESSON_PROMOTED:
            return self._decide_lesson(event, run)
        return RouteDecision(
            DecisionType.CONTINUE,
            None,
            [],
            [event.event_id],
            "R-DEFAULT",
            "no routing rule for this event; continue",
        )

    # ------------------------------------------------------------------ eval
    def _decide_eval(self, event: EvalCompletedEvent, run: WorkflowRun) -> RouteDecision:
        if event.gate_passed:
            if run.run_type == RunType.ADVERSARIAL_HARDENING:
                return RouteDecision(
                    DecisionType.REVISIT,
                    ATTACK_STAGE,
                    ["eval_passed"],
                    [event.report_ref],
                    "R10",
                    "eval gate passed -> proceed to adversarial hardening",
                )
            return RouteDecision(
                DecisionType.EXIT_SUCCESS,
                None,
                ["objective_met"],
                [event.report_ref],
                "EXIT-S",
                "eval gate passed and objective met",
            )
        return RouteDecision(
            DecisionType.REVISIT,
            EVAL_STAGE,
            ["gate_failed"],
            [event.report_ref],
            "R7",
            "eval gate failed; revisit benchmark / validity",
        )

    # ------------------------------------------------------------------ attack
    def _decide_attack(self, event: AttackCompletedEvent, run: WorkflowRun) -> RouteDecision:
        asr = event.success_rate
        if asr > ASR_CEILING:
            # robustness gap detected
            if event.retention_rate is not None and event.retention_rate < RETENTION_FLOOR:
                return RouteDecision(
                    DecisionType.REVISIT,
                    DATA_STAGE,
                    ["high_asr", "forgetting"],
                    [event.report_ref],
                    "R10+R6",
                    "high ASR with forgetting -> revisit data cleaning with replay buffer",
                )
            return RouteDecision(
                DecisionType.REVISIT,
                ATTACK_STAGE,
                ["high_asr"],
                [event.report_ref],
                "R10",
                "robustness gap -> continue adversarial hardening",
            )
        # ASR within ceiling: hardened enough
        if run.run_type == RunType.ADVERSARIAL_HARDENING:
            return RouteDecision(
                DecisionType.CONTINUE,
                None,
                ["asr_low"],
                [event.report_ref],
                "R10-exit",
                "ASR within ceiling -> proceed to experience / lesson reinjection",
            )
        return RouteDecision(
            DecisionType.EXIT_SUCCESS,
            None,
            ["robustness_met"],
            [event.report_ref],
            "EXIT-S",
            "robustness target met",
        )

    # ------------------------------------------------------------------ audit (dual-loop outer)
    def _decide_audit(self, event: AuditCompletedEvent, run: WorkflowRun) -> RouteDecision:
        """Accept / Refine / Restart from the external audit (AREX decision law).

        - ``gate_passed`` (confidence >= threshold) -> Accept (EXIT_SUCCESS)
        - confidence < threshold but recoverable -> Refine (REVISIT the orchestration
          stage with the audit's unresolved claims as the targeted follow-up goal)
        - confidence < restart floor and not recoverable -> Restart (REVISIT the
          orchestration stage from the raw objective, discarding this trajectory)
        """

        if event.gate_passed:
            return RouteDecision(
                DecisionType.EXIT_SUCCESS,
                None,
                ["audit_accept"],
                [event.report_ref],
                "AUDIT-ACCEPT",
                f"external audit confidence={event.confidence} >= threshold -> accept",
            )
        if event.recoverable:
            return RouteDecision(
                DecisionType.REVISIT,
                ORCH_STAGE,
                ["audit_refine"] + event.unresolved_claims,
                [event.report_ref],
                "AUDIT-REFINE",
                "audit found unresolved claims; refine with targeted follow-up research",
            )
        return RouteDecision(
            DecisionType.REVISIT,
            ORCH_STAGE,
            ["audit_restart"] + event.unresolved_claims,
            [event.report_ref],
            "AUDIT-RESTART",
            "audit confidence below restart floor and not recoverable; restart from objective",
        )

    # ------------------------------------------------------------------ lesson
    def _decide_lesson(self, event: LessonPromotedEvent, run: WorkflowRun) -> RouteDecision:
        if run.run_type == RunType.ADVERSARIAL_HARDENING:
            return RouteDecision(
                DecisionType.CONTINUE,
                None,
                ["lesson_reinjected"],
                [event.payload_ref],
                "REINJECT",
                "lesson promoted to platform object; available for next-round reinjection",
            )
        return RouteDecision(
            DecisionType.EXIT_SUCCESS,
            None,
            ["lesson_reinjected"],
            [event.payload_ref],
            "REINJECT-EXIT",
            "lesson promoted; run complete",
        )

    # Backwards-compatible alias: the reference policy used to be the only decider.
    decide = propose_route

    # ------------------------------------------------------------------ guardrail
    def validate_route(self, route: RouteDecision, run: WorkflowRun) -> bool:
        """Safety validator. Rejects a decision that breaks the canonical contract.

        The agent (or the reference policy) may propose any decision; this ensures it
        can never be committed if it violates the decision-record rules — e.g. a
        ``CONTINUE``/``EXIT_*`` decision must not carry a ``target_stage``, and a
        ``REVISIT`` must name a known stage.
        """
        probe = DecisionRecord(
            decision_id="__validate__",
            run_id=run.run_id,
            decision_type=route.decision_type,
            target_stage=route.target_stage,
            reason_codes=list(route.reason_codes),
            evidence_refs=list(route.evidence_refs),
        )
        validate_decision_record_contract(probe)
        if route.decision_type == DecisionType.REVISIT and route.target_stage:
            if route.target_stage not in KNOWN_STAGE_CODES:
                warnings.warn(
                    f"REVISIT target '{route.target_stage}' is not a known stage code; "
                    "ensure the program registers it before committing the decision."
                )
        return True
