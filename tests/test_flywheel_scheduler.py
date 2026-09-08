"""Tests for the B-flywheel event-driven scheduler.

Pins the contract:

* ``POST /workflow-runs/{run_id}/flywheel/schedule`` starts a daemon thread that
  polls ``badcase_path`` every ``poll_interval_sec`` and triggers a
  ``badcase_retrain`` iteration when ``count >= threshold``.
* ``GET  /workflow-runs/{run_id}/flywheel/schedule`` returns scheduler status,
  including ``trigger_count`` / ``last_badcase_count`` / ``last_verdict``.
* ``DELETE /workflow-runs/{run_id}/flywheel/schedule`` stops the thread
  cooperatively (``stop_event.set``) and joins it within the 10-second budget.
* Re-scheduling the same run while the previous thread is alive → 409 conflict.
* Iteration failures must NOT kill the scheduler loop (a transient executor
  error records ``state.error`` but the thread keeps polling).

The badcase CSV is shaped like the real titanic preset (with a ``Survived``
target) so the iteration executes end-to-end against the bundled executor
instead of being mocked out.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

import pandas as pd
from fastapi.testclient import TestClient

from safety_auto_research.control_plane.api import create_app
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store import Repository

# Minimal titanic-shape row that satisfies ``KaggleEvalExecutor._build_xy``
# (which extracts ``Title`` from ``Name`` and one-hot encodes ``Sex`` /
# ``Embarked``). Enough columns to drive a real ``badcase_retrain`` iteration.
_TITANIC_COLUMNS = [
    "PassengerId", "Survived", "Pclass", "Name", "Sex", "Age",
    "SibSp", "Parch", "Ticket", "Fare", "Cabin", "Embarked",
]


def _titanic_badcase_csv(n_rows: int) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "PassengerId": i,
                "Survived": 0,  # all-badcase is fine for a flywheel test
                "Pclass": 3,
                "Name": f"Smith, Mr. John {i}",
                "Sex": "male",
                "Age": 30.0 + (i % 5),
                "SibSp": 0,
                "Parch": 0,
                "Ticket": f"PC {i:06d}",
                "Fare": 7.25,
                "Cabin": "",
                "Embarked": "S",
            }
            for i in range(1, n_rows + 1)
        ],
        columns=_TITANIC_COLUMNS,
    )


def _wait_for(predicate, *, timeout: float = 30.0, interval: float = 0.5, message: str = "") -> bool:
    """Poll ``predicate`` until True or ``timeout`` (seconds). Returns last value."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return True
        time.sleep(interval)
    if message:
        print(f"[wait_for] timed out: {message}; last={last!r}")
    return False


class FlywheelSchedulerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app(ControlPlaneService(Repository())))

    def _create_run(self) -> str:
        r = self.client.post(
            "/workflow-runs",
            json={
                "program_id": "flywheel",
                "run_type": "flywheel",
                "entry_stage": "badcase_retrain",
                "target_id": "flywheel-titanic",
                "objective_snapshot": {"name": "scheduler test"},
            },
        )
        self.assertIn(r.status_code, (200, 201), r.text)
        return r.json()["run_id"]

    def test_starts_status_stops(self) -> None:
        run_id = self._create_run()
        with tempfile.TemporaryDirectory(prefix="flywheel-sched-") as tmp:
            badcase_path = os.path.join(tmp, "badcase.csv")
            # Bootstrap the file with a header so pd.read_csv returns 0 rows.
            pd.DataFrame(columns=_TITANIC_COLUMNS).to_csv(badcase_path, index=False)

            # Start scheduler with a threshold we will hit shortly.
            r = self.client.post(
                f"/workflow-runs/{run_id}/flywheel/schedule",
                json={
                    "badcase_path": badcase_path,
                    "threshold": 3,
                    "poll_interval_sec": 1.0,
                    "auto_clear_after_trigger": True,
                    "preset": "titanic",
                    "model": "logreg",
                    "eval_metric": "accuracy",
                    "badcase_ratio": 0.3,
                },
            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertTrue(body["scheduled"])
            self.assertEqual(body["threshold"], 3)

            # Before the threshold is hit, status reports 0.
            status = self.client.get(f"/workflow-runs/{run_id}/flywheel/schedule").json()
            self.assertTrue(status["scheduled"])
            self.assertIn(status["trigger_count"], (0, None))

            # Push 3 real-shape rows → scheduler should fire end-to-end.
            _titanic_badcase_csv(3).to_csv(badcase_path, index=False)

            fired = _wait_for(
                lambda: self.client.get(f"/workflow-runs/{run_id}/flywheel/schedule").json().get("trigger_count", 0) >= 1,
                timeout=30.0,
                message="scheduler did not fire within 30s",
            )
            if not fired:
                # Surface the last observed status so a regression report includes the
                # underlying error rather than just "did not fire".
                s = self.client.get(f"/workflow-runs/{run_id}/flywheel/schedule").json()
                print(f"[diag] last scheduler status: {s}")
            self.assertTrue(fired)

            # After firing, the run's persisted iteration history should contain a flywheel- eval.
            its = self.client.get(f"/workflow-runs/{run_id}/flywheel").json()
            self.assertGreaterEqual(len(its), 1)
            self.assertTrue(its[0]["eval_suite_id"].startswith("flywheel-"))

            # Stop the scheduler.
            stop = self.client.delete(f"/workflow-runs/{run_id}/flywheel/schedule")
            self.assertEqual(stop.status_code, 200, stop.text)
            self.assertTrue(stop.json()["stopped"])

            # Re-check: no longer running.
            after = self.client.get(f"/workflow-runs/{run_id}/flywheel/schedule").json()
            self.assertFalse(after["scheduled"])

    def test_double_start_returns_conflict(self) -> None:
        run_id = self._create_run()
        with tempfile.TemporaryDirectory(prefix="flywheel-sched-") as tmp:
            badcase_path = os.path.join(tmp, "badcase.csv")
            pd.DataFrame(columns=_TITANIC_COLUMNS).to_csv(badcase_path, index=False)

            first = self.client.post(
                f"/workflow-runs/{run_id}/flywheel/schedule",
                json={"badcase_path": badcase_path, "threshold": 999, "poll_interval_sec": 60.0, "preset": "titanic", "model": "logreg"},
            )
            self.assertEqual(first.status_code, 200)
            second = self.client.post(
                f"/workflow-runs/{run_id}/flywheel/schedule",
                json={"badcase_path": badcase_path, "threshold": 999, "poll_interval_sec": 60.0, "preset": "titanic", "model": "logreg"},
            )
            # Single-flight guard — second start fails with 409 conflict.
            self.assertEqual(second.status_code, 409, second.text)
            # Tear down
            self.client.delete(f"/workflow-runs/{run_id}/flywheel/schedule")

    def test_iteration_failure_does_not_kill_scheduler(self) -> None:
        """A badcase_retrain failure (bad data_dir) must leave the scheduler alive."""
        run_id = self._create_run()
        with tempfile.TemporaryDirectory(prefix="flywheel-sched-") as tmp:
            badcase_path = os.path.join(tmp, "badcase.csv")
            _titanic_badcase_csv(5).to_csv(badcase_path, index=False)

            # 'data_dir' pointing to a non-existent path forces the executor to raise
            # FileNotFoundError AFTER the scheduler decides to fire (so the loop's
            # inner try/except records state.error, but the outer try/except keeps
            # the loop alive).
            r = self.client.post(
                f"/workflow-runs/{run_id}/flywheel/schedule",
                json={
                    "badcase_path": badcase_path,
                    "threshold": 2,
                    "poll_interval_sec": 1.0,
                    "preset": "titanic",
                    "model": "logreg",
                    "eval_metric": "accuracy",
                    "data_dir": "/no/such/data_dir",
                    "auto_clear_after_trigger": False,
                },
            )
            self.assertEqual(r.status_code, 200)

            # Wait for at least one attempt; status must still be running AND error must be set.
            def attempt_seen() -> bool:
                s = self.client.get(f"/workflow-runs/{run_id}/flywheel/schedule").json()
                return bool(s.get("scheduled")) and bool(s.get("error"))

            survived = _wait_for(attempt_seen, timeout=20.0, message="scheduler should record failure but stay alive")
            self.assertTrue(survived)

            # Tear down — thread still alive, so DELETE should succeed.
            stop = self.client.delete(f"/workflow-runs/{run_id}/flywheel/schedule")
            self.assertEqual(stop.status_code, 200, stop.text)


if __name__ == "__main__":
    unittest.main()