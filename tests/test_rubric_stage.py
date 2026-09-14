"""Tests for the rubric stage (layer_12): task-specific executable scoring standards.

Covers the two behaviours the feature must guarantee:

1. a task that declares **no usable evaluation standard** gets a correct, effective and
   scientific rubric SYNTHESIZED for it;
2. a task that **does** declare one gets that standard AUDITED along three dimensions
   (准确性 / 完整性 / 科学性) and normalized into the same executable contract;

plus the invariants that make the stage trustworthy: determinism, freezing, isolation
from the inner loop, correct programmatic evaluation, no misattribution of
task-definition gaps to the research, and the hard-failure veto.
"""

from __future__ import annotations

import os
import unittest

from safety_auto_research.benchmark_tasks import registry as bench_registry
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.execution_plane.agent.harness import assert_inner_capability_allowed
from safety_auto_research.execution_plane.capabilities.audit_executor import AuditExecutor
from safety_auto_research.execution_plane.capabilities.registry import (
    default_capability_registry,
)
from safety_auto_research.execution_plane.capabilities.rubric_executor import build_task_spec
from safety_auto_research.execution_plane.orchestrator import ClosedLoopOrchestrator
from safety_auto_research.openmle_integration.inner_capability import (
    INNER_LOOP_FORBIDDEN_CALLERS,
)
from safety_auto_research.platform_contracts.enums import GateResult
from safety_auto_research.platform_contracts.enums import StageStatus
from safety_auto_research.platform_contracts.objects import StageRun
from safety_auto_research.rubric import RubricEngine
from safety_auto_research.rubric import TaskSpec
from safety_auto_research.rubric import evaluate_criteria
from safety_auto_research.rubric import summarize

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "kaggle", "titanic")

_ENGINE = RubricEngine(use_llm=False)


def _titanic_spec() -> TaskSpec:
    """A task WITH a declared evaluation standard."""

    return TaskSpec(
        task_id="platform.titanic",
        name="Titanic",
        task_type="tabular_classification",
        objective="优化 cv_accuracy",
        dataset_desc="Titanic survival classification; train.csv / test.csv",
        eval_metric="cv_accuracy",
        direction="higher",
        baseline=0.78,
        gates={"threshold": 0.82},
        eval_method="平台内 kaggle_eval 执行交叉验证（默认 5 折），并在留出集上复核",
        harness="kaggle_eval",
        executable=True,
        type_config={"cv_folds": 5},
    )


def _bare_spec() -> TaskSpec:
    """A task with NO usable evaluation standard (only a metric name)."""

    return TaskSpec(
        task_id="custom.bare",
        name="裸任务",
        task_type="text_classification",
        objective="做一个文本分类",
        eval_metric="accuracy",
        direction="higher",
    )


# --------------------------------------------------------------------------- #
# 1. Synthesis: no declared standard -> generate one                          #
# --------------------------------------------------------------------------- #
class RubricSynthesisTest(unittest.TestCase):
    def test_missing_standard_is_synthesized(self) -> None:
        rubric = _ENGINE.induce(_bare_spec())
        self.assertEqual(rubric.source, "synthesized")
        self.assertFalse(rubric.review.standard_provided)
        self.assertTrue(rubric.criteria, "a synthesized rubric must have criteria")
        self.assertTrue(rubric.frozen)
        self.assertTrue(rubric.integrity_hash)

    def test_synthesized_rubric_is_scientific(self) -> None:
        """The generated standard must cover the dimensions that make a claim credible."""

        rubric = _ENGINE.induce(_bare_spec())
        dims = {c.dimension for c in rubric.criteria}
        for required in ("correctness", "generalization", "rigor", "integrity", "reporting"):
            self.assertIn(required, dims, f"synthesized rubric misses the {required} dimension")
        # Success must never be defined as "the desired result was obtained": the rubric
        # forbids outcome-fitting claims ...
        self.assertTrue(
            any("预期" in c for c in rubric.claims_to_avoid),
            f"the rubric must forbid outcome-fitting claims, got {rubric.claims_to_avoid}",
        )
        # ... and explicitly admits negative / inconclusive findings as valid results.
        self.assertTrue(
            any("阴性" in c.satisfaction_condition for c in rubric.criteria),
            "a correctly-framed negative result must be able to satisfy the rubric",
        )

    def test_unmeasurable_requirements_are_retained_as_blocked(self) -> None:
        """A requirement that cannot be checked here is kept + documented, never dropped."""

        rubric = _ENGINE.induce(_bare_spec())
        blocked = [c for c in rubric.criteria if c.blocked_reason]
        self.assertTrue(blocked, "a bare task must retain blocked requirements")
        for c in blocked:
            self.assertTrue(c.requirement, "a blocked criterion still states its requirement")

    def test_synthesis_is_deterministic(self) -> None:
        a = _ENGINE.induce(_bare_spec())
        b = _ENGINE.induce(_bare_spec())
        self.assertEqual(a.rubric_id, b.rubric_id)
        self.assertEqual(a.integrity_hash, b.integrity_hash)
        self.assertEqual(
            [c.criterion_id for c in a.criteria], [c.criterion_id for c in b.criteria]
        )


