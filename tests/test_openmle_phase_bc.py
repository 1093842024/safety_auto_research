"""Tests for Phase B (atomic operators as inner-loop capabilities) and Phase C
(program-level island-model evolution integrated into run_evolutionary_loop).

These run the OpenMLE Task adapter directly (sequential, single-process) so they are
safe on both the managed 3.13 venv and miniforge 3.10 (no thread pool -> no OpenMP
segfault). The orchestrator end-to-end test uses ``audit=False`` to validate program
node writing + island search deterministically; the audit branch reuses the identical
``run_capability("layer_11_external_audit", ...)`` path proven in test_evolution.py.
"""

from __future__ import annotations

import os
import random
import unittest


def _find_titanic() -> str:
    here = os.path.abspath(__file__)
    cur = here
    for _ in range(8):
        cand = os.path.join(os.path.dirname(cur), "data", "kaggle", "titanic")
        if os.path.isdir(cand):
            return cand
        cur = os.path.dirname(cur)
    raise FileNotFoundError("titanic dataset not found")


_DATA = _find_titanic()

from safety_auto_research.openmle_integration.adapter import (  # noqa: E402
    OpenMLETaskAdapter,
    OpenMLETaskConfig,
)
from safety_auto_research.openmle_integration.contracts import (  # noqa: E402
    TEST_FITNESS,
    VALID_SOLUTION,
)
from safety_auto_research.openmle_integration.operators import (  # noqa: E402
    LLMOperatorBackend,
    TemplateOperatorBackend,
    build_debug_feedback,
    crossover_program,
    debug_program,
    draft_program,
    improve_program,
    run_operator,
    select_crossover_parents,
)
from safety_auto_research.openmle_integration.inner_capability import (  # noqa: E402
    OperatorAuditViolation,
    assert_operator_inner_only,
)
from safety_auto_research.control_plane.evolution import (  # noqa: E402
    Candidate,
    EvolutionArchive,
    IslandModel,
    crossover_programs,
    mutate_program,
    novelty_filter,
    seed_program_population,
)
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest  # noqa: E402
from safety_auto_research.control_plane.service import ControlPlaneService  # noqa: E402
from safety_auto_research.control_plane.store_tree import ResearchStateStore  # noqa: E402
from safety_auto_research.execution_plane.orchestrator import ClosedLoopOrchestrator  # noqa: E402

BACKEND = TemplateOperatorBackend()
TASK_CFG = OpenMLETaskConfig(
    name="titanic", data_dir=_DATA, target="Survived", id_col="PassengerId",
)


def _prepared():
    adapter = OpenMLETaskAdapter(TASK_CFG)
    state, info = adapter.prepare()
    return adapter, state, info


class OperatorTests(unittest.TestCase):
    def test_operators_produce_runnable_programs(self) -> None:
        adapter, state, info = _prepared()
        target, id_col = info["target"], info["id_col"]
        for op in ("draft", "improve", "debug"):
            code = run_operator(
                op, BACKEND, task_description=info["TASK_DESCRIPTION"],
                target=target, id_col=id_col, caller_stage="inner_program_evolution",
            )
            _s, outcome = adapter.step_task(state, code)
            self.assertTrue(outcome[VALID_SOLUTION], f"{op} should produce a valid submission")
            self.assertGreater(outcome[TEST_FITNESS], 0.5, f"{op} accuracy too low")
        # crossover needs two parents
        a = draft_program(BACKEND, target=target, id_col=id_col,
                          task_description=info["TASK_DESCRIPTION"], caller_stage="inner")
        b = improve_program(BACKEND, target=target, id_col=id_col,
                            task_description=info["TASK_DESCRIPTION"], current_program=a,
                            caller_stage="inner")
        cx = crossover_program(BACKEND, target=target, id_col=id_col,
                               task_description=info["TASK_DESCRIPTION"],
                               parent_programs=(a, b), caller_stage="inner")
        _s, outcome = adapter.step_task(state, cx)
        self.assertTrue(outcome[VALID_SOLUTION], "crossover should produce a valid submission")
        adapter.close(state)

    def test_operator_inner_only_guard(self) -> None:
        # allowed inner-loop caller
        assert_operator_inner_only("draft", "inner_program_evolution")
        # forbidden outer-loop callers
        for stage in ("layer_11_external_audit", "layer_09_self_iterative_evolution"):
            with self.assertRaises(OperatorAuditViolation):
                assert_operator_inner_only("draft", stage)
            with self.assertRaises(OperatorAuditViolation):
                run_operator("draft", BACKEND, task_description="x", target="Survived",
                             id_col="PassengerId", caller_stage=stage)

    def test_llm_backend_adapter(self) -> None:
        # the LLM backend is a drop-in over any callable; operators need no change
        seen = {}

        def fake_llm(prompt: str) -> str:
            seen["prompt"] = prompt
            return "print('llm-generated')"

        llm = LLMOperatorBackend(fake_llm)
        code = draft_program(llm, target="Survived", id_col="PassengerId",
                             task_description="classify", caller_stage="inner")
        self.assertIn("operator=draft", seen["prompt"])
        self.assertEqual(code, "print('llm-generated')")


