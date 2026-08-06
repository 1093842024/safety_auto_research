"""Regression tests for the 2026-08-05 fix pass.

Covers the cheap, dependency-light invariants that the multi-step review fix
addressed:

* M2  -- ``_clean_score`` rejects NaN/inf so they can never enter the leaderboard.
* M4  -- ``reward_population`` preserves ``novelty=0.0`` (a true duplicate) instead
        of clobbering it to ``1.0`` (max diversity) via ``x or 1.0``.
* L4  -- two empty/zero embeddings compare as identical (so empty configs ARE
        deduped), while empty-vs-nonempty stays dissimilar.
* L6  -- custom-task registry tolerates a corrupt record missing ``task_id``
        (uses ``.get`` instead of a hard subscript).
* 缺陷9 -- ``inner_acc`` is bound at function scope in the dual loop, so it can't
        raise ``UnboundLocalError`` when the loop body never runs.
"""

import math
import os
import tempfile
import unittest
from typing import Any, Optional


class _Candidate:
    """Minimal duck-typed evolution candidate for ``reward_population``."""

    def __init__(
        self,
        fitness: Optional[float] = 0.9,
        novelty: float = 1.0,
        status: str = "ok",
        code: str = "x = 1",
    ) -> None:
        self.fitness = fitness
        self.novelty = novelty
        self.status = status
        self.code = code


class M2CleanScoreTest(unittest.TestCase):
    def test_clean_score_rejects_nan(self) -> None:
        from safety_auto_research.control_plane.service import _clean_score

        with self.assertRaises(ValueError):
            _clean_score(float("nan"))

    def test_clean_score_rejects_inf(self) -> None:
        from safety_auto_research.control_plane.service import _clean_score

        with self.assertRaises(ValueError):
            _clean_score(float("inf"))
        with self.assertRaises(ValueError):
            _clean_score(float("-inf"))

    def test_clean_score_passes_finite(self) -> None:
        from safety_auto_research.control_plane.service import _clean_score

        self.assertEqual(_clean_score(0.8231), 0.8231)
        self.assertEqual(_clean_score(0.0), 0.0)


class M4NoveltyTest(unittest.TestCase):
    def test_zero_novelty_preserved(self) -> None:
        from safety_auto_research.openmle_integration.reward_bridge import (
            reward_population,
        )

        out = reward_population([_Candidate(novelty=0.0)])
        self.assertEqual(out[0].diversity, 0.0)

    def test_missing_novelty_defaults_to_one(self) -> None:
        from safety_auto_research.openmle_integration.reward_bridge import (
            reward_population,
        )

        out = reward_population([_Candidate(novelty=None)])  # type: ignore[arg-type]
        self.assertEqual(out[0].diversity, 1.0 * 1.0)

    def test_full_novelty_diversity(self) -> None:
        from safety_auto_research.openmle_integration.reward_bridge import (
            reward_population,
        )

        out = reward_population([_Candidate(novelty=1.0)])
        self.assertEqual(out[0].diversity, 1.0)


class L4CosineTest(unittest.TestCase):
    def test_empty_embeddings_equal(self) -> None:
        from safety_auto_research.control_plane.evolution import _cosine, _embed

        self.assertEqual(_cosine(_embed(""), _embed("")), 1.0)

    def test_empty_vs_nonempty_dissimilar(self) -> None:
        from safety_auto_research.control_plane.evolution import _cosine, _embed

        self.assertEqual(_cosine(_embed(""), _embed("model gbm cv 5")), 0.0)

    def test_normal_cosine(self) -> None:
        from safety_auto_research.control_plane.evolution import _cosine, _embed

        a = _embed("model gbm fe basic")
        b = _embed("model gbm fe basic")
        # Identical normalized vectors -> cosine 1.0.
        self.assertAlmostEqual(_cosine(a, b), 1.0, places=6)


