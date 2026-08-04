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

import json as _json
import os as _os
import random as _random
import tempfile as _tempfile
import threading
import time as _time
from types import SimpleNamespace as _SimpleNamespace
from typing import Any

from ..control_plane.schemas import CreateStageRunRequest
from ..control_plane.schemas import RecordDecisionRequest
from ..control_plane.schemas import RequestApprovalRequest
from ..control_plane.schemas import UpdateStageStatusRequest
from ..control_plane.service import ControlPlaneService
from ..control_plane.service import NotFoundError
from ..platform_contracts.enums import DecisionType
from ..platform_contracts.enums import GateResult
from ..platform_contracts.enums import StageStatus
from ..platform_contracts.enums import WorkflowStatus
from ..platform_contracts.events import BasePlatformEvent
from .decision.router import IterationRouter
from .registry import AdapterRegistry
from .registry import default_registry
from .sdk import PlatformSDK
from .capabilities.registry import CapabilityRegistry
from ..control_plane.store_tree import ResearchStateStore
from ..control_plane.evolution import DEFAULT_SURFACE
from ..control_plane.evolution import EvolutionArchive
from ..control_plane.evolution import IslandModel
from ..control_plane.evolution import crossover_programs as _evo_crossover_programs
from ..control_plane.evolution import mutate as _evo_mutate
from ..control_plane.evolution import mutate_program as _evo_mutate_program
from ..control_plane.evolution import novelty_filter as _evo_novelty_filter
from ..control_plane.evolution import seed_population as _evo_seed_population
from ..control_plane.evolution import seed_program_population as _evo_seed_program_population
from ..control_plane.evolution import select_parent as _evo_select_parent
from ..control_plane.failure_miner import mine_failure_modes
from ..control_plane.playbook import reflect_round
from ..control_plane.task_manager import BackgroundTaskManager
from .capabilities.registry import default_capability_registry
from .agent.harness import AGENT_TOOL_NAMES
from .agent.harness import AgentHarness
from .agent.harness import LocalAgentHarness
from .agent.harness import RemoteAgentHarness
from .base import StageTaskSpec

# Human-in-the-loop collaboration state: one entry per paused run.  The background
# thread writes the step context into ``_COLLAB_PAUSES`` and blocks until the HTTP
# resolve-collaboration endpoint signals it.  The resolve handler writes adjustments
# (model / fe / threshold / audit_threshold / action) before signalling, so the
# thread can apply them and either continue or abort.
_COLLAB_PAUSES: dict[str, dict[str, Any]] = {}
_COLLAB_LOCK = threading.Lock()

# Re-export for api.py so the resolve-collaboration endpoint can write adjustments.
def _collab_pauses() -> dict[str, dict[str, Any]]:
    return _COLLAB_PAUSES

def _collab_lock() -> threading.Lock:
    return _COLLAB_LOCK


def _cand_dict(c: Any) -> dict[str, Any] | None:
    """Serialize an evolution Candidate for API/summary output."""

    if c is None:
        return None
    return {
        "candidate_id": c.candidate_id,
        "params": c.params,
        "fitness": c.fitness,
        "novelty": c.novelty,
        "generation": c.generation,
        "parent_id": c.parent_id,
        "status": c.status,
        "branch": c.branch,
        "node_kind": getattr(c, "node_kind", "config"),
        "operator": getattr(c, "operator", None),
    }


