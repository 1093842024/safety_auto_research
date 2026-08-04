"""Supplementary regression tests for the 2026-08-04 code-review 五.3 list.

These pin the *behavioral* contracts of fixes that previously had no test coverage:
  * crossover output genuinely depends on (both) parent programs (not byte-identical
    templates);
  * ``reward_func`` does not let NaN / None fitness poison the RL signal, and
    ``op=="le"`` (lower-is-better) yields a positive improvement for a real decrease;
  * the adapter's id-aligned scoring branch returns VALID_SOLUTION=False (not a
    crash) when the submission carries NaN/missing predictions;
  * ``prepare`` label-encodes a string/object target into stable ints so the sklearn
    pipelines + int comparison work;
  * the research leaderboard/capture path picks the BEST run for a lower-is-better
    task whose declared metric is not a recorded key (uses ``primary``, not a
    worst-run ``accuracy``).

All tests are dataset-independent (synthetic data / pure logic) so they run
everywhere, not just where the titanic dataset is present.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

# Make the ``safety_auto_research`` package importable regardless of CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_workspace_root(start: str) -> str:
    cur = start
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "safety_auto_research")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.dirname(os.path.dirname(_HERE))  # best-effort fallback


_ROOT = _find_workspace_root(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest  # noqa: E402
from safety_auto_research.control_plane.service import ControlPlaneService  # noqa: E402
from safety_auto_research.openmle_integration.adapter import (  # noqa: E402
    OpenMLETaskAdapter,
    OpenMLETaskConfig,
)
from safety_auto_research.openmle_integration.contracts import (  # noqa: E402
    TEST_FITNESS,
    VALID_SOLUTION,
    VALID_SOLUTION_FEEDBACK,
)
from safety_auto_research.openmle_integration.operators import (  # noqa: E402
    TemplateOperatorBackend,
    crossover_program,
    draft_program,
)
from safety_auto_research.openmle_integration.reward_bridge import (  # noqa: E402
    RewardConfig,
    reward_func,
)
from safety_auto_research.platform_contracts.enums import RunType  # noqa: E402


# --------------------------------------------------------------------------
# 五.3 (1) -- crossover output must depend on both parents
# --------------------------------------------------------------------------
class CrossoverParentInfluenceTest(unittest.TestCase):
    def _parents(self, backend):
        a = draft_program(
            backend, target="Survived", id_col="PassengerId",
            task_description="classify titanic", variant=0, caller_stage="inner",
        )
        b = draft_program(
            backend, target="Survived", id_col="PassengerId",
            task_description="classify titanic", variant=1, caller_stage="inner",
        )
        return a, b

    def test_crossover_distinct_from_parents_and_deterministic(self):
        backend = TemplateOperatorBackend()
        a, b = self._parents(backend)
        self.assertNotEqual(a, b)
        cx1 = crossover_program(
            backend, target="Survived", id_col="PassengerId",
            task_description="classify titanic", parent_programs=(a, b),
            caller_stage="inner",
        )
        cx2 = crossover_program(
            backend, target="Survived", id_col="PassengerId",
            task_description="classify titanic", parent_programs=(a, b),
            caller_stage="inner",
        )
        # Same parents -> identical program (deterministic, seeded by parent hash).
        self.assertEqual(cx1, cx2)
        # The blend must not silently equal either parent template.
        self.assertNotEqual(cx1, a)
        self.assertNotEqual(cx1, b)

    def test_changing_a_parent_changes_crossover(self):
        backend = TemplateOperatorBackend()
        a, b = self._parents(backend)
        c = draft_program(
            backend, target="Survived", id_col="PassengerId",
            task_description="classify titanic", variant=2, caller_stage="inner",
        )
        cx_ab = crossover_program(
            backend, target="Survived", id_col="PassengerId",
            task_description="classify titanic", parent_programs=(a, b),
            caller_stage="inner",
        )
        cx_ac = crossover_program(
            backend, target="Survived", id_col="PassengerId",
            task_description="classify titanic", parent_programs=(a, c),
            caller_stage="inner",
        )
        # Swapping the second parent must change the blend (parent hash seeds rs).
        self.assertNotEqual(cx_ab, cx_ac)


# --------------------------------------------------------------------------
# 五.3 (2) -- reward_func NaN guards + lower-is-better direction
# --------------------------------------------------------------------------
class RewardNaNAndLowerIsBetterTest(unittest.TestCase):
    def test_nan_fitness_clamped_to_zero(self):
        rc = reward_func(float("nan"), valid=True, prev_fitness=0.8)
        self.assertEqual(rc.improvement, 0.0)
        self.assertTrue(math.isfinite(rc.total))

    def test_nan_prev_fitness_no_improvement(self):
        rc = reward_func(0.82, valid=True, prev_fitness=float("nan"))
        self.assertEqual(rc.improvement, 0.0)
        self.assertTrue(math.isfinite(rc.total))

    def test_nan_novelty_zeroed(self):
        rc = reward_func(0.82, valid=True, prev_fitness=0.8, novelty=float("nan"))
        self.assertEqual(rc.diversity, 0.0)
        self.assertTrue(math.isfinite(rc.total))

    def test_none_fitness_finite_total(self):
        rc = reward_func(None, valid=True, prev_fitness=0.8)
        self.assertEqual(rc.improvement, 0.0)
        self.assertTrue(math.isfinite(rc.total))

    def test_lower_is_better_decrease_is_improvement(self):
        cfg = RewardConfig(maximize=False)
        # 0.5 is a real improvement over 0.8 for a lower-is-better objective.
        rc = reward_func(0.5, valid=True, prev_fitness=0.8, config=cfg)
        self.assertGreater(rc.improvement, 0.0)
        # An increase must NOT be rewarded.
        rc2 = reward_func(0.8, valid=True, prev_fitness=0.5, config=cfg)
        self.assertEqual(rc2.improvement, 0.0)


# --------------------------------------------------------------------------
# 五.3 (3) -- id-aligned scoring branch with NaN/missing predictions
# --------------------------------------------------------------------------
def _write_tiny_numeric_dataset(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    df = pd.DataFrame(
        {
            "PassengerId": list(range(1, 11)),
            "Age": [22, 38, 26, 35, 30, 40, 28, 33, 29, 31],
            "Survived": [0, 1, 1, 0, 1, 0, 1, 0, 1, 0],
        }
    )
    df.to_csv(os.path.join(data_dir, "train.csv"), index=False)


class IdMergeNaNPathTest(unittest.TestCase):
    def _adapter(self, tmp: str) -> OpenMLETaskAdapter:
        data_dir = os.path.join(tmp, "data")
        _write_tiny_numeric_dataset(data_dir)
        cfg = OpenMLETaskConfig(
            name="t", data_dir=data_dir, target="Survived", id_col="PassengerId",
            eval_metric="accuracy", direction="higher",
        )
        return OpenMLETaskAdapter(cfg)

    def test_id_merge_valid_submission_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(tmp)
            state, _ = adapter.prepare()
            eval_df = state["eval_df"]
            ids = eval_df[state["id_col"]].values
            # Perfect predictions aligned on the eval-row id column.
            sub = pd.DataFrame({state["id_col"]: ids, state["target"]: eval_df[state["target"]].values})
            sub_path = os.path.join(adapter._interpreter.workdir, "submission.csv")
            sub.to_csv(sub_path, index=False)
            _, outcome = adapter._score_submission(state, sub_path)
            self.assertTrue(outcome[VALID_SOLUTION])
            self.assertAlmostEqual(outcome[TEST_FITNESS], 1.0, places=4)
            adapter.close(state)

    def test_id_merge_nan_preds_is_valid_false_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(tmp)
            state, _ = adapter.prepare()
            eval_df = state["eval_df"]
            ids = eval_df[state["id_col"]].values
            # Aligned ids but NaN in the prediction column -> cannot cast to int.
            sub = pd.DataFrame({state["id_col"]: ids, state["target"]: [np.nan] * len(ids)})
            sub_path = os.path.join(adapter._interpreter.workdir, "submission.csv")
            sub.to_csv(sub_path, index=False)
            new_state, outcome = adapter._score_submission(state, sub_path)
            # Must degrade gracefully to an invalid solution -- never raise.
            self.assertFalse(outcome[VALID_SOLUTION])
            self.assertIsNone(outcome[TEST_FITNESS])
            self.assertIn("NaN", outcome[VALID_SOLUTION_FEEDBACK])
            adapter.close(new_state)


# --------------------------------------------------------------------------
# 五.3 (4) -- prepare with a string/object target column
# --------------------------------------------------------------------------
class StringLabelPrepareTest(unittest.TestCase):
    def test_string_target_label_encoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            os.makedirs(data_dir, exist_ok=True)
            df = pd.DataFrame(
                {
                    "PassengerId": list(range(1, 11)),
                    "Age": [22, 38, 26, 35, 30, 40, 28, 33, 29, 31],
                    "Species": ["cat", "dog", "dog", "cat", "cat",
                                "dog", "cat", "dog", "dog", "cat"],
                }
            )
            df.to_csv(os.path.join(data_dir, "train.csv"), index=False)
            cfg = OpenMLETaskConfig(
                name="t", data_dir=data_dir, target="Species", id_col="PassengerId",
                eval_metric="accuracy", direction="higher",
            )
            adapter = OpenMLETaskAdapter(cfg)
            state, info = adapter.prepare()
            truth = np.asarray(state["eval_truth"])
            # The held-out truth must be integer-encoded, not raw strings.
            self.assertTrue(
                np.issubdtype(truth.dtype, np.integer),
                f"eval_truth dtype={truth.dtype} (expected integer)",
            )
            # Two distinct classes collapse to two distinct ints.
            self.assertEqual(len(set(truth.tolist())), 2)
            # A builtin-pipeline step must work end-to-end on the encoded target.
            new_state, outcome = adapter.step_task(
                state, {"model": "rf", "fe": "basic", "cv_folds": 3}
            )
            self.assertTrue(outcome[VALID_SOLUTION])
            self.assertIsNotNone(outcome[TEST_FITNESS])
            adapter.close(new_state)


# --------------------------------------------------------------------------
# 五.3 (5) -- P1-3 lower-is-better leaderboard / capture_run_record
# --------------------------------------------------------------------------
class LowerIsBetterLeaderboardTest(unittest.TestCase):
    def _make_run(self, svc: ControlPlaneService, task_id: str) -> object:
        req = CreateWorkflowRunRequest(
            program_id="benchmark", run_type=RunType.STANDARD_RESEARCH,
            entry_stage="inner_research", target_id=task_id,
            objective_snapshot={"benchmark_task_id": task_id},
            requested_outcomes=["x"],
        )
        return svc.create_workflow_run(req)

    def test_capture_picks_best_primary_not_worst_accuracy(self):
        svc = ControlPlaneService()
        # Declared metric "log_loss" is NOT among the recorded metrics (kaggle events
        # only carry `primary` + `accuracy`). direction == lower.
        req = CreateWorkflowRunRequest(
            program_id="benchmark", run_type=RunType.STANDARD_RESEARCH,
            entry_stage="inner_research", target_id="task.lower",
            objective_snapshot={
                "benchmark_task_id": "task.lower",
                "eval_metric": "log_loss",
                "direction": "lower",
                "config": {"inner_loop": {"model": "gbm"}},
            },
            requested_outcomes=["x"],
        )
        run = svc.create_workflow_run(req)
        # Two eval snapshots: primary (real objective, lower-is-better) + accuracy.
        svc._repo.record_metric(run.run_id, "primary", 0.95)
        svc._repo.record_metric(run.run_id, "primary", 0.30)
        svc._repo.record_metric(run.run_id, "accuracy", 0.50)
        svc._repo.record_metric(run.run_id, "accuracy", 0.60)
        rec = svc.capture_run_record(run.run_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["metric_name"], "log_loss")
        self.assertEqual(rec["direction"], "lower")
        # Fix: score = min(primary) = 0.30. The bug would have taken
        # min/max(accuracy) -> 0.50 or 0.60, i.e. the WORST run.
        self.assertAlmostEqual(rec["score"], 0.30, places=4)

    def test_leaderboard_and_top3_direction_aware(self):
        svc = ControlPlaneService()
        r1 = self._make_run(svc, "task.lower")
        r2 = self._make_run(svc, "task.lower")
        svc.report_run_metric(r1.run_id, "log_loss", "lower", 0.50)
        svc.report_run_metric(r2.run_id, "log_loss", "lower", 0.30)
        board = svc.leaderboard()
        by_task = {b["task_id"]: b for b in board}
        self.assertIn("task.lower", by_task)
        # Smaller is better -> 0.30 wins the per-task slot.
        self.assertAlmostEqual(by_task["task.lower"]["score"], 0.30, places=4)
        recs = svc.list_research_records("task.lower")
        self.assertEqual(len(recs), 2)
        # Both records are in the top-3 (only two exist); the smaller score is top3.
        self.assertEqual(len([r for r in recs if r.get("is_top3")]), 2)
        self.assertTrue(min(recs, key=lambda r: r["score"]).get("is_top3"))


if __name__ == "__main__":
    unittest.main()
