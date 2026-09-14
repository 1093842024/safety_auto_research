"""Tests for the MEA framework (LongHorizon-Harness Phases P1–P3).

Covers:
  * TaskState — updated ONLY by verified audit verdicts (executor summary ignored).
  * RoleAgentRegistry — resolve per role/subtask, per-role budgets, auditor≠executor guard.
  * Adapters — deterministic / independent-auditor / manager / meta-decider behavior.
  * run_mea_loop_core — converges using only verified completions; real Manager decomposition.
  * R0–R10 catalog — strong model reserved for R0 (Manager) + R7 (meta-decider) only; R6 auditor independent.
  * Executor override routing — each research step routed to its configured optimal agent.
  * MeaMetrics — quantifies selective-routing savings vs the naive spend-everywhere baseline.
  * Orchestrator.run_capability(agent=) — lets a RoleAgentAdapter fulfill a subtask.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.task_state import AuditVerdict
from safety_auto_research.control_plane.task_state import DEFAULT_PLAN
from safety_auto_research.control_plane.task_state import StateRecord
from safety_auto_research.control_plane.task_state import SubtaskContract
from safety_auto_research.control_plane.task_state import RESEARCH_STEPS
from safety_auto_research.control_plane.task_state import STRONG_MODEL_STEPS
from safety_auto_research.control_plane.task_state import DEFAULT_PLAN
from safety_auto_research.control_plane.task_state import SubtaskSpec
from safety_auto_research.control_plane.task_state import TaskState
from safety_auto_research.control_plane.task_state import default_plan
from safety_auto_research.execution_plane.agents import ClaudeCodeAdapter
from safety_auto_research.execution_plane.agents import CodexAdapter
from safety_auto_research.execution_plane.agents import DeterministicAdapter
from safety_auto_research.execution_plane.agents import IndependentAuditorAdapter
from safety_auto_research.execution_plane.agents import LLMApiAdapter
from safety_auto_research.execution_plane.agents import LocalExecutorAdapter
from safety_auto_research.execution_plane.agents import ManagerAdapter
from safety_auto_research.execution_plane.agents import MetaDeciderAdapter
from safety_auto_research.execution_plane.agents import RoleAgentAdapter
from safety_auto_research.execution_plane.agents import RoleAgentRegistry
from safety_auto_research.execution_plane.agents import WorkBuddyAdapter
from safety_auto_research.execution_plane.agents import ExecOutput
from safety_auto_research.execution_plane.agents import RoleBudget
from safety_auto_research.execution_plane.base import ExecResult
from safety_auto_research.execution_plane.mea import MeaMetrics
from safety_auto_research.execution_plane.mea import run_mea_loop_core
from safety_auto_research.execution_plane.orchestrator import ClosedLoopOrchestrator
from safety_auto_research.platform_contracts.enums import GateResult
from safety_auto_research.platform_contracts.enums import StageStatus


class FakeAgent(RoleAgentAdapter):
    backend = "fake"

    def __init__(self) -> None:
        self.called = False

    def run_contract(self, contract, budget):
        self.called = True
        return ExecOutput(status="succeeded", summary="fake done")


class FakeBrain:
    """Stand-in for an ``LLMApiAdapter`` brain: returns a canned chat() reply."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple] = []

    def chat(self, system, user):
        self.calls.append((system, user))
        return self._reply


class TestTaskState(unittest.TestCase):
    def test_from_objective_seeds_requirements(self):
        s = TaskState.from_objective("r1", "improve safety")
        self.assertEqual(len(s.records), 5)
        self.assertTrue(all(r.kind == "requirement" for r in s.records.values()))
        self.assertTrue(all(r.status == "pending" for r in s.records.values()))

    def test_next_contract_links_record_key(self):
        s = TaskState.from_objective("r1", "improve safety")
        c = s.next_subtask_contract()
        self.assertIsInstance(c, SubtaskContract)
        self.assertIsNotNone(c.record_key)
        self.assertEqual(c.subtask_type, "literature_search")

    def test_apply_verdict_only_verified(self):
        s = TaskState.from_objective("r1", "improve safety")
        c = s.next_subtask_contract()
        # Auditor marks the requirement completed with evidence.
        verdict = AuditVerdict(
            completion="complete",
            integrity="clean",
            state_updates=[
                StateRecord(kind="requirement", key=c.record_key, status="completed")
            ],
            evidence_refs=["aud1"],
            audit_run_id="r1",
        )
        s.apply_verdict(verdict)
        self.assertEqual(s.records[c.record_key].status, "completed")
        self.assertEqual(s.records[c.record_key].updated_by_audit, "r1")
        self.assertIn("aud1", s.audit_log)

    def test_integrity_violation_blocks_completion(self):
        s = TaskState.from_objective("r1", "improve safety")
        c = s.next_subtask_contract()
        verdict = AuditVerdict(
            completion="complete",
            integrity="violation",  # executor mutated a protected artifact
            state_updates=[
                StateRecord(kind="requirement", key=c.record_key, status="completed")
            ],
            evidence_refs=["aud2"],
        )
        s.apply_verdict(verdict)
        # Must NOT be promoted to completed under an integrity violation.
        self.assertEqual(s.records[c.record_key].status, "untrusted")

    def test_executor_summary_ignored(self):
        s = TaskState.from_objective("r1", "improve safety")
        # No verdict applied -> requirement stays pending (executor claim alone is nothing).
        self.assertFalse(s.all_requirements_met())
        self.assertTrue(all(r.status == "pending" for r in s.records.values()))

    def test_save_and_load_roundtrip(self):
        d = tempfile.mkdtemp()
        s = TaskState.from_objective("r1", "improve safety")
        s.save(d)
        loaded = TaskState.load("r1", d)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.objective, "improve safety")
        self.assertEqual(len(loaded.records), 5)