def _primary_score(metrics: dict[str, Any], op: str = "ge") -> float:
    """I8 fix: direction-aware primary score (higher-is-better normalized).

    Reads ``accuracy`` when present (kaggle multi-metric), else ``primary``; for
    ``op == "le"`` objectives (e.g. log_loss) the value is negated so that larger
    always means better everywhere downstream (fitness, patch verification, budgets).
    """

    if not isinstance(metrics, dict):
        return 0.0
    if metrics.get("accuracy") is not None:
        v = float(metrics["accuracy"])
    else:
        v = float(metrics.get("primary", 0.0) or 0.0)
    return -v if op == "le" else v


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
        # C5 fix: per-run capability-call counter, incremented inside run_capability
        # itself so EVERY real call counts (orchestrator-driven AND agent-tool-driven).
        self._cap_call_counts: dict[str, int] = {}

    def _cap_count(self, run_id: str) -> int:
        return self._cap_call_counts.get(run_id, 0)

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
        self._cap_call_counts[run_id] = self._cap_call_counts.get(run_id, 0) + 1
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
        # I3 fix: an executor crash must not leave the stage stuck in RUNNING.
        try:
            result = executor.execute(stage, self.sdk, params or {})
        except Exception:
            self.svc.update_stage_status(
                stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.FAILED)
            )
            stage.gate_result = GateResult.FAILED
            self.svc._repo.put_stage_run(stage)
            raise
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
        self._cap_call_counts[run_id] = self._cap_call_counts.get(run_id, 0) + 1
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
        collaboration_mode: str = "autonomous",
        cancel_event: Any | None = None,
        progress_callback: Any | None = None,
        budget: dict[str, Any] | None = None,
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
        orig_inner_params = dict(inner_params)  # snapshot for restart via collaboration
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

        # Playbook scope: cross-run learning per benchmark task. Isolation invariant:
        # the playbook is injected into the INNER loop only — never into audit_input.
        pb_scope = str(obj.get("benchmark_task_id") or run.target_id or run.run_type.value)
        # Pending meta-loop strategy awaiting post-hoc verification (AHE-style
        # falsifiable edit: applied at the end of an iteration, verified against the
        # NEXT inner-loop metric, rolled back when the prediction is falsified).
        pending_strategy: dict[str, Any] | None = None
        pre_patch_params: dict[str, Any] = {}
        _MISSING: Any = object()

        # Multi-dimensional budget (Phase 2): wall-clock / capability calls / abstract
        # cost units. Exceeding any dimension hard-stops the loop as ``exited_budget``.
        # Capability calls are counted inside run_capability itself (C5 fix), so
        # agent-driven inner loops cannot bypass the budget.
        budget_cfg = dict(budget or (obj.get("config") or {}).get("budget") or {})
        _t0 = _time.monotonic()
        _cost_per_call = float(budget_cfg.get("cost_per_call", 1.0))

        def _budget_exceeded() -> str | None:
            if budget_cfg.get("max_seconds") is not None and (
                _time.monotonic() - _t0 > float(budget_cfg["max_seconds"])
            ):
                return f"时间预算耗尽（>{budget_cfg['max_seconds']}s）"
            if budget_cfg.get("max_capability_calls") is not None and (
                self._cap_count(run_id) >= int(budget_cfg["max_capability_calls"])
            ):
                return f"能力调用预算耗尽（≥{budget_cfg['max_capability_calls']} 次）"
            if budget_cfg.get("max_cost") is not None and (
                self._cap_count(run_id) * _cost_per_call >= float(budget_cfg["max_cost"])
            ):
                return f"成本预算耗尽（≥{budget_cfg['max_cost']} 单位）"
            return None

        def _expire_pending_strategies() -> None:
            """Loop is ending: no pending proposal may survive as if it will still be
            applied/verified. An applied-but-unverified patch expires as
            ``expired_unverified``; not-yet-applied proposals expire as
            ``expired_unapplied``. This keeps the archive's pending() semantics honest
            (pending == will be applied to a future inner loop).
            """
            if self.state_store is None:
                return
            nonlocal pending_strategy
            if pending_strategy is not None:
                self.state_store.strategy_archive.update(
                    str(pending_strategy.get("rollback_id")), status="expired_unverified"
                )
                pending_strategy = None
            for e in self.state_store.strategy_archive.pending(run_id=run_id):
                self.state_store.strategy_archive.update(
                    str(e.get("rollback_id")), status="expired_unapplied"
                )

        def _rescind_pending(reason: str) -> None:
            """C3 fix: a human adjustment/restart supersedes any applied-but-unverified
            meta-loop patch. Undo the patch (restore pre-patch params) and expire the
            entry BEFORE the human's adjustments land, so (a) verification never
            attributes a human-changed run to the patch and (b) a later rollback can
            never clobber the human's settings.
            """
            nonlocal pending_strategy
            if pending_strategy is None or self.state_store is None:
                return
            for k, v in pre_patch_params.items():
                if v is _MISSING:
                    inner_params.pop(k, None)
                else:
                    inner_params[k] = v
            self.state_store.strategy_archive.update(
                str(pending_strategy.get("rollback_id")),
                status="expired_superseded",
                note=reason,
            )
            pending_strategy = None

        # Progress-emission helper (no-op when no callback is wired).
        def _emit(kind: str, **extra: Any) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(run_id, {"kind": kind, "iter": outer, "max_iters": max_outer_iters, **extra})
            except Exception:
                pass  # never let progress failures crash the run

        outer = 0
        while outer < max_outer_iters:
            # ---- CANCELLATION CHECK: user-requested abort ----
            if cancel_event is not None and cancel_event.is_set():
                _emit("finished", reason="cancelled")
                _expire_pending_strategies()
                return self._summary(run_id, steps, "cancelled",
                    detail="用户通过控制面板中止了本次研究。")

            # ---- BUDGET CHECK (Phase 2): hard stop on time / calls / cost ----
            over = _budget_exceeded()
            if over:
                _emit("finished", reason="budget_exceeded", detail=over)
                self.sdk.record_metric(run_id, "budget.exceeded", 1.0, tags={"reason": over})
                _expire_pending_strategies()
                return self._summary(run_id, steps, "exited_budget", detail=over)

            # ---- PLAYBOOK + EXPERIENCE INJECTION (inner loop only; audit stays curated) ----
            dispatch_goal = goal
            if self.state_store is not None:
                pb_text = self.state_store.playbook.render(pb_scope, k=6)
                if pb_text:
                    inner_params["playbook_context"] = pb_text
                    dispatch_goal = f"{goal}\n\n[PLAYBOOK — 跨 run 经验，仅供参考]\n{pb_text}"
                # Experience lifecycle (Phase 2): auto-replay top decayed-confidence
                # lessons into the inner loop — previously write-only, now read back.
                exp_entries = self.state_store.experience_bank.query(stage=inner_capability, k=3)
                if exp_entries:
                    exp_text = "\n".join(
                        f"- [{e.kind}] {e.lesson} (conf={e.confidence:.2f}, uses={e.uses})"
                        for e in exp_entries
                    )
                    inner_params["experience_context"] = exp_text
                    dispatch_goal = f"{dispatch_goal}\n\n[EXPERIENCE — 跨 run 经验回放]\n{exp_text}"

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
                    _expire_pending_strategies()
                    return self._summary(run_id, steps, "failed", detail=msg)
                stage, result = self.dispatch_open_goal(
                    run_id,
                    dispatch_goal,
                    candidate_capabilities=agent_candidates,
                    agent_config=inner_agent_config,
                )
            else:
                stage, result = self.run_capability(run_id, inner_capability, inner_params)
            inner_detail = result.detail
            inner_event = result.event
            inner_acc = 0.0
            inner_metrics: dict[str, Any] = {}
            if inner_event:
                raw_metrics = getattr(inner_event, "metrics", None)
                if isinstance(raw_metrics, dict):
                    inner_metrics = raw_metrics
                    inner_acc = _primary_score(
                        inner_metrics, str(inner_params.get("op") or obj.get("op", "ge"))
                    )
            # C1 fix (agent_inner audit blindness): an agent turn usually ends with
            # event=None — the capability it invoked already emitted the real eval
            # event. Fall back to the run's latest eval_completed so the external
            # audit sees real metrics instead of an empty result.
            fb_ref: str | None = None
            fb_gate = False
            if not inner_metrics:
                for e in reversed(self.svc.list_events(run_id)):
                    if e.get("event_type") == "eval_completed" and isinstance(
                        e.get("metrics"), dict
                    ):
                        inner_metrics = e["metrics"]
                        inner_acc = _primary_score(
                            inner_metrics, str(inner_params.get("op") or obj.get("op", "ge"))
                        )
                        fb_ref = e.get("report_ref")
                        fb_gate = bool(e.get("gate_passed"))
                        break
            result_ref = (getattr(inner_event, "report_ref", None) if inner_event else None) or fb_ref
            result_gate = (
                bool(getattr(inner_event, "gate_passed", False)) if inner_event else False
            ) or fb_gate

            # ---- POST-HOC VERIFICATION of the pending meta-loop patch ----
            # (AHE decision observability: every accepted edit is a falsifiable claim —
            # verify the prediction against THIS inner metric; rollback on regression.)
            if pending_strategy is not None and self.state_store is not None:
                pred = pending_strategy.get("prediction") or {}
                baseline = float(pred.get("baseline", 0.0))
                tol = float(pred.get("tolerance", 0.002))
                delta = round(inner_acc - baseline, 4)
                rid = str(pending_strategy.get("rollback_id"))
                if delta >= -tol:
                    self.state_store.strategy_archive.update(
                        rid, status="verified", actual_accuracy=inner_acc, actual_delta=delta
                    )
                    self.sdk.record_metric(
                        run_id, "improvement.verified", 1.0,
                        # M1: attribution context — the delta also reflects the current
                        # playbook/experience injection, not only the param patch.
                        tags={
                            "rollback_id": rid,
                            "pb_entries": str(len(self.state_store.playbook.list_entries(pb_scope))),
                        },
                    )
                else:
                    # Prediction falsified: REAL rollback — restore pre-patch params and
                    # mark the entry so it is never re-applied.
                    self.state_store.strategy_archive.rollback(rid)
                    for k, v in pre_patch_params.items():
                        if v is _MISSING:
                            inner_params.pop(k, None)
                        else:
                            inner_params[k] = v
                    self.sdk.record_metric(
                        run_id, "improvement.rolled_back", 1.0,
                        tags={"rollback_id": rid, "delta": str(delta)},
                    )
                pending_strategy = None

            # Cumulative state: observe a hypothesis node from this inner answer.
            node_id = self.sdk.observe_hypothesis(
                hypothesis=f"{inner_capability} -> cv accuracy={inner_acc:.4f}",
                evidence_refs=[inner_event.report_ref] if inner_event else [],
                score=inner_acc,
                run_id=run_id,
            )
            steps.append(self._step(f"inner[{outer}]", stage, result, None))
            _emit("inner_done", metrics=inner_metrics, accuracy=inner_acc)

            # ---- COLLABORATION (step_confirm): pause after inner loop for human review ----
            if collaboration_mode == "step_confirm":
                adj = self._collab_pause(run_id, {
                    "stage": "inner_loop",
                    "iteration": outer,
                    "max_iters": max_outer_iters,
                    "summary": f"内循环 #{outer+1}/{max_outer_iters} 完成: accuracy={inner_acc:.4f}",
                    "metrics": inner_metrics,
                    "collab_mode": collaboration_mode,
                })
                if adj is None:
                    self.svc.set_run_status(run_id, "failed", detail="collaboration: user aborted after inner loop")
                    _expire_pending_strategies()
                    return self._summary(run_id, steps, "collaboration_aborted")
                _rescind_pending("human adjustment after inner loop")
                self._apply_collab_adjustments(inner_params, audit_params, adj)

            # ---- OUTER AUDIT: independent constraint-wise verification ----
            # The control plane curates a SCOPED audit input: the outer auditor sees only
            # the objective + the inner loop's *result* (metrics/verdict) + prior outer-loop
            # verdicts. It never sees the inner loop's experimental narrative, which is what
            # keeps the audit scientifically independent and free of evaluation bias.
            audit_input = {
                # I5 fix: the audit's objective is the RAW research goal — never the
                # refine-folded goal (which carries inner-loop instructions/context).
                "objective": base_goal,
                "result_metrics": inner_metrics,
                "result_report_ref": result_ref,
                "result_gate_passed": result_gate,
                "result_real_eval": bool(result_ref and "kaggle" in str(result_ref)),
                "prior_audits": list(prior_audits),
                "constraints": audit_params.get("constraints", []),
            }
            audit_stage, audit_result = self.run_capability(
                run_id,
                "layer_11_external_audit",
                {**audit_params, "objective": base_goal, "audit_input": audit_input},
            )
            audit_event = audit_result.event
            _route, decision = self.decide_and_record(run_id, audit_event)
            prior_audits.append({
                "confidence": getattr(audit_event, "confidence", None),
                "recommendation": decision.decision_type.value,
                "unresolved": getattr(audit_event, "unresolved_claims", []),
            })
            steps.append(self._step(f"audit[{outer}]", audit_stage, audit_result, decision))
            _emit("audit_done",
                confidence=getattr(audit_event, "confidence", 0),
                recommendation=decision.decision_type.value,
                gate_passed=getattr(audit_event, "gate_passed", False),
            )

            # ---- COLLABORATION (step_confirm): pause after outer audit for human review ----
            if collaboration_mode == "step_confirm":
                adj = self._collab_pause(run_id, {
                    "stage": "outer_audit",
                    "iteration": outer,
                    "max_iters": max_outer_iters,
                    "summary": (
                        f"外审计 #{outer+1}/{max_outer_iters} 完成: "
                        f"confidence={getattr(audit_event,'confidence',0):.2f}, "
                        f"recommendation={decision.decision_type.value}"
                    ),
                    "verdict": {
                        "confidence": getattr(audit_event, "confidence", 0),
                        "recoverable": getattr(audit_event, "recoverable", True),
                        "gate_passed": getattr(audit_event, "gate_passed", True),
                        "recommendation": decision.decision_type.value,
                        "unresolved": getattr(audit_event, "unresolved_claims", []),
                    },
                    "collab_mode": collaboration_mode,
                })
                if adj is None:
                    self.svc.set_run_status(run_id, "failed", detail="collaboration: user aborted after audit")
                    _expire_pending_strategies()
                    return self._summary(run_id, steps, "collaboration_aborted")
                _rescind_pending("human adjustment after audit")
                self._apply_collab_adjustments(inner_params, audit_params, adj)

            # Cumulative state: backpropagate the audit insight onto the hypothesis node.
            rejected = list(getattr(audit_event, "rejected_candidates", []) or [])

            # ---- PLAYBOOK REFLECTION (ACE): distill this round into the evolving
            # playbook — positive evidence on accept, negative on refine/restart,
            # plus mined recurring failure modes. Curator dedups deterministically.
            if self.state_store is not None:
                self.state_store.playbook.merge(
                    reflect_round(
                        capability=inner_capability,
                        inner_params=inner_params,
                        metrics=inner_metrics,
                        decision=decision.decision_type.value,
                        confidence=getattr(audit_event, "confidence", None),
                        unresolved=list(getattr(audit_event, "unresolved_claims", []) or []),
                        rejected=rejected,
                        failure_modes=mine_failure_modes(self.svc.list_events(run_id)),
                    ),
                    scope=pb_scope,
                    run_id=run_id,
                    iter_no=outer,
                )
                # Experience lifecycle (Phase 2): auto-distill each round into the
                # cross-run bank. Dedup-merge inside the bank prevents duplicates;
                # success/failure lessons are separated by kind.
                if decision.decision_type == DecisionType.EXIT_SUCCESS:
                    self.sdk.register_experience(
                        "success",
                        pb_scope,
                        f"{inner_capability} 成功路径：model={inner_params.get('model', '?')}, "
                        f"fe={inner_params.get('fe', '?')} → acc={inner_acc:.4f}（外审计通过）",
                        [inner_capability],
                        0.7,
                        source_run_id=run_id,
                    )
                else:
                    bits = rejected[:2] or list(
                        getattr(audit_event, "unresolved_claims", []) or []
                    )[:2]
                    if bits:
                        self.sdk.register_experience(
                            "failure",
                            pb_scope,
                            "避免：" + "；".join(str(b) for b in bits),
                            [inner_capability],
                            0.6,
                            source_run_id=run_id,
                        )

            if decision.decision_type == DecisionType.EXIT_SUCCESS:
                if node_id:
                    self.sdk.backpropagate_insight(node_id, "accepted by external audit", audit_event.confidence)
                    self.sdk.mark_merged(node_id)
                _emit("finished", reason="accepted", accuracy=inner_acc)
                _expire_pending_strategies()
                return self._summary(run_id, steps, "exited_converged")
            # Refine / Restart: fold unresolved claims into the goal and loop again.
            unresolved = [rc for rc in decision.reason_codes if rc not in ("audit_refine", "audit_restart")]
            if node_id:
                self.sdk.backpropagate_insight(node_id, audit_result.detail, audit_event.confidence)
            if "audit_refine" in decision.reason_codes:
                goal = f"{base_goal}\n[REFINE] address: " + "; ".join(unresolved)
                if rejected:
                    # Negative results finally get a consumer: the next trajectory must
                    # not repeat externally-rejected directions (learning from failure
                    # shrinks the search space).
                    goal += "\n[AVOID] previously rejected: " + "; ".join(rejected[:5])
                    inner_params["rejected_candidates"] = rejected[:5]
                # A refine MUST change the inner attempt, otherwise it just repeats
                # the same trajectory. The hook lets callers mutate the inner params
                # (e.g. switch model / features) based on the audit outcome.
                if refine_hook is not None:
                    new_params = refine_hook(outer, dict(inner_params), audit_event)
                    if new_params:
                        inner_params = new_params
            elif "audit_restart" in decision.reason_codes:
                goal = base_goal  # discard trajectory, restart from the raw objective
                # I2 fix: a restart must ACTUALLY discard the trajectory — refine_hook
                # mutations, applied patches and rejected-candidate baggage all reset
                # (this now matches the collab-restart path semantics).
                inner_params = {**orig_inner_params}
                if node_id:
                    self.sdk.mark_pruned(node_id)  # keep as a stepping stone
            else:
                # I6 fix: an unrecognized decision (e.g. the CLI-decide fallback's
                # bare CONTINUE) must CHANGE the trajectory like a refine — silently
                # repeating the same goal/params spins the loop until budget exhaustion.
                self.sdk.record_metric(
                    run_id, "loop.unrecognized_decision", 1.0,
                    tags={"decision": decision.decision_type.value},
                )
                goal = (
                    f"{base_goal}\n[REFINE] address: unrecognized decision "
                    f"{decision.decision_type.value}; improve on the previous attempt"
                )
                if refine_hook is not None:
                    new_params = refine_hook(outer, dict(inner_params), audit_event)
                    if new_params:
                        inner_params = new_params

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
                se_params: dict[str, Any] = {"inner_params": dict(inner_params)}
                if strategy_db:
                    se_params["strategy_db"] = strategy_db
                se_stage, se_result = self.run_self_evolution(run_id, params=se_params)
                steps.append(self._step(f"self_evo[{outer}]", se_stage, se_result, None))
            except NotFoundError:
                pass  # layer_09 still a stub in this deployment

            # ---- APPLY the accepted meta-loop patch (closes propose → apply →
            # evaluate → accept/rollback): the patch lands on the NEXT inner loop and
            # is verified against its metric (see the verification block above).
            if self.state_store is not None and pending_strategy is None:
                pend = self.state_store.strategy_archive.pending(run_id=run_id)
                if pend:
                    strat = pend[-1]
                    param_patch = strat.get("inner_param_patch") or {}
                    if param_patch:
                        pre_patch_params = {
                            k: inner_params.get(k, _MISSING) for k in param_patch
                        }
                        inner_params.update(param_patch)
                        # I1 fix: the falsifiable prediction must be verified against
                        # the PRE-PATCH configuration's metric (the inner loop that
                        # just ran), not the run's historical best — otherwise a
                        # genuinely helpful patch gets falsely rolled back whenever
                        # an earlier configuration scored higher.
                        pred = dict(strat.get("prediction") or {})
                        pred["baseline"] = inner_acc
                        self.state_store.strategy_archive.update(
                            str(strat.get("rollback_id")), prediction=pred
                        )
                        strat["prediction"] = pred
                        pending_strategy = strat

            # ---- COLLABORATION: pause after full outer-loop iteration (step_confirm / outer_confirm) ----
            if collaboration_mode in ("step_confirm", "outer_confirm"):
                adj = self._collab_pause(run_id, {
                    "stage": "outer_complete",
                    "iteration": outer,
                    "max_iters": max_outer_iters,
                    "summary": (
                        f"第 {outer+1}/{max_outer_iters} 轮完成 | "
                        f"inner_acc={inner_acc:.4f} | "
                        f"audit_confidence={getattr(audit_event,'confidence',0):.2f} | "
                        f"next={decision.decision_type.value}"
                    ),
                    "metrics": {"accuracy": inner_acc},
                    "verdict": {"confidence": getattr(audit_event, "confidence", 0)},
                    "collab_mode": collaboration_mode,
                })
                if adj is None:
                    self.svc.set_run_status(run_id, "failed", detail="collaboration: user aborted after outer iter")
                    _expire_pending_strategies()
                    return self._summary(run_id, steps, "collaboration_aborted")
                _rescind_pending("human adjustment after outer iteration")
                self._apply_collab_adjustments(inner_params, audit_params, adj)
                if adj.get("action") == "restart":
                    goal = base_goal
                    inner_params = {**orig_inner_params}
                    # I10 fix: a restart must not silently discard the human's
                    # same-batch adjustments — re-apply them onto the clean params.
                    self._apply_collab_adjustments(inner_params, audit_params, adj)

            outer += 1

        _emit("finished", reason="budget", accuracy=inner_acc)
        _expire_pending_strategies()
        return self._summary(run_id, steps, "exited_budget")

    # ------------------------------------------------------- evolutionary search
    def run_evolutionary_loop(
        self,
        run_id: str,
        inner_capability: str = "kaggle_eval",
        inner_params: dict[str, Any] | None = None,
        audit_params: dict[str, Any] | None = None,
        population_size: int = 4,
        generations: int = 2,
        max_workers: int = 4,
        novelty_threshold: float = 0.92,
        surface: dict[str, tuple[Any, ...]] | None = None,
        budget: dict[str, Any] | None = None,
        seed: int = 42,
        progress_callback: Any | None = None,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        """PARALLEL EVOLUTIONARY SEARCH (Phase 3) over the inner-loop config space.

        AlphaEvolve/ShinkaEvolve mechanics on top of platform primitives:

          * a **population** of candidates (points on the editable surface) is evaluated
            **in parallel** (file-backed task manager: crash-safe, resumable);
          * each candidate becomes a HypothesisTree node on its own **branch**
            (``gen{g}``) — the tree's branch field finally carries real structure;
          * the generation **champion** (best fitness) faces the frozen external audit
            (Accept -> converged; else the population breeds the next generation via
            fitness-proportional parent selection + bounded mutation + novelty
            rejection — the diversity-collapse guard);
          * budgets (time / capability calls / cost) hard-stop the search.
        """

        run = self.svc.get_workflow_run(run_id)
        obj = run.objective_snapshot or {}
        base_goal = obj.get("goal") or obj.get("objective") or run.target_id
        base_params = dict(inner_params or {})
        audit_params = dict(audit_params or {})
        surface = surface or DEFAULT_SURFACE
        rng = _random.Random(seed)
        archive = (
            self.state_store.evolution if self.state_store is not None else EvolutionArchive()
        )
        work_dir = (
            _os.path.join(_os.path.dirname(self.state_store.db_path), "evolution_tasks", run_id)
            if self.state_store is not None and self.state_store.db_path
            else _os.path.join(_tempfile.gettempdir(), "evolution_tasks", run_id)
        )
        task_mgr = BackgroundTaskManager(work_dir)

        cap = self.capability_registry.resolve(inner_capability)
        if cap is None:
            raise NotFoundError(f"no capability registered: {inner_capability}")

        budget_cfg = dict(budget or (obj.get("config") or {}).get("budget") or {})
        _t0 = _time.monotonic()

        def _budget_exceeded() -> str | None:
            if budget_cfg.get("max_seconds") is not None and (
                _time.monotonic() - _t0 > float(budget_cfg["max_seconds"])
            ):
                return f"时间预算耗尽（>{budget_cfg['max_seconds']}s）"
            if budget_cfg.get("max_capability_calls") is not None and (
                self._cap_count(run_id) >= int(budget_cfg["max_capability_calls"])
            ):
                return f"能力调用预算耗尽（≥{budget_cfg['max_capability_calls']} 次）"
            if budget_cfg.get("max_cost") is not None and (
                self._cap_count(run_id) * float(budget_cfg.get("cost_per_call", 1.0))
                >= float(budget_cfg["max_cost"])
            ):
                return f"成本预算耗尽（≥{budget_cfg['max_cost']} 单位）"
            return None

        def _emit(kind: str, **extra: Any) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(run_id, {"kind": kind, **extra})
            except Exception:
                pass

        def _eval_candidate(cand: Any) -> dict[str, Any]:
            """Worker: run the capability with a BUFFERED sdk (no shared-state writes)."""

            buf_events: list[Any] = []
            buf_artifacts: list[tuple[Any, str, Any]] = []
            buf_metrics: list[tuple[Any, ...]] = []

            class _BufSDK:
                def load_object(self, ref: str) -> Any:
                    return self_sdk.load_object(ref)  # read-only: safe across threads

                def emit_event(self, event: Any) -> None:
                    buf_events.append(event)

                def publish_artifact(self, payload: dict, schema_version: str,
                                     metadata: dict | None = None) -> Any:
                    buf_artifacts.append((payload, schema_version, metadata))
                    return _SimpleNamespace(artifact_id=f"buf-{len(buf_artifacts)}")

                def record_metric(self, *a: Any, **k: Any) -> None:
                    buf_metrics.append((a, k))

            self_sdk = self.sdk
            executor = cap.executor or self.registry.resolve(cap.layer_code)
            stub = _SimpleNamespace(
                stage_run_id=f"evo-{cand.candidate_id}", run_id=run_id, input_refs=[]
            )
            result = executor.execute(
                stub, _BufSDK(), {**base_params, **cand.params, "n_jobs": 1}
            )
            return {
                "final_status": result.final_status.value,
                "gate": result.gate_result.value,
                "metrics": getattr(result.event, "metrics", {}) if result.event else {},
                "report_ref": getattr(result.event, "report_ref", None) if result.event else None,
                "gate_passed": bool(getattr(result.event, "gate_passed", False))
                if result.event else False,
                "detail": result.detail,
                "events": [e.model_dump(mode="json") for e in buf_events],
                "artifacts": buf_artifacts,
                "metrics_log": buf_metrics,
            }

        def _replay(cand: Any, payload: dict[str, Any]) -> None:
            """Main thread: fold a worker's buffered output into the control plane."""

            from ..platform_contracts.events import EvalCompletedEvent

            stage = self.svc.create_stage_run(
                run_id,
                CreateStageRunRequest(stage_code=cap.layer_code, executor_family="capability"),
            )
            self.svc.update_stage_status(
                stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.RUNNING)
            )
            result = payload["result"]
            for ev_dict in result["events"]:
                ev_dict["stage_run_id"] = stage.stage_run_id
                # M3 fix: rewrite the worker's stub report_ref to the REAL stage so
                # audit / hypothesis evidence never points at a non-existent stage.
                if ev_dict.get("report_ref"):
                    ev_dict["report_ref"] = f"kaggle-eval://{stage.stage_run_id}"
                try:
                    self.svc.append_event(EvalCompletedEvent.model_validate(ev_dict))
                except Exception:
                    pass
            for art_payload, schema_version, metadata in result["artifacts"]:
                art_payload = {
                    **art_payload,
                    "producer_ref": stage.stage_run_id,
                    "uri": f"kaggle-eval://{stage.stage_run_id}",
                }
                self.sdk.publish_artifact(art_payload, schema_version, metadata)
            for args, kwargs in result["metrics_log"]:
                self.sdk.record_metric(*args, **kwargs)
            self.svc.update_stage_status(
                stage.stage_run_id,
                UpdateStageStatusRequest(to_status=StageStatus(result["final_status"])),
            )
            stage.gate_result = GateResult(result["gate"])
            self.svc._repo.put_stage_run(stage)

        steps: list[dict[str, Any]] = []
        prior_audits: list[dict[str, Any]] = []
        report_refs: dict[str, str] = {}
        gate_flags: dict[str, bool] = {}
        population = _evo_seed_population(
            base_params, surface=surface, size=population_size, run_id=run_id, rng=rng
        )
        for c in population:
            archive.add(c)

        champion = None
        for gen in range(generations):
            # M10 fix: user cancellation + budget checks (generation top AND inside
            # the replay loop so one generation cannot overspend unboundedly).
            if cancel_event is not None and cancel_event.is_set():
                _emit("finished", reason="cancelled")
                return {
                    **self._summary(run_id, steps, "cancelled",
                                    detail="用户通过控制面板中止了本次进化搜索。"),
                    "best_candidate": _cand_dict(archive.best(run_id)),
                }
            over = _budget_exceeded()
            if over:
                self.sdk.record_metric(run_id, "budget.exceeded", 1.0, tags={"reason": over})
                _emit("finished", reason="budget_exceeded", detail=over)
                return {
                    **self._summary(run_id, steps, "exited_budget", detail=over),
                    "best_candidate": _cand_dict(archive.best(run_id)),
                }

            # ---- parallel fitness evaluation (crash-safe, resumable) ----
            pending = [c for c in population if c.status == "proposed"]
            tasks = {c.candidate_id: (lambda c=c: _eval_candidate(c)) for c in pending}
            results = task_mgr.run_all(tasks, max_workers=max_workers)
            for c in pending:
                over = _budget_exceeded()
                if over:
                    self.sdk.record_metric(run_id, "budget.exceeded", 1.0, tags={"reason": over})
                    _emit("finished", reason="budget_exceeded", detail=over)
                    return {
                        **self._summary(run_id, steps, "exited_budget", detail=over),
                        "best_candidate": _cand_dict(archive.best(run_id)),
                    }
                payload = results.get(c.candidate_id) or {}
                if payload.get("status") != "ok":
                    c.status = "error"
                    archive.update(c.candidate_id, status="error")
                    continue
                _replay(c, payload)
                # candidate evals bypass run_capability (direct executor.execute in
                # worker threads), so count them here (C5 fix).
                self._cap_call_counts[run_id] = self._cap_call_counts.get(run_id, 0) + 1
                res = payload["result"]
                metrics = res.get("metrics") or {}
                if res.get("report_ref"):
                    report_refs[c.candidate_id] = res["report_ref"]
                gate_flags[c.candidate_id] = bool(res.get("gate_passed"))
                # I8 fix: direction-aware fitness (log_loss-style objectives select the
                # true best, not the worst).
                c.fitness = _primary_score(
                    metrics, str(base_params.get("op") or obj.get("op", "ge"))
                )
                c.metrics = metrics
                c.status = "evaluated"
                archive.update(
                    c.candidate_id, fitness=c.fitness, metrics=metrics, status="evaluated"
                )
                # HypothesisTree: one node per candidate, on the generation's branch.
                if self.state_store is not None:
                    self.sdk.observe_hypothesis(
                        hypothesis=(
                            f"[evo gen{c.generation}] params={c.params} -> "
                            f"fitness={c.fitness:.4f}"
                        ),
                        evidence_refs=[res.get("report_ref")] if res.get("report_ref") else [],
                        score=max(0.0, min(1.0, c.fitness)),
                        branch=c.branch,
                        run_id=run_id,
                    )
            evaluated = [c for c in population if c.status == "evaluated"]
            if not evaluated:
                return {
                    **self._summary(run_id, steps, "failed",
                                    detail="所有候选评估失败，请检查内循环配置。"),
                    "best_candidate": None,
                }
            champion = max(evaluated, key=lambda c: c.fitness or 0.0)
            archive.update(champion.candidate_id, status="champion")
            steps.append({
                "stage": f"population[{gen}]",
                "evaluated": len(evaluated),
                "champion": champion.candidate_id,
                "champion_fitness": champion.fitness,
                "detail": f"gen{gen}: {len(evaluated)} candidates, best={champion.fitness:.4f}",
            })
            _emit("generation_done", generation=gen, champion_fitness=champion.fitness)

            # ---- generation champion faces the frozen external audit ----
            audit_input = {
                "objective": base_goal,
                "result_metrics": champion.metrics,
                "result_report_ref": report_refs.get(champion.candidate_id),
                "result_gate_passed": gate_flags.get(champion.candidate_id, False),
                "result_real_eval": True,
                "prior_audits": list(prior_audits),
                "constraints": audit_params.get("constraints", []),
            }
            audit_stage, audit_result = self.run_capability(
                run_id,
                "layer_11_external_audit",
                {**audit_params, "objective": base_goal, "audit_input": audit_input},
            )
            audit_event = audit_result.event
            _route, decision = self.decide_and_record(run_id, audit_event)
            prior_audits.append({
                "confidence": getattr(audit_event, "confidence", None),
                "recommendation": decision.decision_type.value,
                "unresolved": getattr(audit_event, "unresolved_claims", []),
            })
            steps.append(self._step(f"audit[{gen}]", audit_stage, audit_result, decision))
            _emit("audit_done", generation=gen,
                  confidence=getattr(audit_event, "confidence", 0),
                  recommendation=decision.decision_type.value)
            if decision.decision_type == DecisionType.EXIT_SUCCESS:
                _emit("finished", reason="accepted", champion=champion.candidate_id)
                return {
                    **self._summary(run_id, steps, "exited_converged"),
                    "best_candidate": _cand_dict(champion),
                }

            # ---- breed the next generation ----
            if gen + 1 < generations:
                archived_params = [c.params for c in archive.list(run_id)]
                bred: list[Any] = []
                while len(bred) < population_size:
                    parent = _evo_select_parent(
                        [c for c in archive.list(run_id) if c.fitness is not None], rng=rng
                    )
                    if parent is None:
                        break
                    child = _evo_mutate(
                        parent, surface=surface, run_id=run_id, generation=gen + 1, rng=rng
                    )
                    parent.offspring_count += 1
                    archive.update(
                        parent.candidate_id, offspring_count=parent.offspring_count
                    )
                    bred.append(child)
                kept = _evo_novelty_filter(
                    bred, archived_params=archived_params, threshold=novelty_threshold
                )
                for c in bred:
                    archive.add(c)  # rejected ones are archived too (negative record)
                population = kept
                if not population:
                    # diversity exhausted: fall back to fresh random points
                    population = _evo_seed_population(
                        base_params, surface=surface, size=population_size,
                        run_id=run_id, rng=rng,
                    )
                    for c in population:
                        c.generation = gen + 1
                        c.branch = f"gen{gen + 1}"
                        archive.add(c)

        _emit("finished", reason="generations", best=champion.candidate_id if champion else None)
        return {
            **self._summary(run_id, steps, "exited_budget",
                            detail=f"已完成 {generations} 代进化搜索，未触发审计通过。"),
            # I7 fix: return the all-time global best, not the last generation's champion.
            "best_candidate": _cand_dict(archive.best(run_id) or champion),
        }

    # ------------------------------------------------------- program-level evolution
    def run_program_evolutionary_loop(
        self,
        run_id: str,
        task_config: Any,
        backend: Any,
        *,
        islands: int = 1,
        pop_per_island: int = 3,
        generations: int = 2,
        max_workers: int = 1,  # sequential by design (avoids macOS OpenMP segfault on 3.13)
        novelty_threshold: float = 0.92,
        audit: bool = True,
        audit_params: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        seed: int = 42,
        progress_callback: Any | None = None,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        """PROGRAM-LEVEL EVOLUTIONARY SEARCH (Phase B/C) over the code space.

        OpenMLE-Evo island model on top of the platform dual-loop primitives: the
        atomic operators (Draft / Improve / Debug / Crossover) produce *program*
        candidates (``node_kind="program"``) instead of hyperparameter configs. Each
        candidate is executed in the verifiable tabular environment (``OpenMLETaskAdapter``)
        and scored; the generation champion faces the SAME frozen external audit as the
        config loop (Accept -> converged).

        Isolation invariants preserved (same guarantees as ``run_evolutionary_loop``):
          * operators run ONLY on the inner loop (``run_operator`` enforces this);
          * ``audit_input`` is built from the champion's *metrics*, never the candidate
            code / identity -- no population narrative leaks to the audit;
          * every candidate carries ``run_id`` (archive + HypothesisTree filtering).
        """

        from ..openmle_integration.adapter import OpenMLETaskAdapter
        from ..openmle_integration.contracts import (
            TEST_FITNESS,
            VALID_SOLUTION,
            VALID_SOLUTION_FEEDBACK,
        )

        run = self.svc.get_workflow_run(run_id)
        obj = run.objective_snapshot or {}
        base_goal = obj.get("goal") or obj.get("objective") or run.target_id
        archive = (
            self.state_store.evolution if self.state_store is not None else EvolutionArchive()
        )
        rng = _random.Random(seed)
        op = str(obj.get("op", "ge"))

        # Build + prepare the verifiable task environment once; state reused per eval.
        adapter = OpenMLETaskAdapter(task_config)
        state, info = adapter.prepare()
        target = info.get("target", "Survived")
        id_col = info.get("id_col", "PassengerId")
        feature_cols = tuple(info.get("feature_cols", []))
        task_description = info.get("TASK_DESCRIPTION", "")
        direction = "lower" if info.get("lower_is_better") else "higher"

        budget_cfg = dict(budget or (obj.get("config") or {}).get("budget") or {})
        _t0 = _time.monotonic()

        def _budget_exceeded() -> str | None:
            if budget_cfg.get("max_seconds") is not None and (
                _time.monotonic() - _t0 > float(budget_cfg["max_seconds"])
            ):
                return f"时间预算耗尽（>{budget_cfg['max_seconds']}s）"
            if budget_cfg.get("max_capability_calls") is not None and (
                self._cap_count(run_id) >= int(budget_cfg["max_capability_calls"])
            ):
                return f"能力调用预算耗尽（≥{budget_cfg['max_capability_calls']} 次）"
            if budget_cfg.get("max_cost") is not None and (
                self._cap_count(run_id) * float(budget_cfg.get("cost_per_call", 1.0))
                >= float(budget_cfg["max_cost"])
            ):
                return f"成本预算耗尽（≥{budget_cfg['max_cost']} 单位）"
            return None

        def _emit(kind: str, **extra: Any) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(run_id, {"kind": kind, **extra})
            except Exception:
                pass

        steps: list[dict[str, Any]] = []
        prior_audits: list[dict[str, Any]] = []

        # ---- seed the island model with Draft-produced program nodes ----
        model = IslandModel(islands, run_id)
        model.seed(
            lambda size, generation=0: _evo_seed_program_population(
                backend, size=size, run_id=run_id, rng=rng, target=target, id_col=id_col,
                feature_cols=feature_cols, task_description=task_description, generation=generation,
            ),
            size=pop_per_island, generation=0,
        )
        for c in model.all_candidates():
            archive.add(c)

        champion: Any = None
        try:
            for gen in range(generations):
                if cancel_event is not None and cancel_event.is_set():
                    _emit("finished", reason="cancelled")
                    return {
                        **self._summary(run_id, steps, "cancelled",
                                        detail="用户通过控制面板中止了本次程序进化搜索。"),
                        "best_candidate": _cand_dict(model.best()),
                    }
                over = _budget_exceeded()
                if over:
                    self.sdk.record_metric(run_id, "budget.exceeded", 1.0, tags={"reason": over})
                    _emit("finished", reason="budget_exceeded", detail=over)
                    return {
                        **self._summary(run_id, steps, "exited_budget", detail=over),
                        "best_candidate": _cand_dict(model.best()),
                    }

                # ---- evaluate every program candidate (sequential, inner loop) ----
                pending = [c for isl in model.islands for c in isl if c.status == "proposed"]
                for c in pending:
                    self._cap_call_counts[run_id] = self._cap_call_counts.get(run_id, 0) + 1
                    state, outcome = adapter.step_task(state, c.code)
                    fitness = outcome.get(TEST_FITNESS)
                    valid = bool(outcome.get(VALID_SOLUTION, False))
                    c.fitness = float(fitness) if (valid and fitness is not None) else None
                    c.metrics = {
                        "primary": c.fitness,
                        "accuracy": fitness,
                        "valid": valid,
                        "feedback": outcome.get(VALID_SOLUTION_FEEDBACK, ""),
                    }
                    c.status = "evaluated" if valid else "error"
                    archive.update(
                        c.candidate_id, fitness=c.fitness, metrics=c.metrics, status=c.status
                    )
                    self.sdk.record_metric(
                        run_id, "program_fitness", c.fitness or 0.0,
                        tags={"operator": c.operator or "draft", "gen": str(c.generation)},
                    )
                    if self.state_store is not None:
                        self.sdk.observe_hypothesis(
                            hypothesis=(
                                f"[prog {c.branch}] {c.operator} -> "
                                f"fitness={c.fitness}"
                            ),
                            evidence_refs=[],
                            branch=c.branch,
                            score=max(0.0, min(1.0, c.fitness or 0.0)),
                            run_id=run_id,
                            node_kind="program",
                        )

                champion = model.best()
                if champion is None:
                    return {
                        **self._summary(run_id, steps, "failed",
                                        detail="所有程序候选评估失败，请检查算子后端与数据。"),
                        "best_candidate": None,
                    }
                archive.update(champion.candidate_id, status="champion")
                steps.append({
                    "stage": f"program_population[{gen}]",
                    "evaluated": len([c for c in model.all_candidates() if c.status == "evaluated"]),
                    "champion": champion.candidate_id,
                    "champion_fitness": champion.fitness,
                    "champion_operator": champion.operator,
                    "detail": f"gen{gen}: best={champion.fitness:.4f} ({champion.operator})",
                })
                _emit("generation_done", generation=gen, champion_fitness=champion.fitness)

                # ---- generation champion faces the frozen external audit ----
                if audit:
                    audit_input = {
                        "objective": base_goal,
                        "result_metrics": champion.metrics,
                        "result_report_ref": None,
                        "result_gate_passed": True,
                        "result_real_eval": True,
                        "prior_audits": list(prior_audits),
                        "constraints": (audit_params or {}).get("constraints", []),
                    }
                    audit_stage, audit_result = self.run_capability(
                        run_id,
                        "layer_11_external_audit",
                        {**(audit_params or {}), "objective": base_goal, "audit_input": audit_input},
                    )
                    audit_event = audit_result.event
                    _route, decision = self.decide_and_record(run_id, audit_event)
                    prior_audits.append({
                        "confidence": getattr(audit_event, "confidence", None),
                        "recommendation": decision.decision_type.value,
                        "unresolved": getattr(audit_event, "unresolved_claims", []),
                    })
                    steps.append(self._step(f"audit[{gen}]", audit_stage, audit_result, decision))
                    _emit("audit_done", generation=gen,
                          confidence=getattr(audit_event, "confidence", 0),
                          recommendation=decision.decision_type.value)
                    if decision.decision_type == DecisionType.EXIT_SUCCESS:
                        _emit("finished", reason="accepted", champion=champion.candidate_id)
                        return {
                            **self._summary(run_id, steps, "exited_converged"),
                            "best_candidate": _cand_dict(champion),
                        }

                # ---- breed the next generation (Improve/Debug + Crossover) ----
                if gen + 1 < generations:
                    self._breed_program_generation(
                        model, backend, gen + 1, rng, target, id_col, feature_cols,
                        task_description, novelty_threshold, archive, run_id, op,
                    )
                    model.migrate(top_k=1, current_generation=gen + 1)
        finally:
            try:
                adapter.close(state)
            except Exception:
                pass

        _emit("finished", reason="generations", best=champion.candidate_id if champion else None)
        return {
            **self._summary(run_id, steps, "exited_budget",
                            detail=f"已完成 {generations} 代程序进化搜索，未触发审计通过。"),
            "best_candidate": _cand_dict(model.best() or champion),
        }

    def _breed_program_generation(
        self,
        model: Any,
        backend: Any,
        generation: int,
        rng: Any,
        target: str,
        id_col: str,
        feature_cols: tuple[str, ...],
        task_description: str,
        novelty_threshold: float,
        archive: Any,
        run_id: str,
        op: str,
    ) -> None:
        """Breed the next program generation per island: Improve valid parents, Debug
        failed ones, Crossover the two best, then novelty-filter by source code."""

        from ..openmle_integration.operators import build_debug_feedback

        for idx, island in enumerate(model.islands):
            evaluated = [c for c in island if c.fitness is not None]
            if not evaluated:
                # island collapsed: reseed it
                fresh = _evo_seed_program_population(
                    backend, size=max(len(island), 1), run_id=run_id,
                    rng=rng, target=target, id_col=id_col, feature_cols=feature_cols,
                    task_description=task_description, generation=generation,
                )
                for c in fresh:
                    c.branch = f"gen{generation}.isl{idx}"
                island[:] = fresh
                for c in fresh:
                    archive.add(c)
                continue

            children = []
            while len(children) < max(len(island), 1):
                parent = _evo_select_parent(evaluated, rng=rng)
                if parent is None:
                    break
                if (parent.fitness or 0.0) < 0.4:
                    child = _evo_mutate_program(
                        parent, "debug", backend, run_id=run_id,
                        generation=generation, rng=rng, target=target, id_col=id_col,
                        feature_cols=feature_cols, task_description=task_description,
                        feedback=build_debug_feedback(parent.metrics or {}, events=None),
                    )
                else:
                    child = _evo_mutate_program(
                        parent, "improve", backend, run_id=run_id,
                        generation=generation, rng=rng, target=target, id_col=id_col,
                        feature_cols=feature_cols, task_description=task_description,
                        feedback=None,
                    )
                children.append(child)
            # one Crossover of the two best parents (multi-parent transform)
            if len(evaluated) >= 2:
                best_two = sorted(evaluated, key=lambda c: c.fitness or 0.0, reverse=True)[:2]
                children.append(_evo_crossover_programs(
                    best_two[0], best_two[1], backend, run_id=run_id,
                    generation=generation, target=target, id_col=id_col,
                    feature_cols=feature_cols, task_description=task_description,
                ))
            archived_codes = [c.code for c in archive.list_by_kind(run_id, "program") if c.code]
            kept = _evo_novelty_filter(children, archived_codes=archived_codes, threshold=novelty_threshold)
            # diversity-collapse guard: never let novelty filtering kill the whole island
            if not kept:
                kept = list(children)
            for c in children:
                c.branch = f"gen{generation}.isl{idx}"
                archive.add(c)
            # elitism: carry the best parent forward so quality/diversity is retained
            best_parent = max(evaluated, key=lambda c: c.fitness or 0.0)
            best_parent.generation = generation
            best_parent.branch = f"gen{generation}.isl{idx}"
            if best_parent not in kept:
                kept.append(best_parent)
            island[:] = kept

    # --------------------------------------------------- human-in-the-loop collaboration
    def _collab_pause(self, run_id: str, step_context: dict[str, Any]) -> dict[str, Any] | None:
        """Pause for human review (blocking, called from the background thread).

        Sets the run to ``WAITING_APPROVAL``, emits an ``ApprovalRequiredEvent``, and blocks
        until the HTTP ``/resolve-collaboration`` endpoint signals completion. Returns the
        human's adjustments dict, or ``None`` when the human rejected / aborted.
        """
        evt = threading.Event()
        with _COLLAB_LOCK:
            _COLLAB_PAUSES[run_id] = {"evt": evt, "adjustments": None, "rejected": False}

        self.svc.request_approval(
            run_id,
            RequestApprovalRequest(
                subject_type="collaboration",
                reason=_json.dumps(step_context, ensure_ascii=False, default=str),
                policy_ref=step_context.get("collab_mode", "step_confirm"),
            ),
        )

        # Wait for the human to resolve (approve + adjustments, or reject).
        while True:
            if evt.wait(timeout=2):
                break
            try:
                r = self.svc.get_workflow_run(run_id)
                if r.status in (WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
                    with _COLLAB_LOCK:
                        _COLLAB_PAUSES.pop(run_id, None)
                    return None
            except Exception:
                pass

        with _COLLAB_LOCK:
            state = _COLLAB_PAUSES.pop(run_id, {"rejected": True})
        if state.get("rejected"):
            return None
        return state.get("adjustments") or {}

    @staticmethod
    def _apply_collab_adjustments(
        inner_params: dict[str, Any],
        audit_params: dict[str, Any],
        adj: dict[str, Any],
    ) -> None:
        """Fold the human's adjustments into the mutable inner/audit param dicts."""
        if adj.get("model"):
            inner_params["model"] = adj["model"]
        if adj.get("fe"):
            inner_params["fe"] = adj["fe"]
        if adj.get("cv_folds"):
            inner_params["cv_folds"] = int(adj["cv_folds"])
        if adj.get("threshold") is not None:
            inner_params["threshold"] = float(adj["threshold"])
        if adj.get("data_dir"):
            inner_params["data_dir"] = adj["data_dir"]
        if adj.get("audit_threshold") is not None:
            audit_params["threshold"] = float(adj["audit_threshold"])

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

    def expire_pending_strategies(self, run_id: str) -> None:
        """Public hook (I3 fix): expire pending meta-loop proposals when a run's loop
        ended abnormally (exception / abort outside ``run_dual_loop``'s own returns),
        so ``pending()`` semantics stay honest for observability.
        """

        if self.state_store is None:
            return
        for e in self.state_store.strategy_archive.pending(run_id=run_id):
            self.state_store.strategy_archive.update(
                str(e.get("rollback_id")), status="expired_unapplied"
            )

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
