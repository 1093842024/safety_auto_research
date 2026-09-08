"""Tests for the opt-in integrity-gate endpoints (integration Phase 2).

What matters here is not that the gates work — ``test_integrity_suite_gates.py``
covers that — but that wiring them into the control plane changed *nothing*:

* the suite is **off by default**: the POST endpoint answers ``503`` until
  ``INTEGRITY_GATES`` is set, so an existing deployment behaves identically;
* the route table is **not** environment-dependent (the descriptor endpoint is
  reachable either way, and reports the switch instead of hiding behind it);
* the check is **read-only**: no research record appears, the run's status does
  not move, and no event is emitted;
* only **terminal** runs are gateable (``409`` otherwise) — a live run is a
  moving target;
* a red gate is ``200`` + ``ok: false``, not an HTTP error, so a client that
  only inspects status codes cannot silently "pass" a failing check... which is
  exactly why the response also carries the CLI's ``code`` vocabulary.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from safety_auto_research.control_plane.api import create_app
from safety_auto_research.control_plane.routers.integrity import ENV_SWITCH
from safety_auto_research.control_plane.routers.integrity import IntegrityCheckRequest
from safety_auto_research.control_plane.routers.integrity import build_gate_context
from safety_auto_research.control_plane.routers.integrity import harvest_scores
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.integrity_suite.gate_runner import STAGE_GATES
from safety_auto_research.platform_contracts.enums import RunType
from safety_auto_research.platform_contracts.events import EvalCompletedEvent


class _EnvMixin(unittest.TestCase):
    """Isolated stores + a controlled INTEGRITY_GATES switch."""

    def setUp(self) -> None:
        self._files = []
        for _ in range(3):
            t = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
            t.close()
            os.remove(t.name)
            self._files.append(t.name)
        keys = ("CUSTOM_TASKS_STORE", "CONTROL_PLANE_STORE", "RESEARCH_STATE_DB", ENV_SWITCH)
        self._old = {k: os.environ.get(k) for k in keys}
        os.environ["CUSTOM_TASKS_STORE"] = self._files[0]
        os.environ["CONTROL_PLANE_STORE"] = self._files[1]
        os.environ["RESEARCH_STATE_DB"] = self._files[2]
        os.environ.pop(ENV_SWITCH, None)  # default state: OFF
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

    def _enable(self) -> None:
        os.environ[ENV_SWITCH] = "1"

    def _make_run(self, *, metric: str = "accuracy", direction: str = "higher") -> dict:
        req = CreateWorkflowRunRequest(
            program_id="benchmark",
            run_type=RunType.STANDARD_RESEARCH,
            entry_stage="inner_research",
            target_id="platform.titanic",
            objective_snapshot={
                "benchmark_task_id": "platform.titanic",
                "name": "titanic",
                "eval_metric": metric,
                "direction": direction,
                "config": {"inner_loop": {"model": "gbm"}},
            },
            requested_outcomes=["x"],
        )
        r = self.client.post("/workflow-runs", json=req.model_dump(mode="json"))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def _finish(self, run_id: str) -> None:
        """Drive the run to a terminal status (cancelled) through the public API."""
        self.assertEqual(self.client.post(f"/workflow-runs/{run_id}/start").status_code, 200)
        self.assertEqual(self.client.post(f"/workflow-runs/{run_id}/cancel").status_code, 200)

    def _emit_eval(self, run_id: str, metrics: dict) -> None:
        self.svc.append_event(
            EvalCompletedEvent(
                run_id=run_id,
                eval_suite_id="suite-int",
                passed=True,
                metrics=metrics,
                report_ref="ref://int",
            )
        )


# ------------------------------------------------------------------ off by default
class SwitchTest(_EnvMixin):
    def test_post_is_503_when_disabled(self) -> None:
        run = self._make_run()
        self._finish(run["run_id"])
        r = self.client.post(f"/workflow-runs/{run['run_id']}/integrity-check", json={})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn(ENV_SWITCH, r.json()["detail"])

    def test_descriptor_is_readable_when_disabled(self) -> None:
        """Discoverability: a client learns about the switch instead of guessing."""
        r = self.client.get("/integrity/gates")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["enabled"])
        self.assertEqual(body["env_switch"], ENV_SWITCH)
        self.assertIn("all", body["stages"])
        self.assertIn("svg", body["gates"])
        self.assertTrue(body["svg_audit_available"])

    def test_descriptor_reflects_the_switch(self) -> None:
        self._enable()
        self.assertTrue(self.client.get("/integrity/gates").json()["enabled"])

    def test_switch_is_read_per_request_not_at_import(self) -> None:
        """Flipping the env var must take effect without rebuilding the app."""
        run = self._make_run()
        self._finish(run["run_id"])
        path = f"/workflow-runs/{run['run_id']}/integrity-check"
        self.assertEqual(self.client.post(path, json={}).status_code, 503)
        self._enable()
        self.assertEqual(self.client.post(path, json={}).status_code, 200)
        os.environ.pop(ENV_SWITCH)
        self.assertEqual(self.client.post(path, json={}).status_code, 503)

    def test_truthy_spellings_accepted(self) -> None:
        run = self._make_run()
        self._finish(run["run_id"])
        path = f"/workflow-runs/{run['run_id']}/integrity-check"
        for value in ("1", "true", "TRUE", "on", "yes"):
            os.environ[ENV_SWITCH] = value
            self.assertEqual(self.client.post(path, json={}).status_code, 200, value)
        for value in ("0", "off", "no", ""):
            os.environ[ENV_SWITCH] = value
            self.assertEqual(self.client.post(path, json={}).status_code, 503, value)


# ------------------------------------------------------------------ preconditions
class PreconditionTest(_EnvMixin):
    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_unknown_run_is_404(self) -> None:
        r = self.client.post("/workflow-runs/run-does-not-exist/integrity-check", json={})
        self.assertEqual(r.status_code, 404, r.text)

    def test_live_run_is_409(self) -> None:
        run = self._make_run()
        self.client.post(f"/workflow-runs/{run['run_id']}/start")
        r = self.client.post(f"/workflow-runs/{run['run_id']}/integrity-check", json={})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("terminal", r.json()["detail"])

    def test_requested_run_is_409(self) -> None:
        """Not started is not terminal either."""
        run = self._make_run()
        r = self.client.post(f"/workflow-runs/{run['run_id']}/integrity-check", json={})
        self.assertEqual(r.status_code, 409, r.text)

    def test_unknown_stage_is_400(self) -> None:
        run = self._make_run()
        self._finish(run["run_id"])
        r = self.client.post(
            f"/workflow-runs/{run['run_id']}/integrity-check", json={"stage": "nope"}
        )
        self.assertEqual(r.status_code, 400, r.text)


# ---------------------------------------------------------------------- verdicts
class VerdictTest(_EnvMixin):
    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_healthy_run_passes(self) -> None:
        run = self._make_run()
        rid = run["run_id"]
        self._emit_eval(rid, {"accuracy": 0.83})
        self._emit_eval(rid, {"accuracy": 0.86})
        self._finish(rid)
        body = self.client.post(f"/workflow-runs/{rid}/integrity-check", json={}).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["run_id"], rid)
        self.assertEqual(body["status"], "cancelled")
        gates = {g["gate"]: g for g in body["gates"]}
        self.assertEqual(gates["metric_direction"]["status"], "passed")
        self.assertEqual(gates["metric_direction"]["payload"]["selected"], 0.86)
        # Nothing was supplied for these, so they must waive — not silently pass.
        for gate in ("novelty", "claim_support", "svg"):
            self.assertEqual(gates[gate]["status"], "waived", gates[gate])

    def test_run_without_metrics_waives(self) -> None:
        """No measurements is not the same as bad measurements."""
        run = self._make_run()
        self._finish(run["run_id"])
        body = self.client.post(f"/workflow-runs/{run['run_id']}/integrity-check", json={}).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["counts"]["waived"], 4)

    def test_nan_metric_reddens_the_gate(self) -> None:
        """The harvest deliberately keeps non-finite samples so the gate can catch
        them — ``compare_runs`` filters them out, which would blind this check."""
        run = self._make_run(metric="eval_loss", direction="lower")
        rid = run["run_id"]
        self._emit_eval(rid, {"eval_loss": float("nan")})
        self._finish(rid)
        r = self.client.post(f"/workflow-runs/{rid}/integrity-check", json={})
        self.assertEqual(r.status_code, 200, r.text)  # red gate is data, not an error
        body = r.json()
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["code"], 1)
        self.assertEqual(body["gates"][-1]["gate"], "metric_direction")

    def test_lower_is_better_selects_min(self) -> None:
        run = self._make_run(metric="eval_loss", direction="lower")
        rid = run["run_id"]
        self._emit_eval(rid, {"eval_loss": 0.9})
        self._emit_eval(rid, {"eval_loss": 0.4})
        self._finish(rid)
        body = self.client.post(f"/workflow-runs/{rid}/integrity-check", json={}).json()
        self.assertTrue(body["ok"], body)
        gate = next(g for g in body["gates"] if g["gate"] == "metric_direction")
        self.assertEqual(gate["payload"]["selected"], 0.4)
        self.assertEqual(gate["payload"]["expected_extreme"], "min")

    def test_unsupported_claim_reddens_the_gate(self) -> None:
        run = self._make_run()
        rid = run["run_id"]
        self._emit_eval(rid, {"accuracy": 0.86})
        self._finish(rid)
        body = self.client.post(
            f"/workflow-runs/{rid}/integrity-check",
            json={
                "stage": "audit",
                "claims": [{"claim": "generalises to every unseen distribution", "answer": "", "evidence": ""}],
            },
        ).json()
        self.assertFalse(body["ok"], body)
        self.assertIn("lack evidentiary support", body["summary"])

    def test_single_stage_runs_only_that_gate(self) -> None:
        run = self._make_run()
        rid = run["run_id"]
        self._emit_eval(rid, {"accuracy": 0.86})
        self._finish(rid)
        body = self.client.post(
            f"/workflow-runs/{rid}/integrity-check", json={"stage": "records"}
        ).json()
        self.assertEqual([g["gate"] for g in body["gates"]], ["metric_direction"])

    def test_scores_override_is_honoured(self) -> None:
        run = self._make_run()
        self._finish(run["run_id"])
        body = self.client.post(
            f"/workflow-runs/{run['run_id']}/integrity-check",
            json={"stage": "records", "scores": {"accuracy": [0.5, 0.91]}},
        ).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["gates"][0]["payload"]["selected"], 0.91)

    def test_empty_body_is_accepted(self) -> None:
        """The request model is entirely optional — POST with no body must work,
        and must return a clean all-waived verdict (N4⑤: 200 alone would pass a
        handler that lost the gate report entirely)."""
        run = self._make_run()
        self._finish(run["run_id"])
        r = self.client.post(f"/workflow-runs/{run['run_id']}/integrity-check")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["stage"], "all", body)
        self.assertEqual(body["counts"]["waived"], len(STAGE_GATES["all"]), body)
        self.assertEqual(body["counts"]["failed"], 0, body)
        self.assertEqual(len(body["gates"]), len(STAGE_GATES["all"]), body)


# --------------------------------------------------------------------- read-only
class ReadOnlyTest(_EnvMixin):
    """The whole point of Phase 2: gating must not touch platform state."""

    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_no_state_is_mutated(self) -> None:
        run = self._make_run()
        rid = run["run_id"]
        self._emit_eval(rid, {"accuracy": 0.86})
        self._finish(rid)

        before_status = self.client.get(f"/workflow-runs/{rid}").json()["status"]
        before_events = len(self.client.get(f"/events?run_id={rid}").json())
        before_records = len(self.client.get("/research-records").json())

        for _ in range(3):  # repeat: also proves the check is idempotent
            self.assertEqual(
                self.client.post(f"/workflow-runs/{rid}/integrity-check", json={}).status_code, 200
            )

        self.assertEqual(self.client.get(f"/workflow-runs/{rid}").json()["status"], before_status)
        self.assertEqual(len(self.client.get(f"/events?run_id={rid}").json()), before_events)
        self.assertEqual(len(self.client.get("/research-records").json()), before_records)

    def test_leaderboard_is_untouched(self) -> None:
        run = self._make_run()
        rid = run["run_id"]
        self._emit_eval(rid, {"accuracy": 0.86})
        self._finish(rid)
        before = self.client.get("/research-records/leaderboard").json()
        self.client.post(f"/workflow-runs/{rid}/integrity-check", json={})
        self.assertEqual(self.client.get("/research-records/leaderboard").json(), before)


# ------------------------------------------------------------ adapter unit tests
class HarvestTest(unittest.TestCase):
    """``harvest_scores`` is a pure projection — testable without an app."""

    def test_collects_eval_completed_metrics(self) -> None:
        events = [
            {"event_type": "eval_completed", "metrics": {"accuracy": 0.8}},
            {"event_type": "eval_completed", "metrics": {"accuracy": 0.9, "f1": 0.7}},
        ]
        self.assertEqual(harvest_scores(events), {"accuracy": [0.8, 0.9], "f1": [0.7]})

    def test_ignores_other_event_types(self) -> None:
        events = [{"event_type": "stage_started", "metrics": {"accuracy": 0.99}}]
        self.assertEqual(harvest_scores(events), {})

    def test_keeps_non_finite_samples(self) -> None:
        events = [{"event_type": "eval_completed", "metrics": {"eval_loss": float("nan")}}]
        harvested = harvest_scores(events)
        self.assertEqual(list(harvested), ["eval_loss"])
        self.assertTrue(math.isnan(harvested["eval_loss"][0]))

    def test_skips_non_numeric_and_malformed(self) -> None:
        events = [
            {"event_type": "eval_completed", "metrics": {"note": "n/a", "accuracy": 0.9}},
            {"event_type": "eval_completed", "metrics": "not-a-dict"},
            {"event_type": "eval_completed"},
            "not-a-dict",
        ]
        self.assertEqual(harvest_scores(events), {"accuracy": [0.9]})

    def test_empty_input(self) -> None:
        self.assertEqual(harvest_scores([]), {})


class GateContextTest(unittest.TestCase):
    class _Run:
        run_id = "run-x"
        objective_snapshot = {"eval_metric": "Eval_Loss", "direction": "Lower"}

    def test_metric_and_direction_are_normalised_from_the_snapshot(self) -> None:
        ctx = build_gate_context(self._Run(), [], IntegrityCheckRequest())
        self.assertEqual(ctx["metric"], "eval_loss")
        self.assertEqual(ctx["direction"], "lower")
        self.assertEqual(ctx["run_id"], "run-x")

    def test_defaults_when_snapshot_is_empty(self) -> None:
        class Bare:
            run_id = "run-y"
            objective_snapshot = None

        ctx = build_gate_context(Bare(), [], IntegrityCheckRequest())
        self.assertEqual(ctx["metric"], "accuracy")
        self.assertEqual(ctx["direction"], "higher")

    def test_absent_optionals_stay_absent_so_gates_waive(self) -> None:
        """A key that is present-but-empty would *run* the gate; absence waives it."""
        ctx = build_gate_context(self._Run(), [], IntegrityCheckRequest())
        for key in ("claims", "figures", "min_claim_score", "min_kept_frac", "svg_audit_args"):
            self.assertNotIn(key, ctx)

    def test_caller_additions_are_passed_through(self) -> None:
        req = IntegrityCheckRequest(
            claims=[{"claim": "c"}],
            figures=["a.svg"],
            workdir="/tmp/wd",
            min_claim_score=0.5,
            min_kept_frac=0.75,
            svg_audit_args=["--min-font-px", "8"],
        )
        ctx = build_gate_context(self._Run(), [], req)
        self.assertEqual(ctx["claims"], [{"claim": "c"}])
        self.assertEqual(ctx["figures"], ["a.svg"])
        self.assertEqual(ctx["workdir"], "/tmp/wd")
        self.assertEqual(ctx["min_claim_score"], 0.5)
        self.assertEqual(ctx["min_kept_frac"], 0.75)
        self.assertEqual(ctx["svg_audit_args"], ["--min-font-px", "8"])

    def test_candidates_are_never_synthesised(self) -> None:
        """A finished run's population belongs to the evolution archive; re-filtering
        it here would re-litigate a decision the loop already made."""
        ctx = build_gate_context(self._Run(), [], IntegrityCheckRequest())
        self.assertNotIn("candidates", ctx)


# ------------------------------------------------------- HTTP-level gaps (N4)
# The gate semantics are covered in test_integrity_suite_gates.py; these classes
# close the wiring-level blind spots found by review 2026-09-08 N4: the svg gate
# and its hardening over HTTP, the "deep" stage through the endpoint, and the
# terminal-status boundary beyond the single CANCELLED case.

# Two cards, two labels — audits clean against the auditor's publication defaults
# (kept byte-identical to the fixture in test_integrity_suite_gates.py).
_CLEAN_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200" viewBox="0 0 400 200">
  <rect x="20" y="20" width="220" height="60" fill="#dde" stroke="#334"/>
  <text x="34" y="60" font-family="Times New Roman" font-size="24">Alpha stage</text>
  <rect x="20" y="110" width="220" height="60" fill="#edd" stroke="#433"/>
  <text x="34" y="150" font-family="Times New Roman" font-size="24">Beta stage</text>
</svg>
"""