class TestRoleAgentRegistry(unittest.TestCase):
    def setUp(self):
        # WorkBuddyAdapter needs an endpoint to construct its transport.
        os.environ.setdefault("WORKBUDDY_ENDPOINT", "http://localhost/test")

    def _spec(self, auditor_deterministic=False):
        aud = {"backend": "deterministic", "model": "none"} if auditor_deterministic else {
            "backend": "llm_api", "model": "judge"
        }
        # The guard only fires when the (independent) Auditor shares backend+model
        # with the Executor; keep the requirement in both branches to test it.
        aud["require_different_from"] = "executor"
        return {
            "roles": {
                "manager": {"backend": "llm_api", "model": "opus"},
                "executor_default": {"backend": "deterministic", "model": "none", "budget_seconds": 1800},
                "executor_overrides": {
                    "literature_search": {"backend": "workbuddy", "model": "web", "budget_seconds": 1200},
                    "hypothesis_gen": {"backend": "llm_api", "model": "opus"},
                },
                "auditor": aud,
            }
        }

    def test_resolve_roles(self):
        reg = RoleAgentRegistry.load(self._spec())
        self.assertIsInstance(reg.resolve("executor"), DeterministicAdapter)
        self.assertIsInstance(reg.resolve("executor", "literature_search"), WorkBuddyAdapter)
        self.assertIsInstance(reg.resolve("executor", "hypothesis_gen"), LLMApiAdapter)
        self.assertIsInstance(reg.resolve("auditor"), IndependentAuditorAdapter)
        # P2: the manager role is wrapped in a ManagerAdapter whose brain is the
        # configured llm_api adapter (so decompose() can ask it for a JSON plan).
        mgr = reg.resolve("manager")
        self.assertIsInstance(mgr, ManagerAdapter)
        self.assertIsInstance(mgr._brain, LLMApiAdapter)

    def test_codex_claude_adapters_resolve(self):
        spec = {"roles": {
            "executor_default": {"backend": "codex", "model": "auto", "budget_seconds": 1800},
            "auditor": {"backend": "llm_api", "model": "judge", "require_different_from": "executor"},
        }}
        reg = RoleAgentRegistry.load(spec)
        self.assertIsInstance(reg.resolve("executor"), CodexAdapter)
        # auditor llm_api distinct from executor codex -> IndependentAuditorAdapter
        self.assertIsInstance(reg.resolve("auditor"), IndependentAuditorAdapter)

    def test_budgets(self):
        reg = RoleAgentRegistry.load(self._spec())
        self.assertEqual(reg.budget_for("executor").max_seconds, 1800)
        self.assertEqual(reg.budget_for("executor", "literature_search").max_seconds, 1200)
        self.assertEqual(reg.budget_for("auditor").max_seconds, 300)
        self.assertEqual(reg.budget_for("manager").max_seconds, 300)

    def test_separation_guard(self):
        reg = RoleAgentRegistry.load(self._spec())  # executor deterministic, auditor llm_api
        reg.enforce_separation()  # passes
        reg_same = RoleAgentRegistry.load(self._spec(auditor_deterministic=True))
        with self.assertRaises(ValueError):
            reg_same.enforce_separation()


class TestAdapters(unittest.TestCase):
    def test_local_executor_with_runner(self):
        calls = {}

        def runner(run_id, cid, params):
            calls["cid"] = cid
            res = ExecResult(
                final_status=StageStatus.SUCCEEDED, gate_result=GateResult.PASSED,
                event=None, output_refs=["o1"], detail="ran",
            )
            return SimpleNamespace(stage_run_id="s1"), res

        a = LocalExecutorAdapter(capability_runner=runner)
        out = a.run_contract(
            SubtaskContract(subtask_type="experiment_code", goal="g", capability_id="kaggle_eval",
                            params={"run_id": "r"}),
            RoleBudget(),
        )
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(calls["cid"], "kaggle_eval")

    def test_local_executor_no_runner_logs(self):
        a = LocalExecutorAdapter()
        out = a.run_contract(
            SubtaskContract(subtask_type="x", goal="g", params={}),
            RoleBudget(),
        )
        self.assertEqual(out.status, "succeeded")

    def test_independent_auditor_clean(self):
        a = IndependentAuditorAdapter()  # no LLM -> fail-soft deterministic
        out = ExecOutput(
            status="succeeded", summary="done"
        )
        v = a.audit(SubtaskContract(subtask_type="x", goal="g", record_key="k"), out, None)
        self.assertEqual(v.completion, "complete")
        self.assertEqual(v.integrity, "clean")
        self.assertEqual(v.state_updates[0].status, "completed")

    def test_independent_auditor_integrity_violation(self):
        a = IndependentAuditorAdapter()
        out = ExecOutput(
            status="succeeded", summary="done"
        )
        env = {"before": {"/p": (1, 1)}, "after": {"/p": (2, 2)}}
        v = a.audit(SubtaskContract(subtask_type="x", goal="g", record_key="k"), out, env)
        self.assertEqual(v.integrity, "violation")