# --------------------------------------------------------------------------- #
# 2. Review: declared standard -> audit accuracy / completeness / scientificity #
# --------------------------------------------------------------------------- #
class RubricReviewTest(unittest.TestCase):
    def test_declared_standard_is_reviewed_not_synthesized(self) -> None:
        rubric = _ENGINE.induce(_titanic_spec())
        self.assertEqual(rubric.source, "reviewed")
        self.assertTrue(rubric.review.standard_provided)
        self.assertEqual(rubric.provided_standard["eval_metric"], "cv_accuracy")

    def test_inverted_direction_is_a_critical_accuracy_defect(self) -> None:
        spec = TaskSpec(
            task_id="bad.dir", eval_metric="eval_loss", direction="higher",
            baseline=0.5, gates={"threshold": 0.9},
        )
        review = _ENGINE.review(spec)
        crit = [f for f in review.findings if f.severity == "critical"]
        self.assertTrue(crit, "an inverted metric direction must be critical")
        self.assertEqual(crit[0].dimension, "accuracy")
        # And it must propose the machine-applicable correction.
        self.assertEqual(review.suggested_fixes.get("direction"), "lower")

    def test_gate_not_beating_baseline_is_critical(self) -> None:
        """A gate at or below the baseline makes 'success' meaningless."""

        spec = TaskSpec(
            task_id="bad.gate", eval_metric="accuracy", direction="higher",
            baseline=0.90, gates={"threshold": 0.85},
        )
        review = _ENGINE.review(spec)
        self.assertTrue(
            any(f.severity == "critical" and "基线" in f.message for f in review.findings)
        )

    def test_unreachable_gate_is_flagged(self) -> None:
        spec = TaskSpec(
            task_id="bad.ref", eval_metric="accuracy", direction="higher",
            baseline=0.7, reference=0.9, gates={"threshold": 0.95},
        )
        review = _ENGINE.review(spec)
        self.assertTrue(any("不可达" in f.message for f in review.findings))

    def test_missing_generalization_check_is_a_completeness_defect(self) -> None:
        spec = TaskSpec(
            task_id="c.gen", eval_metric="accuracy", direction="higher",
            baseline=0.7, gates={"threshold": 0.8}, eval_method="用交叉验证算准确率",
            type_config={"cv_folds": 5},
        )
        review = _ENGINE.review(spec)
        self.assertTrue(
            any(f.dimension == "completeness" and "留出" in f.message for f in review.findings)
        )

    def test_kaggle_eval_substring_does_not_fake_a_generalization_check(self) -> None:
        """Regression: a bare 'val' keyword matched 'kaggle_eval' and silently marked
        every kaggle task as generalization-checked."""

        spec = TaskSpec(
            task_id="c.substr", eval_metric="accuracy", direction="higher",
            baseline=0.7, gates={"threshold": 0.8},
            eval_method="平台内 kaggle_eval 执行交叉验证",
        )
        review = _ENGINE.review(spec)
        self.assertTrue(
            any(f.dimension == "completeness" and "留出" in f.message for f in review.findings)
        )

    def test_outcome_fitting_language_is_a_scientificity_defect(self) -> None:
        spec = TaskSpec(
            task_id="s.fit", eval_metric="accuracy", direction="higher",
            baseline=0.7, gates={"threshold": 0.8},
            eval_method="研究必须证明假设成立，指标必须得到提升",
        )
        review = _ENGINE.review(spec)
        self.assertTrue(
            any(
                f.dimension == "scientificity" and f.severity == "critical"
                for f in review.findings
            ),
            "defining success as 'the hypothesis must hold' must be critical",
        )

    def test_trivial_gate_is_flagged(self) -> None:
        spec = TaskSpec(
            task_id="s.triv", eval_metric="accuracy", direction="higher",
            baseline=0.3, gates={"threshold": 0.45},
        )
        review = _ENGINE.review(spec)
        self.assertTrue(any("随机" in f.message for f in review.findings))

    def test_scores_are_bounded_and_verdict_consistent(self) -> None:
        for spec in (_titanic_spec(), _bare_spec()):
            review = _ENGINE.review(spec)
            for v in (review.accuracy, review.completeness, review.scientificity, review.overall):
                self.assertGreaterEqual(v, 0.0)
                self.assertLessEqual(v, 1.0)
            self.assertIn(review.verdict, {"sound", "acceptable", "needs_work", "unusable"})


