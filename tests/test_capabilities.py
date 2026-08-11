"""Tests for the infrastructure-layer capability registry + agent ``run_capability`` tool."""

import os
import sys
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


class SandboxRunCapabilityTest(unittest.TestCase):
    """F3 regression: agent-mode capabilities run INSIDE the sandbox via run_capability.

    The suite runs with AGENT_SANDBOX_DISABLE=1 so the research command executes on
    the host (soft isolation) — this still exercises the full executor contract
    (command build -> subprocess -> result.json parse -> EvalCompletedEvent). The hard
    Docker-isolation proof (data_readonly + network_blocked) is verified separately by
    scripts/run_f3_sandbox_demo.py against a live daemon.
    """

    def setUp(self) -> None:
        self._prev_disable = os.environ.get("AGENT_SANDBOX_DISABLE")
        os.environ["AGENT_SANDBOX_DISABLE"] = "1"
        # Soft-fallback subprocess must use the same (sklearn-capable) interpreter
        # that runs this test, so the kaggle sandbox script can import sklearn.
        self._prev_path = os.environ.get("PATH")
        os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")
        self.orch = ClosedLoopOrchestrator(ControlPlaneService())
        self.run_id = _make_run(self.orch.svc)

    def tearDown(self) -> None:
        if self._prev_disable is None:
            os.environ.pop("AGENT_SANDBOX_DISABLE", None)
        else:
            os.environ["AGENT_SANDBOX_DISABLE"] = self._prev_disable
        if self._prev_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._prev_path

    def test_kaggle_eval_runs_in_sandbox_and_emits_event(self) -> None:
        stage, result = self.orch.run_capability(
            self.run_id, "kaggle_eval",
            {"sandbox": True, "preset": "titanic", "model": "gbm", "fe": "basic"},
        )
        self.assertEqual(stage.stage_code, "kaggle_eval")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.event_type.value, "eval_completed")
        # A genuine metric was produced (not a mock hash).
        self.assertIn("primary", result.event.metrics)
        self.assertGreater(result.event.metrics["primary"], 0.0)
        self.assertTrue(result.output_refs)

    def test_generic_research_cmd_runs_in_sandbox(self) -> None:
        # A research command that just writes a compliant result.json.
        code = (
            "import json, os\n"
            "p = os.path.join(os.environ['AGENT_SCRATCH_DIR'], 'result.json')\n"
            "json.dump({'passed': True, 'gate_passed': True, 'eval_metric': 'accuracy',\n"
            "           'preset': 'x', 'model': 'gbm', 'metrics': {'primary': 0.9},\n"
            "           'isolation': {'data_readonly': False, 'network_blocked': False}}, open(p, 'w'))\n"
        )
        stage, result = self.orch.run_capability(
            self.run_id, "run_research_sandbox",
            {"sandbox": True, "research_cmd": ["python", "-c", code], "result_name": "result.json"},
        )
        self.assertEqual(stage.stage_code, "run_research_sandbox")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.metrics["primary"], 0.9)

    def test_sandbox_routing_is_opt_in(self) -> None:
        # Without sandbox opt-in, kaggle_eval uses the host executor (no subprocess).
        stage, result = self.orch.run_capability(
            self.run_id, "kaggle_eval", {"preset": "titanic"}
        )
        self.assertIsNotNone(result.event)
        self.assertIn("primary", result.event.metrics)