class TestManagerDecompose(unittest.TestCase):
    """P2: the Manager's real subtask decomposition (LLM plan + fail-soft fallback)."""

    def test_llm_decompose_parses_plan(self):
        brain = FakeBrain(json.dumps([
            {"subtask_type": "literature_search", "goal": "find refs",
             "acceptance_criteria": ["a1"], "depends_on": []},
            {"subtask_type": "hypothesis_gen", "goal": "make h",
             "depends_on": ["literature_search"]},
            {"subtask_type": "experiment_design", "depends_on": ["hypothesis_gen"]},
            {"subtask_type": "experiment_code", "depends_on": ["experiment_design"]},
            {"subtask_type": "eval_metrics", "depends_on": ["experiment_code"]},
        ], ensure_ascii=False))
        mgr = ManagerAdapter(brain=brain)
        specs = mgr.decompose("obj", state=None, max_subtasks=8)
        self.assertEqual(
            [s.subtask_type for s in specs],
            ["literature_search", "hypothesis_gen", "experiment_design",
             "experiment_code", "eval_metrics"],
        )
        self.assertEqual(specs[1].depends_on, ["literature_search"])
        self.assertEqual(specs[0].acceptance_criteria, ["a1"])
        self.assertTrue(brain.calls)  # the brain's chat() was actually used

    def test_fails_soft_to_default_plan(self):
        # No brain at all -> deterministic default_plan.
        mgr = ManagerAdapter(brain=None)
        specs = mgr.decompose("objective X", state=None)
        self.assertEqual([s.subtask_type for s in specs], DEFAULT_PLAN)

        # Brain that raises on every call -> also deterministic default_plan.
        class BoomBrain:
            def chat(self, system, user):
                raise RuntimeError("network down")

        mgr2 = ManagerAdapter(brain=BoomBrain())
        specs2 = mgr2.decompose("objective X", state=None)
        self.assertEqual([s.subtask_type for s in specs2], DEFAULT_PLAN)

    def test_decompose_filters_unknown_types(self):
        # All unknown subtask types -> _parse_plan_json raises -> decompose catches
        # and falls back to default_plan.
        mgr = ManagerAdapter(brain=FakeBrain(json.dumps([
            {"subtask_type": "telepathy", "goal": "x"},
            {"subtask_type": "time_travel", "goal": "y"},
        ])))
        specs = mgr.decompose("obj")
        self.assertEqual([s.subtask_type for s in specs], DEFAULT_PLAN)

        # Mix: unknown dropped, known kept (and depends_on preserved).
        mgr2 = ManagerAdapter(brain=FakeBrain(json.dumps([
            {"subtask_type": "telepathy", "goal": "x"},
            {"subtask_type": "literature_search", "goal": "find", "depends_on": []},
            {"subtask_type": "hypothesis_gen", "goal": "h",
             "depends_on": ["literature_search"]},
        ])))
        specs2 = mgr2.decompose("obj")
        self.assertEqual(
            [s.subtask_type for s in specs2],
            ["literature_search", "hypothesis_gen"],
        )
        self.assertEqual(specs2[1].depends_on, ["literature_search"])

    def test_decompose_respects_max_subtasks(self):
        brain = FakeBrain(json.dumps([
            {"subtask_type": "literature_search"},
            {"subtask_type": "hypothesis_gen"},
            {"subtask_type": "experiment_design"},
            {"subtask_type": "experiment_code"},
            {"subtask_type": "eval_metrics"},
        ]))
        mgr = ManagerAdapter(brain=brain)
        specs = mgr.decompose("obj", max_subtasks=3)
        self.assertEqual(len(specs), 3)
        self.assertEqual([s.subtask_type for s in specs],
                         ["literature_search", "hypothesis_gen", "experiment_design"])


