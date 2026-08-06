"""Tests for the infrastructure-layer capability registry + agent ``run_capability`` tool."""

import unittest
from typing import Any

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.execution_plane import ClosedLoopOrchestrator
from safety_auto_research.execution_plane.agent.harness import RemoteAgentHarness
from safety_auto_research.execution_plane.agent.transport import CallableTransport
from safety_auto_research.execution_plane.capabilities.registry import default_capability_registry
from safety_auto_research.platform_contracts.enums import RunType


def _make_run(svc: ControlPlaneService) -> str:
    run = svc.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="p1",
            run_type=RunType.STANDARD_RESEARCH,
            entry_stage="03_eval",
            target_id="m1",
            objective_snapshot={"target_metric": "accuracy", "target_threshold": 0.8},
        )
    )
    svc.start_workflow_run(run.run_id)  # stages require a running workflow
    return run.run_id


class CapabilityRegistryTest(unittest.TestCase):
    def test_registry_lists_all_ten_layers(self) -> None:
        reg = default_capability_registry()
        self.assertEqual(len(reg.list_capabilities()), 10)
        ids = {c["capability_id"] for c in reg.list_capabilities()}
        self.assertIn("layer_03_eval", ids)
        self.assertIn("layer_10_adversarial_data_generation", ids)
        # Real adapters are bound for ③ / ⑧ / ⑩; others use stubs (still "bound").
        for cap in reg.list_capabilities():
            self.assertTrue(cap["bound"])


class RunCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = ClosedLoopOrchestrator(ControlPlaneService())
        self.run_id = _make_run(self.orch.svc)

    def test_stub_capability_creates_artifact(self) -> None:
        stage, result = self.orch.run_capability(
            self.run_id, "layer_01_literature_research", {"label": "seed_papers"}
        )
        self.assertEqual(stage.stage_code, "01_literature_research")
        self.assertTrue(result.output_refs)  # a real artifact was published
        self.assertIsNone(result.event)  # stub emits no decision-triggering event
        events = self.orch.svc.list_events(self.run_id)
        self.assertTrue(any(e["event_type"] == "artifact_published" for e in events))

    def test_real_capability_emits_event(self) -> None:
        stage, result = self.orch.run_capability(
            self.run_id, "layer_03_eval", {"target_metric": "accuracy", "threshold": 0.8}
        )
        self.assertEqual(stage.stage_code, "03_eval")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.event_type.value, "eval_completed")

    def test_unknown_capability_raises(self) -> None:
        with self.assertRaises(Exception):
            self.orch.run_capability(self.run_id, "layer_does_not_exist", {})


class AgentCapabilityToolTest(unittest.TestCase):
    """A remote agent invokes ``run_capability`` as a tool; the platform records it."""

    def test_agent_invokes_capability_tool(self) -> None:
        def fake_agent(message: dict[str, Any], tool_handler: Any) -> dict[str, Any]:
            if message.get("msg_type") == "task.run_stage":
                tool_handler(
                    "run_capability",
                    {
                        "run_id": message["run_id"],
                        "capability_id": "layer_01_literature_research",
                        "params": {"label": "seed_papers"},
                    },
                )
                return {
                    "msg_type": "agent.stage_result",
                    "event": None,
                    "final_status": "succeeded",
                    "gate_result": "passed",
                    "output_refs": [],
                    "detail": "agent orchestrated a literature search via capability",
                }
            return {"msg_type": "agent.stage_result", "detail": "noop"}

        orch = ClosedLoopOrchestrator(
            ControlPlaneService(),
            mode="agent",
            harness=RemoteAgentHarness(transport=CallableTransport(fake_agent)),
        )
        run_id = _make_run(orch.svc)
        orch.dispatch_stage(run_id, "03_eval")
        codes = [s.stage_code for s in orch.svc.list_stage_runs(run_id)]
        # Assigned stage + the capability the agent chose to invoke.
        self.assertIn("03_eval", codes)
        self.assertIn("01_literature_research", codes)

    def test_open_goal_agent_orchestrates_multiple_layers(self) -> None:
        def fake_open_goal(message: dict[str, Any], tool_handler: Any) -> dict[str, Any]:
            if message.get("msg_type") == "task.run_stage":
                tool_handler(
                    "run_capability",
                    {"run_id": message["run_id"], "capability_id": "layer_01_literature_research", "params": {}},
                )
                tool_handler(
                    "run_capability",
                    {
                        "run_id": message["run_id"],
                        "capability_id": "layer_03_eval",
                        "params": {"target_metric": "accuracy", "threshold": 0.8},
                    },
                )
                return {
                    "msg_type": "agent.stage_result",
                    "event": None,
                    "final_status": "succeeded",
                    "gate_result": "passed",
                    "output_refs": [],
                    "detail": "open goal satisfied",
                }
            return {"msg_type": "agent.stage_result", "detail": "noop"}

        orch = ClosedLoopOrchestrator(
            ControlPlaneService(),
            mode="agent",
            harness=RemoteAgentHarness(transport=CallableTransport(fake_open_goal)),
        )
        run_id = _make_run(orch.svc)
        stage, _ = orch.dispatch_open_goal(run_id, "improve model robustness")
        codes = [s.stage_code for s in orch.svc.list_stage_runs(run_id)]
        self.assertEqual(stage.stage_code, "00_agent_orchestration")
        self.assertIn("01_literature_research", codes)
        self.assertIn("03_eval", codes)