class L6RegistryRobustnessTest(unittest.TestCase):
    def test_missing_task_id_does_not_raise(self) -> None:
        from safety_auto_research.benchmark_tasks import registry as reg

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "custom_tasks.json")
            # Corrupt record: no "task_id" key.
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('[{"name": "broken", "task_type": "tabular_classification"}]')
            orig = reg._store_path
            try:
                reg._store_path = lambda: path  # type: ignore[assignment]
                # Both of these used to raise KeyError on the missing key.
                items = reg.list_custom_tasks()
                self.assertEqual(len(items), 1)
                deleted = reg.delete_custom_task("does-not-exist")
                self.assertFalse(deleted)
            finally:
                reg._store_path = orig  # type: ignore[assignment]


class Defect9InnerAccTest(unittest.TestCase):
    def test_inner_acc_bound_at_function_scope(self) -> None:
        """The dual loop must have ``inner_acc`` bound even if the loop body never
        runs (e.g. max_outer_iters <= 0), so it can't raise UnboundLocalError."""
        import inspect

        from safety_auto_research.execution_plane.orchestrator import (
            ClosedLoopOrchestrator,
        )

        src = inspect.getsource(ClosedLoopOrchestrator.run_dual_loop)
        # The guard we added initialises inner_acc before the while loop.
        self.assertIn("inner_acc = 0.0", src)
        # And the only loop-local (re)assignment must still be present.
        self.assertIn("inner_acc = _primary_score(", src)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# L1 / L2 / L3 / L7 — deferred-item fixes (2026-08-05 review)                 #
# --------------------------------------------------------------------------- #

class _FakeRepo:
    """Minimal in-memory research-record store for testing ``_recompute_top3``."""

    def __init__(self, recs: list[dict[str, Any]]) -> None:
        self._recs = recs
        self.updates: list[tuple[str, bool]] = []

    def list_research_records(self, task_id: str) -> list[dict[str, Any]]:
        return list(self._recs)

    def update_research_record(self, record_id: str, is_top3: bool = False) -> None:
        self.updates.append((record_id, is_top3))


class _FakeTask:
    """Duck-typed benchmark task exposing only ``.direction``."""

    def __init__(self, direction: str) -> None:
        self.direction = direction


class L1DirectionTest(unittest.TestCase):
    def _run(self, recs, canonical_direction):
        import safety_auto_research.control_plane.service as service
        from safety_auto_research.control_plane.service import ControlPlaneService

        repo = _FakeRepo(recs)
        svc = ControlPlaneService.__new__(ControlPlaneService)
        svc._repo = repo
        original = service.get_task
        try:
            service.get_task = lambda task_id: (
                _FakeTask(canonical_direction) if canonical_direction is not None else None
            )
            svc._recompute_top3("task.x")
        finally:
            service.get_task = original
        return {rid: ist for rid, ist in repo.updates}

    def test_canonical_lower_wins_over_wrong_record_direction(self) -> None:
        # Records carry the WRONG direction ("higher"); the task's canonical
        # direction is "lower" -> ranking must flip to ascending (best = lowest).
        recs = [
            {"record_id": "a", "score": 0.10, "direction": "higher"},
            {"record_id": "b", "score": 0.50, "direction": "higher"},
            {"record_id": "c", "score": 0.90, "direction": "higher"},
            {"record_id": "d", "score": 1.20, "direction": "higher"},
        ]
        top = self._run(recs, "lower")
        self.assertTrue(top["a"])  # lowest score -> top3 under "lower"
        self.assertTrue(top["b"])
        self.assertTrue(top["c"])
        self.assertFalse(top["d"])

    def test_unknown_task_falls_back_to_first_record_direction(self) -> None:
        # No canonical task -> historical behaviour: use recs[0].direction ("higher").
        # Highest score wins; the lowest (d) must be excluded from the top-3.
        recs = [
            {"record_id": "a", "score": 1.20, "direction": "higher"},
            {"record_id": "b", "score": 0.90, "direction": "higher"},
            {"record_id": "c", "score": 0.50, "direction": "higher"},
            {"record_id": "d", "score": 0.10, "direction": "higher"},
        ]
        top = self._run(recs, None)
        self.assertTrue(top["a"])  # highest score -> top3 under "higher"
        self.assertTrue(top["b"])
        self.assertTrue(top["c"])
        self.assertFalse(top["d"])