class TestTaskStatePlan(unittest.TestCase):
    """P2: plan-driven, dependency-aware TaskState scheduling."""

    def test_from_plan_seeds_requirement_records(self):
        plan = default_plan("obj")
        s = TaskState.from_plan("r", "obj", plan)
        self.assertEqual(len(s.records), len(plan))
        self.assertEqual([s2.subtask_type for s2 in s.plan],
                         [s.subtask_type for s in plan])
        # Each record carries its depends_on so next_subtask_contract can schedule.
        self.assertEqual(
            s.records["subtask.experiment_design"].content["depends_on"],
            ["hypothesis_gen"],
        )

    def test_next_contract_honors_dependencies(self):
        plan = default_plan("obj")
        s = TaskState.from_plan("r", "obj", plan)
        # First offered is the first (dependency-free) spec.
        c1 = s.next_subtask_contract()
        self.assertEqual(c1.subtask_type, "literature_search")
        # experiment_code must NOT be offered until experiment_design is verified complete.
        # Mark the first three complete via verified verdicts.
        for st in ("literature_search", "hypothesis_gen", "experiment_design"):
            s.apply_verdict(AuditVerdict(
                completion="complete", integrity="clean",
                state_updates=[StateRecord(
                    kind="requirement", key=f"subtask.{st}", status="completed")],
                evidence_refs=[f"e_{st}"],
            ))
        c2 = s.next_subtask_contract()
        self.assertEqual(c2.subtask_type, "experiment_code")
        # eval_metrics still depends on experiment_code, so it is not offered yet.
        c3 = s.next_subtask_contract()
        self.assertEqual(c3.subtask_type, "experiment_code")

    def test_resume_loads_plan(self):
        import tempfile
        d = tempfile.mkdtemp()
        plan = default_plan("obj")
        TaskState.from_plan("r", "obj", plan).save(d)
        loaded = TaskState.load("r", d)
        self.assertEqual([s.subtask_type for s in loaded.plan],
                         [s.subtask_type for s in plan])


class TestMeaLoopCore(unittest.TestCase):
    def setUp(self):
        # Keep the auditor's optional LLM judge from reaching the network in tests:
        # when no base_url is configured it degrades to a deterministic pass-through.
        self._saved_base = os.environ.pop("OPENMLE_API_BASE_URL", None)
        self._saved_model = os.environ.pop("OPENMLE_API_MODEL", None)

    def tearDown(self):
        if self._saved_base is not None:
            os.environ["OPENMLE_API_BASE_URL"] = self._saved_base
        if self._saved_model is not None:
            os.environ["OPENMLE_API_MODEL"] = self._saved_model

    def test_converges_on_verified(self):
        d = tempfile.mkdtemp()
        spec = {"roles": {
            "executor_default": {"backend": "deterministic", "model": "none", "budget_seconds": 1800},
            "auditor": {"backend": "llm_api", "model": "judge", "require_different_from": "executor"},
        }}
        reg = RoleAgentRegistry.load(spec)
        status, state, _m = run_mea_loop_core(reg, "run-x", "improve safety", max_rounds=10, data_dir=d)
        self.assertEqual(status, "exited_converged")
        self.assertTrue(state.all_requirements_met())
        # persisted
        self.assertIsNotNone(TaskState.load("run-x", d))

    def test_persists_across_resume(self):
        d = tempfile.mkdtemp()
        spec = {"roles": {
            "executor_default": {"backend": "deterministic", "model": "none"},
            "auditor": {"backend": "llm_api", "model": "judge", "require_different_from": "executor"},
        }}
        reg = RoleAgentRegistry.load(spec)
        # First run with 2 rounds -> not converged yet, state persisted.
        s1, _, _ = run_mea_loop_core(reg, "run-y", "obj", max_rounds=2, data_dir=d)
        self.assertEqual(s1, "exited_budget")
        self.assertIsNotNone(TaskState.load("run-y", d))
        # Resume -> continues from verified state to convergence.
        s2, state2, _m2 = run_mea_loop_core(reg, "run-y", "obj", max_rounds=10, data_dir=d)
        self.assertEqual(s2, "exited_converged")
        self.assertTrue(state2.all_requirements_met())

    def test_uses_manager_decomposition(self):
        # On a fresh run the loop must decompose via the configured Manager (not the
        # static default_plan). The brain returns a CUSTOM 2-node DAG so we can prove the
        # Manager's plan — not default_plan's 5 nodes — drove the run.
        d = tempfile.mkdtemp()
        brain = FakeBrain(json.dumps([
            {"subtask_type": "literature_search", "goal": "custom lit",
             "acceptance_criteria": ["c1"], "depends_on": []},
            {"subtask_type": "hypothesis_gen", "goal": "custom hyp",
             "depends_on": ["literature_search"]},
        ], ensure_ascii=False))
        spec = {"roles": {
            "manager": {"backend": "llm_api", "model": "opus"},
            "executor_default": {"backend": "deterministic", "model": "none", "budget_seconds": 1800},
            "auditor": {"backend": "llm_api", "model": "judge", "require_different_from": "executor"},
        }}
        reg = RoleAgentRegistry.load(spec)
        # Pre-seed the cache so resolve("manager") returns our brain-backed ManagerAdapter.
        reg._cache["manager:None"] = ManagerAdapter(brain=brain)
        status, state, _m = run_mea_loop_core(reg, "run-mgr", "obj", max_rounds=10, data_dir=d)
        self.assertEqual(status, "exited_converged")
        # The plan reflects the custom decomposition, not default_plan (which has 5 types).
        self.assertEqual(
            [s.subtask_type for s in state.plan],
            ["literature_search", "hypothesis_gen"],
        )
        self.assertEqual(state.plan[0].acceptance_criteria, ["c1"])
        self.assertTrue(brain.calls)  # the Manager actually asked the brain for a plan