# --------------------------------------------------------------------------- #
# 3. Executable evaluation of criteria                                         #
# --------------------------------------------------------------------------- #
class RubricCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rubric = _ENGINE.induce(_titanic_spec())

    def _ctx(self, **over):
        ctx = {
            "metrics": {"accuracy": 0.845, "heldout_accuracy": 0.82},
            "report_ref": "kaggle-eval://s1",
            "real_eval": True,
            "artifact_types": ["eval_report"],
            "config": {"cv_folds": 5},
        }
        ctx.update(over)
        return ctx

    def test_good_result_passes_every_machine_check(self) -> None:
        cons = evaluate_criteria(self.rubric.criteria, self._ctx(), judge=lambda a, c, e: 0.9)
        for c in cons:
            self.assertEqual(c["status"], "verified", f"{c['criterion_id']}: {c['note']}")
        self.assertTrue(summarize(cons)["all_satisfied"])

    def test_overfitting_is_caught_by_the_generalization_criterion(self) -> None:
        """The exact failure a single aggregate metric hides: CV looks great, held-out
        collapses."""

        ctx = self._ctx(metrics={"accuracy": 0.95, "heldout_accuracy": 0.62})
        cons = evaluate_criteria(self.rubric.criteria, ctx, judge=lambda a, c, e: 0.9)
        gen = next(c for c in cons if c["dimension"] == "generalization")
        self.assertEqual(gen["status"], "conflict")
        self.assertIn("gap", gen["note"])

    def test_gate_miss_is_a_conflict_not_a_pass(self) -> None:
        ctx = self._ctx(metrics={"accuracy": 0.80, "heldout_accuracy": 0.79})
        cons = evaluate_criteria(self.rubric.criteria, ctx, judge=lambda a, c, e: 0.9)
        threshold_c = next(c for c in cons if c["check_kind"] == "metric_threshold")
        self.assertEqual(threshold_c["status"], "conflict")

    def test_mocked_eval_fails_the_integrity_criterion(self) -> None:
        ctx = self._ctx(report_ref=None, real_eval=False)
        cons = evaluate_criteria(self.rubric.criteria, ctx, judge=lambda a, c, e: 0.9)
        integ = next(c for c in cons if c["check_kind"] == "real_eval")
        self.assertNotEqual(integ["status"], "verified")

    def test_checks_are_evaluated_programmatically_not_by_judge(self) -> None:
        cons = evaluate_criteria(self.rubric.criteria, self._ctx(), judge=lambda a, c, e: 0.9)
        programmatic = [c for c in cons if str(c["evaluated_by"]).startswith("programmatic")]
        self.assertGreaterEqual(
            len(programmatic), 5,
            "most criteria of a platform-executable task must be machine-checked",
        )

    def test_missing_evidence_is_reported_but_not_charged_to_the_research(self) -> None:
        """A criterion the platform could not decide must be reported as unmet yet
        excluded from the weighted score (no misattribution)."""

        ctx = self._ctx(metrics={"accuracy": 0.845}, config={})  # no held-out, no folds
        cons = evaluate_criteria(self.rubric.criteria, ctx, judge=lambda a, c, e: 0.9)
        undecided = [c for c in cons if not c["evaluable"]]
        self.assertTrue(undecided)
        for c in undecided:
            self.assertNotEqual(c["status"], "verified")
        s = summarize(cons)
        self.assertFalse(s["all_satisfied"])
        self.assertEqual(s["not_evaluable"], len(undecided))
        # The scalar is computed over decided criteria only, so it stays high.
        self.assertGreater(s["weighted_score"], 0.8)

    def test_blocked_criterion_can_never_be_verified(self) -> None:
        rubric = _ENGINE.induce(_bare_spec())
        # Feed a context that would satisfy everything if blockers were ignored.
        ctx = self._ctx()
        cons = evaluate_criteria(rubric.criteria, ctx, judge=lambda a, c, e: 1.0)
        for c in cons:
            if c["blocked_reason"]:
                self.assertNotEqual(c["status"], "verified")


