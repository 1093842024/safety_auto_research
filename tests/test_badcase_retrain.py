"""Tests for the B Flywheel minimal closed loop — ``BadcaseRetrainExecutor``.

Covers:
  * capability registration (``badcase_retrain`` is discoverable + bound to a real executor)
  * happy path: replay-retraining on true hard negatives improves badcase recall while the
    frozen original eval set does not degrade -> regression gate passes + model accepted
  * rejection path: poisoned (mislabeled) badcase degrades the frozen original eval set ->
    the regression gate fails + the new model is rejected
  * ``_regression_ok`` direction handling (lower-is-better vs. higher-is-better)

The executor is deterministic: ``LogisticRegression`` on ``make_classification`` with fixed
seeds, and a positive ``regression_tol`` on the happy path so minor CV noise never flips it.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import pandas as pd

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.execution_plane import ClosedLoopOrchestrator
from safety_auto_research.execution_plane.capabilities.badcase_retrain_executor import (
    BadcaseRetrainExecutor,
)
from safety_auto_research.execution_plane.capabilities.registry import default_capability_registry
from safety_auto_research.platform_contracts.enums import RunType


def _make_flywheel_data(tmpdir: str) -> tuple[str, str]:
    """Synthesize a train set + a *true* badcase set (baseline mispredictions)."""
    from sklearn.datasets import make_classification
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split

    X, y = make_classification(
        n_samples=600,
        n_features=6,
        n_informative=4,
        n_redundant=0,
        n_clusters_per_class=2,
        class_sep=0.7,
        random_state=0,
    )
    cols = [f"f{i}" for i in range(6)]
    df = pd.DataFrame(X, columns=cols)
    df["label"] = y
    train_path = os.path.join(tmpdir, "train.csv")
    df.to_csv(train_path, index=False)

    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.5, random_state=1, stratify=y)
    clf = LogisticRegression(max_iter=2000).fit(Xtr, ytr)
    pred = clf.predict(Xte)
    wrong = Xte[pred != yte]
    wrong_y = yte[pred != yte]
    bc = pd.DataFrame(wrong, columns=cols)
    bc["label"] = wrong_y
    badcase_path = os.path.join(tmpdir, "badcase.csv")
    bc.to_csv(badcase_path, index=False)
    return train_path, badcase_path


def _make_poisoned_badcase(tmpdir: str) -> str:
    """A badcase CSV that is *mislabeled* (labels flipped) — retraining on it must degrade."""
    from sklearn.datasets import make_classification

    X, y = make_classification(
        n_samples=600,
        n_features=6,
        n_informative=4,
        n_redundant=0,
        n_clusters_per_class=2,
        class_sep=0.7,
        random_state=0,
    )
    cols = [f"f{i}" for i in range(6)]
    df = pd.DataFrame(X, columns=cols)
    df["label"] = 1 - y  # flipped labels: pure noise w.r.t. the original signal
    train_path = os.path.join(tmpdir, "train.csv")
    # Original train stays correctly labeled; only the badcase is poisoned.
    df["label"] = y
    df.to_csv(train_path, index=False)
    poisoned = pd.DataFrame(X, columns=cols)
    poisoned["label"] = 1 - y
    badcase_path = os.path.join(tmpdir, "badcase.csv")
    poisoned.to_csv(badcase_path, index=False)
    return badcase_path


class BadcaseRetrainExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.orch = ClosedLoopOrchestrator(ControlPlaneService())

    def _start_run(self) -> str:
        run = self.orch.svc.create_workflow_run(
            CreateWorkflowRunRequest(
                program_id="p1",
                run_type=RunType.BADCASE_RETRAIN,
                entry_stage="badcase_retrain",
                target_id="m1",
                objective_snapshot={"target_metric": "accuracy", "target_threshold": 0.8},
            )
        )
        self.orch.svc.start_workflow_run(run.run_id)
        return run.run_id

    def _run(self, badcase_path: str, **extra) -> tuple:
        run_id = self._start_run()
        return self.orch.run_capability(
            run_id,
            "badcase_retrain",
            {
                "preset": "custom",
                "target": "label",
                "model": "logreg",
                "data_dir": self.tmpdir,
                "badcase_path": badcase_path,
                **extra,
            },
        )


class BadcaseRetrainCapabilityRegistryTest(unittest.TestCase):
    def test_badcase_retrain_is_registered_and_bound(self) -> None:
        reg = default_capability_registry()
        cap = reg.resolve("badcase_retrain")
        self.assertIsNotNone(cap)
        self.assertIsInstance(cap.executor, BadcaseRetrainExecutor)
        self.assertIn("badcase_retrain", {c["capability_id"] for c in reg.list_all_capabilities()})
        # It is an *extra* (non-infra-layer) capability like kaggle_eval.
        self.assertFalse(cap.is_infra)


class BadcaseRetrainHappyPathTest(BadcaseRetrainExecutorTest):
    def test_replay_retrain_improves_badcase_and_passes_regression(self) -> None:
        _train_path, badcase_path = _make_flywheel_data(self.tmpdir)
        stage, result = self._run(badcase_path, regression_tol=0.05)
        self.assertEqual(stage.stage_code, "badcase_retrain")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.event_type.value, "eval_completed")
        m = result.event.metrics
        # Badcase recall improves (objective / reward).
        self.assertGreater(m["retrained_badcase_acc"], m["baseline_badcase_acc"])
        self.assertEqual(m["badcase_improved"], 1.0)
        # Regression gate holds on the frozen original eval set (hard constraint).
        self.assertEqual(m["regression_passed"], 1.0)
        self.assertTrue(result.gate_result.value == "passed")
        self.assertTrue(result.output_refs)


class BadcaseRetrainRegressionGateTest(BadcaseRetrainExecutorTest):
    def test_poisoned_badcase_is_rejected_by_regression_gate(self) -> None:
        badcase_path = _make_poisoned_badcase(self.tmpdir)
        stage, result = self._run(badcase_path, badcase_ratio=0.5, regression_tol=0.0)
        m = result.event.metrics
        # Retraining on mislabeled badcase degrades the frozen original eval set.
        self.assertLess(m["retrained_primary"], m["baseline_primary"])
        self.assertEqual(m["regression_passed"], 0.0)
        # The hard constraint forces a reject, so the gate fails overall.
        self.assertTrue(result.gate_result.value == "failed")

    def test_missing_badcase_path_raises(self) -> None:
        run_id = self._start_run()
        with self.assertRaises(ValueError):
            self.orch.run_capability(
                run_id,
                "badcase_retrain",
                {"preset": "custom", "target": "label", "data_dir": self.tmpdir},
            )


class RegressionOkDirectionTest(unittest.TestCase):
    def test_higher_is_better(self) -> None:
        # No degradation: retrained >= baseline - tol.
        self.assertTrue(BadcaseRetrainExecutor._regression_ok(0.80, 0.82, 0.0, "higher"))
        self.assertTrue(BadcaseRetrainExecutor._regression_ok(0.80, 0.79, 0.02, "higher"))
        self.assertFalse(BadcaseRetrainExecutor._regression_ok(0.80, 0.75, 0.0, "higher"))

    def test_lower_is_better(self) -> None:
        # Degradation for lower-is-better means the metric *increased* beyond tol.
        self.assertTrue(BadcaseRetrainExecutor._regression_ok(0.20, 0.18, 0.0, "lower"))
        self.assertFalse(BadcaseRetrainExecutor._regression_ok(0.20, 0.25, 0.0, "lower"))
        self.assertTrue(BadcaseRetrainExecutor._regression_ok(0.20, 0.23, 0.03, "lower"))


if __name__ == "__main__":
    unittest.main()