class ProtocolEndpointTest(unittest.TestCase):
    def test_protocol_exposes_capabilities(self) -> None:
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        app = create_app(ControlPlaneService())
        client = TestClient(app)
        resp = client.get("/agent/protocol")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("capabilities", data)
        # 10 infrastructure layers + the extra real kaggle_eval + layer_11_external_audit
        self.assertEqual(len(data["capabilities"]), 12)
        cap_ids = [c["capability_id"] for c in data["capabilities"]]
        self.assertIn("kaggle_eval", cap_ids)
        self.assertIn("layer_11_external_audit", cap_ids)
        self.assertIn("run_capability", data["agent_tools"])

    def test_capability_run_endpoint(self) -> None:
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        app = create_app(ControlPlaneService())
        client = TestClient(app)
        run = client.post(
            "/workflow-runs",
            json={
                "program_id": "p1",
                "run_type": "standard_research",
                "entry_stage": "03_eval",
                "target_id": "m1",
                "objective_snapshot": {"target_metric": "accuracy", "target_threshold": 0.8},
            },
        ).json()
        client.post(f"/workflow-runs/{run['run_id']}/start")
        resp = client.post(
            f"/workflow-runs/{run['run_id']}/capabilities/layer_01_literature_research/run",
            json={"params": {"label": "seed"}},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.json()["stage_run_id"])

    def test_capability_run_rejects_reserved_outer_caps(self) -> None:
        """缺陷1 regression: the HTTP tool endpoint must NOT let an external caller
        run the reserved OUTER-loop capabilities (audit / self-evolution) on its own
        result — that would destroy the evaluation-bias / self-confirmation guarantee."""
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        app = create_app(ControlPlaneService())
        client = TestClient(app)
        run = client.post(
            "/workflow-runs",
            json={
                "program_id": "p1",
                "run_type": "standard_research",
                "entry_stage": "03_eval",
                "target_id": "m1",
                "objective_snapshot": {"target_metric": "accuracy", "target_threshold": 0.8},
            },
        ).json()
        client.post(f"/workflow-runs/{run['run_id']}/start")
        for reserved in ("layer_11_external_audit", "layer_09_self_iterative_evolution"):
            resp = client.post(
                f"/workflow-runs/{run['run_id']}/capabilities/{reserved}/run",
                json={"params": {}},
            )
            self.assertEqual(
                resp.status_code, 400, msg=f"reserved cap {reserved} should be rejected (400)"
            )
        # A legitimate inner capability must still be allowed through this endpoint.
        ok = client.post(
            f"/workflow-runs/{run['run_id']}/capabilities/layer_01_literature_research/run",
            json={"params": {"label": "seed"}},
        )
        self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()