_OVERFLOW_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120" viewBox="0 0 240 120">
  <text x="180" y="60" font-family="Times New Roman" font-size="24">This label runs far past the right edge of the canvas</text>
</svg>
"""


class HttpSvgGateTest(_EnvMixin):
    """The svg gate end-to-end over HTTP, including the N2 injection rejection —
    precisely the wiring the unit layer cannot see."""

    def setUp(self) -> None:
        super().setUp()
        self._enable()
        self._run = self._make_run()
        self._finish(self._run["run_id"])
        self._path = f"/workflow-runs/{self._run['run_id']}/integrity-check"

    def test_clean_figure_passes_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            Path(td, "fig.svg").write_text(_CLEAN_SVG, encoding="utf-8")
            r = self.client.post(
                self._path, json={"stage": "figures", "figures": ["fig.svg"], "workdir": td}
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["counts"]["passed"], 1, body)
        self.assertEqual(body["gates"][0]["gate"], "svg", body)

    def test_dirty_figure_fails_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            Path(td, "bad.svg").write_text(_OVERFLOW_SVG, encoding="utf-8")
            r = self.client.post(
                self._path, json={"stage": "figures", "figures": ["bad.svg"], "workdir": td}
            )
        self.assertEqual(r.status_code, 200, r.text)  # a red gate is data, not an error
        body = r.json()
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["code"], 1, body)
        self.assertEqual(body["gates"][0]["gate"], "svg", body)
        self.assertIn("canvas_overflow", body["gates"][0]["detail"], body)

    def test_json_flag_injection_rejected_over_http(self) -> None:
        """The N2 primitive must be dead at the endpoint too, and must not have
        written the attacker-chosen report path."""
        with tempfile.TemporaryDirectory() as td:
            Path(td, "ok.svg").write_text(_CLEAN_SVG, encoding="utf-8")
            victim = str(Path(td) / "victim.json")
            r = self.client.post(
                self._path,
                json={
                    "stage": "figures",
                    "figures": ["ok.svg"],
                    "workdir": td,
                    "svg_audit_args": ["--json", victim],
                },
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["ok"], body)
        self.assertIn("svg_audit_args rejected", body["gates"][0]["detail"], body)
        self.assertFalse(Path(victim).exists(), "the retargeted report must never be written")


class DeepStageHttpTest(_EnvMixin):
    """stage="deep" must be reachable through the endpoint, not just unit-level."""

    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_deep_all_waived_over_http(self) -> None:
        run = self._make_run()
        self._finish(run["run_id"])
        r = self.client.post(
            f"/workflow-runs/{run['run_id']}/integrity-check", json={"stage": "deep"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["stage"], "deep", body)
        self.assertEqual(body["counts"]["waived"], len(STAGE_GATES["deep"]), body)
        self.assertEqual(body["counts"]["failed"], 0, body)

    def test_deep_red_claim_short_circuits_over_http(self) -> None:
        """An unsupported claim in the deep battery must come back as a red gate
        whose name is the first failing deep gate (claim_support), proving the
        stop rule survives the HTTP round-trip."""
        run = self._make_run()
        self._finish(run["run_id"])
        r = self.client.post(
            f"/workflow-runs/{run['run_id']}/integrity-check",
            json={
                "stage": "deep",
                "claims": [
                    {
                        "claim": "generalises across every unseen distribution",
                        "answer": "",
                        "evidence": "",
                    }
                ],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["code"], 1, body)
        self.assertEqual(body["gates"][-1]["gate"], "claim_support", body)


class TerminalStatusBoundaryTest(_EnvMixin):
    """Every terminal status is gateable; every non-terminal status is a 409.
    (Before this, only CANCELLED had ever been driven to terminal.)"""

    def setUp(self) -> None:
        super().setUp()
        self._enable()

    def test_every_terminal_status_is_gateable(self) -> None:
        for status in ("succeeded", "failed", "exited_budget", "exited_converged"):
            with self.subTest(status=status):
                run = self._make_run()
                self.client.post(f"/workflow-runs/{run['run_id']}/start")
                self.svc.set_run_status(run["run_id"], status)
                r = self.client.post(
                    f"/workflow-runs/{run['run_id']}/integrity-check", json={}
                )
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["status"], status)

    def test_waiting_approval_is_409(self) -> None:
        """WAITING_APPROVAL is not terminal — an approval-paused run is still live."""
        run = self._make_run()
        self.client.post(f"/workflow-runs/{run['run_id']}/start")
        self.svc.set_run_status(run["run_id"], "waiting_approval")
        r = self.client.post(f"/workflow-runs/{run['run_id']}/integrity-check", json={})
        self.assertEqual(r.status_code, 409, r.text)


if __name__ == "__main__":
    unittest.main()