class SandboxExecutorPlanTest(unittest.TestCase):
    """Pure unit tests for the command-planning contract (no subprocess / Docker)."""

    def _fake_stage(self, run_id: str = "r1") -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            run_id=run_id, stage_run_id=f"s_{run_id}", input_refs=[], _sdk=None
        )

    def test_kaggle_command_plan(self) -> None:
        from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
            SandboxResearchExecutor,
        )

        ex = SandboxResearchExecutor()
        cmd, data_dir, scratch, result_name, meta = ex._plan(
            self._fake_stage(), {"preset": "titanic", "model": "gbm"}
        )
        self.assertEqual(cmd[0], "python")
        self.assertIn("/repo/scripts/sandbox_examples/run_kaggle_eval_sandbox.py", cmd)
        self.assertIn("--preset", cmd)
        self.assertEqual(result_name, "result_titanic.json")
        self.assertTrue(data_dir.endswith("data/kaggle"))

    def test_custom_tabular_command_plan(self) -> None:
        from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
            SandboxResearchExecutor,
        )

        ex = SandboxResearchExecutor()
        cmd, _data_dir, _scratch, result_name, meta = ex._plan(
            self._fake_stage(),
            {
                "preset": "custom", "target": "label", "data_subdir": "my-data",
                "threshold": 0.75, "eval_metric": "f1_macro", "op": "ge",
            },
        )
        self.assertIn("--target", cmd)
        self.assertIn("label", cmd)
        self.assertIn("--data-dir", cmd)
        self.assertIn("/data/my-data", cmd)
        self.assertEqual(result_name, "result_custom.json")

    def test_generic_command_passthrough(self) -> None:
        from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
            SandboxResearchExecutor,
        )

        ex = SandboxResearchExecutor()
        cmd, _d, scratch, result_name, meta = ex._plan(
            self._fake_stage(),
            {"research_cmd": "python /repo/train.py --epochs 3", "result_name": "r.json"},
        )
        self.assertEqual(cmd, ["python", "/repo/train.py", "--epochs", "3"])
        self.assertEqual(result_name, "r.json")

    def test_text_cls_command_plan(self) -> None:
        from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
            SandboxResearchExecutor,
        )

        ex = SandboxResearchExecutor()
        cmd, data_dir, _scratch, result_name, meta = ex._plan(
            self._fake_stage(),
            {
                "task_type": "text_classification",
                "text_col": "text",
                "label_col": "label",
                "data_subdir": "text_cls_demo",
                "eval_metric": "f1_macro",
                "threshold": 0.5,
            },
        )
        self.assertIn("/repo/scripts/sandbox_examples/run_text_cls_sandbox.py", cmd)
        self.assertIn("--text-col", cmd)
        self.assertIn("text", cmd)
        self.assertIn("--data-dir", cmd)
        self.assertIn("/data/text_cls_demo", cmd)
        self.assertEqual(result_name, "result.json")
        self.assertTrue(data_dir.endswith("data/benchmark"))

    def test_text_cls_sandbox_routing_flag(self) -> None:
        # Regression: text_cls_sandbox must be sandbox-eligible so the agent-driven path
        # (AGENT_SANDBOX=1, no explicit sandbox:True) routes into the sandbox and _plan
        # receives _capability_id -> text_cls branch (not the kaggle branch).
        from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
            SandboxResearchExecutor,
            sandbox_requested,
        )

        prev = os.environ.get("AGENT_SANDBOX")
        os.environ["AGENT_SANDBOX"] = "1"
        try:
            self.assertTrue(sandbox_requested("text_cls_sandbox", {}))
            ex = SandboxResearchExecutor()
            cmd, _d, _s, _rn, _m = ex._plan(
                self._fake_stage(),
                {"_capability_id": "text_cls_sandbox", "data_subdir": "text_cls_demo"},
            )
            self.assertIn(
                "/repo/scripts/sandbox_examples/run_text_cls_sandbox.py", cmd
            )
        finally:
            if prev is None:
                os.environ.pop("AGENT_SANDBOX", None)
            else:
                os.environ["AGENT_SANDBOX"] = prev


