"""Tests for the layer_05 data-pipeline capability (Phase 2).

Pins the contract:

* ``DataPipelineExecutor`` is bound to ``layer_05_data_evaluation_cleaning``
  (replacing the long-standing stub).
* ``mode="cleaning"`` is fully implemented end-to-end:
    - minhash dedup drops near-duplicate rows
    - z-score noise filter drops outliers on numeric columns
    - PII regex redacts SSN / email / IPv4 / phone patterns
* ``mode="label"`` delegates to ``AutoLabelExecutor`` (no duplicate logic).
* ``mode="synthesis"`` and ``mode="collection"`` return a deferred-stub artifact
  with ``gate_passed=False, gate_result=WAIVED`` (so they don't falsely pass).
* Unknown modes fail cleanly (no crash).
* Artifact + EvalCompletedEvent are produced on the happy path.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import pandas as pd

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.execution_plane import ClosedLoopOrchestrator
from safety_auto_research.execution_plane.capabilities.data_pipeline_executor import (
    DataPipelineExecutor,
    _minhash_signature,
    _jaccard,
    _redact_pii,
    _run_cleaning,
    _PII_PATTERNS,
)
from safety_auto_research.execution_plane.capabilities.registry import default_capability_registry
from safety_auto_research.platform_contracts.enums import GateResult
from safety_auto_research.platform_contracts.enums import RunType


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------
class MinhashTest(unittest.TestCase):
    def test_same_text_same_signature(self) -> None:
        a = _minhash_signature("the quick brown fox")
        b = _minhash_signature("the quick brown fox")
        self.assertEqual(a, b)

    def test_different_text_different_signature(self) -> None:
        a = _minhash_signature("alpha beta gamma")
        b = _minhash_signature("delta epsilon zeta")
        self.assertNotEqual(a, b)

    def test_short_text_returns_some_signature(self) -> None:
        a = _minhash_signature("hi")
        self.assertIsInstance(a, set)


class JaccardTest(unittest.TestCase):
    def test_identical_sets(self) -> None:
        s = {1, 2, 3, 4}
        self.assertAlmostEqual(_jaccard(s, s), 1.0)

    def test_disjoint_sets(self) -> None:
        self.assertAlmostEqual(_jaccard({1, 2}, {3, 4}), 0.0)

    def test_overlap(self) -> None:
        # {1,2,3,4} ∩ {3,4,5,6} = {3,4} → |A ∩ B| = 2; ∪ = {1,2,3,4,5,6} → |A ∪ B| = 6 → jaccard = 1/3
        self.assertAlmostEqual(_jaccard({1, 2, 3, 4}, {3, 4, 5, 6}), 2 / 6)

    def test_empty_returns_zero(self) -> None:
        self.assertEqual(_jaccard(set(), {1, 2}), 0.0)
        self.assertEqual(_jaccard({1, 2}, set()), 0.0)


class PiiRedactTest(unittest.TestCase):
    def test_redacts_ssn_email_phone_ipv4(self) -> None:
        s = "ssn 123-45-6789 email alice@x.com phone 415-555-1234 ip 10.0.0.1"
        out, counts = _redact_pii(s)
        self.assertIn("[REDACTED:ssn]", out)
        self.assertIn("[REDACTED:email]", out)
        self.assertIn("[REDACTED:phone_us]", out)
        self.assertIn("[REDACTED:ipv4]", out)
        self.assertEqual(counts.get("ssn", 0) >= 1, True)
        self.assertEqual(counts.get("email", 0) >= 1, True)
        self.assertEqual(counts.get("phone_us", 0) >= 1, True)
        self.assertEqual(counts.get("ipv4", 0) >= 1, True)

    def test_no_pii_no_change(self) -> None:
        s = "no pii here just plain text"
        out, counts = _redact_pii(s)
        self.assertEqual(out, s)
        self.assertEqual(counts, {})

    def test_patterns_loaded(self) -> None:
        # Sanity check: the four canonical labels are present.
        self.assertEqual(set(_PII_PATTERNS.keys()), {"ssn", "email", "ipv4", "phone_us"})


# ---------------------------------------------------------------------------
# Cleaning end-to-end (real CSV in / CSV out)
# ---------------------------------------------------------------------------
class CleaningPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="data-pipeline-")

    def _write_csv(self, name: str, df: pd.DataFrame) -> str:
        path = os.path.join(self.tmp, name)
        df.to_csv(path, index=False)
        return path

    def test_dedup_z_filter_pii_all_combine(self) -> None:
        # 4 distinct rows + 2 near-duplicates + 1 extreme outlier.
        # No id column on purpose: the z-score filter scans ALL numeric columns,
        # so adding an id (1..7) would dampen the outlier signal. Real cleaning
        # scenarios either don't have id columns or pre-cast them to non-numeric.
        df = pd.DataFrame([
            {"text": "the quick brown fox", "score": 0.5},
            {"text": "the quick brown fox", "score": 0.6},  # near-dup
            {"text": "completely different", "score": 0.7},
            {"text": "yet another unique row", "score": 0.8},
            {"text": "completely different", "score": 0.9},  # near-dup of row 2
            {"text": "outlier row", "score": 1_000_000.0},   # extreme z-score outlier
            {"text": "contact alice@x.com", "score": 0.4},   # has email
        ])
        in_path = self._write_csv("input.csv", df)
        out_path = os.path.join(self.tmp, "cleaned.csv")

        # Use a generous z_threshold=1.5 because with only ~5 rows the outlier
        # also inflates the std, so a 5-sigma filter wouldn't trip. The point
        # of the test is "extreme values get dropped", not "exact sigma math".
        metrics = _run_cleaning(in_path, out_path, z_threshold=1.5)
        self.assertEqual(metrics["rows_in"], 7.0)

        out_df = pd.read_csv(out_path)
        # After dedup + outlier filter, we expect 4 rows max. We assert >=2 since
        # the exact count depends on minhash behaviour; the key invariants are:
        #  - no near-duplicates remain (each text is unique)
        #  - the email was redacted
        #  - the extreme outlier row is gone (score not 1_000_000)
        texts = list(out_df["text"].astype(str))
        self.assertEqual(len(texts), len(set(texts)), "no duplicate texts should remain")
        self.assertNotIn("alice@x.com", " ".join(texts))
        # The outlier's score should be gone (extreme enough to always trip z>2).
        self.assertNotIn(1_000_000.0, out_df["score"].astype(float).tolist())

    def test_empty_rows(self) -> None:
        df = pd.DataFrame({"id": [], "text": [], "score": []})
        in_path = self._write_csv("empty.csv", df)
        out_path = os.path.join(self.tmp, "empty.cleaned.csv")
        metrics = _run_cleaning(in_path, out_path)
        self.assertEqual(metrics["rows_in"], 0.0)
        self.assertEqual(metrics["rows_out"], 0.0)


# ---------------------------------------------------------------------------
# Executor integration (real orchestrator)
# ---------------------------------------------------------------------------
class ExecutorIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="data-pipeline-it-")
        self.orch = ClosedLoopOrchestrator(ControlPlaneService())

    def _start_run(self) -> str:
        run = self.orch.svc.create_workflow_run(
            CreateWorkflowRunRequest(
                program_id="discovery",
                run_type=RunType.DISCOVERY,
                entry_stage="data_cleaning",
                target_id="discovery-data-test",
                objective_snapshot={"name": "data pipeline discovery test"},
            )
        )
        self.orch.svc.start_workflow_run(run.run_id)
        return run.run_id

    def test_layer_05_capability_resolves_to_real_executor(self) -> None:
        cap = default_capability_registry().resolve("layer_05_data_evaluation_cleaning")
        self.assertIsNotNone(cap)
        self.assertIsInstance(cap.executor, DataPipelineExecutor)

    def test_cleaning_mode_end_to_end(self) -> None:
        run_id = self._start_run()
        df = pd.DataFrame([
            {"id": 1, "text": "alpha beta gamma", "score": 0.5},
            {"id": 2, "text": "alpha beta gamma", "score": 0.6},  # dup
            {"id": 3, "text": "totally different text", "score": 0.7},
            {"id": 4, "text": "totally different text", "score": 0.8},  # dup
        ])
        in_path = os.path.join(self.tmp, "input.csv")
        df.to_csv(in_path, index=False)
        out_path = os.path.join(self.tmp, "output.csv")

        _, result = self.orch.run_capability(
            run_id,
            "layer_05_data_evaluation_cleaning",
            {"mode": "cleaning", "input_path": in_path, "output_path": out_path},
        )

        self.assertIsNotNone(result.event)
        metrics = result.event.metrics
        self.assertEqual(metrics["rows_in"], 4.0)
        self.assertGreaterEqual(metrics["rows_out"], 1.0)
        # Output file actually exists.
        self.assertTrue(os.path.exists(out_path))
        cleaned_df = pd.read_csv(out_path)
        # No duplicates should remain.
        self.assertEqual(len(cleaned_df), len(set(cleaned_df["text"].astype(str))))

    def test_synthesis_mode_emits_deferred_stub(self) -> None:
        run_id = self._start_run()
        _, result = self.orch.run_capability(
            run_id,
            "layer_05_data_evaluation_cleaning",
            {"mode": "synthesis", "provider": {"base_url": "http://x.test", "model": "fake"}},
        )
        # Stub should not auto-pass.
        self.assertEqual(result.gate_result, GateResult.WAIVED)
        self.assertEqual(result.event.gate_passed, False)
        self.assertEqual(result.event.metrics["deferred"], 1.0)
        self.assertIn("deferred", (result.detail or "").lower())

    def test_collection_mode_emits_deferred_stub(self) -> None:
        run_id = self._start_run()
        _, result = self.orch.run_capability(
            run_id,
            "layer_05_data_evaluation_cleaning",
            {"mode": "collection"},
        )
        self.assertEqual(result.gate_result, GateResult.WAIVED)
        self.assertIn("robots.txt", (result.detail or "").lower())

    def test_unknown_mode_fails_cleanly(self) -> None:
        run_id = self._start_run()
        _, result = self.orch.run_capability(
            run_id,
            "layer_05_data_evaluation_cleaning",
            {"mode": "this_mode_does_not_exist"},
        )
        self.assertEqual(result.final_status.value, "failed")
        self.assertIn("unknown", (result.detail or "").lower())

    def test_missing_input_path_fails(self) -> None:
        run_id = self._start_run()
        _, result = self.orch.run_capability(
            run_id,
            "layer_05_data_evaluation_cleaning",
            {"mode": "cleaning"},
        )
        self.assertEqual(result.final_status.value, "failed")
        self.assertIn("input_path", (result.detail or "").lower())


if __name__ == "__main__":
    unittest.main()