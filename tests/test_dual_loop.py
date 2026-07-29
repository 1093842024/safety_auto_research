"""Tests for the dual-loop architecture: external audit + recursive improvement + state."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store_tree import ResearchStateStore
from safety_auto_research.execution_plane.agent.harness import RemoteAgentHarness
from safety_auto_research.execution_plane.agent.harness import assert_inner_capability_allowed
from safety_auto_research.execution_plane.capabilities.audit_executor import AuditExecutor
from safety_auto_research.execution_plane.decision.router import IterationRouter
from safety_auto_research.execution_plane.orchestrator import ClosedLoopOrchestrator
from safety_auto_research.platform_contracts.enums import DecisionType
from safety_auto_research.platform_contracts.enums import EventType
from safety_auto_research.platform_contracts.enums import GateResult
from safety_auto_research.platform_contracts.enums import StageStatus
from safety_auto_research.platform_contracts.events import AuditCompletedEvent
from safety_auto_research.platform_contracts.objects import StageRun

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "kaggle", "titanic")


def _make_run(svc: ControlPlaneService, target: str = "titanic", threshold: float = 0.82):
    run = svc.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="dl-test",
            run_type="standard_research",
            entry_stage="00_agent_orchestration",
            target_id=target,
            objective_snapshot={"goal": f"classify {target}", "target_threshold": threshold},
        )
    )
    svc.start_workflow_run(run.run_id)
    return run


class DualLoopAuditTest(unittest.TestCase):
    def test_audit_accept_on_strong_inner(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        summary = orch.run_dual_loop(
            run.run_id,
            inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "model": "gbm", "data_dir": _DATA, "threshold": 0.82},
            audit_params={"threshold": 0.8},
            max_outer_iters=2,
        )
        self.assertEqual(summary["status"], "exited_converged")
        self.assertIn("audit_completed", [e["event_type"] for e in svc.list_events(run.run_id)])
        self.assertIn("exit_success", summary["decisions"])

    def test_audit_refine_on_weak_inner(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        # RF on Titanic is below 0.82 -> audit should REFINE and trigger self-evolution.
        summary = orch.run_dual_loop(
            run.run_id,
            inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "model": "rf", "data_dir": _DATA, "threshold": 0.82},
            audit_params={"threshold": 0.8},
            max_outer_iters=2,
        )
        self.assertIn("revisit", summary["decisions"])
        self.assertIn(
            "improvement_applied", [e["event_type"] for e in svc.list_events(run.run_id)]
        )

    def test_router_maps_audit_to_accept_refine(self) -> None:
        router = IterationRouter()
        accept = AuditCompletedEvent(
            run_id="r", audit_id="a", audited_target="t", gate_passed=True,
            confidence=0.9, recoverable=True, audit_confidence=0.9, report_ref="x",
        )
        dec = router.propose_route(accept, _make_run(ControlPlaneService()))
        self.assertEqual(dec.decision_type, DecisionType.EXIT_SUCCESS)

        refine = AuditCompletedEvent(
            run_id="r", audit_id="a", audited_target="t", gate_passed=False,
            confidence=0.6, recoverable=True, audit_confidence=0.9, report_ref="x",
            unresolved_claims=["claim X unsupported"],
        )
        dec2 = router.propose_route(refine, _make_run(ControlPlaneService()))
        self.assertEqual(dec2.decision_type, DecisionType.REVISIT)
        self.assertIn("audit_refine", dec2.reason_codes)


class SelfEvolutionTest(unittest.TestCase):
    def test_frozen_verifier_revert(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        orch.run_capability(
            run.run_id, "kaggle_eval",
            {"preset": "titanic", "model": "gbm", "data_dir": _DATA, "threshold": 0.82},
        )
        _stage, result = orch.run_self_evolution(run.run_id, params={"inject_frozen_violation": True})
        self.assertIsNotNone(result.event)
        self.assertTrue(result.event.reverted)
        self.assertFalse(result.event.validated_heldout)

    def test_improvement_applied_on_real_eval(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        orch.run_capability(
            run.run_id, "kaggle_eval",
            {"preset": "titanic", "model": "gbm", "data_dir": _DATA, "threshold": 0.82},
        )
        _stage, result = orch.run_self_evolution(run.run_id, params={})
        self.assertFalse(result.event.reverted)
        self.assertTrue(result.event.validated_heldout)


class CumulativeStateTest(unittest.TestCase):
    def test_hypothesis_tree_accumulates_and_compacts(self) -> None:
        svc = ControlPlaneService()
        store = ResearchStateStore()
        orch = ClosedLoopOrchestrator(svc, mode="scripted", state_store=store)
        run = _make_run(svc)
        orch.run_dual_loop(
            run.run_id,
            inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "model": "gbm", "data_dir": _DATA, "threshold": 0.82},
            audit_params={"threshold": 0.8},
            max_outer_iters=2,
        )
        nodes = store.hypo_tree.list_nodes()
        self.assertTrue(len(nodes) >= 1)
        self.assertTrue(any(n.status == "merged" for n in nodes))
        # compact preserves stepping stones (no crash, returns a dict)
        self.assertIsInstance(store.hypo_tree.compact(), dict)


class ContextSeparationTest(unittest.TestCase):
    """The two context-separation invariants from the dual-loop design.

    1. The OUTER auditor sees only a control-plane-curated result (metrics/verdict) +
       objective + prior outer-loop verdicts. It must never read the inner loop's
       experimental narrative (hypotheses explored / experiences / lessons), otherwise
       the audit could be biased by *how* the answer was produced.
    2. The INNER-loop agent may only drive research/optimization capabilities via
       ``run_capability``; the outer-loop audit (layer_11) and recursive improvement
       (layer_09) are reserved for the control plane, so the inner loop cannot evaluate
       or mutate its own result.
    """

    def test_outer_audit_ignores_inner_narrative(self) -> None:
        seen: list[tuple[str, str, str]] = []

        def spy_judge(answer: str, claim: str, evidence: str) -> float:
            seen.append((answer, claim, evidence))
            return 0.9  # strong claim support

        # A run whose event log is polluted with inner-loop *narrative* the auditor
        # must not see. The real eval result is delivered via `audit_input` instead.
        class _FakeSDK:
            def __init__(self) -> None:
                self.loaded: list[str] = []

            def load_object(self, ref: str):
                self.loaded.append(ref)
                if ref.startswith("run:"):
                    return SimpleNamespace(objective_snapshot={}, target_id="t")
                if ref.startswith("events:"):
                    # LEGITIMATE inner result lives alongside NARRATIVE noise; the
                    # curator (orchestrator) strips the noise before the auditor runs.
                    return [
                        {"event_type": "hypothesis_observed", "hypothesis": "NOISE_INNER_SECRET"},
                        {"event_type": "experience_registered", "payload": "NOISE_EXP_SECRET"},
                        {
                            "event_type": "eval_completed",
                            "metrics": {"accuracy": 0.85},
                            "gate_passed": True,
                            "report_ref": "kaggle://real",
                        },
                    ]
                return None

            def emit_event(self, e):
                return e

            def publish_artifact(self, **_kw):
                return SimpleNamespace(artifact_id="art")

            def record_metric(self, *_a, **_k):
                return None

        sdk = _FakeSDK()
        curated = {
            "objective": "classify titanic",
            "result_metrics": {"accuracy": 0.85},
            "result_report_ref": "kaggle://real",
            "result_gate_passed": True,
            "result_real_eval": True,
            "prior_audits": [],
            "constraints": [],
        }
        stage_run = StageRun(
            stage_run_id="sr1", run_id="r1", stage_code="layer_11_external_audit",
            executor_family="capability", gate_result=GateResult.FAILED,
            status=StageStatus.QUEUED,
        )
        result = AuditExecutor().execute(
            stage_run, sdk,
            {"judge": spy_judge, "objective": "classify titanic", "audit_input": curated,
             "threshold": 0.8},
        )
        # (a) the auditor did NOT open the full event log in curated mode.
        self.assertNotIn("events:r1", sdk.loaded)
        # (b) none of the judge's inputs ever contained the inner-loop noise.
        blob = " ".join(" ".join(p) for p in seen)
        self.assertNotIn("NOISE_INNER_SECRET", blob)
        self.assertNotIn("NOISE_EXP_SECRET", blob)
        # (c) the verdict follows the curated result (0.85 >= 0.8 -> accept).
        self.assertTrue(result.gate_result.value == "passed")

    def test_inner_agent_cannot_invoke_outer_capabilities(self) -> None:
        # allowlist helper rejects outer-loop capabilities
        for cap in ("layer_11_external_audit", "layer_09_self_iterative_evolution"):
            with self.assertRaises(ValueError):
                assert_inner_capability_allowed(cap)
        self.assertIsNone(assert_inner_capability_allowed("kaggle_eval"))

        # the agent tool surface enforces the same: a remote agent calling
        # run_capability with a reserved capability is refused.
        h = RemoteAgentHarness()
        h.sdk = object()
        # dummy runner shaped like the real one: returns (stage, ExecResult-like)
        _stage = SimpleNamespace(stage_run_id="s", stage_code="kaggle_eval")
        _res = SimpleNamespace(
            event=None, gate_result=SimpleNamespace(value="passed"),
            output_refs=[], detail="ok",
        )
        h._capability_runner = lambda **_kw: (_stage, _res)

        with self.assertRaises(ValueError):
            h._tool_handler(
                "run_capability",
                {"run_id": "r", "capability_id": "layer_11_external_audit", "params": {}},
            )
        with self.assertRaises(ValueError):
            h._tool_handler(
                "run_capability",
                {"run_id": "r", "capability_id": "layer_09_self_iterative_evolution", "params": {}},
            )
        # inner-loop research capability is permitted
        out = h._tool_handler(
            "run_capability",
            {"run_id": "r", "capability_id": "kaggle_eval", "params": {}},
        )
        self.assertEqual(out["layer_code"], "kaggle_eval")


if __name__ == "__main__":
    unittest.main()
