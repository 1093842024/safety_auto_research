"""Tests for the 缺陷7 fix: the program-level evolution REST endpoint.

These verify that ``run_program_evolutionary_loop`` (OpenRSI island model) is no
longer an orphan path — it is now reachable over HTTP via
``POST /workflow-runs/{run_id}/program-evolution`` and observable via the
``GET`` twin. The loop runs with the offline ``TemplateOperatorBackend`` and
``audit=False`` so the test stays deterministic and free of any LLM dependency.
"""

from __future__ import annotations

import os
import time
import unittest

from safety_auto_research.control_plane.service import ControlPlaneService


def _find_titanic() -> str:
    here = os.path.abspath(__file__)
    cur = here
    for _ in range(8):
        cand = os.path.join(os.path.dirname(cur), "data", "kaggle", "titanic")
        if os.path.isdir(cand):
            return cand
        cur = os.path.dirname(cur)
    raise FileNotFoundError("titanic dataset not found")


_TITANIC_DIR = _find_titanic()


class ProgramEvolutionEndpointTests(unittest.TestCase):
    def _make_client(self):
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        svc = ControlPlaneService()
        app = create_app(svc)
        return TestClient(app), svc

    def test_program_evolution_endpoint_drives_loop_and_lists_candidates(self) -> None:
        client, svc = self._make_client()
        run_id = "api-prog-evo-1"
        resp = client.post(
            f"/workflow-runs/{run_id}/program-evolution",
            json={
                "task_config": {"data_dir": _TITANIC_DIR, "target": "Survived", "id_col": "PassengerId"},
                "backend_type": "template",
                "islands": 1,
                "pop_per_island": 2,
                "generations": 1,
                "max_workers": 1,
                "audit": False,
                "seed": 7,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["run_id"], run_id)
        # 缺陷2 pattern: returns immediately with running; the loop runs in the background.
        self.assertEqual(body["status"], "running")

        # Poll until the background loop reaches a terminal status.
        terminal = {"exited_converged", "exited_budget", "failed", "cancelled"}
        deadline = time.time() + 60.0
        final_status = svc.get_workflow_run(run_id).status
        while final_status not in terminal and time.time() < deadline:
            time.sleep(0.1)
            final_status = svc.get_workflow_run(run_id).status
        self.assertIn(final_status, terminal)

        # The GET twin returns the *program* (code) candidates only, with island lineage.
        got = client.get(f"/workflow-runs/{run_id}/program-evolution")
        self.assertEqual(got.status_code, 200)
        cands = got.json()
        self.assertTrue(cands, "expected at least one program candidate to be archived")
        self.assertTrue(
            all(c["node_kind"] == "program" for c in cands),
            "endpoint must return only node_kind=program candidates",
        )
        # island branch tag (gen{g}.isl{i}) confirms the OpenRSI island model ran.
        self.assertTrue(
            any(".isl" in (c.get("branch") or "") for c in cands),
            "program candidates should carry an island branch tag",
        )

    def test_program_evolution_endpoint_defaults_to_titanic_preset(self) -> None:
        """An empty-ish body (no data_dir) must still resolve to the bundled preset."""
        client, svc = self._make_client()
        run_id = "api-prog-evo-default"
        resp = client.post(
            f"/workflow-runs/{run_id}/program-evolution",
            json={"islands": 1, "pop_per_island": 1, "generations": 1, "audit": False},
        )
        self.assertEqual(resp.status_code, 200)
        terminal = {"exited_converged", "exited_budget", "failed", "cancelled"}
        deadline = time.time() + 60.0
        st = svc.get_workflow_run(run_id).status
        while st not in terminal and time.time() < deadline:
            time.sleep(0.1)
            st = svc.get_workflow_run(run_id).status
        self.assertIn(st, terminal)

    def test_program_evolution_get_404_for_unknown_run(self) -> None:
        client, _ = self._make_client()
        got = client.get("/workflow-runs/does-not-exist/program-evolution")
        self.assertEqual(got.status_code, 404)


if __name__ == "__main__":
    unittest.main()
