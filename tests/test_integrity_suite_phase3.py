"""Tests for the Phase 3 opt-in integrity gates (claims-lint / number-trace / adversarial-review).

These gates are ported from spark-to-paper's superior mechanisms and are
deliberately kept OUT of the default loop: they run only in the explicit
``deep`` stage. The tests assert both the gate *logic* and the *non-invasiveness*
guarantee (the default ``all`` stage and the platform are unchanged).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import TestCase

from safety_auto_research.integrity_suite import gate_runner as gr
from safety_auto_research.integrity_suite import claims_lint
from safety_auto_research.integrity_suite import number_trace
from safety_auto_research.integrity_suite import adversarial_review

_SUITE = Path(__file__).resolve().parents[1] / "integrity_suite"

# Curated, well-formed claims that every Phase 3 gate should accept.
_GOOD_CLAIMS = [
    {"id": "c1", "claim": "accuracy reached 0.91", "answer": "yes",
     "evidence": "run_abc eval_completed accuracy series accuracy [0.88, 0.90, 0.91]"},
    {"id": "c2", "claim": "loss decreased to 0.40", "answer": "yes",
     "evidence": "run_abc eval_completed loss series loss 0.40 csv"},
]
_GOOD_SCORES = {"accuracy": [0.88, 0.90, 0.91], "loss": [0.42, 0.40]}
# A judge that returns a maximal float score, so the base claim_support gate stays
# green and the Phase 3 gates actually execute in the deep stage. The platform's
# judge contract is judge(answer, claim, evidence) -> float (see audit_executor).
_ALWAYS_SUPPORT = lambda answer, claim, evidence: 1.0


class ClaimsLintTest(TestCase):
    def test_waives_without_claims(self):
        out = claims_lint.gate_claims_lint({})
        self.assertTrue(out.ok)
        self.assertEqual(out.status, "waived")

    def test_stub_evidence_fails(self):
        ctx = {"claims": [{"id": "c", "claim": "model beats baseline", "evidence": "TBD"}]}
        out = claims_lint.gate_claims_lint(ctx)
        self.assertFalse(out.ok)
        self.assertEqual(out.payload["issues"][0]["rule"], "stub_or_incomplete")

    def test_duplicate_claim_fails(self):
        ctx = {"claims": [
            {"id": "c1", "claim": "accuracy improved", "evidence": "run_abc accuracy csv 0.9"},
            {"id": "c2", "claim": "ACCURACY improved!", "evidence": "run_abc accuracy csv 0.91"},
        ]}
        out = claims_lint.gate_claims_lint(ctx)
        self.assertFalse(out.ok)
        self.assertTrue(any(i["rule"] == "duplicate_claim" for i in out.payload["issues"]))

    def test_short_anchorless_evidence_fails(self):
        ctx = {"claims": [{"id": "c", "claim": "model is robust", "evidence": "see above"}]}
        out = claims_lint.gate_claims_lint(ctx)
        self.assertFalse(out.ok)
        self.assertTrue(any(i["rule"] == "claim_without_anchor" for i in out.payload["issues"]))

    def test_well_formed_claims_pass(self):
        out = claims_lint.gate_claims_lint({"claims": _GOOD_CLAIMS})
        self.assertTrue(out.ok, out.detail)


class NumberTraceTest(TestCase):
    def test_waives_without_claims(self):
        out = number_trace.gate_number_trace({"scores": _GOOD_SCORES})
        self.assertTrue(out.ok)
        self.assertEqual(out.status, "waived")

    def test_waives_without_scores(self):
        out = number_trace.gate_number_trace({"claims": _GOOD_CLAIMS})
        self.assertTrue(out.ok)
        self.assertEqual(out.status, "waived")

    def test_traced_number_passes(self):
        ctx = {"claims": [{"id": "c", "claim": "accuracy reached 0.91", "evidence": "accuracy series"}],
               "scores": _GOOD_SCORES}
        out = number_trace.gate_number_trace(ctx)
        self.assertTrue(out.ok, out.detail)

    def test_orphan_number_fails(self):
        ctx = {"claims": [{"id": "c", "claim": "accuracy reached 0.99",
                           "evidence": "run_abc run_id"}],
               "scores": {"accuracy": [0.88, 0.90]}}
        out = number_trace.gate_number_trace(ctx)
        self.assertFalse(out.ok)
        self.assertTrue(any(i["rule"] == "orphan_number" for i in out.payload["issues"]))

    def test_contradiction_fails(self):
        ctx = {"claims": [{"id": "c", "claim": "accuracy reached 0.99",
                           "evidence": "accuracy series from run_abc"}],
               "scores": {"accuracy": [0.88, 0.90]}}
        out = number_trace.gate_number_trace(ctx)
        self.assertFalse(out.ok)
        self.assertTrue(any(i["rule"] == "number_contradicts_data" for i in out.payload["issues"]))

    def test_percentage_scaling_is_tolerated(self):
        ctx = {"claims": [{"id": "c", "claim": "accuracy reached 95%",
                           "evidence": "accuracy series"}],
               "scores": {"accuracy": [0.95]}}
        out = number_trace.gate_number_trace(ctx)
        self.assertTrue(out.ok, out.detail)


class AdversarialReviewTest(TestCase):
    def test_waives_without_claims(self):
        out = adversarial_review.gate_adversarial_review({"scores": _GOOD_SCORES})
        self.assertTrue(out.ok)
        self.assertEqual(out.status, "waived")

    def test_overclaim_superlative_fails_without_comparison(self):
        ctx = {"claims": [{"id": "c", "claim": "We present the first novel approach",
                           "evidence": "we trained a model on the dataset"}],
               "scores": _GOOD_SCORES}
        out = adversarial_review.gate_adversarial_review(ctx)
        self.assertFalse(out.ok)
        self.assertTrue(any(i["rule"] == "overclaim_superlative" for i in out.payload["issues"]))

    def test_significance_claim_without_variance_fails(self):
        ctx = {"claims": [{"id": "c", "claim": "improves accuracy by 0.05",
                           "evidence": "single run, no seeds"}],
               "scores": {"accuracy": [0.90]}}
        out = adversarial_review.gate_adversarial_review(ctx)
        self.assertFalse(out.ok)
        self.assertTrue(any(i["rule"] == "significance_no_variance" for i in out.payload["issues"]))

    def test_well_formed_claims_pass(self):
        out = adversarial_review.gate_adversarial_review(
            {"claims": _GOOD_CLAIMS, "scores": _GOOD_SCORES})
        self.assertTrue(out.ok, out.detail)


class DeepStageTest(TestCase):
    def test_phase3_gates_registered(self):
        for nm in ("claims_lint", "number_trace", "adversarial_review"):
            self.assertIn(nm, gr.GATES)

    def test_deep_stage_exists_and_is_opt_in(self):
        self.assertIn("deep", gr.STAGE_GATES)
        # The deep battery must NOT have leaked into the default "all" loop.
        self.assertNotIn("claims_lint", gr.STAGE_GATES["all"])
        self.assertNotIn("number_trace", gr.STAGE_GATES["all"])
        self.assertNotIn("adversarial_review", gr.STAGE_GATES["all"])

    def test_deep_stage_executes_all_four_gates(self):
        ctx = {"run_id": "run_abc", "claims": _GOOD_CLAIMS, "scores": _GOOD_SCORES,
               "judge": _ALWAYS_SUPPORT}
        report = gr.run_gates("deep", ctx)
        names = [g["gate"] for g in report["gates"]]
        self.assertEqual(
            names, ["claim_support", "claims_lint", "number_trace", "adversarial_review"]
        )
        self.assertTrue(report["ok"], report["summary"])

    def test_deep_stage_stops_on_bad_claim_lint(self):
        bad = [{"id": "c", "claim": "model is robust", "evidence": "TBD"}]
        ctx = {"run_id": "run_abc", "claims": bad, "scores": _GOOD_SCORES,
               "judge": _ALWAYS_SUPPORT}
        report = gr.run_gates("deep", ctx)
        self.assertFalse(report["ok"])
        self.assertEqual(report["gates"][-1]["gate"], "claims_lint")


class DeepStageNonInvasivenessTest(TestCase):
    def test_importable_without_web_stack(self):
        repo_root = Path(__file__).resolve().parents[2]
        code = (
            "import sys, importlib;\n"
            "blocked = ('fastapi', 'pydantic', 'pandas', 'sklearn');\n"
            "class Block:\n"
            "    def find_module(self, name, path=None):\n"
            "        return self if name.split('.')[0] in blocked else None\n"
            "    def load_module(self, name):\n"
            "        raise ImportError('blocked: ' + name)\n"
            "sys.meta_path.insert(0, Block());\n"
            "m = importlib.import_module('safety_auto_research.integrity_suite.gate_runner');\n"
            "assert 'deep' in m.STAGE_GATES and 'claims_lint' in m.GATES;\n"
            "print('import-ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(repo_root)),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("import-ok", proc.stdout)


if __name__ == "__main__":
    import unittest

    unittest.main()
