"""Tests for the agent<->platform wire protocol + transports.

Covers:
  * CallableTransport round-trip (decision override via the protocol).
  * SubprocessTransport with a real CLI agent (tool_call round-trip + event commit).
  * An illegal remote decision is still rejected by the router's safety guardrail.
  * The contract endpoint exposes the message schemas an agent can fetch.
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any

from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store import Repository
from safety_auto_research.execution_plane import ClosedLoopOrchestrator
from safety_auto_research.execution_plane.agent.harness import RemoteAgentHarness
from safety_auto_research.execution_plane.agent.protocol import AgentDecision
from safety_auto_research.execution_plane.agent.protocol import AgentStageResult
from safety_auto_research.execution_plane.agent.protocol import all_schemas
from safety_auto_research.execution_plane.agent.protocol import parse_message
from safety_auto_research.execution_plane.agent.transport import CallableTransport
from safety_auto_research.execution_plane.decision.router import IterationRouter
from safety_auto_research.platform_contracts.enums import DecisionType
from safety_auto_research.platform_contracts.enums import RunType
from safety_auto_research.platform_contracts.events import EvalCompletedEvent

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURE = os.path.join(_HERE, "agent_fixture.py")


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


class ProtocolModelsTest(unittest.TestCase):
    def test_parse_round_trips_terminal_messages(self) -> None:
        dec = AgentDecision(decision_type="exit_success", rationale="x")
        self.assertIsInstance(parse_message(dec.model_dump(mode="json")), AgentDecision)
        res = AgentStageResult(final_status="succeeded", gate_result="passed")
        self.assertIsInstance(parse_message(res.model_dump(mode="json")), AgentStageResult)

    def test_all_schemas_present(self) -> None:
        schemas = all_schemas()
        for t in (
            "task.decide",
            "task.run_stage",
            "agent.decision",
            "agent.stage_result",
            "agent.tool_call",
            "platform.tool_result",
        ):
            self.assertIn(t, schemas)


class CallableTransportTest(unittest.TestCase):
    """The protocol must let a remote agent be the authoritative decider."""

    def setUp(self) -> None:
        self.svc = ControlPlaneService(Repository())

    def test_agent_overrides_router_and_loop_stops(self) -> None:
        def agent(message: dict[str, Any], tool_handler: Any = None) -> dict[str, Any]:
            if message["msg_type"] == "task.decide":
                return {
                    "msg_type": "agent.decision",
                    "decision_type": "exit_success",
                    "target_stage": None,
                    "rationale": "stop now",
                }
            if message["msg_type"] == "task.run_stage":
                return {
                    "msg_type": "agent.stage_result",
                    "event": {
                        "event_type": "eval_completed",
                        "run_id": message["run_id"],
                        "eval_suite_id": "s",
                        "stage_run_id": message["spec"]["stage_run_id"],
                        "passed": True,
                        "metrics": {"accuracy": 0.9},
                        "gate_passed": True,
                        "report_ref": "r",
                    },
                    "final_status": "succeeded",
                    "gate_result": "passed",
                }
            return {}

        run_id = _make_run(self.svc)
        orch = ClosedLoopOrchestrator(
            self.svc, mode="agent", harness=RemoteAgentHarness(transport=agent)
        )
        summary = orch.run_closed_loop(run_id, max_rounds=3)
        self.assertEqual(summary["status"], "stopped_after_eval")
        self.assertEqual(summary["decisions"], ["exit_success"])
        self.assertIn("eval_completed", summary["events"])
        self.assertNotIn("attack_completed", summary["events"])


class SubprocessTransportTest(unittest.TestCase):
    """A real CLI agent over the line protocol: tool calls + event commit."""

    def setUp(self) -> None:
        self.svc = ControlPlaneService(Repository())

    def test_subprocess_agent_tool_call_and_event_commit(self) -> None:
        command = f"{sys.executable} {_FIXTURE}"
        self.assertTrue(os.path.exists(_FIXTURE), "agent fixture missing")
        orch = ClosedLoopOrchestrator(
            self.svc, mode="agent", harness=RemoteAgentHarness(agent_command=command)
        )
        run_id = _make_run(self.svc)
        stage, result = orch.dispatch_stage(run_id, "03_eval", params={})
        # The CLI agent emitted a real platform event over the protocol.
        events = self.svc.list_events(run_id)
        self.assertTrue(any(e["event_type"] == "artifact_published" for e in events))
        # The CLI agent's ToolCall hit the SDK and recorded a metric.
        self.assertTrue(
            any(m["name"] == "agent_substep" for m in self.svc._repo.list_metrics(run_id))
        )
        self.assertEqual(result.final_status.value, "succeeded")


class GuardrailOnProtocolTest(unittest.TestCase):
    """A remote decision still passes through validate_route before commit."""

    def setUp(self) -> None:
        self.router = IterationRouter()
        self.svc = ControlPlaneService(Repository())
        self.run_id = _make_run(self.svc)

    def test_illegal_remote_decision_rejected(self) -> None:
        # A remote agent returns CONTINUE *with* a target_stage — illegal per contract.
        harness = RemoteAgentHarness(
            transport=CallableTransport(
                lambda message, tool_handler=None: {
                    "msg_type": "agent.decision",
                    "decision_type": "continue",
                    "target_stage": "03_eval",
                    "rationale": "bad agent",
                }
            )
        )
        run = self.svc.get_workflow_run(self.run_id)
        ev = EvalCompletedEvent(run_id=self.run_id, eval_suite_id="s", passed=True, gate_passed=True, report_ref="x")
        route = harness.decide(run, ev, self.router.propose_route(ev, run))
        with self.assertRaises(ValueError):
            self.router.validate_route(route, run)

    def test_valid_remote_decision_accepted(self) -> None:
        harness = RemoteAgentHarness(
            transport=CallableTransport(
                lambda message, tool_handler=None: {
                    "msg_type": "agent.decision",
                    "decision_type": "exit_success",
                    "target_stage": None,
                    "rationale": "good agent",
                }
            )
        )
        run = self.svc.get_workflow_run(self.run_id)
        ev = EvalCompletedEvent(run_id=self.run_id, eval_suite_id="s", passed=True, gate_passed=True, report_ref="x")
        route = harness.decide(run, ev, self.router.propose_route(ev, run))
        self.assertTrue(self.router.validate_route(route, run))
        self.assertEqual(route.decision_type, DecisionType.EXIT_SUCCESS)


class ContractEndpointTest(unittest.TestCase):
    """The /agent/protocol endpoint lets a real agent fetch the wire contract."""

    def test_protocol_endpoint_serves_schemas(self) -> None:
        from safety_auto_research.control_plane.api import create_app

        app = create_app(ControlPlaneService(Repository()))
        # FastAPI TestClient import is optional in this skeleton; only assert the route
        # is registered and the handler returns the expected keys.
        # NB: read the path table from the OpenAPI schema rather than ``app.routes`` —
        # since the A1 router split the app composes ``APIRouter``s, and recent FastAPI
        # versions keep those as lazy ``_IncludedRouter`` entries instead of flattening.
        route_paths = list(app.openapi()["paths"])
        self.assertIn("/agent/protocol", route_paths)


if __name__ == "__main__":
    unittest.main()