class TestResearchStepCatalog(unittest.TestCase):
    """P3: the R0–R10 catalog encodes the selective-routing cost strategy."""

    def test_catalog_covers_r0_through_r10(self):
        self.assertEqual(set(RESEARCH_STEPS), {f"R{i}" for i in range(11)})

    def test_strong_model_only_on_r0_and_r7(self):
        # The reserved expensive reasoning model is spent ONLY on the budget-saving
        # meta/decision steps (R0 Manager + R7 meta-decider) — never on executor steps.
        self.assertEqual(STRONG_MODEL_STEPS, {"R0", "R7"})
        for step_id, step in RESEARCH_STEPS.items():
            if step_id in STRONG_MODEL_STEPS:
                self.assertTrue(step["uses_strong_model"])
            else:
                self.assertFalse(step.get("uses_strong_model", False))

    def test_auditor_is_independent_model(self):
        # R6 Auditor must use a DISTINCT model from the executor (anti-self-confirmation),
        # and must NOT be counted as a strong-model consumer.
        r6 = RESEARCH_STEPS["R6"]
        self.assertTrue(r6.get("independent_model"))
        self.assertNotIn("R6", STRONG_MODEL_STEPS)
        self.assertEqual(r6["role"], "auditor")


class TestMetaDecider(unittest.TestCase):
    """P3: R7 meta-decider resolves and decides with fail-soft fallback."""

    def _spec(self, manager_brain_reply=None):
        roles = {
            "meta_decider": {"backend": "llm_api", "model": "opus"},
            "executor_default": {"backend": "deterministic", "model": "none"},
            "auditor": {"backend": "llm_api", "model": "judge", "require_different_from": "executor"},
        }
        if manager_brain_reply is not None:
            roles["manager"] = {"backend": "llm_api", "model": "opus"}
        return {"roles": roles}

    def test_resolve_meta_decider(self):
        reg = RoleAgentRegistry.load(self._spec())
        adapter = reg.resolve("meta_decider")
        self.assertIsInstance(adapter, MetaDeciderAdapter)
        self.assertEqual(adapter.backend, "meta_decider")

    def test_decide_parses_llm_json(self):
        brain = FakeBrain(json.dumps({"action": "escalate", "rationale": "slow progress"}))
        decider = MetaDeciderAdapter(brain=brain)
        decision = decider.decide("obj", state=None)
        self.assertEqual(decision["action"], "escalate")
        self.assertEqual(decision["rationale"], "slow progress")
        self.assertTrue(brain.calls)

    def test_decide_fail_soft_without_brain(self):
        # No brain -> deterministic "continue" so the loop always progresses.
        decider = MetaDeciderAdapter(brain=None)
        decision = decider.decide("obj", state=None)
        self.assertEqual(decision["action"], "continue")

    def test_decide_fail_soft_on_bad_json(self):
        brain = FakeBrain("the model went off the rails, no json here")
        decider = MetaDeciderAdapter(brain=brain)
        decision = decider.decide("obj", state=None)
        self.assertEqual(decision["action"], "continue")


class TestExecutorOverrideRouting(unittest.TestCase):
    """P3: per-subtask executor_overrides route each R* step to its optimal agent."""

    def test_routing_from_yaml_overrides(self):
        # Resolve directly from the real config so the override layout is exercised.
        reg = RoleAgentRegistry.load(
            os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "config",
                "role_agents.yaml",
            )
        )
        # R5 eval -> deterministic (no LLM, reproducible).
        self.assertIsInstance(reg.resolve("executor", "eval_metrics"), DeterministicAdapter)
        # R1 lit search -> workbuddy.
        self.assertIsInstance(reg.resolve("executor", "literature_search"), WorkBuddyAdapter)
        # R2/R3 hypothesis/design -> llm_api.
        self.assertIsInstance(reg.resolve("executor", "hypothesis_gen"), LLMApiAdapter)
        self.assertIsInstance(reg.resolve("executor", "experiment_design"), LLMApiAdapter)
        # R4 code -> claude_code.
        self.assertIsInstance(reg.resolve("executor", "experiment_code"), ClaudeCodeAdapter)
        # R10 report -> workbuddy.
        self.assertIsInstance(reg.resolve("executor", "report_synthesis"), WorkBuddyAdapter)

    def test_override_layout_nested_under_executor_default(self):
        # The registry must accept executor_overrides nested under executor_default
        # (the layout used in role_agents.yaml), not silently fall back to default.
        reg = RoleAgentRegistry.load({
            "roles": {
                "executor_default": {
                    "backend": "claude_code", "model": "auto",
                    "executor_overrides": {
                        "eval_metrics": {"backend": "deterministic", "model": "none"},
                    },
                },
                "auditor": {"backend": "llm_api", "model": "judge", "require_different_from": "executor"},
            }
        })
        self.assertIsInstance(reg.resolve("executor", "eval_metrics"), DeterministicAdapter)
        # uncovered subtask falls through to the executor_default backend.
        self.assertIsInstance(reg.resolve("executor", "hypothesis_gen"), ClaudeCodeAdapter)


