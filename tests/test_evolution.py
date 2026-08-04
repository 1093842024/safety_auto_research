"""Tests for the phase-3 parallel evolutionary search.

Covers: population seeding, ShinkaEvolve-style parent selection, bounded mutation,
novelty rejection, the file-backed task manager (crash recovery), the run-scoped
EvolutionArchive, and the end-to-end ``run_evolutionary_loop`` (parallel evaluation,
branch-labeled HypothesisTree, frozen audit of generation champions, audit isolation).
"""

from __future__ import annotations

import os
import random
import tempfile
import unittest

from safety_auto_research.control_plane.evolution import DEFAULT_SURFACE
from safety_auto_research.control_plane.evolution import EvolutionArchive
from safety_auto_research.control_plane.evolution import mutate
from safety_auto_research.control_plane.evolution import novelty_filter
from safety_auto_research.control_plane.evolution import seed_population
from safety_auto_research.control_plane.evolution import select_parent
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store_tree import ResearchStateStore
from safety_auto_research.control_plane.task_manager import BackgroundTaskManager
from safety_auto_research.execution_plane.orchestrator import ClosedLoopOrchestrator

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "kaggle", "titanic")
_BASE = {"preset": "titanic", "model": "rf", "data_dir": _DATA, "threshold": 0.82}


def _make_run(svc: ControlPlaneService):
    run = svc.create_workflow_run(
        CreateWorkflowRunRequest(
            program_id="evo-test",
            run_type="standard_research",
            entry_stage="00_agent_orchestration",
            target_id="titanic",
            objective_snapshot={"goal": "classify titanic", "target_threshold": 0.82},
        )
    )
    svc.start_workflow_run(run.run_id)
    return run


class PopulationOpsTest(unittest.TestCase):
    def test_seed_population_distinct_and_based(self) -> None:
        rng = random.Random(1)
        pop = seed_population(_BASE, surface=DEFAULT_SURFACE, size=4, run_id="r", rng=rng)
        self.assertEqual(len(pop), 4)
        self.assertEqual(pop[0].params, _BASE)  # base config is always candidate 0
        keys = {tuple(sorted(c.params.items())) for c in pop}
        self.assertEqual(len(keys), 4)  # all distinct
        self.assertTrue(all(c.branch == "gen0" for c in pop))

    def test_select_parent_favors_fitness_penalizes_offspring(self) -> None:
        rng = random.Random(3)
        pop = seed_population(_BASE, surface=DEFAULT_SURFACE, size=3, run_id="r", rng=rng)
        pop[0].fitness, pop[1].fitness, pop[2].fitness = 0.9, 0.5, 0.1
        picks = [select_parent(pop, rng=rng).candidate_id for _ in range(300)]
        self.assertGreater(picks.count(pop[0].candidate_id), picks.count(pop[2].candidate_id))
        # after the top candidate produces many offspring it loses its edge
        # Reset accumulated offspring counts from round 1, then impose a heavy penalty
        for c in pop:
            c.offspring_count = 0
        pop[0].offspring_count = 50
        picks2 = [select_parent(pop, rng=rng).candidate_id for _ in range(300)]
        self.assertLess(picks2.count(pop[0].candidate_id), picks.count(pop[0].candidate_id))

    def test_mutate_stays_on_surface(self) -> None:
        rng = random.Random(5)
        parent = seed_population(_BASE, surface=DEFAULT_SURFACE, size=1, run_id="r", rng=rng)[0]
        child = mutate(parent, surface=DEFAULT_SURFACE, run_id="r", generation=1, rng=rng)
        self.assertEqual(child.parent_id, parent.candidate_id)
        self.assertEqual(child.branch, "gen1")
        for key, val in child.params.items():
            if key in DEFAULT_SURFACE:
                self.assertIn(val, DEFAULT_SURFACE[key])

    def test_novelty_filter_rejects_duplicates(self) -> None:
        rng = random.Random(7)
        pop = seed_population(_BASE, surface=DEFAULT_SURFACE, size=2, run_id="r", rng=rng)
        dup = mutate(pop[0], surface=DEFAULT_SURFACE, run_id="r", generation=1, rng=rng)
        dup.params = dict(pop[0].params)  # force an exact duplicate
        kept = novelty_filter([dup, pop[1]], archived_params=[_BASE], threshold=0.92)
        self.assertEqual(dup.status, "rejected_novelty")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].candidate_id, pop[1].candidate_id)


