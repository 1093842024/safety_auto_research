"""Closed-loop orchestrator — wires the three events into one continuous loop.

Sequence (the user's requested loop)::

    评测(EvalCompletedEvent) ─► 决策(AgentHarness, router=参考策略+护栏)
            │ (R10: proceed to hardening)
            ▼
    红队(AttackCompletedEvent) ─► 决策(AgentHarness)  ──loop until ASR≤ceiling──
            │ (R10-exit: ASR low)
            ▼
    经验(LessonPromotedEvent) ─► 回注(register_lesson) ─► 决策(CONTINUE/EXIT)

Each arrow is a real control-plane mutation: a ``StageRun`` is created, run via the
registered adapter (or an agent harness), transitioned through the validated state
machine, emits its event, and a ``DecisionRecord`` is recorded.

Agent-driven mode (``mode="agent"``): the authoritative decision comes from the
``AgentHarness``; ``IterationRouter.propose_route`` is only a *suggestion* and
``IterationRouter.validate_route`` is the hard *guardrail*. In ``mode="scripted"``
(the default, for backward compatibility) the router is the decider and executors run
deterministically.
"""

from __future__ import annotations

from typing import Any

from ..control_plane.schemas import CreateStageRunRequest
from ..control_plane.schemas import RecordDecisionRequest
from ..control_plane.schemas import UpdateStageStatusRequest
from ..control_plane.service import ControlPlaneService
from ..control_plane.service import NotFoundError
from ..platform_contracts.enums import DecisionType
from ..platform_contracts.enums import StageStatus
from ..platform_contracts.events import BasePlatformEvent
from .decision.router import IterationRouter
from .registry import AdapterRegistry
from .registry import default_registry
from .sdk import PlatformSDK
from .capabilities.registry import CapabilityRegistry
from ..control_plane.store_tree import ResearchStateStore
from .capabilities.registry import default_capability_registry
from .agent.harness import AGENT_TOOL_NAMES
from .agent.harness import AgentHarness
from .agent.harness import LocalAgentHarness
from .agent.harness import RemoteAgentHarness
from .base import StageTaskSpec


