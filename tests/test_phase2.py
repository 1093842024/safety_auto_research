"""Tests for the phase-2 harness-engineering upgrades (evaluator hardening +
experience lifecycle):

  A. kaggle_eval held-out split + audit ``heldout_consistency`` constraint;
  B. real LLM judge client (HTTP path, evidence refs, deterministic fallback);
  C. ExperienceBank dedup-merge / decay + dual-loop auto write & auto injection;
  D. multi-dimensional budget (time / capability calls / cost) hard stop.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from safety_auto_research.control_plane.eval_runner import llm_judge_eval
from safety_auto_research.control_plane.llm_judge import LLMJudgeError
from safety_auto_research.control_plane.llm_judge import call_llm_judge
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store_tree import ExperienceBank
from safety_auto_research.control_plane.store_tree import ResearchStateStore
from safety_auto_research.execution_plane.capabilities.audit_executor import AuditExecutor
from safety_auto_research.execution_plane.orchestrator import ClosedLoopOrchestrator
from safety_auto_research.platform_contracts.enums import GateResult
from safety_auto_research.platform_contracts.enums import StageStatus
from safety_auto_research.platform_contracts.objects import StageRun

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "kaggle", "titanic")


def _make_run(svc: ControlPlaneService, target: str = "titanic", threshold: float = 0.82):
    run = svc.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="p2-test",
            run_type="standard_research",
            entry_stage="00_agent_orchestration",
            target_id=target,
            objective_snapshot={"goal": f"classify {target}", "target_threshold": threshold},
        )
    )
    svc.start_workflow_run(run.run_id)
    return run


def _run_dual(svc, orch, run, **kw):
    params = {"preset": "titanic", "model": "rf", "data_dir": _DATA, "threshold": 0.82}
    params.update(kw.pop("inner_overrides", {}))
    return orch.run_dual_loop(
        run.run_id,
        inner_capability="kaggle_eval",
        inner_params=params,
        audit_params={"threshold": 0.8},
        max_outer_iters=kw.pop("max_outer_iters", 2),
        **kw,
    )


# --------------------------------------------------------------------------- #
# A. held-out                                                                 #
# --------------------------------------------------------------------------- #
class HeldOutEvalTest(unittest.TestCase):
    def test_kaggle_eval_emits_heldout_metrics(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        _stage, res = orch.run_capability(
            run.run_id, "kaggle_eval",
            {"preset": "titanic", "model": "gbm", "data_dir": _DATA, "threshold": 0.82},
        )
        m = res.event.metrics
        self.assertIn("heldout_accuracy", m)
        self.assertIn("generalization_gap", m)
        self.assertAlmostEqual(
            m["generalization_gap"], round(m["accuracy"] - m["heldout_accuracy"], 4), places=4
        )

    def test_audit_flags_large_generalization_gap(self) -> None:
        stage_run = StageRun(
            stage_run_id="sr-ho", run_id="r-ho", stage_code="layer_11_external_audit",
            executor_family="capability", gate_result=GateResult.FAILED,
            status=StageStatus.QUEUED,
        )
        sdk = SimpleNamespace(
            load_object=lambda ref: SimpleNamespace(objective_snapshot={}, target_id="t"),
            emit_event=lambda e: e,
            publish_artifact=lambda **_kw: SimpleNamespace(artifact_id="a"),
            record_metric=lambda *a, **k: None,
        )
        curated_base = {
            "objective": "classify",
            "result_report_ref": "kaggle://real",
            "result_gate_passed": True,
            "result_real_eval": True,
            "prior_audits": [],
            "constraints": [],
        }
        good = AuditExecutor().execute(
            stage_run, sdk,
            {"objective": "classify", "threshold": 0.8,
             "audit_input": {**curated_base,
                             "result_metrics": {"accuracy": 0.85, "heldout_accuracy": 0.84}}},
        )
        bad = AuditExecutor().execute(
            stage_run, sdk,
            {"objective": "classify", "threshold": 0.8,
             "audit_input": {**curated_base,
                             "result_metrics": {"accuracy": 0.85, "heldout_accuracy": 0.70}}},
        )
        ho_good = [c for c in good.event.constraints if c["id"] == "heldout_consistency"]
        ho_bad = [c for c in bad.event.constraints if c["id"] == "heldout_consistency"]
        self.assertEqual(ho_good[0]["status"], "verified")
        self.assertEqual(ho_bad[0]["status"], "conflict")
        self.assertLess(bad.event.confidence, good.event.confidence)


# --------------------------------------------------------------------------- #
# B. real LLM judge                                                           #
# --------------------------------------------------------------------------- #
def _fake_urlopen(payload: dict | None = None, fail: bool = False):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        if fail:
            raise TimeoutError("judge service down")
        return _Resp(json.dumps(payload or {"score": 0.87, "rationale": "ok",
                                            "evidence_refs": ["ev-1"]}).encode())

    return _open


class LLMJudgeTest(unittest.TestCase):
    def test_call_llm_judge_parses_and_clamps(self) -> None:
        with mock.patch("urllib.request.urlopen", _fake_urlopen(
            {"score": 1.7, "rationale": "over", "evidence_refs": ["ev-1"]}
        )):
            out = call_llm_judge(prompt="p", reference="r", prediction="x", url="http://judge")
        self.assertEqual(out["score"], 1.0)
        self.assertEqual(out["evidence_refs"], ["ev-1"])

    def test_call_llm_judge_transport_error(self) -> None:
        with mock.patch("urllib.request.urlopen", _fake_urlopen(fail=True)):
            with self.assertRaises(LLMJudgeError):
                call_llm_judge(prompt="p", reference="r", prediction="x", url="http://judge")

    def _write_pair(self, tmp: str) -> tuple[str, str]:
        ds = os.path.join(tmp, "ds.jsonl")
        pr = os.path.join(tmp, "pr.jsonl")
        with open(ds, "w") as fh:
            fh.write(json.dumps({"prompt": "q1", "reference": "hello world"}) + "\n")
        with open(pr, "w") as fh:
            fh.write(json.dumps({"prompt": "q1", "output": "hello world"}) + "\n")
        return ds, pr

    def test_eval_runner_uses_real_judge(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ds, pr = self._write_pair(tmp)
            with mock.patch("urllib.request.urlopen", _fake_urlopen({"score": 0.91})):
                out = llm_judge_eval(ds, pr, judge_url="http://judge")
        self.assertEqual(out["details"]["judge"], "llm")
        self.assertAlmostEqual(out["score"], 0.91, places=4)

    def test_eval_runner_falls_back_on_judge_outage(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ds, pr = self._write_pair(tmp)
            with mock.patch("urllib.request.urlopen", _fake_urlopen(fail=True)):
                out = llm_judge_eval(ds, pr, judge_url="http://judge")
        self.assertEqual(out["details"]["judge"], "heuristic-token-f1")
        self.assertIn("fallback_reason", out["details"])
        self.assertGreater(out["score"], 0.9)  # identical strings -> F1 ~ 1.0


# --------------------------------------------------------------------------- #
# C. experience lifecycle                                                     #
# --------------------------------------------------------------------------- #
class ExperienceLifecycleTest(unittest.TestCase):
    def test_dedup_merge_bumps_confidence(self) -> None:
        bank = ExperienceBank()
        bank.add("failure", "titanic", "避免：rf+basic", ["kaggle_eval"], 0.6)
        e2 = bank.add("failure", "titanic", "避免：RF+Basic", ["kaggle_eval"], 0.6)
        self.assertEqual(len(bank.list_all()), 1)
        self.assertEqual(e2.uses, 1)
        self.assertAlmostEqual(e2.confidence, 0.65, places=4)

    def test_query_decays_unreinforced_entries(self) -> None:
        bank = ExperienceBank()
        old = bank.add("failure", "t", "old lesson", ["kaggle_eval"], 0.7)
        # push many newer entries so `old` decays
        for i in range(20):
            bank.add("failure", "t", f"lesson {i}", ["kaggle_eval"], 0.65)
        top = bank.query(stage="kaggle_eval", k=1)[0]
        self.assertNotEqual(top.entry_id, old.entry_id)
        # reinforcing the old lesson refreshes its recency
        bank.add("failure", "t", "old lesson", ["kaggle_eval"], 0.7)
        top2 = bank.query(stage="kaggle_eval", k=1)[0]
        self.assertEqual(top2.entry_id, old.entry_id)

    def test_dual_loop_auto_writes_and_injects_experience(self) -> None:
        store = ResearchStateStore()
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted", state_store=store)
        run = _make_run(svc)
        _run_dual(svc, orch, run)
        entries = store.experience_bank.list_all()
        # rf below gate -> refine path -> failure lessons distilled automatically
        self.assertTrue(entries, "dual loop should auto-distill experiences")
        self.assertTrue(any(e.kind == "failure" for e in entries))
        self.assertTrue(any("kaggle_eval" in e.applicable_stages for e in entries))


# --------------------------------------------------------------------------- #
# D. budget                                                                   #
# --------------------------------------------------------------------------- #
class BudgetTest(unittest.TestCase):
    def test_capability_call_budget_hard_stops(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        summary = _run_dual(svc, orch, run, budget={"max_capability_calls": 2})
        self.assertEqual(summary["status"], "exited_budget")
        self.assertIn("能力调用预算", summary["status_detail"])
        # 2 calls = inner + audit of iteration 0; loop stops before iteration 1
        inner_steps = [s for s in summary["steps"] if s["stage"].startswith("inner")]
        self.assertEqual(len(inner_steps), 1)

    def test_time_budget_hard_stops(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        summary = _run_dual(svc, orch, run, budget={"max_seconds": 0})
        self.assertEqual(summary["status"], "exited_budget")
        self.assertIn("时间预算", summary["status_detail"])
        self.assertEqual(summary["steps"], [])  # stopped before any inner loop

    def test_cost_budget_hard_stops(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        summary = _run_dual(
            svc, orch, run, budget={"max_cost": 2.0, "cost_per_call": 1.0}
        )
        self.assertEqual(summary["status"], "exited_budget")
        self.assertIn("成本预算", summary["status_detail"])

    def test_no_budget_is_unlimited(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = _make_run(svc)
        summary = _run_dual(svc, orch, run)
        self.assertNotEqual(summary["status_detail"] or "", "预算")


if __name__ == "__main__":
    unittest.main()