class DebugSeamTest(unittest.TestCase):
    def test_debug_repairs_buggy_program(self) -> None:
        adapter, state, info = _prepared()
        target, id_col = info["target"], info["id_col"]
        buggy = "import os\nraise ValueError('deliberate bug')\n"
        _s, bad = adapter.step_task(state, buggy)
        self.assertFalse(bad[VALID_SOLUTION])
        # Debug consumes the failure feedback (failure_miner seam)
        feedback = build_debug_feedback(bad, events=None)
        self.assertTrue(feedback)
        fixed = debug_program(
            BACKEND, target=target, id_col=id_col, task_description=info["TASK_DESCRIPTION"],
            current_program=buggy, feedback=feedback, caller_stage="inner",
        )
        _s, good = adapter.step_task(state, fixed)
        self.assertTrue(good[VALID_SOLUTION], "debug should repair the buggy program")
        adapter.close(state)


class CrossoverSeamTest(unittest.TestCase):
    def test_crossover_pulls_two_parents(self) -> None:
        adapter, state, info = _prepared()
        target, id_col = info["target"], info["id_col"]
        arch = EvolutionArchive()
        a = draft_program(BACKEND, target=target, id_col=id_col,
                         task_description=info["TASK_DESCRIPTION"], variant=0, caller_stage="inner")
        b = draft_program(BACKEND, target=target, id_col=id_col,
                         task_description=info["TASK_DESCRIPTION"], variant=1, caller_stage="inner")
        ca = Candidate(candidate_id="p1", run_id="r", params={}, generation=0,
                       node_kind="program", code=a, operator="draft", fitness=0.8)
        cb = Candidate(candidate_id="p2", run_id="r", params={}, generation=0,
                       node_kind="program", code=b, operator="draft", fitness=0.7)
        arch.add(ca)
        arch.add(cb)
        parents = select_crossover_parents(arch, "r", k=2)
        self.assertEqual({p.candidate_id for p in parents}, {"p1", "p2"})
        cx = crossover_program(BACKEND, target=target, id_col=id_col,
                               task_description=info["TASK_DESCRIPTION"],
                               parent_programs=tuple(p.code for p in parents),
                               caller_stage="inner")
        _s, outcome = adapter.step_task(state, cx)
        self.assertTrue(outcome[VALID_SOLUTION])
        adapter.close(state)