class L2GateOpTest(unittest.TestCase):
    def test_derives_le_for_lower_direction(self) -> None:
        from safety_auto_research.execution_plane.capabilities.kaggle_eval_executor import (
            derive_gate_op,
        )

        self.assertEqual(derive_gate_op({}, {"direction": "lower"}), "le")
        self.assertEqual(derive_gate_op({}, {"direction": "LOWER"}), "le")

    def test_derives_ge_for_higher_direction(self) -> None:
        from safety_auto_research.execution_plane.capabilities.kaggle_eval_executor import (
            derive_gate_op,
        )

        self.assertEqual(derive_gate_op({}, {"direction": "higher"}), "ge")
        # missing direction defaults to higher -> ge
        self.assertEqual(derive_gate_op({}, {}), "ge")

    def test_explicit_op_wins_over_direction(self) -> None:
        from safety_auto_research.execution_plane.capabilities.kaggle_eval_executor import (
            derive_gate_op,
        )

        # An explict op must NOT be flipped by a conflicting direction.
        self.assertEqual(
            derive_gate_op({"op": "ge"}, {"direction": "lower"}), "ge"
        )
        self.assertEqual(
            derive_gate_op({}, {"direction": "lower", "op": "ge"}), "ge"
        )
        self.assertEqual(
            derive_gate_op({"op": "le"}, {"direction": "higher"}), "le"
        )


class L3PartialJudgeTest(unittest.TestCase):
    def test_partial_failure_still_scores_successful_subset(self) -> None:
        from unittest import mock

        from safety_auto_research.control_plane import llm_judge
        from safety_auto_research.control_plane.eval_runner import llm_judge_eval

        # 3 aligned pairs; the 2nd judge call fails -> partial_failed == 1,
        # score must be the average of the two successful LLM scores (0.9, 0.5).
        refs = _write_csv(
            ["prompt,reference", "p1,cat", "p2,dog", "p3,fish"]
        )
        preds = _write_csv(
            ["prompt,output", "p1,A", "p2,B", "p3,C"]
        )

        calls = {"n": 0}

        def _fake_call(*, prompt, reference, prediction, url):
            calls["n"] += 1
            if calls["n"] == 2:
                raise llm_judge.LLMJudgeError("judge timeout")
            return {
                "score": 0.9 if calls["n"] == 1 else 0.5,
                "rationale": "r",
                "evidence_refs": [],
            }

        with mock.patch.object(llm_judge, "call_llm_judge", _fake_call):
            result = llm_judge_eval(
                refs, preds, judge_url="http://test-judge", metric="f1"
            )

        self.assertEqual(result["n"], 2)
        self.assertAlmostEqual(result["score"], 0.7, places=5)
        self.assertEqual(result["details"]["judge"], "llm")
        self.assertEqual(result["details"]["partial_failed"], 1)
        self.assertIsNotNone(result["details"]["judge_error_sample"])


class L7AlignmentTest(unittest.TestCase):
    def test_partial_alignment_reports_dropped_count(self) -> None:
        from safety_auto_research.control_plane.eval_runner import llm_judge_eval

        # 2 references, but one prediction key ("pX") doesn't match -> dropped == 1.
        refs = _write_csv(["prompt,reference", "p1,cat", "p2,dog"])
        preds = _write_csv(["prompt,output", "p1,A", "pX,B"])

        result = llm_judge_eval(refs, preds, judge_url=None, metric="f1")
        self.assertEqual(result["details"]["alignment"]["matched"], 1)
        self.assertEqual(result["details"]["alignment"]["preds"], 2)
        self.assertEqual(result["details"]["alignment"]["dropped"], 1)
        # Score is still computed on the matched subset (no crash, no silent drop).
        self.assertIn("f1", result["details"])


def _write_csv(lines: list[str]) -> str:
    """Write CSV lines to a temp file and return its path."""
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="eval_test_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
