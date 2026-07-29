"""Tests for the agent-driven refactor: AgentHarness seam + router guardrail.

Minimal-pivot design (architecture discussion 2026-07-20):
  * The agent (AgentHarness) is the authoritative decider in agent mode.
  * IterationRouter.propose_route is only a *suggestion*; validate_route is the
    *guardrail* that rejects illegal decisions before they are committed.
  * The deterministic executors stay as the LocalAgentHarness fallback / autopilot.
"""

from __future__ import annotations

import unittest
from typing import Any

from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store import Repository
from safety_auto_research.execution_plane import ClosedLoopOrchestrator
from safety_auto_research.execution_plane.agent.harness import LocalAgentHarness
from safety_auto_research.execution_plane.agent.harness import RemoteAgentHarness
from safety_auto_research.execution_plane.base import StageTaskSpec
from safety_auto_research.execution_plane.decision.router import IterationRouter
from safety_auto_research.execution_plane.decision.router import RouteDecision
from safety_auto_research.platform_contracts.enums import DecisionType
from safety_auto_research.platform_contracts.enums import GateResult
from safety_auto_research.platform_contracts.enums import RunType
from safety_auto_research.platform_contracts.enums import StageStatus
from safety_auto_research.platform_contracts.events import EvalCompletedEvent
from safety_auto_research.platform_contracts.objects import StageRun


def _make_run(svc: ControlPlaneService, run_type: RunType = RunType.ADVERSARIAL_HARDENING) -> str:
    req = type(
        "Req",
        (),
        {
            "program_id": "prog-1",
            "run_type": run_type,
            "entry_stage": "10_adversarial_data_generation",
            "target_id": "model:target-1",
            "objective_snapshot": {"target_metric": "accuracy", "target_threshold": 0.8},
            "requested_outcomes": ["harden"],
        },
    )()
    run = svc.create_workflow_run(req)
    svc.start_workflow_run(run.run_id)
    return run.run_id


class LocalAgentHarnessTest(unittest.TestCase):
    """LocalAgentHarness is the default backend; it must keep the loop working."""

    def setUp(self) -> None:
        self.svc = ControlPlaneService(Repository())

    def test_agent_mode_local_completes_closed_loop(self) -> None:
        run_id = _make_run(self.svc)
        orch = ClosedLoopOrchestrator(self.svc, mode="agent", harness=LocalAgentHarness())
        summary = orch.run_closed_loop(run_id, max_rounds=3)
        # Terminal status must be a legal WorkflowStatus name (closed_loop_complete
        # was replaced by exited_converged when set_run_status started enum-validating).
        self.assertEqual(summary["status"], "exited_converged")
        self.assertIn("eval_completed", summary["events"])
        self.assertIn("attack_completed", summary["events"])
        self.assertIn("lesson_promoted", summary["events"])
        self.assertTrue(summary["lessons"])

    def test_local_harness_run_stage_matches_deterministic_executor(self) -> None:
        run_id = _make_run(self.svc)
        orch = ClosedLoopOrchestrator(self.svc, mode="agent", harness=LocalAgentHarness())
        stage, result = orch.dispatch_stage(run_id, "03_eval", params={"measured_metrics": {"accuracy": 0.95}})
        self.assertIsInstance(result.event, EvalCompletedEvent)
        self.assertTrue(result.event.gate_passed)