class ProgramEvolutionUnitTest(unittest.TestCase):
    def test_seed_population_diverse(self) -> None:
        rng = random.Random(11)
        pop = seed_program_population(BACKEND, size=4, run_id="r", rng=rng,
                                      target="Survived", id_col="PassengerId")
        self.assertEqual(len(pop), 4)
        self.assertTrue(all(c.node_kind == "program" for c in pop))
        self.assertTrue(all(c.operator == "draft" for c in pop))
        self.assertEqual(len({c.code for c in pop}), 4)  # all distinct (variant diversity)

    def test_island_model_migration(self) -> None:
        rng = random.Random(3)
        model = IslandModel(2, "r")
        model.seed(
            lambda size, generation=0: seed_program_population(
                BACKEND, size=size, run_id="r", rng=rng, target="Survived",
                id_col="PassengerId", generation=generation),
            size=2, generation=0,
        )
        self.assertEqual(len(model.all_candidates()), 4)
        self.assertTrue(any(".isl0" in c.branch for c in model.all_candidates()))
        self.assertTrue(any(".isl1" in c.branch for c in model.all_candidates()))
        # give island 0 a clear winner, then migrate it into island 1
        model.islands[0][0].fitness = 0.95
        before = len(model.islands[1])
        model.migrate(top_k=1)
        self.assertGreater(len(model.islands[1]), before)
        # migrated clone must not duplicate the origin island
        self.assertFalse(any(".isl0" in c.branch for c in model.islands[1]))

    def test_novelty_filter_rejects_duplicate_code(self) -> None:
        a = draft_program(BACKEND, target="Survived", id_col="PassengerId",
                          task_description="classify titanic", variant=0, caller_stage="inner")
        # a crossover program is structurally distinct from a draft -> clearly "novel"
        b = crossover_program(BACKEND, target="Survived", id_col="PassengerId",
                              task_description="classify", parent_programs=(a, a),
                              caller_stage="inner")
        dup = Candidate(candidate_id="dup", run_id="r", params={}, generation=1,
                       node_kind="program", code=a, operator="draft")
        other = Candidate(candidate_id="o", run_id="r", params={}, generation=1,
                         node_kind="program", code=b, operator="crossover")
        # archived_codes = what is ALREADY in the archive (a); the batch [dup, other] is
        # filtered against it, so `other`'s own code must NOT be pre-included.
        kept = novelty_filter([dup, other], archived_codes=[a], threshold=0.92)
        self.assertEqual(dup.status, "rejected_novelty")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].candidate_id, "o")


class ProgramEvolutionE2ETest(unittest.TestCase):
    def _run(self, **kw):
        svc = ControlPlaneService()
        store = ResearchStateStore()
        orch = ClosedLoopOrchestrator(svc, mode="scripted", state_store=store)
        run = svc.create_workflow_run(CreateWorkflowRunRequest(
            program_id="prog-evo", run_type="standard_research",
            entry_stage="00_agent_orchestration", target_id="titanic",
            objective_snapshot={"goal": "classify titanic", "target_threshold": 0.82},
        ))
        svc.start_workflow_run(run.run_id)
        args = dict(islands=1, pop_per_island=3, generations=2, audit=False, seed=1)
        args.update(kw)
        summary = orch.run_program_evolutionary_loop(run.run_id, TASK_CFG, BACKEND, **args)
        return svc, store, run, summary

    def test_program_nodes_written_and_searched(self) -> None:
        svc, store, run, summary = self._run()
        self.assertIn(summary["status"], ("exited_converged", "exited_budget"))
        self.assertIsNotNone(summary["best_candidate"])
        # program candidates archived with node_kind="program"
        progs = store.evolution.list_by_kind(run.run_id, "program")
        self.assertGreaterEqual(len(progs), 2)
        self.assertTrue(all(c.node_kind == "program" for c in progs))
        # champion is a program node
        champ = store.evolution.best(run.run_id)
        self.assertEqual(champ.node_kind, "program")
        self.assertIsNotNone(champ.fitness)
        self.assertGreater(champ.fitness, 0.5)
        # hypothesis tree carries program nodes on gen branches
        nodes = store.hypo_tree.list_nodes(run_id=run.run_id)
        self.assertTrue(any(n.node_kind == "program" for n in nodes))
        self.assertTrue(any(n.branch.startswith("gen") for n in nodes))


if __name__ == "__main__":
    unittest.main()
