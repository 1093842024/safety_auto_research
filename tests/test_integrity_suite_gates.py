"""Tests for the integrity suite (Phase 0/1 of the spark-to-paper integration).

Scope — the *additive* integrity layer only:

* ``integrity_suite.svg_audit``   -- the ported geometric SVG auditor (its own
  ``--selftest`` is the authority on defect detection; here we only assert the
  CLI contract the gate depends on: exit codes and single-line JSON on stdout).
* ``integrity_suite.gate_runner`` -- the fail-closed scheduler: registry shape,
  first-failure short-circuit, skip-vs-fail discipline, exit-code vocabulary,
  and each gate's verdict against the platform validator it wraps.

Two invariants matter more than any individual assertion, and both are tested:

1. **Non-invasiveness.** Importing the suite must not touch, monkeypatch or
   re-implement platform behaviour. ``novelty_filter`` / ``_select_objective`` /
   ``evaluate_constraint`` keep their exact semantics; the gates only read their
   answers.
2. **Fail-closed, not fail-silent.** An absent *input* is a waiver (code 0, status
   ``waived``); absent *machinery* is a failure (code 1). A check that could not
   run must never look like a check that passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from safety_auto_research.control_plane.evolution import Candidate
from safety_auto_research.integrity_suite import gate_runner as gr

_SUITE = Path(gr.__file__).resolve().parent


def _cand(cid: str, params: dict, *, kind: str = "config", code: str | None = None) -> Candidate:
    return Candidate(
        candidate_id=cid,
        run_id="r-int",
        params=params,
        generation=1,
        node_kind=kind,
        code=code,
    )


# Two cards, two labels inside them, 24px Times — audits clean (0 errors, 0 warnings)
# against the auditor's publication defaults (20px type floor, card containment).
_CLEAN_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200" viewBox="0 0 400 200">
  <rect x="20" y="20" width="220" height="60" fill="#dde" stroke="#334"/>
  <text x="34" y="60" font-family="Times New Roman" font-size="24">Alpha stage</text>
  <rect x="20" y="110" width="220" height="60" fill="#edd" stroke="#433"/>
  <text x="34" y="150" font-family="Times New Roman" font-size="24">Beta stage</text>
</svg>
"""

# Text that starts inside the canvas but runs past its right edge -> canvas_overflow.
_OVERFLOW_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120" viewBox="0 0 240 120">
  <text x="180" y="60" font-family="Times New Roman" font-size="24">This label runs far past the right edge of the canvas</text>
</svg>
"""

# Legible-to-a-human 12px type, below the auditor's 20px publication floor — used to
# prove the --min-font-px passthrough actually reaches the auditor.
_SMALL_TYPE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200" viewBox="0 0 400 200">
  <rect x="20" y="20" width="220" height="60" fill="#dde" stroke="#334"/>
  <text x="34" y="55" font-family="Times New Roman" font-size="12">Alpha stage</text>
</svg>
"""