class TestMeaMetrics(unittest.TestCase):
    """P3: quantify the selective-routing savings vs the naive spend-everywhere baseline."""

    # 8 distinct valid subtask types — each maps to a distinct record key, so the loop
    # runs exactly len(plan) rounds with a deterministic executor (one subtask per round).
    PLAN_8 = [
        "literature_search", "hypothesis_gen", "experiment_design", "experiment_code",
        "eval_metrics", "self_evolve_patch", "evolution_orchestrate", "report_synthesis",
    ]

    def _spec(self):
        return {"roles": {
            "manager": {"backend": "llm_api", "model": "opus"},
            "meta_decider": {"backend": "llm_api", "model": "opus"},
            "executor_default": {"backend": "deterministic", "model": "none", "budget_seconds": 1800},
            "auditor": {"backend": "llm_api", "model": "judge", "require_different_from": "executor"},
        }}

    def test_metrics_returned_and_strong_model_sparse(self):
        d = tempfile.mkdtemp()
        reg = RoleAgentRegistry.load(self._spec())
        status, state, m = run_mea_loop_core(
            reg, "run-m", "obj", max_rounds=50, data_dir=d, meta_every=5, plan=self.PLAN_8
        )
        self.assertEqual(status, "exited_converged")
        self.assertEqual(m.rounds, len(self.PLAN_8))  # one subtask per round
        self.assertTrue(m.converged)
        # An explicit plan means NO LLM Manager call (R0 costs nothing here); only R7
        # fires every 5 rounds within `rounds` (round 5 only -> 1 strong-model call).
        expected_r7 = m.rounds // 5
        self.assertEqual(m.strong_model_calls, expected_r7)
        # Executor used only the cheap deterministic agent — never the strong model.
        self.assertEqual(m.executor_calls_by_backend, {"deterministic": len(self.PLAN_8)})

    def test_r0_counts_when_manager_decomposes(self):
        # With no explicit plan the Manager runs (LLM or fallback) and R0 IS counted.
        d = tempfile.mkdtemp()
        spec = self._spec()
        reg = RoleAgentRegistry.load(spec)
        # Seed a brain-backed Manager so decompose consumes a strong-model call (R0).
        brain = FakeBrain(json.dumps([
            {"subtask_type": "literature_search", "goal": "g", "depends_on": []},
            {"subtask_type": "hypothesis_gen", "goal": "h", "depends_on": ["literature_search"]},
        ], ensure_ascii=False))
        reg._cache["manager:None"] = ManagerAdapter(brain=brain)
        status, state, m = run_mea_loop_core(
            reg, "run-r0", "obj", max_rounds=50, data_dir=d, meta_every=5
        )
        self.assertEqual(status, "exited_converged")
        # R0 (1) + R7 every 5 rounds over `rounds` (2 subtasks -> 2 rounds -> 0 R7) = 1.
        expected_r7 = m.rounds // 5
        self.assertEqual(m.strong_model_calls, 1 + expected_r7)
        self.assertTrue(brain.calls)  # the Manager actually decomposed via its brain

    def test_savings_ratio_exceeds_one(self):
        d = tempfile.mkdtemp()
        reg = RoleAgentRegistry.load(self._spec())
        status, state, m = run_mea_loop_core(
            reg, "run-s", "obj", max_rounds=50, data_dir=d, meta_every=5, plan=self.PLAN_8
        )
        # Actual selective cost must be below the naive "strong model everywhere" baseline.
        self.assertGreater(m.naive_cost_proxy, m.cost_proxy)
        self.assertGreater(m.savings_ratio, 1.0)
        # naive = R0(20) + rounds*20 (executor everywhere) + rounds*20 (meta every round).
        self.assertEqual(m.naive_cost_proxy, 20 + m.rounds * 40)
        # actual = strong_model_calls * 20 (deterministic executor costs 0; explicit plan -> no R0).
        self.assertEqual(m.cost_proxy, m.strong_model_calls * 20)
        self.assertLess(m.cost_proxy, 200)

    def test_meta_every_one_invokes_decider_every_round(self):
        d = tempfile.mkdtemp()
        reg = RoleAgentRegistry.load(self._spec())
        status, state, m = run_mea_loop_core(
            reg, "run-e", "obj", max_rounds=50, data_dir=d, meta_every=1, plan=self.PLAN_8
        )
        # meta_every=1 -> R7 every round, EXCEPT the final round (which converges and
        # returns before the meta block). 8 rounds -> 7 R7 calls; explicit plan -> no R0.
        self.assertEqual(m.rounds, len(self.PLAN_8))
        self.assertEqual(m.strong_model_calls, len(self.PLAN_8) - 1)
        self.assertIsNotNone(m.last_meta_decision)


