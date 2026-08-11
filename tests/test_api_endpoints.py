"""Integration tests for the control-plane REST API (``control_plane/api.py``).

Closes A4 in ``doc/product_ux_backlog.md``: previously the api.py endpoint surface had
no dedicated endpoint-level integration coverage. These tests drive the real FastAPI app
through ``fastapi.testclient.TestClient`` and assert request/response + side effects
(events emitted, state transitions) for the core endpoints:

  * WorkflowRun lifecycle: create / get / list / start / cancel / 404s
  * DecisionRecord: record / list / get
  * StageRun listing (empty for a fresh run)
  * Audit & improvement observability endpoints (empty for a fresh run)
  * Agent wire-protocol endpoint
  * Benchmark-task catalog listing
  * Debug endpoint: returns "debugging" and emits a ``debug_result`` event

Heavy long-running loops (dual-loop / closed-loop / full experiment) are intentionally NOT
exercised here — those spawn background threads and run real eval; they are covered by the
execution-plane suites instead.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from safety_auto_research.control_plane.api import create_app
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.schemas import RecordDecisionRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.platform_contracts.enums import DecisionType
from safety_auto_research.platform_contracts.enums import RunType


class _EnvMixin(unittest.TestCase):
    """Mirror the isolated env setup used by other api integration tests."""

    def setUp(self) -> None:
        self._files = []
        for _ in range(3):
            t = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
            t.close()
            os.remove(t.name)
            self._files.append(t.name)
        self._old = {
            k: os.environ.get(k)
            for k in ("CUSTOM_TASKS_STORE", "CONTROL_PLANE_STORE", "RESEARCH_STATE_DB")
        }
        os.environ["CUSTOM_TASKS_STORE"] = self._files[0]
        os.environ["CONTROL_PLANE_STORE"] = self._files[1]
        os.environ["RESEARCH_STATE_DB"] = self._files[2]
        self.svc = ControlPlaneService()
        self.client = TestClient(create_app(self.svc))

    def tearDown(self) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for f in self._files:
            if os.path.exists(f):
                os.remove(f)

    def _make_run(self, target_id: str = "platform.titanic") -> dict:
        req = CreateWorkflowRunRequest(
            program_id="benchmark",
            run_type=RunType.STANDARD_RESEARCH,
            entry_stage="inner_research",
            target_id=target_id,
            objective_snapshot={
                "benchmark_task_id": target_id,
                "name": "titanic",
                "eval_metric": "accuracy",
                "direction": "higher",
                "config": {"inner_loop": {"model": "gbm"}},
            },
            requested_outcomes=["x"],
        )
        r = self.client.post("/workflow-runs", json=req.model_dump(mode="json"))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()


class WorkflowRunLifecycleTest(_EnvMixin):
    def test_create_returns_201_and_get_matches(self) -> None:
        run = self._make_run()
        run_id = run["run_id"]
        self.assertEqual(run["status"], "requested")
        got = self.client.get(f"/workflow-runs/{run_id}")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["run_id"], run_id)
        self.assertEqual(got.json()["target_id"], "platform.titanic")

    def test_list_includes_created_run(self) -> None:
        run = self._make_run()
        listing = self.client.get("/workflow-runs").json()
        self.assertTrue(any(r["run_id"] == run["run_id"] for r in listing))

    def test_start_then_cancel_transitions_state(self) -> None:
        run = self._make_run()
        run_id = run["run_id"]
        started = self.client.post(f"/workflow-runs/{run_id}/start")
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["status"], "running")
        cancelled = self.client.post(f"/workflow-runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_get_unknown_run_is_404(self) -> None:
        r = self.client.get("/workflow-runs/does-not-exist")
        self.assertEqual(r.status_code, 404)

    def test_cancel_unknown_run_is_404(self) -> None:
        r = self.client.post("/workflow-runs/does-not-exist/cancel")
        self.assertEqual(r.status_code, 404)

    def test_stages_empty_for_fresh_run(self) -> None:
        run_id = self._make_run()["run_id"]
        stages = self.client.get(f"/workflow-runs/{run_id}/stages").json()
        self.assertEqual(stages, [])

    def test_audit_and_improvement_endpoints_empty_for_fresh_run(self) -> None:
        run_id = self._make_run()["run_id"]
        self.assertEqual(self.client.get(f"/workflow-runs/{run_id}/audit").json(), [])
        self.assertEqual(self.client.get(f"/workflow-runs/{run_id}/improvements").json(), [])

    def test_events_for_unknown_run_empty(self) -> None:
        evs = self.client.get("/events", params={"run_id": "nope"}).json()
        self.assertEqual(evs, [])


class DecisionRecordTest(_EnvMixin):
    def test_record_list_and_get_decision(self) -> None:
        run_id = self._make_run()["run_id"]
        body = RecordDecisionRequest(
            decision_type=DecisionType.REVISIT,
            target_stage="orchestration",
            reason_codes=["audit_refine"],
            evidence_refs=["audit-report://x"],
        )
        r = self.client.post(
            f"/workflow-runs/{run_id}/decisions", json=body.model_dump(mode="json")
        )
        self.assertEqual(r.status_code, 201, r.text)
        decision_id = r.json()["decision_id"]

        listing = self.client.get(f"/workflow-runs/{run_id}/decisions").json()
        self.assertTrue(any(d["decision_id"] == decision_id for d in listing))

        got = self.client.get(f"/decisions/{decision_id}")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["decision_id"], decision_id)

    def test_record_decision_unknown_run_is_404(self) -> None:
        body = RecordDecisionRequest(decision_type=DecisionType.REVISIT)
        r = self.client.post("/workflow-runs/nope/decisions", json=body.model_dump(mode="json"))
        self.assertEqual(r.status_code, 404)


class ObservabilityAndCatalogTest(_EnvMixin):
    def test_agent_protocol_exposes_schemas_and_capabilities(self) -> None:
        r = self.client.get("/agent/protocol")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("schemas", body)
        self.assertIn("capabilities", body)
        self.assertIn("tool_reference", body)
        self.assertGreaterEqual(len(body["capabilities"]), 1)

    def test_benchmark_tasks_catalog_nonempty(self) -> None:
        r = self.client.get("/benchmark-tasks")
        self.assertEqual(r.status_code, 200, r.text)
        tasks = r.json()
        self.assertTrue(any(t.get("task_id", "").startswith("platform.") for t in tasks))


class DebugEndpointTest(_EnvMixin):
    def test_debug_outer_returns_debugging_and_emits_event(self) -> None:
        run_id = self._make_run()["run_id"]
        r = self.client.post(
            f"/workflow-runs/{run_id}/debug", json={"stage": "outer"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "debugging")

        # The debug runs in a background thread that always emits a DebugEvent
        # (immediately, when there is no inner result to audit).
        deadline = time.time() + 6.0
        found = False
        while time.time() < deadline:
            evs = self.client.get("/events", params={"run_id": run_id}).json()
            if any(e.get("event_type") == "debug_result" for e in evs):
                found = True
                break
            time.sleep(0.1)
        self.assertTrue(found, "debug endpoint did not emit a debug_result event")

    def test_debug_invalid_stage_is_400(self) -> None:
        run_id = self._make_run()["run_id"]
        r = self.client.post(
            f"/workflow-runs/{run_id}/debug", json={"stage": "sideways"}
        )
        self.assertEqual(r.status_code, 400)

    def test_debug_unknown_run_is_404(self) -> None:
        r = self.client.post("/workflow-runs/nope/debug", json={"stage": "outer"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