# --------------------------------------------------------------------------- #
# 4. Isolation: the inner loop may read the rubric but never produce it        #
# --------------------------------------------------------------------------- #
class RubricIsolationTest(unittest.TestCase):
    def test_inner_loop_cannot_invoke_the_rubric_capability(self) -> None:
        for cap in ("layer_12_rubric_induction", "rubric_induction", "rubric"):
            with self.assertRaises(ValueError, msg=f"{cap} must be reserved"):
                assert_inner_capability_allowed(cap)

    def test_operators_cannot_run_on_the_rubric_stage(self) -> None:
        self.assertIn("layer_12_rubric_induction", INNER_LOOP_FORBIDDEN_CALLERS)

    def test_rubric_capability_is_registered_and_non_infra(self) -> None:
        reg = default_capability_registry()
        cap = reg.resolve("layer_12_rubric_induction")
        self.assertIsNotNone(cap)
        self.assertFalse(cap.is_infra, "the rubric stage is not one of the ten R&D layers")
        # Still discoverable for the control plane / observability.
        self.assertIn(
            "layer_12_rubric_induction",
            {c["capability_id"] for c in reg.list_all_capabilities()},
        )

    def test_http_capability_endpoint_rejects_the_rubric_capability(self) -> None:
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        client = TestClient(create_app(ControlPlaneService()))
        run = client.post(
            "/workflow-runs",
            json={
                "program_id": "p", "run_type": "standard_research",
                "entry_stage": "03_eval", "target_id": "m1",
                "objective_snapshot": {},
            },
        ).json()
        client.post(f"/workflow-runs/{run['run_id']}/start")
        resp = client.post(
            f"/workflow-runs/{run['run_id']}/capabilities/layer_12_rubric_induction/run",
            json={"params": {}},
        )
        self.assertEqual(resp.status_code, 400)


# --------------------------------------------------------------------------- #
# 5. Audit integration: rubric criteria drive the verdict                     #
# --------------------------------------------------------------------------- #
class _FakeSDK:
    """Minimal SDK stand-in (mirrors the pattern in test_dual_loop)."""

    def __init__(self, run) -> None:
        self._run = run
        self.events: list = []

    def load_object(self, ref: str):
        if ref.startswith("run:"):
            return self._run
        return []

    def emit_event(self, event) -> None:
        self.events.append(event)

    def publish_artifact(self, payload, schema_version, metadata=None):
        self.metadata = metadata or {}
        return type("A", (), {"artifact_id": "art-1"})()

    def record_metric(self, *a, **k) -> None:
        pass


