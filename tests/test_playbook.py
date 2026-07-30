"""Tests for the phase-1 harness-engineering upgrades.

Covers the four mechanisms added after the Lilian Weng harness-engineering gap
analysis:

  1. ACE-style PlaybookStore — itemized bullets, deterministic dedup-merge, counters;
  2. the rule-based reflector + failure-mode miner (weakness mining / negative results);
  3. StrategyArchive lifecycle — pending_verification → verified / rolled_back;
  4. dual-loop integration — playbook is injected into the inner loop, the meta-loop
     param patch is actually applied and post-hoc verified, and the outer audit never
     sees playbook content (context-separation invariant regression).
"""

from __future__ import annotations

import os
import tempfile
import unittest

from safety_auto_research.control_plane.failure_miner import mine_failure_modes
from safety_auto_research.control_plane.playbook import PlaybookStore
from safety_auto_research.control_plane.playbook import reflect_round
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store_tree import ResearchStateStore
from safety_auto_research.control_plane.store_tree import StrategyArchive
from safety_auto_research.execution_plane.orchestrator import ClosedLoopOrchestrator

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "kaggle", "titanic")


def _make_run(svc: ControlPlaneService, target: str = "titanic", threshold: float = 0.82):
    run = svc.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="pb-test",
            run_type="standard_research",
            entry_stage="00_agent_orchestration",
            target_id=target,
            objective_snapshot={"goal": f"classify {target}", "target_threshold": threshold},
        )
    )
    svc.start_workflow_run(run.run_id)
    return run


class PlaybookStoreTest(unittest.TestCase):
    def test_merge_dedups_and_counts(self) -> None:
        store = PlaybookStore()
        cands = [{"section": "strategy", "content": "gbm → acc=0.80，外审计通过", "helpful": 1}]
        store.merge(cands, scope="titanic", run_id="r1", iter_no=0)
        # Same bullet again (with messy casing/spacing) -> counters, not a new entry.
        store.merge(
            [{"section": "strategy", "content": "GBM  →   acc=0.80，外审计通过", "helpful": 1}],
            scope="titanic", run_id="r2", iter_no=1,
        )
        entries = store.list_entries("titanic")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].helpful, 2)
        self.assertEqual(entries[0].iter_no, 1)

    def test_render_orders_by_score_and_tags_feedback(self) -> None:
        store = PlaybookStore()
        store.merge(
            [
                {"section": "strategy", "content": "good approach", "helpful": 2},
                {"section": "failure", "content": "bad approach", "harmful": 3},
            ],
            scope="t",
        )
        text = store.render("t", k=5)
        self.assertIn("good approach", text)
        self.assertIn("bad approach", text)
        self.assertLess(text.index("good approach"), text.index("bad approach"))
        entry = store.list_entries("t")[0]
        store.tag(entry.entry_id, helpful=False)
        self.assertEqual(store.list_entries("t")[0].harmful, 1)

    def test_sqlite_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.db")
            store = PlaybookStore(db)
            store.merge(
                [{"section": "fact", "content": "待解决约束：X", "helpful": 1}], scope="s"
            )
            reloaded = PlaybookStore(db)
            entries = reloaded.list_entries("s")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].content, "待解决约束：X")
            self.assertEqual(entries[0].helpful, 1)


class ReflectorAndMinerTest(unittest.TestCase):
    def test_reflect_round_sections(self) -> None:
        cands = reflect_round(
            capability="kaggle_eval",
            inner_params={"model": "rf", "fe": "basic"},
            metrics={"accuracy": 0.79},
            decision="revisit",
            unresolved=["claim X unsupported"],
            rejected=["rf + basic features"],
            failure_modes=[{"mode": "eval_below_gate", "count": 3}],
        )
        by_section = {}
        for c in cands:
            by_section.setdefault(c["section"], []).append(c["content"])
        self.assertIn("strategy", by_section)
        self.assertIn("被要求改进", by_section["strategy"][0])
        # rejected candidate + recurring failure mode -> failure bullets
        self.assertTrue(any("勿重复" in c for c in by_section["failure"]))
        self.assertTrue(any("反复出现" in c for c in by_section["failure"]))
        self.assertIn("fact", by_section)

    def test_accept_marks_helpful(self) -> None:
        cands = reflect_round(
            capability="kaggle_eval",
            inner_params={"model": "gbm"},
            metrics={"accuracy": 0.85},
            decision="exit_success",
        )
        strat = [c for c in cands if c["section"] == "strategy"]
        self.assertEqual(strat[0]["helpful"], 1)
        self.assertEqual(strat[0]["harmful"], 0)

    def test_failure_miner_clusters_and_counts(self) -> None:
        events = [
            {"event_type": "eval_completed", "gate_passed": False,
             "metrics": {"primary": 0.79, "accuracy": 0.79}, "report_ref": "kaggle://a"},
            {"event_type": "eval_completed", "gate_passed": False,
             "metrics": {"primary": 0.80, "accuracy": 0.80}, "report_ref": "kaggle://b"},
            {"event_type": "audit_completed", "unresolved_claims": ["claim X unsupported"],
             "audit_id": "aud-1"},
            {"event_type": "audit_completed", "unresolved_claims": ["claim X unsupported"],
             "audit_id": "aud-2"},
            {"event_type": "improvement_applied", "reverted": True, "rollback_id": "rb-1"},
        ]
        modes = mine_failure_modes(events)
        by_mode = {m["mode"].split(":")[0]: m for m in modes}
        # two below-gate evals cluster into ONE mode (numbers normalized away)
        self.assertEqual(by_mode["eval_below_gate"]["count"], 2)
        self.assertEqual(len(by_mode["eval_below_gate"]["evidence_refs"]), 2)
        self.assertEqual(by_mode["audit_unresolved"]["count"], 2)
        self.assertEqual(by_mode["self_evolution_reverted"]["count"], 1)