class TestClosedLoopExecFn(unittest.TestCase):
    """P4: real executor backends are fulfilled INSIDE a real StageRun via run_capability.

    The deterministic placeholder keeps its own runner binding (run_contract ->
    run_capability default executor); real backends (llm_api / claude_code / codex /
    workbuddy) are routed through ``exec_fn(agent=...)`` so each subtask gets the full
    control-plane audit trail instead of executing blindly.
    """

    def _fake_exec_fn(self):
        calls = []

        def fn(run_id, capability_id, params, agent=None):
            calls.append((run_id, capability_id, agent))
            return (
                SimpleNamespace(stage_run_id="s", stage_code=capability_id),
                ExecResult(
                    final_status=StageStatus.SUCCEEDED, gate_result=GateResult.PASSED,
                    event=None, output_refs=[], detail="ran via exec_fn",
                ),
            )

        fn.calls = calls
        return fn

    def test_exec_fn_invoked_with_agent_for_real_backend(self):
        d = tempfile.mkdtemp()
        spec = {"roles": {
            "executor_default": {"backend": "llm_api", "model": "opus"},
            "auditor": {"backend": "llm_api", "model": "judge",
                        "require_different_from": "executor"},
        }}
        reg = RoleAgentRegistry.load(spec)
        fake = self._fake_exec_fn()
        status, state, m = run_mea_loop_core(
            reg, "run-ef", "obj", max_rounds=50, data_dir=d,
            meta_every=5, exec_fn=fake, plan=list(DEFAULT_PLAN),
        )
        self.assertEqual(status, "exited_converged")
        self.assertGreater(len(fake.calls), 0)
        # Each call must carry the resolved executor adapter as `agent` (closed loop).
        for _rid, cid, agent in fake.calls:
            self.assertIsInstance(agent, LLMApiAdapter)
            self.assertIsNotNone(cid)
        self.assertEqual(m.executor_calls_by_backend.get("llm_api"), len(DEFAULT_PLAN))

    def test_exec_fn_not_used_for_deterministic(self):
        # Deterministic keeps its runner binding; exec_fn is reserved for real backends,
        # so a provided exec_fn must NOT fire for the deterministic placeholder.
        d = tempfile.mkdtemp()
        spec = {"roles": {
            "executor_default": {"backend": "deterministic", "model": "none"},
            "auditor": {"backend": "llm_api", "model": "judge",
                        "require_different_from": "executor"},
        }}
        reg = RoleAgentRegistry.load(spec)
        fake = self._fake_exec_fn()
        status, state, m = run_mea_loop_core(
            reg, "run-dt", "obj", max_rounds=50, data_dir=d,
            meta_every=5, exec_fn=fake, plan=list(DEFAULT_PLAN),
        )
        self.assertEqual(status, "exited_converged")
        self.assertEqual(len(fake.calls), 0)
        self.assertEqual(m.executor_calls_by_backend, {"deterministic": len(DEFAULT_PLAN)})


class TestMeaClosedLoopE2E(unittest.TestCase):
    """P4 end-to-end: real orchestrator + real capability registry run MEA to convergence.

    Uses the deterministic executor (local, no credentials) wired to the orchestrator's
    real ``run_capability``: every executor step executes a REAL capability (stub or the
    offline EvalExecutor) inside a real StageRun. Quantifies the selective-routing savings.
    """

    def setUp(self) -> None:
        # The default plan's ``literature_search`` subtask maps to the REAL
        # ``layer_01_literature_research`` executor (a stub until Phase 2), which queries
        # arXiv. Left alone this suite would (a) depend on the network and (b) write the
        # live response into the repo's ``data/literature/`` cache. Isolate both: no
        # sockets, and a throwaway cache dir. The executor is network-tolerant, so the
        # simulated outage degrades to 0 hits + SUCCEEDED/WAIVED and the loop still
        # converges — which is exactly what these tests are about.
        self._cache_dir = tempfile.mkdtemp(prefix="mea-lit-cache-")
        self._patches = [
            patch(
                "safety_auto_research.execution_plane.capabilities"
                ".literature_research_executor._DEFAULT_CACHE_DIR",
                self._cache_dir,
            ),
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("offline (test isolation)"),
            ),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self) -> None:
        for p in self._patches:
            p.stop()

    def _build_orchestrator(self):
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        return svc, orch

    def _spec(self):
        return {"roles": {
            "manager": {"backend": "llm_api", "model": "opus"},
            "meta_decider": {"backend": "llm_api", "model": "opus"},
            "executor_default": {"backend": "deterministic", "model": "none",
                                 "budget_seconds": 1800},
            "auditor": {"backend": "llm_api", "model": "judge",
                        "require_different_from": "executor"},
        }}

    def _make_run(self, svc, run_id):
        # create_workflow_run mints its own run id; return the ACTUAL id so the loop,
        # run_capability (get_workflow_run), and the metric assertions all agree.
        run = svc.create_workflow_run(CreateWorkflowRunRequest(
            program_id="mea-e2e", run_type="standard_research",
            entry_stage="00_agent_orchestration", target_id="mea",
            objective_snapshot={"goal": "improve model robustness"},
        ))
        svc.start_workflow_run(run.run_id)
        return run.run_id

    def test_production_run_mea_loop_creates_stageruns(self):
        svc, orch = self._build_orchestrator()
        d = tempfile.mkdtemp()
        actual = self._make_run(svc, "mea-e2e-prod")
        status = orch.run_mea_loop(
            actual, role_spec=self._spec(), max_rounds=25,
            objective="improve model robustness", data_dir=d,
        )
        self.assertEqual(status, "exited_converged")
        # Every executor subtask went through run_capability -> a real StageRun each.
        self.assertEqual(orch._cap_call_counts.get(actual, 0), len(DEFAULT_PLAN))
        self.assertIsNotNone(TaskState.load(actual, d))

    def test_metrics_quantify_selective_routing(self):
        svc, orch = self._build_orchestrator()
        d = tempfile.mkdtemp()
        actual = self._make_run(svc, "mea-e2e-metrics")
        registry = RoleAgentRegistry.load(self._spec())
        registry.bind(orch.run_capability)
        registry.enforce_separation()
        status, state, m = run_mea_loop_core(
            registry, actual, "improve model robustness", max_rounds=25,
            data_dir=d, exec_fn=orch.run_capability,
        )
        self.assertEqual(status, "exited_converged")
        # All executor work ran on the cheap deterministic agent (offline, no strong model).
        self.assertEqual(m.executor_calls_by_backend, {"deterministic": len(DEFAULT_PLAN)})
        self.assertGreaterEqual(m.strong_model_calls, 1)  # at least R0 Manager decompose
        # Selective routing beats the naive "strong model everywhere + meta every round".
        self.assertGreater(m.naive_cost_proxy, m.cost_proxy)
        self.assertGreater(m.savings_ratio, 1.0)
        self.assertEqual(orch._cap_call_counts.get(actual, 0), len(DEFAULT_PLAN))