def _fake_run(snapshot: dict):
    return type("R", (), {"objective_snapshot": snapshot, "target_id": "t"})()


class RubricAuditIntegrationTest(unittest.TestCase):
    def _audit(self, metrics: dict, rubric=None, threshold: float = 0.8):
        run = _fake_run({"goal": "classify titanic"})
        sdk = _FakeSDK(run)
        stage = StageRun(
            stage_run_id="s1", run_id="r1", stage_code="layer_11_external_audit",
            executor_family="capability", gate_result=GateResult.PENDING,
            status=StageStatus.QUEUED,
        )
        audit_input = {
            "objective": "classify titanic",
            "result_metrics": metrics,
            "result_report_ref": "kaggle-eval://s0",
            "result_gate_passed": metrics.get("accuracy", 0) >= 0.82,
            "result_real_eval": True,
            "prior_audits": [],
            "constraints": [],
            "result_config": {"cv_folds": 5},
        }
        if rubric is not None:
            audit_input["rubric"] = rubric.model_dump(mode="json")
        return AuditExecutor().execute(
            stage, sdk,
            {"objective": "classify titanic", "audit_input": audit_input,
             "threshold": threshold, "judge": lambda a, c, e: 0.9},
        ), sdk

    def test_rubric_criteria_appear_as_audit_constraints(self) -> None:
        rubric = _ENGINE.induce(_titanic_spec())
        result, sdk = self._audit({"accuracy": 0.845, "heldout_accuracy": 0.83}, rubric)
        ids = {c.get("criterion_id") for c in result.event.constraints if c.get("criterion_id")}
        self.assertEqual(ids, {c.criterion_id for c in rubric.criteria})
        self.assertEqual(result.gate_result, GateResult.PASSED)
        self.assertTrue(sdk.metadata["rubric_summary"]["all_satisfied"])

    def test_overfitting_vetoes_accept_despite_high_confidence(self) -> None:
        """The dilution failure mode: many easy passes must not average away a hard
        generalization failure."""

        rubric = _ENGINE.induce(_titanic_spec())
        result, _ = self._audit({"accuracy": 0.95, "heldout_accuracy": 0.60}, rubric)
        self.assertEqual(result.gate_result, GateResult.FAILED)
        self.assertFalse(result.event.gate_passed)
        self.assertTrue(
            any("硬性判定条目未通过" in u for u in result.event.unresolved_claims),
            result.event.unresolved_claims,
        )

    def test_gate_miss_vetoes_accept(self) -> None:
        result, _ = self._audit({"accuracy": 0.70, "heldout_accuracy": 0.69})
        self.assertEqual(result.gate_result, GateResult.FAILED)

    def test_audit_without_rubric_keeps_legacy_behaviour(self) -> None:
        result, _ = self._audit({"accuracy": 0.90, "heldout_accuracy": 0.89})
        self.assertEqual(result.gate_result, GateResult.PASSED)
        self.assertFalse(
            any(c.get("criterion_id") for c in result.event.constraints),
            "no rubric supplied -> no rubric constraints",
        )

    def test_malformed_rubric_does_not_break_the_audit(self) -> None:
        run = _fake_run({"goal": "g"})
        sdk = _FakeSDK(run)
        stage = StageRun(
            stage_run_id="s2", run_id="r1", stage_code="layer_11_external_audit",
            executor_family="capability", gate_result=GateResult.PENDING,
            status=StageStatus.QUEUED,
        )
        result = AuditExecutor().execute(
            stage, sdk,
            {"objective": "g", "threshold": 0.8, "judge": lambda a, c, e: 0.9,
             "audit_input": {
                 "objective": "g", "result_metrics": {"accuracy": 0.9},
                 "result_report_ref": "kaggle-eval://x", "result_gate_passed": True,
                 "result_real_eval": True, "prior_audits": [], "constraints": [],
                 "rubric": {"not": "a rubric"},
             }},
        )
        self.assertIsNotNone(result.event)  # the audit still produced a verdict