class StrategyArchiveLifecycleTest(unittest.TestCase):
    def test_pending_verify_rollback(self) -> None:
        arch = StrategyArchive()
        rid = arch.commit(
            {"run_id": "r1", "accepted": True, "inner_param_patch": {"fe": "rich"},
             "prediction": {"baseline": 0.8}, "status": "pending_verification"}
        )
        self.assertEqual(len(arch.pending(run_id="r1")), 1)
        self.assertEqual(arch.pending(run_id="other"), [])
        arch.update(rid, status="verified", actual_accuracy=0.81)
        self.assertEqual(arch.pending(run_id="r1"), [])
        self.assertEqual(arch.get(rid)["status"], "verified")

        rid2 = arch.commit({"run_id": "r1", "status": "pending_verification"})
        arch.rollback(rid2)
        self.assertEqual(arch.get(rid2)["status"], "rolled_back")
        self.assertEqual(arch.pending(run_id="r1"), [])
        # run_id filter
        self.assertEqual(len(arch.list_all(run_id="r1")), 2)
        self.assertEqual(arch.list_all(run_id="nope"), [])


class DualLoopPlaybookIntegrationTest(unittest.TestCase):
    def _run_refine_loop(self, store: ResearchStateStore, judge=None):
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted", state_store=store)
        run = _make_run(svc)
        audit_params = {"threshold": 0.8}
        if judge is not None:
            audit_params["judge"] = judge
        summary = orch.run_dual_loop(
            run.run_id,
            inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "model": "rf", "data_dir": _DATA, "threshold": 0.82},
            audit_params=audit_params,
            max_outer_iters=2,
        )
        return svc, run, summary

    def test_playbook_populated_and_injected(self) -> None:
        store = ResearchStateStore()
        _svc, _run, summary = self._run_refine_loop(store)
        entries = store.playbook.list_entries("titanic")
        # at least one strategy bullet from the reflected rounds
        self.assertTrue(any(e.section == "strategy" for e in entries))
        self.assertIn("revisit", summary["decisions"])

    def test_meta_loop_patch_applied_and_verified(self) -> None:
        store = ResearchStateStore()
        self._run_refine_loop(store)
        strategies = store.strategy_archive.list_all()
        patched = [s for s in strategies if s.get("inner_param_patch")]
        self.assertTrue(patched, "meta-loop should propose a concrete param patch")
        # rf + basic -> policy proposes fe=rich first
        self.assertEqual(patched[0]["inner_param_patch"], {"fe": "rich"})
        # the patch was applied to the next inner loop and post-hoc verified:
        # no entry may remain stuck in pending_verification
        self.assertEqual(store.strategy_archive.pending(), [])
        final = patched[0]
        self.assertIn(final["status"], ("verified", "rolled_back"))
        self.assertIn("actual_accuracy", final)
        self.assertIn("actual_delta", final)

    def test_audit_never_sees_playbook(self) -> None:
        """Isolation invariant regression: playbook content is inner-loop-only."""
        seen: list[str] = []

        def spy_judge(answer: str, claim: str, evidence: str) -> float:
            seen.append(" ".join([answer, claim, evidence]))
            return 0.9

        store = ResearchStateStore()
        self._run_refine_loop(store, judge=spy_judge)
        # the playbook really was populated (otherwise the assertion below is vacuous)
        self.assertTrue(store.playbook.list_entries("titanic"))
        blob = " ".join(seen)
        self.assertNotIn("PLAYBOOK", blob)
        self.assertNotIn("playbook_context", blob)
        self.assertNotIn("pb-", blob)  # no playbook entry id leaks into the audit


if __name__ == "__main__":
    unittest.main()
