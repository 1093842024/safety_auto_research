"""Regression tests for the 2026-08-11 review fix pass (appendix C, R9-R22).

Each test pins the *behaviour that used to be wrong*, so a future refactor that
reintroduces the bug fails here rather than silently corrupting a measurement:

* R9  -- the sandbox launcher fails closed under ``AGENT_SANDBOX_REQUIRE_HARD=1``
        (it used to run unconfined research code on the host), writes an
        attestation the payload cannot reach, and the executor derives the
        isolation label from that attestation instead of the payload's
        self-reported ``result.json`` block.
* R12 -- ``_select_objective`` is direction-aware and shared by the leaderboard and
        the compare view, which used to disagree (compare always took ``max``).
* R15 -- ``ProgressBus`` replays a backlog to late subscribers and terminates
        streams via an EOF sentinel instead of hanging forever.
* R19 -- ``embedding_retrieval_eval`` matches integer doc ids (gold was stringified
        while ranked kept raw JSON types, so recall was silently 0).
* R20 -- ``_score_declared_metric`` reports the declared metric, and marks an
        honest proxy when the metric needs probabilities.
* R21 -- ``ReportMetricRequest`` accepts both the field name and its alias
        (field-name payloads used to be dropped silently).
"""

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "scripts" / "build_agent_sandbox.sh"