# --------------------------------------------------------------------------- #
# 6. Orchestrator wiring (end-to-end)                                         #
# --------------------------------------------------------------------------- #
class RubricOrchestrationTest(unittest.TestCase):
    def _run(self, svc):
        run = svc.create_workflow_run(CreateWorkflowRunRequest(
            program_id="rb", run_type="standard_research", entry_stage="inner_research",
            target_id="platform.titanic",
            objective_snapshot={
                "benchmark_task_id": "platform.titanic", "name": "Titanic",
                "goal": "优化 cv_accuracy", "eval_metric": "cv_accuracy",
                "direction": "higher", "baseline": 0.78, "gates": {"threshold": 0.82},
                "dataset_desc": "Titanic survival", "harness": "kaggle_eval",
                "supported_by_platform": True,
                "eval_method": "平台内 kaggle_eval 交叉验证并在留出集复核",
                "config": {"inner_loop": {"cv_folds": 5}},
            },
        ))
        svc.start_workflow_run(run.run_id)
        return run

    def test_dual_loop_establishes_the_rubric_before_the_inner_loop(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = self._run(svc)
        summary = orch.run_dual_loop(
            run.run_id, inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "model": "gbm", "data_dir": _DATA,
                          "cv_folds": 5, "threshold": 0.82},
            audit_params={"threshold": 0.8}, max_outer_iters=1,
        )
        stages = [s["stage"] for s in summary["steps"]]
        self.assertEqual(stages[0], "rubric", "the rubric must precede every inner loop")
        self.assertIn("rubric_summary", summary)
        self.assertEqual(summary["rubric_summary"]["source"], "reviewed")
        # The event log proves the standard predates the results it grades.
        types = [e["event_type"] for e in svc.list_events(run.run_id)]
        self.assertLess(types.index("rubric_synthesized"), types.index("eval_completed"))

    def test_rubric_can_be_disabled(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = self._run(svc)
        summary = orch.run_dual_loop(
            run.run_id, inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "model": "gbm", "data_dir": _DATA,
                          "threshold": 0.82},
            audit_params={"threshold": 0.8}, max_outer_iters=1, rubric_stage=False,
        )
        self.assertNotIn("rubric", [s["stage"] for s in summary["steps"]])
        self.assertNotIn("rubric_summary", summary)

    def test_inner_loop_receives_the_rubric_as_a_read_only_contract(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = self._run(svc)
        seen: list[dict] = []
        real = orch.run_capability

        def spy(run_id, capability_id, params=None, agent=None):
            if capability_id == "kaggle_eval":
                seen.append(dict(params or {}))
            return real(run_id, capability_id, params, agent)

        orch.run_capability = spy  # type: ignore[method-assign]
        orch.run_dual_loop(
            run.run_id, inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "model": "gbm", "data_dir": _DATA,
                          "cv_folds": 5, "threshold": 0.82},
            audit_params={"threshold": 0.8}, max_outer_iters=1,
        )
        self.assertTrue(seen)
        self.assertIn("rubric_context", seen[0])
        self.assertIn("不可修改", seen[0]["rubric_context"])

    def test_rubric_not_visible_to_inner_when_disabled(self) -> None:
        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = self._run(svc)
        seen: list[dict] = []
        real = orch.run_capability

        def spy(run_id, capability_id, params=None, agent=None):
            if capability_id == "kaggle_eval":
                seen.append(dict(params or {}))
            return real(run_id, capability_id, params, agent)

        orch.run_capability = spy  # type: ignore[method-assign]
        orch.run_dual_loop(
            run.run_id, inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "model": "gbm", "data_dir": _DATA,
                          "threshold": 0.82},
            audit_params={"threshold": 0.8}, max_outer_iters=1,
            rubric_visible_to_inner=False,
        )
        self.assertTrue(seen)
        self.assertNotIn("rubric_context", seen[0])

    def test_rubric_prelude_respects_the_budget(self) -> None:
        """An exhausted budget must stop the run before the rubric spends a call."""

        svc = ControlPlaneService()
        orch = ClosedLoopOrchestrator(svc, mode="scripted")
        run = self._run(svc)
        summary = orch.run_dual_loop(
            run.run_id, inner_capability="kaggle_eval",
            inner_params={"preset": "titanic", "data_dir": _DATA},
            audit_params={"threshold": 0.8}, max_outer_iters=2,
            budget={"max_seconds": 0},
        )
        self.assertEqual(summary["status"], "exited_budget")
        self.assertEqual(summary["steps"], [])

    def test_effective_config_keeps_the_gate_criterion_checkable(self) -> None:
        """The run's real threshold must reach the rubric even when the snapshot omits it,
        otherwise the gate criterion silently degrades to a blocked one."""

        spec = build_task_spec(
            {"effective_config": {"threshold": 0.82, "cv_folds": 5}},
            {"goal": "g", "eval_metric": "accuracy", "direction": "higher"},
        )
        self.assertEqual(spec.target_value, 0.82)
        self.assertEqual(spec.cv_folds, 5)

    def test_legacy_target_threshold_is_honoured(self) -> None:
        spec = build_task_spec({}, {"goal": "g", "target_threshold": 0.75})
        self.assertEqual(spec.target_value, 0.75)


