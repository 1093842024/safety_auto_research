"""MEA loop core (Manage-Execute-Audit) — LongHorizon-Harness upgrade, Phase P1.

This is a self-contained, testable function. ``ClosedLoopOrchestrator.run_mea_loop``
delegates to it with a real ``RoleAgentRegistry`` and an environment snapshot.

Loop (per the paper, §2):
  Manager : ``TaskState.next_subtask_contract`` -> bounded ``SubtaskContract`` ``c_i``
  Executor: fresh-context agent runs ``c_i`` -> unverified ``ExecOutput`` ``o_i``
  Auditor : read-only, independent agent verifies -> ``AuditVerdict`` ``v_i``
  Manager : apply ONLY ``v_i`` to the trusted ``TaskState`` ``S``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..control_plane.task_state import STRONG_MODEL_STEPS  # noqa: F401  (catalog re-export)
from ..control_plane.task_state import TaskState
from .agents import RoleAgentRegistry


# ------------------------------------------------------------------ cost model
# Per-backend relative cost weight for the simulated proxy. The reserved strong
# reasoning model (R0 Manager + R7 meta-decider) is priced highest; cheap/appropriate
# execution agents are priced far lower. This is a *proxy* for the real spend curve,
# not a billing figure — it exists so P3 can quantify how much the selective-routing
# strategy saves versus the naive "spend the strong model on every step" baseline.
BACKEND_COST: dict[str, int] = {
    "deterministic": 0,
    "workbuddy": 3,
    "llm_api": 5,
    "claude_code": 10,
    "codex": 10,
}
STRONG_MODEL_COST = 20


@dataclass
class MeaMetrics:
    """Quantified routing/cost telemetry for one MEA run (P3 measurement goal).

    ``savings_ratio`` answers the core P3 question: how much cheaper is the selective
    strategy (strong model only on R0+R7; cheap agents on R1–R5/R8–R10; meta invoked
    only every ``meta_every`` rounds) than the naive baseline where the strong model is
    spent on *every* executor step and a meta decision is taken *every* round.
    """

    rounds: int = 0
    executor_calls_by_backend: dict[str, int] = field(default_factory=dict)
    strong_model_calls: int = 0
    converged: bool = False
    cost_proxy: int = 0
    naive_cost_proxy: int = 0
    last_meta_decision: str | None = None

    @property
    def savings_ratio(self) -> float:
        return (self.naive_cost_proxy / self.cost_proxy) if self.cost_proxy else 0.0

    def executor_cost(self) -> int:
        return sum(BACKEND_COST.get(b, 5) * n for b, n in self.executor_calls_by_backend.items())


def _finalize_metrics(metrics: MeaMetrics, meta_every: int) -> None:
    """Compute the actual + naive cost proxies from the raw counters."""

    metrics.cost_proxy = metrics.executor_cost() + metrics.strong_model_calls * STRONG_MODEL_COST
    # Naive baseline: strong model on EVERY executor step + a meta decision EVERY round,
    # plus the one R0 Manager decomposition both strategies share.
    metrics.naive_cost_proxy = (
        STRONG_MODEL_COST  # R0 Manager decompose (always happens on both strategies)
        + metrics.rounds * STRONG_MODEL_COST  # every executor step would use the strong model
        + metrics.rounds * STRONG_MODEL_COST  # meta decision invoked every round
    )


def run_mea_loop_core(
    registry: RoleAgentRegistry,
    run_id: str,
    objective: str,
    max_rounds: int = 25,
    data_dir: str | None = None,
    env_snapshot_fn: Any = None,
    plan: list[str] | None = None,
    max_subtasks: int = 8,
    meta_every: int = 5,
    metrics: MeaMetrics | None = None,
    exec_fn: Any = None,
    cancel_event: Any = None,
) -> tuple[str, TaskState, MeaMetrics]:
    """Drive the MEA loop until all requirements are verified or the round budget ends.

    Returns ``(status, final_task_state, metrics)`` where ``status`` is a valid
    workflow-status name (``exited_converged`` / ``exited_budget``).

    R0–R10 wiring (P3):
      - R0  Manager decomposes the objective on a fresh run (one strong-model call).
      - R1–R5 / R8–R10 Executor steps use their configured *optimal* (cheap) agent,
        resolved per ``subtask_type`` via ``executor_overrides``.
      - R6 Auditor is resolved independently of the executor (anti-self-confirmation).
      - R7 meta-decider is invoked only every ``meta_every`` rounds (sparse strong-model
        spend) and its routing action is recorded in ``metrics``.

    ``metrics`` quantifies the selective-routing savings vs the naive baseline.
    """

    metrics = metrics or MeaMetrics()

    state = TaskState.load(run_id, data_dir)
    if state is None:
        if plan is not None:
            state = TaskState.from_objective(run_id, objective, plan=plan)
        else:
            manager = registry.resolve("manager")
            decomposed = manager.decompose(objective, state=None, max_subtasks=max_subtasks)
            metrics.strong_model_calls += 1  # R0 Manager decompose — one reserved strong-model call
            state = TaskState.from_plan(run_id, objective, decomposed)

    for round_i in range(max_rounds):
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled", state, metrics
        metrics.rounds += 1
        contract = state.next_subtask_contract()
        if contract is None:
            metrics.converged = state.all_requirements_met()
            _finalize_metrics(metrics, meta_every)
            return (
                "exited_converged" if state.all_requirements_met() else "exited_budget",
                state,
                metrics,
            )

        contract.params.setdefault("run_id", run_id)

        # Executor (R1–R5 / R8–R10): fresh context, bounded budget, per-subtask optimal agent.
        exec_agent = registry.resolve("executor", contract.subtask_type)
        backend = getattr(exec_agent, "backend", "unknown")
        metrics.executor_calls_by_backend[backend] = (
            metrics.executor_calls_by_backend.get(backend, 0) + 1
        )
        before = env_snapshot_fn() if env_snapshot_fn else {}

        if exec_fn is not None and backend != "deterministic":
            # P4 closed loop: a REAL agent backend (claude_code / codex / workbuddy /
            # llm_api) fulfills the bounded subtask INSIDE a real ``StageRun`` via
            # ``run_capability(agent=...)`` — so every executor step gets the full
            # control-plane audit trail (StageRun + status transition + events). The
            # agent may call back ``run_capability`` for sub-capabilities through its
            # tool_handler; it is never re-entered for the outer step.
            _stage, exec_result = exec_fn(
                run_id, contract.capability_id, contract.params, agent=exec_agent
            )
            out = _exec_result_to_output(exec_result)
        else:
            # Deterministic placeholder (or no exec_fn wired): the adapter executes via
            # its bound capability runner (``run_capability`` default executor) — already
            # a closed loop, counted once. Kept on this branch to avoid a nested StageRun.
            out = exec_agent.run_contract(
                contract, registry.budget_for("executor", contract.subtask_type)
            )
        after = env_snapshot_fn() if env_snapshot_fn else {}

        # Auditor (R6): read-only, independent of the executor.
        aud_agent = registry.resolve("auditor")
        verdict = aud_agent.audit(contract, out, {"before": before, "after": after})

        # Manager: update trusted state using ONLY the verified verdict.
        state.apply_verdict(verdict)
        state.save(data_dir)

        if state.all_requirements_met():
            metrics.converged = True
            _finalize_metrics(metrics, meta_every)
            return "exited_converged", state, metrics

        # R7 meta-decider: sparse strong-model spend (only every meta_every rounds).
        if meta_every and (round_i + 1) % meta_every == 0:
            meta = registry.resolve("meta_decider")
            metrics.strong_model_calls += 1  # R7 — one reserved strong-model call
            decision = meta.decide(objective, state, verdict) or {}
            metrics.last_meta_decision = decision.get("action", "continue")

    _finalize_metrics(metrics, meta_every)
    return "exited_budget", state, metrics


def _exec_result_to_output(result: Any) -> "ExecOutput":
    """Adapt an orchestrator ``ExecResult`` (what ``run_capability(agent=)`` returns)
    into the ``ExecOutput`` shape the Auditor expects (status / summary / output_refs /
    metrics)."""

    from .agents.adapter import ExecOutput

    final = getattr(result, "final_status", None)
    status_val = getattr(final, "value", str(final)) if final is not None else ""
    status = "succeeded" if status_val in ("succeeded", "SUCCEEDED") else "failed"
    return ExecOutput(
        status=status,
        summary=getattr(result, "detail", "") or "",
        output_refs=list(getattr(result, "output_refs", []) or []),
        metrics={},
    )
