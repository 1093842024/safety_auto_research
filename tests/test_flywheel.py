"""Tests for the B-flywheel endpoint + badcase collection (Phase 1.5).

Covers:
  * ``BadcaseRetrainExecutor.collect_badcase`` — auto-derives a labeled badcase CSV from
    the baseline's held-out mispredictions, written as *original* rows so the executor's
    feature engineering round-trips correctly (titanic: Title re-extracted from Name).
  * ``POST /workflow-runs/{run_id}/flywheel`` — one full iteration (collect -> retrain ->
    regression gate) returns before/after metrics + an accept/reject verdict.
  * ``GET /workflow-runs/{run_id}/flywheel`` — replays the iteration history.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import pandas as pd

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.execution_plane.capabilities.badcase_retrain_executor import (
    BadcaseRetrainExecutor,
)
from safety_auto_research.platform_contracts.enums import RunType

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TITANIC_DIR = os.path.join(_PKG_ROOT, "data", "kaggle", "titanic")


class CollectBadcaseTest(unittest.TestCase):
    def test_collect_badcase_roundtrips_original_rows(self) -> None:
        # The titanic fe extracts "Title" from "Name" and drops Name/PassengerId/Ticket/
        # Cabin — so a badcase CSV MUST carry the original columns for a correct re-read.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "badcase.csv")
            path, n = BadcaseRetrainExecutor.collect_badcase(
                data_dir=_TITANIC_DIR, preset="titanic", model_name="logreg", out_path=out
            )
            self.assertGreater(n, 0, "baseline logistic regression should mispredict some rows")
            self.assertTrue(os.path.exists(path))
            bc = pd.read_csv(path)
            # Original columns survived (Name is needed to re-derive Title on re-read).
            self.assertIn("Name", bc.columns)
            self.assertIn("Survived", bc.columns)
            # Re-read through _build_xy to prove the round-trip works.
            from safety_auto_research.execution_plane.capabilities.kaggle_eval_executor import (
                KaggleEvalExecutor,
            )

            X, y = KaggleEvalExecutor._build_xy(bc, "Survived", "titanic", [], "basic")
            self.assertEqual(len(X), n)
            self.assertEqual(len(y), n)


class FlywheelEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        self.client = TestClient(create_app(ControlPlaneService()))

    def _create_run(self) -> str:
        run = self.client.post(
            "/workflow-runs",
            json={
                "program_id": "p1",
                "run_type": "flywheel",
                "entry_stage": "badcase_retrain",
                "target_id": "m1",
                "objective_snapshot": {"target_metric": "accuracy"},
            },
        ).json()
        self.client.post(f"/workflow-runs/{run['run_id']}/start")
        return run["run_id"]

    def test_flywheel_iteration_end_to_end(self) -> None:
        run_id = self._create_run()
        resp = self.client.post(
            f"/workflow-runs/{run_id}/flywheel",
            json={"model": "logreg", "regression_tol": 0.05},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("metrics", body)
        self.assertIn("baseline_primary", body["metrics"])
        self.assertIn("retrained_primary", body["metrics"])
        self.assertIn("regression_passed", body)
        self.assertIn("badcase_improved", body)
        self.assertTrue(body["badcase_collected"], "no badcase_path -> should auto-collect")
        self.assertIn(body["verdict"], ("ACCEPT", "REJECT"))

    def test_flywheel_history_is_replayed(self) -> None:
        run_id = self._create_run()
        self.client.post(
            f"/workflow-runs/{run_id}/flywheel",
            json={"model": "logreg", "regression_tol": 0.05},
        )
        resp = self.client.get(f"/workflow-runs/{run_id}/flywheel")
        self.assertEqual(resp.status_code, 200)
        iters = resp.json()
        self.assertEqual(len(iters), 1)
        self.assertTrue(iters[0]["eval_suite_id"].startswith("flywheel-"))
        self.assertIn("metrics", iters[0])

    def test_flywheel_missing_run_returns_404(self) -> None:
        resp = self.client.post("/workflow-runs/does-not-exist/flywheel", json={})
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
