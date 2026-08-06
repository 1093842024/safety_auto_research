"""P4 end-to-end benefit measurement for the MEA (Manage-Execute-Audit) harness.

Runs the *real* closed loop on a concrete research objective:

    Manager (R0) decomposes -> Executor runs each subtask as a REAL capability
    inside a real StageRun (via run_capability) -> Auditor (R6, independent model)
    verifies -> Meta-decider (R7) routes only every N rounds.

The executor is the local, offline ``deterministic`` agent wired to the orchestrator's
real ``run_capability`` (no credentials / no network). This exercises the genuine
control-plane audit trail (StageRuns + artifacts + events) while quantifying how much
the selective-routing strategy (strong model ONLY on R0+R7; cheap agents on R1–R5 /
R8–R10) saves versus the naive "spend the strong model on every step + meta every round"
baseline.

Usage:
    python tools/measure_mea_e2e.py
"""

from __future__ import annotations

import os
import sys
import tempfile

# Make the safety_auto_research package importable when run as a standalone script.
# Repo root (the parent of safety_auto_research) must be on sys.path so that
# ``safety_auto_research.control_plane`` resolves — matching how the test suite imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.task_state import DEFAULT_PLAN
from safety_auto_research.control_plane.task_state import SUBTASK_TO_CAPABILITY
from safety_auto_research.execution_plane.agents import RoleAgentRegistry
from safety_auto_research.execution_plane.mea import run_mea_loop_core
from safety_auto_research.execution_plane.orchestrator import ClosedLoopOrchestrator

# Offline measurement config: deterministic executor (local capabilities), strong-model
# roles fail-soft to deterministic fallback (no LLM credentials needed for measurement).
SPEC = {
    "roles": {
        "manager": {"backend": "llm_api", "model": "opus"},
        "meta_decider": {"backend": "llm_api", "model": "opus"},
        "executor_default": {"backend": "deterministic", "model": "none", "budget_seconds": 1800},
        "auditor": {
            "backend": "llm_api", "model": "judge", "require_different_from": "executor"
        },
    }
}


def measure(objective: str, meta_every: int = 5) -> dict[str, object]:
    svc = ControlPlaneService()
    orch = ClosedLoopOrchestrator(svc, mode="scripted")
    run = svc.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="mea-e2e", run_type="standard_research",
            entry_stage="00_agent_orchestration", target_id="mea",
            objective_snapshot={"goal": objective},
        )
    )
    svc.start_workflow_run(run.run_id)

    registry = RoleAgentRegistry.load(SPEC)
    registry.bind(orch.run_capability)
    registry.enforce_separation()

    status, state, m = run_mea_loop_core(
        registry, run.run_id, objective, max_rounds=50,
        data_dir=tempfile.mkdtemp(), meta_every=meta_every,
        exec_fn=orch.run_capability,
    )

    executed = [
        (r.content.get("subtask_type"), SUBTASK_TO_CAPABILITY.get(r.content.get("subtask_type")))
        for r in state.records.values()
        if r.kind == "requirement"
    ]
    return {
        "status": status,
        "objective": objective,
        "rounds": m.rounds,
        "executor_calls_by_backend": dict(m.executor_calls_by_backend),
        "strong_model_calls": m.strong_model_calls,
        "stageruns_created": orch._cap_call_counts.get(run.run_id, 0),
        "cost_proxy": m.cost_proxy,
        "naive_cost_proxy": m.naive_cost_proxy,
        "savings_ratio": round(m.savings_ratio, 2),
        "converged": m.converged,
        "planned_subtasks": list(DEFAULT_PLAN),
        "executed_capabilities": executed,
    }


def _print_report(r: dict[str, object]) -> None:
    print("=" * 72)
    print("MEA closed-loop end-to-end measurement (P4)")
    print("=" * 72)
    print(f"Objective            : {r['objective']}")
    print(f"Status / converged   : {r['status']} / {r['converged']}")
    print(f"Rounds               : {r['rounds']}")
    print(f"Planned subtasks     : {', '.join(r['planned_subtasks'])}")  # type: ignore[arg-type]
    print(f"StageRuns created    : {r['stageruns_created']} (real audit trail)")
    print(f"Executor backend use : {r['executor_calls_by_backend']}")
    print(f"Strong-model calls   : {r['strong_model_calls']}  (R0 + R7 only)")
    print("-" * 72)
    print(f"Selective cost proxy : {r['cost_proxy']}")
    print(f"Naive cost proxy     : {r['naive_cost_proxy']}")
    print(f"Savings ratio        : {r['savings_ratio']}x  (naive / selective)")
    print("=" * 72)
    print("Executed capabilities (subtask_type -> capability_id):")
    for st, cid in r["executed_capabilities"]:  # type: ignore[union-attr]
        print(f"  - {st:<20} -> {cid}")
    print("=" * 72)


if __name__ == "__main__":
    result = measure("Improve adversarial robustness of a vision classifier")
    _print_report(result)