class TestOrchestratorAgentHook(unittest.TestCase):
    def test_run_capability_uses_agent(self):
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        # A workflow run must exist before run_capability can attach a StageRun to it.
        run = svc.create_workflow_run(
            CreateWorkflowRunRequest(
                program_id="mea-test",
                run_type="standard_research",
                entry_stage="00_agent_orchestration",
                target_id="mea",
                objective_snapshot={"goal": "do x"},
            )
        )
        svc.start_workflow_run(run.run_id)
        # Stub the capability registry so any capability_id resolves to a fake cap.
        orch.capability_registry = SimpleNamespace(
            resolve=lambda cid: SimpleNamespace(layer_code="x", executor=None)
        )
        agent = FakeAgent()
        stage, result = orch.run_capability(
            run.run_id, "fake_cap", {"open_goal": "do x"}, agent=agent
        )
        self.assertTrue(agent.called)
        self.assertEqual(result.final_status, StageStatus.SUCCEEDED)
        self.assertEqual(result.gate_result, GateResult.PASSED)

    def test_run_mea_loop_wires_registry(self):
        # Exercises the full orchestrator MEA path: registry load -> bind runners ->
        # separation guard -> core loop -> persistence, all via run_mea_loop (Task #4/#5).
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        d = tempfile.mkdtemp()
        spec = {
            "roles": {
                "executor_default": {
                    "backend": "deterministic", "model": "none", "budget_seconds": 1800
                },
                "auditor": {
                    "backend": "llm_api", "model": "judge",
                    "require_different_from": "executor",
                },
            }
        }
        # Stub run_capability so the deterministic executor reports success; this tests
        # the MEA wiring, not the capability internals.
        orch.run_capability = lambda rid, cid, params, agent=None: (  # type: ignore[assignment]
            SimpleNamespace(stage_run_id="s", stage_code=cid),
            ExecResult(
                final_status=StageStatus.SUCCEEDED, gate_result=GateResult.PASSED,
                event=None, output_refs=[], detail="ok",
            ),
        )
        status = orch.run_mea_loop(
            "mea-wiring", role_spec=spec, max_rounds=8,
            objective="improve safety", data_dir=d,
        )
        self.assertEqual(status, "exited_converged")
        self.assertIsNotNone(TaskState.load("mea-wiring", d))


class TestMeaApi(unittest.TestCase):
    def test_mea_endpoints_create_and_report(self):
        import yaml

        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        svc = ControlPlaneService()
        app = create_app(svc)
        client = TestClient(app)
        d = tempfile.mkdtemp()
        spec_path = os.path.join(d, "roles.yaml")
        with open(spec_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "roles": {
                        "executor_default": {
                            "backend": "deterministic", "model": "none",
                            "budget_seconds": 1800,
                        },
                        "auditor": {
                            "backend": "llm_api", "model": "judge",
                            "require_different_from": "executor",
                        },
                    }
                },
                f,
            )
        run_id = "api-mea-1"
        resp = client.post(
            f"/workflow-runs/{run_id}/mea",
            params={
                "role_spec": spec_path,
                "objective": "improve safety",
                "max_rounds": 6,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["run_id"], run_id)
        # The endpoint backgrounds the loop (缺陷2 fix) and returns immediately.
        self.assertEqual(body["status"], "running")

        # Poll until the background loop reaches a terminal status.
        import time

        terminal = {"exited_converged", "exited_budget", "failed", "cancelled"}
        deadline = time.time() + 30.0
        final_status = svc.get_workflow_run(run_id).status
        while final_status not in terminal and time.time() < deadline:
            time.sleep(0.1)
            final_status = svc.get_workflow_run(run_id).status
        self.assertIn(final_status, terminal)

        # State endpoint must report the persisted, verified TaskState.
        st = client.get(f"/workflow-runs/{run_id}/mea/state")
        self.assertEqual(st.status_code, 200)
        self.assertTrue(st.json()["exists"])


if __name__ == "__main__":
    unittest.main()