# --------------------------------------------------------------- svg_audit CLI
class SvgAuditCliTest(unittest.TestCase):
    """The gate shells out to svg_audit.py, so its CLI contract is load-bearing.

    That contract, verified here: ``--json <path>`` *writes* the report to a file
    (it takes an argument — it is NOT a boolean flag), and the exit code is
    0 ok / 1 defects / 2 usage.
    """

    def _audit(self, svg_text: str, *extra: str):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "fig.svg"
            p.write_text(svg_text, encoding="utf-8")
            rep = Path(td) / "fig.audit.json"
            proc = subprocess.run(
                [sys.executable, str(_SUITE / "svg_audit.py"), str(p), "--json", str(rep), *extra],
                capture_output=True,
                text=True,
            )
            self.assertTrue(
                rep.is_file(),
                f"--json must write a report file (stdout={proc.stdout!r} stderr={proc.stderr!r})",
            )
            return proc.returncode, json.loads(rep.read_text(encoding="utf-8"))

    def test_selftest_passes(self):
        """The upstream self-test is the auditor's own correctness proof."""
        proc = subprocess.run(
            [sys.executable, str(_SUITE / "svg_audit.py"), "--selftest"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("selftest ok", (proc.stdout + proc.stderr).lower())

    def test_clean_svg_exits_zero_with_ok_report(self):
        rc, report = self._audit(_CLEAN_SVG)
        self.assertEqual(rc, 0, report)
        self.assertTrue(report.get("ok"), report)
        self.assertEqual(report.get("errors"), [], report)

    def test_defective_svg_exits_nonzero_with_error_codes(self):
        rc, report = self._audit(_OVERFLOW_SVG)
        self.assertEqual(rc, 1, report)
        self.assertFalse(report.get("ok"), report)
        self.assertTrue(report.get("errors"), "a failing audit must name its defects")
        self.assertTrue(all("code" in e for e in report["errors"]), report["errors"])
        self.assertIn("canvas_overflow", {e["code"] for e in report["errors"]})

    def test_usage_error_exits_two(self):
        proc = subprocess.run(
            [sys.executable, str(_SUITE / "svg_audit.py"), "/nonexistent/fig.svg"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_min_font_px_threshold_is_respected(self):
        """The type floor is a knob, not a constant — the gate passes it through."""
        rc_default, rep_default = self._audit(_SMALL_TYPE_SVG)
        rc_relaxed, rep_relaxed = self._audit(_SMALL_TYPE_SVG, "--min-font-px", "8")
        self.assertEqual(rc_default, 1, rep_default)
        self.assertEqual(rc_relaxed, 0, rep_relaxed)


# ------------------------------------------------------------- registry shape
class RegistryTest(unittest.TestCase):
    def test_every_stage_references_registered_gates(self):
        for stage, names in gr.STAGE_GATES.items():
            for nm in names:
                self.assertIn(nm, gr.GATES, f"stage {stage!r} references unknown gate {nm!r}")

    def test_all_stage_covers_every_gate(self):
        """`all` is the Definition of Done for the DEFAULT loop — a new default gate
        must not be silently omitted. The Phase 3 heuristic gates are opt-in: they
        run only in the `deep` stage, so they are intentionally NOT in `all`."""
        _OPTIN_DEEP_ONLY = {"claims_lint", "number_trace", "adversarial_review"}
        self.assertEqual(set(gr.STAGE_GATES["all"]), set(gr.GATES) - _OPTIN_DEEP_ONLY)

    def test_unknown_stage_is_usage_error_not_a_pass(self):
        report = gr.run_gates("no-such-stage", {})
        self.assertFalse(report["ok"])
        self.assertEqual(report["code"], gr.GATE_USAGE)

    def test_status_vocabulary_matches_platform_enum(self):
        """Statuses reuse GateResult's values so consumers speak one language."""
        from safety_auto_research.platform_contracts.enums import GateResult

        vocabulary = {g.value for g in GateResult}
        for status in (gr.STATUS_PASSED, gr.STATUS_FAILED, gr.STATUS_WAIVED):
            self.assertIn(status, vocabulary)


# ---------------------------------------------------------------- novelty gate
class NoveltyGateTest(unittest.TestCase):
    def test_diverse_batch_passes(self):
        cands = [_cand("a", {"model": "rf", "n": 100}), _cand("b", {"model": "gbm", "n": 400})]
        out = gr.gate_novelty({"candidates": cands})
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(out.status, gr.STATUS_PASSED)
        self.assertEqual(out.payload["kept"], 2)

    def test_total_collapse_fails(self):
        """Every proposal a duplicate of the archive == the search stopped exploring."""
        archived = [{"model": "rf", "n": 100}]
        cands = [_cand("a", {"model": "rf", "n": 100})]
        out = gr.gate_novelty({"candidates": cands, "archived_params": archived})
        self.assertFalse(out.ok)
        self.assertEqual(out.code, gr.GATE_ISSUES)
        self.assertEqual(out.status, gr.STATUS_FAILED)
        self.assertIn("diversity collapse", out.detail)
        self.assertEqual(out.payload["rejected_ids"], ["a"])

    def test_thin_diversity_fails_against_min_kept_frac(self):
        cands = [
            _cand("dup", {"model": "rf", "n": 100}),
            _cand("dup2", {"model": "rf", "n": 100}),
            _cand("new", {"model": "gbm", "n": 400}),
        ]
        out = gr.gate_novelty(
            {
                "candidates": cands,
                "archived_params": [{"model": "rf", "n": 100}],
                "min_kept_frac": 0.9,
            }
        )
        self.assertFalse(out.ok)
        self.assertIn("diversity thin", out.detail)
        self.assertLess(out.payload["kept_frac"], 0.9)

    def test_program_nodes_use_exact_code_dedup(self):
        """Program grain: two ~95%-identical templates are NOT duplicates (cosine would
        say they are). Only byte-identical code is rejected. Mirrors the platform rule."""
        code_a = "def solve(df):\n    return RandomForestClassifier().fit(df)\n"
        code_b = "def solve(df):\n    return GradientBoostingClassifier().fit(df)\n"
        out = gr.gate_novelty(
            {
                "candidates": [
                    _cand("p1", {}, kind="program", code=code_a),
                    _cand("p2", {}, kind="program", code=code_b),
                ],
                "archived_codes": [code_a],
            }
        )
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(out.payload["kept"], 1)
        self.assertEqual(out.payload["rejected_ids"], ["p1"])

    def test_absent_input_is_waived_not_failed(self):
        out = gr.gate_novelty({})
        self.assertTrue(out.ok)
        self.assertEqual(out.status, gr.STATUS_WAIVED)

    def test_dict_candidates_from_cli_are_accepted(self):
        out = gr.gate_novelty(
            {"candidates": [{"candidate_id": "x", "params": {"model": "rf"}, "generation": 0}]}
        )
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(out.payload["total"], 1)

    def test_malformed_candidate_fails(self):
        out = gr.gate_novelty({"candidates": [{"generation": "not-an-int"}]})
        self.assertFalse(out.ok)
        self.assertIn("malformed", out.detail)


# ------------------------------------------------------- metric direction gate
class MetricDirectionGateTest(unittest.TestCase):
    def test_higher_is_better_picks_max(self):
        out = gr.gate_metric_direction(
            {"scores": {"eval.accuracy": [0.80, 0.86, 0.83]}, "metric": "accuracy", "direction": "higher"}
        )
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(out.payload["selected"], 0.86)

    def test_lower_is_better_picks_min(self):
        out = gr.gate_metric_direction(
            {"scores": {"eval_loss": [0.9, 0.4, 0.6]}, "metric": "eval_loss", "direction": "lower"}
        )
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(out.payload["selected"], 0.4)
        self.assertEqual(out.payload["expected_extreme"], "min")

    def test_accuracy_exemption_does_not_false_positive(self):
        """The selector documents that accuracy-like metrics are never inverted. A
        lower-is-better task that also reports accuracy must NOT trip the gate."""
        out = gr.gate_metric_direction(
            {"scores": {"accuracy": [0.7, 0.9]}, "metric": "", "direction": "lower"}
        )
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(out.payload["selected"], 0.9)
        self.assertTrue(out.payload["accuracy_exempt"])

    def test_empty_series_is_unrankable_failure(self):
        out = gr.gate_metric_direction({"scores": {"eval.accuracy": []}, "direction": "higher"})
        self.assertFalse(out.ok)
        self.assertIn("unrankable", out.detail)

    def test_non_numeric_score_fails(self):
        out = gr.gate_metric_direction({"scores": {"acc": ["n/a"]}, "direction": "higher"})
        self.assertFalse(out.ok)
        self.assertIn("non-numeric", out.detail)

    def test_nan_is_rejected(self):
        """NaN breaks Python's sort comparator; it must never reach a leaderboard."""
        out = gr.gate_metric_direction(
            {"scores": {"eval_loss": [float("nan")]}, "metric": "eval_loss", "direction": "lower"}
        )
        self.assertFalse(out.ok)
        self.assertEqual(out.code, gr.GATE_ISSUES)

    def test_absent_scores_are_waived(self):
        out = gr.gate_metric_direction({})
        self.assertTrue(out.ok)
        self.assertEqual(out.status, gr.STATUS_WAIVED)

    def test_direction_regression_is_caught(self):
        """Simulate the R12 regression — a selector that always takes max — and assert
        the gate reddens for a lower-is-better task. Patches only the *gate's* view of
        the selector; the platform function is untouched."""
        import safety_auto_research.control_plane.service as svc_mod

        original = svc_mod._select_objective
        svc_mod._select_objective = lambda scores, metric, direction: max(
            next(iter(scores.values()))
        )
        try:
            out = gr.gate_metric_direction(
                {"scores": {"eval_loss": [0.9, 0.4]}, "metric": "eval_loss", "direction": "lower"}
            )
        finally:
            svc_mod._select_objective = original
        self.assertFalse(out.ok)
        self.assertIn("direction inconsistency", out.detail)
        # Sanity: the real selector is intact and still direction-aware.
        self.assertEqual(svc_mod._select_objective({"eval_loss": [0.9, 0.4]}, "eval_loss", "lower"), 0.4)

    def test_fabricated_objective_is_caught(self):
        """A value that is no series' extreme did not come from the measured data."""
        import safety_auto_research.control_plane.service as svc_mod

        original = svc_mod._select_objective
        svc_mod._select_objective = lambda scores, metric, direction: 42.0
        try:
            out = gr.gate_metric_direction(
                {"scores": {"eval.accuracy": [0.8, 0.9]}, "direction": "higher"}
            )
        finally:
            svc_mod._select_objective = original
        self.assertFalse(out.ok)
        self.assertIn("did not come", out.detail)


# ---------------------------------------------------------- claim support gate
class ClaimSupportGateTest(unittest.TestCase):
    def test_supported_claim_passes(self):
        out = gr.gate_claim_support(
            {
                "claims": [
                    {
                        "claim": "accuracy exceeded threshold",
                        "answer": "accuracy exceeded threshold on held-out data",
                        "evidence": "accuracy exceeded threshold: 0.86 vs 0.82",
                    }
                ]
            }
        )
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(out.payload["unsupported"], 0)

    def test_unsupported_claim_fails(self):
        out = gr.gate_claim_support(
            {"claims": [{"claim": "generalises across every unseen distribution", "answer": "", "evidence": ""}]}
        )
        self.assertFalse(out.ok)
        self.assertIn("lack evidentiary support", out.detail)

    def test_min_score_threshold_is_honoured(self):
        claims = [{"claim": "partially covered assertion here", "answer": "partially covered", "evidence": ""}]
        lenient = gr.gate_claim_support({"claims": claims})
        strict = gr.gate_claim_support({"claims": list(claims), "min_claim_score": 0.99})
        self.assertNotEqual(lenient.ok, strict.ok, "threshold must change the verdict")
        self.assertFalse(strict.ok)

    def test_injected_judge_is_used(self):
        """A real deployment injects a separate-model judge; the swap is a one-liner."""
        out = gr.gate_claim_support(
            {"claims": [{"claim": "anything", "answer": "", "evidence": ""}], "judge": lambda a, c, e: 1.0}
        )
        self.assertTrue(out.ok, out.detail)

    def test_throwing_judge_is_a_red_gate(self):
        def boom(answer, claim, evidence):
            raise RuntimeError("judge endpoint down")

        out = gr.gate_claim_support({"claims": [{"claim": "x y z w"}], "judge": boom})
        self.assertFalse(out.ok)
        self.assertIn("judge raised", out.detail)

    def test_empty_claim_text_fails(self):
        out = gr.gate_claim_support({"claims": [{"claim": "   "}]})
        self.assertFalse(out.ok)
        self.assertIn("empty", out.detail)

    def test_absent_claims_are_waived(self):
        out = gr.gate_claim_support({})
        self.assertTrue(out.ok)
        self.assertEqual(out.status, gr.STATUS_WAIVED)


# ------------------------------------------------------------------- svg gate
class SvgGateTest(unittest.TestCase):
    def test_clean_figure_passes(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ok.svg").write_text(_CLEAN_SVG, encoding="utf-8")
            out = gr.gate_svg({"workdir": td, "figures": ["ok.svg"]})
        self.assertTrue(out.ok, out.detail)

    def test_defective_figure_fails_with_codes(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "bad.svg").write_text(_OVERFLOW_SVG, encoding="utf-8")
            out = gr.gate_svg({"workdir": td, "figures": ["bad.svg"]})
        self.assertFalse(out.ok)
        self.assertIn("SVG audit FAILED", out.detail)
        self.assertIn("canvas_overflow", out.payload["reports"][0]["errors"])

    def test_auditor_args_are_passed_through(self):
        """A figure whose type floor legitimately differs must be tunable, not
        un-gateable — proof the extra flags actually reach the auditor."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "small.svg").write_text(_SMALL_TYPE_SVG, encoding="utf-8")
            strict = gr.gate_svg({"workdir": td, "figures": ["small.svg"]})
            relaxed = gr.gate_svg(
                {"workdir": td, "figures": ["small.svg"], "svg_audit_args": ["--min-font-px", "8"]}
            )
        self.assertFalse(strict.ok, "12px type must fail the 20px publication floor")
        self.assertTrue(relaxed.ok, relaxed.detail)

    def test_absolute_figure_path_is_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "abs.svg"
            p.write_text(_CLEAN_SVG, encoding="utf-8")
            out = gr.gate_svg({"figures": [str(p)]})  # no workdir at all
        self.assertTrue(out.ok, out.detail)

    def test_stops_at_the_first_bad_figure(self):
        """Fail-closed within a gate too: the second figure is never audited."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "a_bad.svg").write_text(_OVERFLOW_SVG, encoding="utf-8")
            (Path(td) / "b_ok.svg").write_text(_CLEAN_SVG, encoding="utf-8")
            out = gr.gate_svg({"workdir": td, "figures": ["a_bad.svg", "b_ok.svg"]})
        self.assertFalse(out.ok)
        self.assertEqual([r["figure"] for r in out.payload["reports"]], ["a_bad.svg"])

    def test_missing_figure_is_a_failure_not_a_skip(self):
        with tempfile.TemporaryDirectory() as td:
            out = gr.gate_svg({"workdir": td, "figures": ["nope.svg"]})
        self.assertFalse(out.ok)
        self.assertIn("not found", out.detail)

    def test_absent_figures_are_waived(self):
        out = gr.gate_svg({})
        self.assertTrue(out.ok)
        self.assertEqual(out.status, gr.STATUS_WAIVED)

    def test_missing_machinery_fails_closed(self):
        """Absent gate script == unavailable check. It must never read as a pass."""
        original = gr.SVG_AUDIT
        gr.SVG_AUDIT = Path("/nonexistent/svg_audit.py")
        try:
            out = gr.gate_svg({"figures": ["whatever.svg"]})
        finally:
            gr.SVG_AUDIT = original
        self.assertFalse(out.ok)
        self.assertIn("gate script not found", out.detail)


class SvgGateHardeningTest(unittest.TestCase):
    """Review 2026-09-08 hardening: closed flag whitelist (N2) + subprocess
    timeout (N1). Both must be RED gates, never skips and never passes."""

    def test_json_flag_injection_is_rejected_before_spawn(self):
        """``--json`` after the gate's own ``--json`` would be argparse-last-wins
        and retarget the report write — the exact primitive this blocks."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ok.svg").write_text(_CLEAN_SVG, encoding="utf-8")
            victim = Path(td) / "victim.json"
            out = gr.gate_svg(
                {
                    "workdir": td,
                    "figures": ["ok.svg"],
                    "svg_audit_args": ["--json", str(victim)],
                }
            )
            self.assertFalse(out.ok)
            self.assertEqual(out.status, gr.STATUS_FAILED)
            self.assertIn("svg_audit_args rejected", out.detail)
            self.assertIn("--json", out.detail)
            self.assertFalse(
                victim.exists(), "no subprocess may run with a retargeted report path"
            )

    def test_unknown_dangling_positional_and_flaglike_values_rejected(self):
        base = {"figures": ["whatever.svg"]}
        cases = {
            "selftest bypass": ["--selftest"],
            "unknown flag": ["--evil", "1"],
            "positional": ["extra.svg"],
            "missing value": ["--min-font-px"],
            "flag-like value": ["--min-font-px", "-8"],
        }
        for label, bad in cases.items():
            with self.subTest(case=label):
                out = gr.gate_svg({**base, "svg_audit_args": bad})
                self.assertFalse(out.ok, (label, out.detail))
                self.assertEqual(out.status, gr.STATUS_FAILED, (label, out.detail))
                self.assertIn("svg_audit_args rejected", out.detail, (label, out.detail))

    def test_whitelisted_flags_still_reach_the_auditor(self):
        """Hardening must not break the sanctioned passthrough."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "small.svg").write_text(_SMALL_TYPE_SVG, encoding="utf-8")
            out = gr.gate_svg(
                {
                    "workdir": td,
                    "figures": ["small.svg"],
                    "svg_audit_args": ["--min-font-px", "8"],
                }
            )
        self.assertTrue(out.ok, out.detail)

    def test_timeout_kills_a_stuck_auditor(self):
        """An auditor that never terminates must not wedge the calling thread
        forever — it is killed at the deadline and reported as a red gate."""
        with tempfile.TemporaryDirectory() as td:
            fig = Path(td) / "ok.svg"
            fig.write_text(_CLEAN_SVG, encoding="utf-8")
            stub = Path(td) / "stuck_audit.py"
            stub.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            original = gr.SVG_AUDIT
            gr.SVG_AUDIT = stub
            try:
                out = gr.gate_svg({"figures": [str(fig)], "svg_audit_timeout": 0.3})
            finally:
                gr.SVG_AUDIT = original
        self.assertFalse(out.ok)
        self.assertEqual(out.status, gr.STATUS_FAILED)
        self.assertIn("exceeded", out.detail)
        self.assertIn("killed", out.detail)

    def test_non_positive_timeout_is_rejected_upfront(self):
        for bad in (0, -1, "abc"):
            with self.subTest(timeout=bad):
                out = gr.gate_svg({"figures": ["x.svg"], "svg_audit_timeout": bad})
                self.assertFalse(out.ok)
                self.assertIn("svg_audit_timeout", out.detail)


class MachineryUnavailableTest(unittest.TestCase):
    """Review 2026-09-08 N4①: for every in-process gate, an import that cannot
    happen is a RED gate — never a skip, and never a pass. (The svg gate's
    missing-script branch is covered above; these three poison sys.modules.)"""

    def _fails_closed(self, gate, ctx, poisoned_module):
        import sys
        from unittest import mock

        with mock.patch.dict(sys.modules, {poisoned_module: None}):
            out = gate(ctx)
        self.assertFalse(out.ok, out.detail)
        self.assertEqual(out.status, gr.STATUS_FAILED, out.detail)
        self.assertIn("machinery unavailable", out.detail)

    def test_novelty_machinery_unavailable_fails_closed(self):
        self._fails_closed(
            gr.gate_novelty,
            {"candidates": [_cand("a", {"model": "rf"})]},
            "safety_auto_research.control_plane.evolution",
        )

    def test_metric_direction_machinery_unavailable_fails_closed(self):
        self._fails_closed(
            gr.gate_metric_direction,
            {"scores": {"eval.accuracy": [0.9]}},
            "safety_auto_research.control_plane.service",
        )

    def test_claim_support_machinery_unavailable_fails_closed(self):
        self._fails_closed(
            gr.gate_claim_support,
            {"claims": [{"claim": "x y z", "answer": "a", "evidence": "e"}]},
            "safety_auto_research.execution_plane.capabilities.audit_executor",
        )


# --------------------------------------------------------------- scheduler/CLI
class SchedulerTest(unittest.TestCase):
    def test_stops_at_first_failing_gate(self):
        """Fail-closed short-circuit: nothing after the red gate runs."""
        ctx = {
            "candidates": [_cand("a", {"model": "rf"})],
            "archived_params": [{"model": "rf"}],  # -> novelty collapses
            "scores": {"eval.accuracy": [0.9]},  # would pass, must not run
        }
        code, outcomes = gr.run_stage("all", ctx)
        self.assertEqual(code, gr.GATE_ISSUES)
        self.assertEqual([o.name for o in outcomes], ["novelty"])

    def test_all_stage_waives_everything_on_empty_context(self):
        report = gr.run_gates("all", {})
        self.assertTrue(report["ok"])
        self.assertEqual(report["code"], gr.GATE_OK)
        # "all" runs only the default-loop gates; the opt-in deep gates are excluded.
        self.assertEqual(report["counts"]["waived"], len(gr.STAGE_GATES["all"]))
        self.assertEqual(report["counts"]["failed"], 0)

    def test_report_is_json_serialisable(self):
        report = gr.run_gates("records", {"scores": {"eval.accuracy": [0.9]}, "direction": "higher"})
        json.dumps(report)  # must not raise
        self.assertIn("summary", report)

    def test_cli_usage_error_exit_code(self):
        self.assertEqual(gr.main([]), gr.GATE_USAGE)
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(gr.main([str(Path(td) / "absent.json"), "all"]), gr.GATE_USAGE)

    def test_cli_end_to_end_via_module(self):
        """Workdir-independence: run from a different cwd and resolve figures anyway."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ok.svg").write_text(_CLEAN_SVG, encoding="utf-8")
            ctx_path = Path(td) / "ctx.json"
            ctx_path.write_text(
                json.dumps(
                    {
                        "figures": ["ok.svg"],
                        "scores": {"eval.accuracy": [0.83, 0.86]},
                        "metric": "accuracy",
                        "direction": "higher",
                    }
                ),
                encoding="utf-8",
            )
            repo_root = Path(__file__).resolve().parents[2]
            env = dict(os.environ, PYTHONPATH=str(repo_root))
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "safety_auto_research.integrity_suite.gate_runner",
                    str(ctx_path),
                    "all",
                ],
                capture_output=True,
                text=True,
                cwd=tempfile.gettempdir(),  # deliberately NOT the repo or the workdir
                env=env,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["stage"], "all")


# --------------------------------------------------------------- separation
class NonInvasivenessTest(unittest.TestCase):
    """The suite is additive: the platform must behave identically with it loaded."""

    def test_platform_validators_are_not_wrapped_or_patched(self):
        from safety_auto_research.control_plane.evolution import novelty_filter
        from safety_auto_research.control_plane.service import _select_objective
        from safety_auto_research.execution_plane.capabilities.audit_executor import (
            evaluate_constraint,
        )

        # Still the original functions, defined in their original modules.
        self.assertEqual(novelty_filter.__module__, "safety_auto_research.control_plane.evolution")
        self.assertEqual(_select_objective.__module__, "safety_auto_research.control_plane.service")
        self.assertEqual(
            evaluate_constraint.__module__,
            "safety_auto_research.execution_plane.capabilities.audit_executor",
        )
        # And they still answer exactly as the platform's own suites expect.
        self.assertEqual(_select_objective({"eval_loss": [0.9, 0.4]}, "eval_loss", "lower"), 0.4)
        kept = novelty_filter([_cand("a", {"model": "rf"})], archived_params=[{"model": "rf"}])
        self.assertEqual(kept, [])

    def test_suite_is_importable_without_the_web_stack(self):
        """gate_runner imports platform modules lazily, inside the gates — so the
        module itself loads in a bare stdlib environment (no fastapi/pydantic)."""
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
            "assert m.STAGE_GATES and m.GATES;\n"
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

    def test_no_reserved_outer_stage_is_referenced(self):
        """The gates must not smuggle the outer-loop stages into an inner context —
        the dual loop's isolation invariant. (Grep-level guard on the source.)"""
        src = (_SUITE / "gate_runner.py").read_text(encoding="utf-8")
        # Mentioning them in prose/docstrings is fine; executing them is not.
        code_lines = [
            ln for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        for reserved in ("layer_11_external_audit", "layer_09"):
            occurrences = [
                ln for ln in code_only.splitlines() if reserved in ln and '"""' not in ln
            ]
            for ln in occurrences:
                self.assertTrue(
                    ln.strip().startswith(("*", "#")) or "--" in ln or "``" in ln,
                    f"reserved outer stage {reserved!r} used in executable code: {ln!r}",
                )


if __name__ == "__main__":
    unittest.main()