# --------------------------------------------------------------------------- #
# 7. Registration-time review                                                 #
# --------------------------------------------------------------------------- #
class RubricRegistrationTest(unittest.TestCase):
    def test_review_registration_standard_returns_review_and_preview(self) -> None:
        out = bench_registry.review_registration_standard(
            "tabular_classification",
            {"name": "t", "eval_metric": "accuracy", "direction": "higher"},
        )
        self.assertIsNotNone(out["review"])
        self.assertIsNotNone(out["rubric_preview"])
        self.assertGreater(out["rubric_preview"]["criteria_count"], 0)

    def test_review_never_raises_on_garbage_input(self) -> None:
        out = bench_registry.review_registration_standard("nope_not_a_type", {})
        self.assertIn("review", out)  # degraded, not crashed

    def test_validate_endpoint_reports_form_and_standard_separately(self) -> None:
        """A form can be perfectly VALID while its evaluation standard is weak — the two
        verdicts must not be conflated."""

        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        client = TestClient(create_app(ControlPlaneService()))
        resp = client.post("/benchmark-tasks/validate", json={
            "task_type": "tabular_classification",
            "values": {"name": "t1", "eval_metric": "accuracy", "direction": "higher",
                       "data_dir": "/tmp/nonexistent-rubric-test", "target_col": "y"},
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["valid"])          # the form is fine
        self.assertIsNotNone(body["review"])
        self.assertNotEqual(body["review"]["verdict"], "sound")  # the standard is not

    def test_task_rubric_endpoint(self) -> None:
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        client = TestClient(create_app(ControlPlaneService()))
        resp = client.get("/benchmark-tasks/platform.titanic/rubric")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["source"], "reviewed")
        self.assertTrue(body["criteria"])
        self.assertEqual(client.get("/benchmark-tasks/nope.nope/rubric").status_code, 404)

    def test_run_rubric_endpoint_reports_criterion_verdicts(self) -> None:
        from fastapi.testclient import TestClient

        from safety_auto_research.control_plane.api import create_app

        client = TestClient(create_app(ControlPlaneService()))
        launched = client.post(
            "/benchmark-tasks/platform.titanic/launch?auto_run=false", json={},
        ).json()
        resp = client.get(f"/workflow-runs/{launched['run_id']}/rubric")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Not launched -> no rubric event yet, but the endpoint must answer cleanly.
        self.assertEqual(body["run_id"], launched["run_id"])
        self.assertEqual(body["iterations"], [])


if __name__ == "__main__":
    unittest.main()