class TextClsSandboxRunCapabilityTest(unittest.TestCase):
    """F2 regression: a text_classification task truly runs (real data + real script).

    Runs with AGENT_SANDBOX_DISABLE=1 (host soft-isolation) so it exercises the full
    executor contract without needing a live Docker daemon in CI. The hard-isolation
    proof is verified separately by scripts/run_f3_sandbox_demo.py against a live daemon
    and by the manual F2 verification (Stanford SST2 -> f1_macro=0.81 in-container).
    """

    _PREV_DISABLE = None
    _PREV_PATH = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._PREV_disable = os.environ.get("AGENT_SANDBOX_DISABLE")
        os.environ["AGENT_SANDBOX_DISABLE"] = "1"
        cls._prev_path = os.environ.get("PATH")
        os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._PREV_disable is None:
            os.environ.pop("AGENT_SANDBOX_DISABLE", None)
        else:
            os.environ["AGENT_SANDBOX_DISABLE"] = cls._PREV_disable
        if cls._PREV_PATH is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = cls._PREV_PATH

    def setUp(self) -> None:
        self.orch = ClosedLoopOrchestrator(ControlPlaneService())
        self.run_id = _make_run(self.orch.svc)

    def test_text_cls_runs_in_sandbox_and_emits_event(self) -> None:
        # Point the executor at the real demo dataset the F2 step materialized on the host.
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = os.path.join(pkg_root, "data", "benchmark")
        stage, result = self.orch.run_capability(
            self.run_id, "text_cls_sandbox",
            {
                "sandbox": True,
                "task_type": "text_classification",
                "text_col": "text",
                "label_col": "label",
                "data_subdir": "text_cls_demo",
                "eval_metric": "f1_macro",
                "threshold": 0.5,
                "data_dir": data_dir,
            },
        )
        self.assertEqual(stage.stage_code, "text_cls_sandbox")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.event_type.value, "eval_completed")
        self.assertIn("primary", result.event.metrics)
        # A genuine f1_macro was produced on real SST2 data (not a mock).
        self.assertGreater(result.event.metrics["primary"], 0.5)
        self.assertTrue(result.output_refs)

    def test_text_cls_routes_without_explicit_sandbox_flag(self) -> None:
        # Agent-driven path: the agent calls run_capability(text_cls_sandbox, params) with
        # AGENT_SANDBOX=1 but NO explicit sandbox:True. Routing must still enter the sandbox
        # (via SANDBOX_CAPABILITY_IDS) and produce a real eval event on real SST2 data.
        prev = os.environ.get("AGENT_SANDBOX")
        os.environ["AGENT_SANDBOX"] = "1"
        try:
            pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_dir = os.path.join(pkg_root, "data", "benchmark")
            stage, result = self.orch.run_capability(
                self.run_id, "text_cls_sandbox",
                {
                    "task_type": "text_classification",
                    "text_col": "text",
                    "label_col": "label",
                    "data_subdir": "text_cls_demo",
                    "eval_metric": "f1_macro",
                    "threshold": 0.5,
                    "data_dir": data_dir,
                },
            )
        finally:
            if prev is None:
                os.environ.pop("AGENT_SANDBOX", None)
            else:
                os.environ["AGENT_SANDBOX"] = prev
        self.assertEqual(stage.stage_code, "text_cls_sandbox")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.event_type.value, "eval_completed")
        # A genuine f1_macro was produced on real SST2 data (not a mock).
        self.assertGreater(result.event.metrics["primary"], 0.5)


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
        # + the three F3 sandbox capabilities (kaggle_eval_sandbox / run_research_sandbox /
        # text_cls_sandbox) + the three 2026-08-11 classifier sandbox capabilities
        # (image_cls_sandbox / audio_cls_sandbox / embedding_sandbox) = 18.
        self.assertEqual(len(data["capabilities"]), 18)
        cap_ids = [c["capability_id"] for c in data["capabilities"]]
        self.assertIn("kaggle_eval", cap_ids)
        self.assertIn("layer_11_external_audit", cap_ids)
        self.assertIn("kaggle_eval_sandbox", cap_ids)
        self.assertIn("run_research_sandbox", cap_ids)
        self.assertIn("text_cls_sandbox", cap_ids)
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