class ClosedLoopOrchestrator:
    """Drives the 评测→决策 / 红队→决策 / 经验→回注 closed loop."""

    def __init__(
        self,
        service: ControlPlaneService,
        registry: AdapterRegistry | None = None,
        capability_registry: CapabilityRegistry | None = None,
        mode: str = "scripted",
        harness: AgentHarness | None = None,
        state_store: ResearchStateStore | None = None,
    ) -> None:
        self.svc = service
        self.state_store = state_store
        self.sdk = PlatformSDK(service, state_store=state_store)
        self.registry = registry or default_registry()
        self.capability_registry = capability_registry or default_capability_registry()
        self.router = IterationRouter()
        self.mode = mode
        if harness is not None:
            self.harness: AgentHarness | None = harness
        elif mode == "agent":
            # Default agent backend: local autopilot that mimics an agent. A real
            # deployment passes a RemoteAgentHarness wired to Codex / WorkBuddy.
            self.harness = LocalAgentHarness(self.registry)
        else:
            self.harness = None
        # Wire the agent-facing capability runner so a remote agent can invoke any
        # infrastructure-layer capability as a tool (see ``run_capability``).
        if self.harness is not None:
            self.harness.attach_runner(self.run_capability)

    def has_real_agent(self) -> bool:
        """True only when a *real* external agent (RemoteAgentHarness) is wired.

        ``LocalAgentHarness`` is a local autopilot/mimic — it is NOT a remote agent, so
        an inner loop that *requires* autonomous agent execution must not proceed with it
        (it would silently behave like the scripted executor). Callers that need a real
        remote agent should check this before requesting ``agent_inner``.
        """
        return isinstance(self.harness, RemoteAgentHarness)

    # ---------------------------------------------------------- single dispatch
    def dispatch_stage(
        self,
        run_id: str,
        stage_code: str,
        params: dict[str, Any] | None = None,
        executor_family: str = "default",
    ) -> tuple[Any, Any]:
        """Create + run one stage, emit its event, transition status.

        In agent mode the stage is handed to the harness as a ``StageTaskSpec``; the
        agent fulfills it autonomously. In scripted mode the deterministic executor runs.
        """

        run = self.svc.get_workflow_run(run_id)
        stage = self.svc.create_stage_run(
            run_id, CreateStageRunRequest(stage_code=stage_code, executor_family=executor_family)
        )
        self.svc.update_stage_status(
            stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.RUNNING)
        )

        if self.mode == "agent" and self.harness is not None:
            spec = StageTaskSpec(
                stage_run=stage,
                params=params or {},
                available_tools=list(AGENT_TOOL_NAMES),
                run_context={
                    "run_id": run.run_id,
                    "run_type": run.run_type.value,
                    "objective": run.objective_snapshot,
                    "capabilities": [
                        c["capability_id"] for c in self.capability_registry.list_capabilities()
                    ],
                },
            )
            result = self.harness.run_stage(spec, self.sdk)
        else:
            executor = self.registry.resolve(stage_code)
            if executor is None:
                raise NotFoundError(f"no adapter registered for stage {stage_code}")
            result = executor.execute(stage, self.sdk, params or {})

        self.svc.update_stage_status(
            stage.stage_run_id, UpdateStageStatusRequest(to_status=result.final_status)
        )
        stage.gate_result = result.gate_result
        self.svc._repo.put_stage_run(stage)
        return stage, result

    # ----------------------------------------------------- agent capability tool
    def run_capability(
        self,
        run_id: str,
        capability_id: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        """Agent-invoked tool: run one infrastructure-layer capability.

        Creates a real ``StageRun`` for the capability's layer, runs its executor (real
        or stub), transitions status, and emits events — exactly like ``dispatch_stage``
        but initiated by the agent through the ``run_capability`` tool. This is what lets
        an agent orchestrate the ten layers end-to-end instead of being confined to one
        assigned stage. Returns ``(stage_run, exec_result)``.
        """

        cap = self.capability_registry.resolve(capability_id)
        if cap is None:
            raise NotFoundError(f"no capability registered: {capability_id}")
        self.svc.get_workflow_run(run_id)  # 404 if missing
        stage = self.svc.create_stage_run(
            run_id,
            CreateStageRunRequest(stage_code=cap.layer_code, executor_family="capability"),
        )
        self.svc.update_stage_status(
            stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.RUNNING)
        )
        executor = cap.executor or self.registry.resolve(cap.layer_code)
        if executor is None:
            raise NotFoundError(f"no executor bound for capability {capability_id}")
        result = executor.execute(stage, self.sdk, params or {})
        self.svc.update_stage_status(
            stage.stage_run_id, UpdateStageStatusRequest(to_status=result.final_status)
        )
        stage.gate_result = result.gate_result
        self.svc._repo.put_stage_run(stage)
        return stage, result

    # --------------------------------------------------- open-goal agent orchestration
    def dispatch_open_goal(
        self,
        run_id: str,
        goal: str,
        candidate_capabilities: list[str] | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        """Agent-driven open orchestration.

        Hands the agent a goal plus the full capability catalog (or a candidate subset)
        and lets it invoke ``run_capability`` for whichever layers it chooses. The
        orchestrator only wraps the turn in a container ``StageRun``; the agent decides
        the sequence. Requires ``mode='agent'`` with a wired harness.

        ``agent_config`` is the researcher's inner-loop configuration (system prompt,
        skills, enabled tools, ordered step plan, data knobs) and is forwarded verbatim
        to the agent inside ``StageTaskSpec.agent_config`` (and mirrored into the run
        context), so the inner loop actually runs under the configured settings.
        """

        if self.harness is None:
            raise NotFoundError(
                "open orchestration needs agent mode; construct the orchestrator with "
                "mode='agent' (and a RemoteAgentHarness for a real external agent)."
            )
        run = self.svc.get_workflow_run(run_id)
        stage = self.svc.create_stage_run(
            run_id,
            CreateStageRunRequest(stage_code="00_agent_orchestration", executor_family="agent"),
        )
        spec = StageTaskSpec(
            stage_run=stage,
            params={"open_goal": goal},
            available_tools=list(AGENT_TOOL_NAMES),
            run_context={
                "run_id": run.run_id,
                "run_type": run.run_type.value,
                "objective": run.objective_snapshot,
                "open_goal": goal,
                "capabilities": [
                    c["capability_id"] for c in self.capability_registry.list_capabilities()
                ],
                "candidate_capabilities": candidate_capabilities or [],
                "agent_config": agent_config or {},
            },
            open_goal=goal,
            candidate_capabilities=candidate_capabilities or [],
            agent_config=agent_config,
        )
        self.svc.update_stage_status(
            stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.RUNNING)
        )
        result = self.harness.run_stage(spec, self.sdk)
        self.svc.update_stage_status(
            stage.stage_run_id, UpdateStageStatusRequest(to_status=result.final_status)
        )
        self.svc._repo.put_stage_run(stage)
        return stage, result

    def decide_and_record(
        self, run_id: str, event: BasePlatformEvent
    ) -> tuple[Any, Any]:
        """Decide the next step and persist the DecisionRecord.

        In agent mode the harness decides (the router supplies a *suggestion*); in
        scripted mode the router decides. Either way ``validate_route`` is the hard
        guardrail that rejects an illegal decision before it is committed.
        """

        run = self.svc.get_workflow_run(run_id)
        suggestion = self.router.propose_route(event, run)
        if self.mode == "agent" and self.harness is not None:
            self.harness.attach_sdk(self.sdk)
            route = self.harness.decide(run, event, suggestion)
        else:
            route = suggestion
        self.router.validate_route(route, run)
        decision = self.svc.record_decision(
            run_id,
            RecordDecisionRequest(
                decision_type=route.decision_type,
                target_stage=route.target_stage,
                reason_codes=route.reason_codes,
                evidence_refs=route.evidence_refs,
            ),
        )
        return route, decision

    # ------------------------------------------------------------ closed loop
    def run_closed_loop(
        self,
        run_id: str,
        max_rounds: int = 4,
        auto_loop: bool = True,
    ) -> dict[str, Any]:
        """Run the full 评测→红队→经验 closed loop and return a trace summary."""

        run = self.svc.get_workflow_run(run_id)
        obj = run.objective_snapshot or {}
        steps: list[dict[str, Any]] = []

        # 1) 评测
        stage, result = self.dispatch_stage(
            run_id,
            "03_eval",
            params={
                "target_metric": obj.get("target_metric", "accuracy"),
                "threshold": obj.get("target_threshold", 0.8),
            },
        )
        _route, decision = self.decide_and_record(run_id, result.event)
        steps.append(self._step("eval", stage, result, decision))
        if decision.decision_type in (
            DecisionType.EXIT_SUCCESS,
            DecisionType.EXIT_BUDGET,
            DecisionType.EXIT_CONVERGED,
        ):
            return self._summary(run_id, steps, "stopped_after_eval")

        # 2) 红队 (loop until ASR within ceiling or max_rounds)
        round_idx = 0
        while auto_loop and round_idx < max_rounds:
            stage, result = self.dispatch_stage(
                run_id, "10_adversarial_data_generation", params={"target_model_id": run.target_id}
            )
            _route, decision = self.decide_and_record(run_id, result.event)
            steps.append(self._step("attack", stage, result, decision))
            if decision.decision_type == DecisionType.EXIT_SUCCESS:
                break
            # R10+R6: forgetting detected -> next step is a data re-clean (documented).
            if decision.target_stage == "05_data_evaluation_cleaning":
                break
            # otherwise R10 -> keep hardening (loop again)
            if decision.target_stage != "10_adversarial_data_generation":
                break
            round_idx += 1

        # 3) 经验 → 回注
        stage, result = self.dispatch_stage(run_id, "08_result_analysis_experience", params={})
        if result.event is not None:
            _route, decision = self.decide_and_record(run_id, result.event)
            steps.append(self._step("lesson", stage, result, decision))
        else:
            steps.append(self._step("lesson", stage, result, None))

        return self._summary(run_id, steps, "exited_converged")

    # ------------------------------------------------------------ dual loop
    def run_dual_loop(
        self,
        run_id: str,
        inner_capability: str = "kaggle_eval",
        inner_params: dict[str, Any] | None = None,
        audit_params: dict[str, Any] | None = None,
        max_outer_iters: int = 3,
        agent_inner: bool = False,
        strategy_db: str | None = None,
        refine_hook: Any | None = None,
        inner_agent_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """DUAL-LOOP driver: inner research -> external audit -> (refine/restart) -> repeat.

        Implements the AREX-style outer self-improvement loop on top of the existing
        inner research capability (e.g. ``kaggle_eval`` or an agent-driven open goal):

          1. INNER LOOP  — produce a research answer (real eval / or an agent open goal).
          2. OUTER AUDIT — ``layer_11_external_audit`` runs an independent constraint-wise
             audit and emits ``AuditCompletedEvent`` (confidence ``s`` + recoverable ``v``).
          3. DECIDE       — the router maps the audit to Accept / Refine / Restart.
             - Accept  -> stop (research complete)
             - Refine  -> re-enter the inner loop with the audit's unresolved claims folded
                          into the goal (keep verified findings)
             - Restart -> re-enter from the raw objective, discarding this trajectory

        Every step is a real ``StageRun`` + event, so the whole loop is auditable and the
        ``validate_route`` guardrail still applies to every decision.
        """

        run = self.svc.get_workflow_run(run_id)
        obj = run.objective_snapshot or {}
        base_goal = obj.get("goal") or obj.get("objective") or run.target_id
        goal = base_goal
        inner_params = dict(inner_params or {})
        audit_params = dict(audit_params or {})
        steps: list[dict[str, Any]] = []
        # When an inner-loop agent config is supplied, fold the researcher's system prompt
        # into the goal and surface the enabled tools as candidate capabilities. The full
        # config (skills / tools / step_plan / data knobs) travels via StageTaskSpec.agent_config.
        agent_candidates: list[str] | None = None
        if inner_agent_config:
            sp = (inner_agent_config.get("system_prompt") or "").strip()
            if sp:
                goal = f"{base_goal}\n\n[INNER-LOOP INSTRUCTIONS]\n{sp}"
            agent_candidates = inner_agent_config.get("tools") or None
        # Outer-loop audit history (curated, cross-round) carried into each audit as
        # legitimate outer-loop context — NOT inner-loop experimental detail.
        prior_audits: list[dict[str, Any]] = []

        # Mutable audit params so cumulative state (rejected candidates) carries forward.
        audit_params = dict(audit_params or {})

        outer = 0
        while outer < max_outer_iters:
            # ---- INNER LOOP: produce a research answer ----
            if agent_inner:
                # Hard guard (Task 1): agent mode REQUIRES a real remote agent. Without
                # one we must NOT silently fall back to the scripted executor — that would
                # mask a misconfiguration and produce a misleading "success". Instead we
                # terminate the run loudly with a clear, actionable reason.
                if not self.has_real_agent():
                    msg = (
                        "agent 模式启动失败：当前环境未接入远程 Agent（RemoteAgentHarness），"
                        "无法执行自主内循环，已终止。请通过环境变量 AGENT_COMMAND 接入远程 Agent "
                        "(Codex / WorkBuddy CLI)，或将内循环改为「脚本化」模式（仅平台原生任务可用）。"
                    )
                    self.svc.set_run_status(run.run_id, "failed", detail=msg)
                    return self._summary(run_id, steps, "failed", detail=msg)
                stage, result = self.dispatch_open_goal(
                    run_id,
                    goal,
                    candidate_capabilities=agent_candidates,
                    agent_config=inner_agent_config,
                )
            else:
                stage, result = self.run_capability(run_id, inner_capability, inner_params)
            inner_detail = result.detail
            inner_event = result.event
            inner_acc = (
                float(inner_event.metrics.get("accuracy", 0.0))
                if inner_event and getattr(inner_event, "metrics", None)
                else 0.0
            )
            # Cumulative state: observe a hypothesis node from this inner answer.
            node_id = self.sdk.observe_hypothesis(
                hypothesis=f"{inner_capability} -> cv accuracy={inner_acc:.4f}",
                evidence_refs=[inner_event.report_ref] if inner_event else [],
                score=inner_acc,
                run_id=run_id,
            )
            steps.append(self._step(f"inner[{outer}]", stage, result, None))

            # ---- OUTER AUDIT: independent constraint-wise verification ----
            # The control plane curates a SCOPED audit input: the outer auditor sees only
            # the objective + the inner loop's *result* (metrics/verdict) + prior outer-loop
            # verdicts. It never sees the inner loop's experimental narrative, which is what
            # keeps the audit scientifically independent and free of evaluation bias.
            audit_input = {
                "objective": goal,
                "result_metrics": (
                    dict(inner_event.metrics) if inner_event and getattr(inner_event, "metrics", None) else {}
                ),
                "result_report_ref": getattr(inner_event, "report_ref", None) if inner_event else None,
                "result_gate_passed": (
                    bool(getattr(inner_event, "gate_passed", False)) if inner_event else False
                ),
                "result_real_eval": bool(
                    inner_event and "kaggle" in str(getattr(inner_event, "report_ref", "") or "")
                ),
                "prior_audits": list(prior_audits),
                "constraints": audit_params.get("constraints", []),
            }
            audit_stage, audit_result = self.run_capability(
                run_id,
                "layer_11_external_audit",
                {**audit_params, "objective": goal, "audit_input": audit_input},
            )
            audit_event = audit_result.event
            _route, decision = self.decide_and_record(run_id, audit_event)
            prior_audits.append({
                "confidence": getattr(audit_event, "confidence", None),
                "recommendation": decision.decision_type.value,
                "unresolved": getattr(audit_event, "unresolved_claims", []),
            })
            steps.append(self._step(f"audit[{outer}]", audit_stage, audit_result, decision))

            # Cumulative state: backpropagate the audit insight onto the hypothesis node.
            rejected = list(getattr(audit_event, "rejected_candidates", []) or [])
            if decision.decision_type == DecisionType.EXIT_SUCCESS:
                if node_id:
                    self.sdk.backpropagate_insight(node_id, "accepted by external audit", audit_event.confidence)
                    self.sdk.mark_merged(node_id)
                return self._summary(run_id, steps, "exited_converged")
            # Refine / Restart: fold unresolved claims into the goal and loop again.
            unresolved = [rc for rc in decision.reason_codes if rc not in ("audit_refine", "audit_restart")]
            if node_id:
                self.sdk.backpropagate_insight(node_id, audit_result.detail, audit_event.confidence)
            if "audit_refine" in decision.reason_codes:
                goal = f"{base_goal}\n[REFINE] address: " + "; ".join(unresolved)
                # A refine MUST change the inner attempt, otherwise it just repeats
                # the same trajectory. The hook lets callers mutate the inner params
                # (e.g. switch model / features) based on the audit outcome.
                if refine_hook is not None:
                    new_params = refine_hook(outer, dict(inner_params), audit_event)
                    if new_params:
                        inner_params = new_params
            elif "audit_restart" in decision.reason_codes:
                goal = base_goal  # discard trajectory, restart from the raw objective
                if node_id:
                    self.sdk.mark_pruned(node_id)  # keep as a stepping stone

            # Cumulative state: compact the turning point and carry rejected candidates
            # into the next audit (AREX update_context — preserves rejected candidates).
            self.sdk.compact_research_state(
                unresolved=audit_event.unresolved_claims,
                rejected_candidates=rejected,
                next_plan=goal,
                run_id=run_id,
            )
            audit_params = {**audit_params, "rejected_candidates": rejected, "objective": goal}

            # ---- RECURSIVE IMPROVEMENT (meta-loop): improve the *process* for next iter ----
            try:
                se_stage, se_result = self.run_self_evolution(
                    run_id, params={"strategy_db": strategy_db} if strategy_db else {}
                )
                steps.append(self._step(f"self_evo[{outer}]", se_stage, se_result, None))
            except NotFoundError:
                pass  # layer_09 still a stub in this deployment

            outer += 1

        return self._summary(run_id, steps, "exited_budget")

    # --------------------------------------------------- recursive improvement hook
    def run_self_evolution(self, run_id: str, params: dict[str, Any] | None = None) -> tuple[Any, Any]:
        """Run the recursive-improvement meta-loop (layer 09) if a real executor is bound.

        Returns ``(stage_run, exec_result)``. If ``layer_09`` is still a stub (no executor),
        it raises ``NotFoundError`` so callers can skip gracefully.
        """

        cap = self.capability_registry.resolve("layer_09_self_iterative_evolution")
        if cap is None or cap.executor is None:
            raise NotFoundError("layer_09_self_iterative_evolution has no real executor bound")
        return self.run_capability(run_id, "layer_09_self_iterative_evolution", params or {})

    # ------------------------------------------------------------------ helpers
    def _step(self, name: str, stage: Any, result: Any, decision: Any | None) -> dict[str, Any]:
        return {
            "stage": name,
            "stage_run_id": stage.stage_run_id,
            "event": result.event.event_type if result.event else None,
            "gate_result": result.gate_result.value,
            "decision": decision.decision_type.value if decision else None,
            "target_stage": decision.target_stage if decision else None,
            "detail": result.detail,
        }

    def _summary(self, run_id: str, steps: list[dict[str, Any]], status: str, detail: str | None = None) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": status,
            "status_detail": detail,
            "steps": steps,
            "lessons": [l.lesson_id for l in self.svc._repo.list_lessons(run_id)],
            "events": [e["event_type"] for e in self.svc.list_events(run_id)],
            "decisions": [d.decision_type.value for d in self.svc.list_decisions(run_id)],
        }