class RemoteAgentHarnessAuthoritativeTest(unittest.TestCase):
    """The agent must be able to override the router's suggestion."""

    def setUp(self) -> None:
        self.svc = ControlPlaneService(Repository())

    @staticmethod
    def _fake_transport(message: dict[str, Any], tool_handler: Any = None) -> dict[str, Any]:
        if message["msg_type"] == "task.decide":
            # Agent overrides the reference policy: stop the run immediately.
            return {
                "msg_type": "agent.decision",
                "decision_type": "exit_success",
                "target_stage": None,
                "reason_codes": ["agent_override"],
                "evidence_refs": [],
                "rationale": "agent chose to stop after eval",
            }
        if message["msg_type"] == "task.run_stage":
            spec = message["spec"]
            rc = spec["run_context"]
            return {
                "msg_type": "agent.stage_result",
                "final_status": "succeeded",
                "gate_result": "passed",
                "event": {
                    "event_type": "eval_completed",
                    "run_id": rc["run_id"],
                    "eval_suite_id": "suite-acc",
                    "stage_run_id": spec["stage_run_id"],
                    "passed": True,
                    "metrics": {"accuracy": 0.9},
                    "gate_passed": True,
                    "report_ref": "eval-report://x",
                },
                "output_refs": [],
                "detail": "agent ran eval",
            }
        return {}

    def test_agent_overrides_router_and_loop_stops(self) -> None:
        run_id = _make_run(self.svc)
        orch = ClosedLoopOrchestrator(
            self.svc,
            mode="agent",
            harness=RemoteAgentHarness(transport=self._fake_transport),
        )
        summary = orch.run_closed_loop(run_id, max_rounds=3)
        # The agent overrode the R7/R10 suggestion with exit_success -> no attack/lesson.
        self.assertEqual(summary["status"], "stopped_after_eval")
        self.assertEqual(summary["decisions"], ["exit_success"])
        self.assertIn("eval_completed", summary["events"])
        self.assertNotIn("attack_completed", summary["events"])

    def test_unconfigured_remote_harness_raises_explicitly(self) -> None:
        harness = RemoteAgentHarness()
        ev = EvalCompletedEvent(run_id="r", eval_suite_id="s", passed=True, gate_passed=True, report_ref="x")
        run_id = _make_run(self.svc)
        run = self.svc.get_workflow_run(run_id)  # real WorkflowRun (has model_dump)
        with self.assertRaises(NotImplementedError):
            harness.decide(run, ev, RouteDecision(DecisionType.CONTINUE, None, [], [], "X", ""))
        stage = StageRun(
            stage_run_id="s1",
            run_id="r",
            stage_code="03_eval",
            executor_family="default",
            gate_result=GateResult.PENDING,
            status=StageStatus.QUEUED,
        )
        spec = StageTaskSpec(stage_run=stage, params={}, available_tools=[], run_context={})
        with self.assertRaises(NotImplementedError):
            harness.run_stage(spec, self.svc)


class RouterGuardrailTest(unittest.TestCase):
    """validate_route is the hard guardrail, independent of who decided."""

    def setUp(self) -> None:
        self.router = IterationRouter()
        self.run = type("Run", (), {"run_id": "r", "run_type": RunType.ADVERSARIAL_HARDENING})()

    def test_rejects_continue_with_target_stage(self) -> None:
        bad = RouteDecision(DecisionType.CONTINUE, "03_eval", [], [], "X", "")
        with self.assertRaises(ValueError):
            self.router.validate_route(bad, self.run)

    def test_accepts_valid_revisit(self) -> None:
        ok = RouteDecision(DecisionType.REVISIT, "03_eval", [], [], "R7", "")
        self.assertTrue(self.router.validate_route(ok, self.run))

    def test_accepts_exit_without_target(self) -> None:
        ok = RouteDecision(DecisionType.EXIT_SUCCESS, None, [], [], "EXIT", "")
        self.assertTrue(self.router.validate_route(ok, self.run))

    def test_propose_route_alias_still_works(self) -> None:
        # Backwards-compatible: the old `decide` name must still return the reference policy.
        ev = EvalCompletedEvent(run_id="r", eval_suite_id="s", passed=True, gate_passed=True, report_ref="x")
        route = self.router.decide(ev, self.run)
        self.assertEqual(route.decision_type, DecisionType.REVISIT)
        self.assertEqual(route.target_stage, "10_adversarial_data_generation")


if __name__ == "__main__":
    unittest.main()