class TaskManagerTest(unittest.TestCase):
    def test_run_all_persists_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tm = BackgroundTaskManager(tmp)
            calls = {"n": 0}

            def work():
                calls["n"] += 1
                return {"value": 42}

            out = tm.run_all({"t1": work}, max_workers=1)
            self.assertEqual(out["t1"]["result"]["value"], 42)
            self.assertTrue(tm.is_done("t1"))
            # crash-recovery: a second manager over the same dir does NOT re-run
            tm2 = BackgroundTaskManager(tmp)
            out2 = tm2.run_all({"t1": work}, max_workers=1)
            self.assertEqual(out2["t1"]["result"]["value"], 42)
            self.assertEqual(calls["n"], 1)

    def test_error_task_does_not_kill_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tm = BackgroundTaskManager(tmp)

            def boom():
                raise RuntimeError("kaboom")

            out = tm.run_all({"bad": boom, "good": lambda: 1}, max_workers=2)
            self.assertEqual(out["bad"]["status"], "error")
            self.assertIn("kaboom", out["bad"]["error"])
            self.assertEqual(out["good"]["status"], "ok")


class EvolutionArchiveTest(unittest.TestCase):
    def test_run_scoped_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.db")
            arch = EvolutionArchive(db)
            rng = random.Random(2)
            c = seed_population(_BASE, surface=DEFAULT_SURFACE, size=1, run_id="r1", rng=rng)[0]
            arch.add(c)
            arch.update(c.candidate_id, fitness=0.83, status="evaluated")
            reloaded = EvolutionArchive(db)
            got = reloaded.list("r1")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].fitness, 0.83)
            self.assertEqual(reloaded.best("r1").candidate_id, c.candidate_id)
            self.assertEqual(reloaded.list("other"), [])


class EvolutionLoopTest(unittest.TestCase):
    def _run(self, judge=None, **kw):
        svc = ControlPlaneService()
        store = ResearchStateStore()
        orch = ClosedLoopOrchestrator(svc, mode="scripted", state_store=store)
        run = _make_run(svc)
        audit_params = {"threshold": 0.8}
        if judge is not None:
            audit_params["judge"] = judge
        args = dict(
            inner_params=dict(_BASE),
            audit_params=audit_params,
            population_size=3,
            generations=2,
            max_workers=3,
            seed=7,
        )
        args.update(kw)
        summary = orch.run_evolutionary_loop(run.run_id, **args)
        return svc, store, run, summary

    def test_end_to_end_population_audit_and_branches(self) -> None:
        svc, store, run, summary = self._run()
        self.assertIn(summary["status"], ("exited_converged", "exited_budget"))
        self.assertIsNotNone(summary["best_candidate"])
        # population archived with fitness + statuses
        cands = store.evolution.list(run.run_id)
        self.assertGreaterEqual(len(cands), 3)
        self.assertTrue(any(c.fitness is not None for c in cands))
        self.assertTrue(any(c.status == "champion" for c in cands))
        # hypothesis tree carries real branch structure
        branches = {n.branch for n in store.hypo_tree.list_nodes(run_id=run.run_id)}
        self.assertIn("gen0", branches)
        # generation champions were audited by the frozen external audit
        self.assertIn("audit_completed", [e["event_type"] for e in svc.list_events(run.run_id)])

    def test_audit_isolation_no_population_narrative(self) -> None:
        seen: list[str] = []

        def spy_judge(answer: str, claim: str, evidence: str) -> float:
            seen.append(" ".join([answer, claim, evidence]))
            return 0.9

        self._run(judge=spy_judge)
        self.assertTrue(seen)
        blob = " ".join(seen)
        self.assertNotIn("cand-", blob)  # no candidate identity leaks into the audit
        self.assertNotIn("params=", blob)

    def test_budget_hard_stop(self) -> None:
        _svc, _store, _run, summary = self._run(budget={"max_capability_calls": 1})
        self.assertEqual(summary["status"], "exited_budget")
        self.assertIn("预算", summary["status_detail"])


if __name__ == "__main__":
    unittest.main()