# --------------------------------------------------------------------------- #
# R9 -- sandbox fail-closed + unforgeable isolation attestation                #
# --------------------------------------------------------------------------- #
class R9SandboxIsolationTest(unittest.TestCase):
    """The launcher must never degrade silently when confinement is demanded."""

    def _run(self, env_extra: dict, cmd: list[str], marker: str, scratch: str):
        env = os.environ.copy()
        env.update(
            {
                "AGENT_SCRATCH_DIR": scratch,
                "AGENT_SANDBOX_MARKER_PATH": marker,
                # Force the "no hard isolation available" branch deterministically,
                # so the test result does not depend on whether this machine has
                # a running Docker daemon.
                "AGENT_SANDBOX_DISABLE": "1",
            }
        )
        env.update(env_extra)
        return subprocess.run(
            ["/bin/bash", str(_LAUNCHER), "--", *cmd],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_require_hard_refuses_to_run_on_the_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "mode.json")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            canary = os.path.join(tmp, "canary")
            proc = self._run(
                {"AGENT_SANDBOX_REQUIRE_HARD": "1"},
                ["/bin/sh", "-c", f"touch {canary}"],
                marker, scratch,
            )
            # EX_CONFIG, and — the point of the fix — the payload never executed.
            self.assertEqual(proc.returncode, 78, proc.stderr)
            self.assertFalse(
                os.path.exists(canary),
                "payload ran on the host despite AGENT_SANDBOX_REQUIRE_HARD=1",
            )
            with open(marker, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mode"], "unavailable")

    def test_soft_fallback_is_labelled_soft_and_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "mode.json")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            canary = os.path.join(tmp, "canary")
            proc = self._run({}, ["/bin/sh", "-c", f"touch {canary}"], marker, scratch)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.exists(canary))
            with open(marker, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mode"], "soft")

    def test_payload_cannot_locate_its_own_attestation(self):
        """Soft mode runs the payload as the host user; don't hand it the marker path."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "mode.json")
            scratch = os.path.join(tmp, "scratch")
            os.makedirs(scratch)
            proc = self._run(
                {},
                ["/bin/sh", "-c", 'echo "SEEN=[$AGENT_SANDBOX_MARKER_PATH]"'],
                marker, scratch,
            )
            self.assertIn("SEEN=[]", proc.stdout)
            # And the attestation is not sitting inside the writable scratch mount.
            self.assertFalse(os.path.exists(os.path.join(scratch, ".sandbox_mode")))

    def test_missing_or_garbled_marker_is_never_read_as_hard(self):
        from safety_auto_research.execution_plane.capabilities.sandbox_executor import (
            SandboxResearchExecutor,
        )

        self.assertEqual(SandboxResearchExecutor._read_marker("/nonexistent/x.json"), {})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            bad = fh.name
        try:
            self.assertEqual(SandboxResearchExecutor._read_marker(bad), {})
        finally:
            os.unlink(bad)

    def test_hard_isolation_required_precedence(self):
        from safety_auto_research.execution_plane.capabilities import sandbox_executor as sx

        prev = os.environ.get("AGENT_SANDBOX_REQUIRE_HARD")
        try:
            os.environ.pop("AGENT_SANDBOX_REQUIRE_HARD", None)
            self.assertFalse(sx.hard_isolation_required({}))
            self.assertTrue(sx.hard_isolation_required({"require_hard": True}))
            os.environ["AGENT_SANDBOX_REQUIRE_HARD"] = "1"
            self.assertTrue(sx.hard_isolation_required({}))
            # An explicit per-call opt-out still wins over the global default.
            self.assertFalse(sx.hard_isolation_required({"require_hard": False}))
        finally:
            if prev is None:
                os.environ.pop("AGENT_SANDBOX_REQUIRE_HARD", None)
            else:
                os.environ["AGENT_SANDBOX_REQUIRE_HARD"] = prev


# --------------------------------------------------------------------------- #
# R12 -- one direction-aware objective selector for leaderboard + compare      #
# --------------------------------------------------------------------------- #
class R12ObjectiveSelectorTest(unittest.TestCase):
    def setUp(self):
        from safety_auto_research.control_plane.service import _select_objective

        self.sel = _select_objective

    def test_lower_is_better_takes_the_minimum(self):
        scores = {"eval_loss": [0.9, 0.4, 0.7]}
        self.assertAlmostEqual(self.sel(scores, "eval_loss", "lower"), 0.4)
        # The pre-fix compare view reported 0.9 (the worst iteration) here.
        self.assertAlmostEqual(self.sel(scores, "eval_loss", "higher"), 0.9)

    def test_declared_metric_beats_primary_and_accuracy(self):
        scores = {"accuracy": [0.7], "primary": [0.6], "f1_macro": [0.55]}
        self.assertAlmostEqual(self.sel(scores, "f1_macro", "higher"), 0.55)

    def test_accuracy_fallback_is_never_inverted(self):
        # Even for a lower-is-better task, an accuracy-like fallback stays max().
        scores = {"accuracy": [0.61, 0.88]}
        self.assertAlmostEqual(self.sel(scores, "eval_loss", "lower"), 0.88)

    def test_empty_scores_return_none(self):
        self.assertIsNone(self.sel({}, "accuracy", "higher"))


# --------------------------------------------------------------------------- #
# R15 -- SSE backlog replay + EOF termination                                  #
# --------------------------------------------------------------------------- #
class R15ProgressBusTest(unittest.TestCase):
    def _drain(self, bus, run_id, limit=20):
        async def go():
            out = []
            agen = bus.subscribe(run_id)
            try:
                async for chunk in agen:
                    out.append(chunk)
                    if len(out) >= limit:
                        break
            finally:
                await agen.aclose()
            return out

        return asyncio.run(asyncio.wait_for(go(), timeout=5))

    def test_events_emitted_before_subscribe_are_replayed(self):
        from safety_auto_research.control_plane.progress_bus import ProgressBus

        bus = ProgressBus()
        bus.emit("run-1", {"msg": "step-1"})
        bus.emit("run-1", {"msg": "step-2"})
        bus.close("run-1")

        chunks = self._drain(bus, "run-1")
        body = "".join(chunks)
        # Pre-fix: emits with no subscriber were dropped and the stream then hung.
        self.assertIn("step-1", body)
        self.assertIn("step-2", body)
        self.assertIn("event: done", body)

    def test_finished_run_closes_immediately(self):
        from safety_auto_research.control_plane.progress_bus import ProgressBus

        bus = ProgressBus()
        bus.emit("run-2", {"msg": "only"})
        bus.close("run-2")
        chunks = self._drain(bus, "run-2")
        self.assertTrue(chunks[-1].startswith("event: done"), chunks[-1])

    def test_backlog_is_bounded_per_run(self):
        from safety_auto_research.control_plane import progress_bus as pb

        bus = pb.ProgressBus()
        for i in range(pb._BACKLOG_SIZE + 25):
            bus.emit("run-3", {"i": i})
        self.assertEqual(len(bus._backlog["run-3"]), pb._BACKLOG_SIZE)

    def test_backlog_evicts_old_runs(self):
        from safety_auto_research.control_plane import progress_bus as pb

        bus = pb.ProgressBus()
        for r in range(pb._MAX_BACKLOG_RUNS + 5):
            bus.emit(f"run-{r}", {"x": r})
        self.assertLessEqual(len(bus._backlog), pb._MAX_BACKLOG_RUNS)


# --------------------------------------------------------------------------- #
# R19 -- retrieval recall must not depend on the JSON type of the doc id       #
# --------------------------------------------------------------------------- #
class R19RetrievalTypeTest(unittest.TestCase):
    def _eval(self, rows):
        from safety_auto_research.control_plane.eval_runner import embedding_retrieval_eval

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ranked.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
            return embedding_retrieval_eval(path, ks=(1, 5))

    def test_integer_doc_ids_match(self):
        out = self._eval([{"query": "q1", "gold": 42, "ranked": [42, 7, 9]}])
        # Pre-fix: gold "42" vs ranked 42 -> recall 0.0 for every integer dataset.
        self.assertEqual(out["details"]["recall@1"], 1.0)
        self.assertEqual(out["n"], 1)

    def test_string_doc_ids_still_match(self):
        out = self._eval([{"query": "q", "gold": "d7", "ranked": ["d1", "d7"]}])
        self.assertEqual(out["details"]["recall@1"], 0.0)
        self.assertEqual(out["details"]["recall@5"], 1.0)

    def test_genuine_miss_stays_a_miss(self):
        out = self._eval([{"query": "q", "gold": 1, "ranked": [2, 3]}])
        self.assertEqual(out["details"]["recall@5"], 0.0)


# --------------------------------------------------------------------------- #
# R20 -- adapter scores the DECLARED metric, and is honest about proxies       #
# --------------------------------------------------------------------------- #
class R20DeclaredMetricTest(unittest.TestCase):
    def _score(self, metric, direction, preds, truth):
        import numpy as np

        from safety_auto_research.openmle_integration.adapter import OpenMLETaskAdapter
        from safety_auto_research.openmle_integration.adapter import OpenMLETaskConfig

        cfg = OpenMLETaskConfig(eval_metric=metric, direction=direction)
        preds, truth = np.array(preds), np.array(truth)
        acc = float((preds == truth).mean())
        # Unbound call: only ``self.cfg`` is used, so skip interpreter construction.
        return OpenMLETaskAdapter._score_declared_metric(
            SimpleNamespace(cfg=cfg), preds, truth, acc
        )

    def test_f1_is_actually_computed_not_accuracy(self):
        preds = [1, 1, 1, 0]
        truth = [1, 0, 1, 0]
        score, used, proxy, direction = self._score("f1", "higher", preds, truth)
        self.assertEqual(used, "f1")
        self.assertFalse(proxy)
        self.assertEqual(direction, "higher")
        self.assertNotAlmostEqual(score, 0.75)  # 0.75 would be the accuracy

    def test_lower_is_better_proxy_is_error_rate_not_accuracy(self):
        # roc_auc cannot be recomputed from hard labels. Pre-fix this reported
        # accuracy with maximize=False, i.e. "minimize accuracy".
        score, used, proxy, direction = self._score(
            "roc_auc", "lower", [1, 1, 0, 0], [1, 0, 0, 0]
        )
        self.assertEqual(used, "error_rate")
        self.assertTrue(proxy)
        self.assertEqual(direction, "lower")
        self.assertAlmostEqual(score, 0.25)

    def test_error_rate_declared_directly(self):
        score, used, proxy, direction = self._score(
            "error_rate", "lower", [1, 0, 0, 0], [1, 0, 0, 0]
        )
        self.assertEqual((used, proxy, direction), ("error_rate", False, "lower"))
        self.assertAlmostEqual(score, 0.0)


# --------------------------------------------------------------------------- #
# R21 -- alias vs field name must both be accepted                             #
# --------------------------------------------------------------------------- #
class R21RequestAliasTest(unittest.TestCase):
    def test_field_name_and_alias_both_populate(self):
        from safety_auto_research.control_plane.schemas import ReportMetricRequest

        snapshot = {"model": "gbm"}
        by_alias = ReportMetricRequest(
            metric_name="accuracy", score=0.9, config_snapshot=snapshot
        )
        by_field = ReportMetricRequest(metric_name="accuracy", score=0.9, config=snapshot)
        # Pre-fix the field-name form was silently dropped (config stayed empty).
        self.assertEqual(by_alias.config, snapshot)
        self.assertEqual(by_field.config, snapshot)


if __name__ == "__main__":
    unittest.main()
