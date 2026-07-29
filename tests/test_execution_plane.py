"""Tests for the execution-plane adapters and the closed-loop orchestrator."""

from __future__ import annotations

import unittest

from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store import Repository
from safety_auto_research.execution_plane import ClosedLoopOrchestrator
from safety_auto_research.execution_plane import default_registry
from safety_auto_research.execution_plane.decision.router import IterationRouter
from safety_auto_research.platform_contracts.enums import DecisionType
from safety_auto_research.platform_contracts.enums import EventType
from safety_auto_research.platform_contracts.enums import RunType
from safety_auto_research.platform_contracts.events import AttackCompletedEvent
from safety_auto_research.platform_contracts.events import EvalCompletedEvent
from safety_auto_research.platform_contracts.events import LessonPromotedEvent


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = ControlPlaneService(Repository())
        self.orchestrator = ClosedLoopOrchestrator(self.svc)

    def _make_run(self, run_type: RunType = RunType.ADVERSARIAL_HARDENING) -> str:
        run = self.svc.create_workflow_run(
            type(
                "Req",
                (),
                {
                    "program_id": "prog-1",
                    "run_type": run_type,
                    "entry_stage": "10_adversarial_data_generation",
                    "target_id": "model:target-1",
                    "objective_snapshot": {"target_metric": "accuracy", "target_threshold": 0.8},
                    "requested_outcomes": ["harden"],
                },
            )()
        )
        self.svc.start_workflow_run(run.run_id)
        return run.run_id


class RegistryTest(_Base):
    def test_registry_resolves_all_three_stages(self) -> None:
        reg = default_registry()
        self.assertIsNotNone(reg.resolve("03_eval"))
        self.assertIsNotNone(reg.resolve("10_adversarial_data_generation"))
        self.assertIsNotNone(reg.resolve("08_result_analysis_experience"))
        self.assertIsNone(reg.resolve("nonexistent_stage"))


class EvalAdapterTest(_Base):
    def test_eval_emits_event_and_passes_gate(self) -> None:
        run_id = self._make_run()
        stage, result = self.orchestrator.dispatch_stage(
            run_id, "03_eval", params={"measured_metrics": {"accuracy": 0.95}}
        )
        self.assertIsInstance(result.event, EvalCompletedEvent)
        self.assertTrue(result.event.gate_passed)
        self.assertEqual(result.gate_result.value, "passed")
        # artifacts + metrics recorded
        self.assertEqual(len(self.svc._repo.artifacts), 1)
        self.assertTrue(self.svc._repo.list_metrics(run_id))

    def test_eval_gate_fail_routes_to_revisit_eval(self) -> None:
        run_id = self._make_run()
        self.orchestrator.dispatch_stage(run_id, "03_eval", params={"measured_metrics": {"accuracy": 0.5}})
        _route, decision = self.orchestrator.decide_and_record(
            run_id, self._last_eval_event(run_id)
        )
        self.assertEqual(decision.decision_type, DecisionType.REVISIT)
        self.assertEqual(decision.target_stage, "03_eval")

    def _last_eval_event(self, run_id: str) -> EvalCompletedEvent:
        for e in reversed(self.svc.list_events(run_id)):
            if e.get("event_type") == "eval_completed":
                return EvalCompletedEvent(**{k: v for k, v in e.items() if k in EvalCompletedEvent.model_fields})
        raise AssertionError("no eval event")


class AttackAdapterTest(_Base):
    def test_attack_emits_event_with_asr_and_retention(self) -> None:
        run_id = self._make_run()
        stage, result = self.orchestrator.dispatch_stage(
            run_id, "10_adversarial_data_generation", params={"target_model_id": "model:t1"}
        )
        self.assertIsInstance(result.event, AttackCompletedEvent)
        ev = result.event
        self.assertGreaterEqual(ev.success_rate, 0.0)
        self.assertLessEqual(ev.success_rate, 1.0)
        self.assertIsNotNone(ev.retention_rate)
        # router: high ASR -> revisit attack (continue hardening)
        _route, decision = self.orchestrator.decide_and_record(run_id, ev)
        self.assertIn(decision.decision_type, (DecisionType.REVISIT, DecisionType.CONTINUE))


class LessonAdapterTest(_Base):
    def test_lesson_promotes_and_reinjects(self) -> None:
        run_id = self._make_run()
        # seed some history so the lesson has a pattern to extract
        self.orchestrator.dispatch_stage(run_id, "10_adversarial_data_generation", params={"prior_patterns": ["prompt_injection"]})
        stage, result = self.orchestrator.dispatch_stage(run_id, "08_result_analysis_experience", params={})
        self.assertIsInstance(result.event, LessonPromotedEvent)
        self.assertEqual(len(self.svc._repo.list_lessons(run_id)), 1)
        self.assertEqual(result.event.pattern_type, "vulnerability")


class RouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = IterationRouter()

    def _run(self, run_type: RunType) -> object:
        # minimal WorkflowRun-shaped object carrying only the field the router reads
        return type("Run", (), {"run_type": run_type})()

    def test_eval_pass_in_hardening_routes_to_attack(self) -> None:
        ev = EvalCompletedEvent(run_id="r", eval_suite_id="s", passed=True, gate_passed=True, report_ref="x")
        route = self.router.decide(ev, self._run(RunType.ADVERSARIAL_HARDENING))
        self.assertEqual(route.decision_type, DecisionType.REVISIT)
        self.assertEqual(route.target_stage, "10_adversarial_data_generation")

    def test_attack_low_asr_routes_to_lesson(self) -> None:
        ev = AttackCompletedEvent(
            run_id="r", campaign_id="c", target_model_id="m", success_rate=0.05,
            total_attempts=100, successful_attempts=5, retention_rate=0.95,
            forgetting_rate=0.05, vulnerability_patterns=[], report_ref="x",
        )
        route = self.router.decide(ev, self._run(RunType.ADVERSARIAL_HARDENING))
        # CONTINUE carries no target_stage (contract); the orchestrator chains to the
        # lesson stage itself.
        self.assertEqual(route.decision_type, DecisionType.CONTINUE)
        self.assertIsNone(route.target_stage)

    def test_attack_forgetting_routes_to_data_clean(self) -> None:
        ev = AttackCompletedEvent(
            run_id="r", campaign_id="c", target_model_id="m", success_rate=0.4,
            total_attempts=100, successful_attempts=40, retention_rate=0.7,
            forgetting_rate=0.3, vulnerability_patterns=[], report_ref="x",
        )
        route = self.router.decide(ev, self._run(RunType.ADVERSARIAL_HARDENING))
        self.assertEqual(route.target_stage, "05_data_evaluation_cleaning")


class ClosedLoopTest(_Base):
    def test_full_closed_loop_completes_with_lesson(self) -> None:
        run_id = self._make_run()
        summary = self.orchestrator.run_closed_loop(run_id, max_rounds=3)
        self.assertEqual(summary["status"], "exited_converged")
        event_types = summary["events"]
        self.assertIn("eval_completed", event_types)
        self.assertIn("attack_completed", event_types)
        self.assertIn("lesson_promoted", event_types)
        self.assertTrue(summary["lessons"])  # at least one lesson promoted
        # the loop recorded a decision for each completed stage
        self.assertGreaterEqual(len(summary["decisions"]), 3)


if __name__ == "__main__":
    unittest.main()
