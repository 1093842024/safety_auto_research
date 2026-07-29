from __future__ import annotations

import unittest

from safety_auto_research.control_plane.service import ConflictError
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.service import NotFoundError
from safety_auto_research.control_plane.store import Repository
from safety_auto_research.platform_contracts.enums import DecisionType
from safety_auto_research.platform_contracts.enums import StageStatus
from safety_auto_research.platform_contracts.enums import WorkflowStatus
from safety_auto_research.control_plane.schemas import CreateStageRunRequest
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.schemas import RecordDecisionRequest
from safety_auto_research.control_plane.schemas import RequestApprovalRequest
from safety_auto_research.control_plane.schemas import ResolveApprovalRequest
from safety_auto_research.control_plane.schemas import UpdateStageStatusRequest


class ControlPlaneServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = ControlPlaneService(Repository())

    def _create_run(self) -> str:
        run = self.svc.create_workflow_run(
            CreateWorkflowRunRequest(
                program_id="prog-1",
                run_type="adversarial_hardening",
                entry_stage="10_adversarial_data_generation",
                target_id="target-1",
            )
        )
        return run.run_id

    def test_workflow_lifecycle_states_and_events(self) -> None:
        run_id = self._create_run()
        run = self.svc.get_workflow_run(run_id)
        self.assertEqual(run.status, WorkflowStatus.REQUESTED)

        run = self.svc.start_workflow_run(run_id)
        self.assertEqual(run.status, WorkflowStatus.RUNNING)

        events = self.svc.list_events(run_id)
        types = [e["event_type"] for e in events]
        self.assertIn("workflow_requested", types)
        self.assertIn("workflow_started", types)

    def test_invalid_workflow_transition_is_rejected(self) -> None:
        # A requested run cannot have stages created (must be running first).
        run_id = self._create_run()
        with self.assertRaises(ConflictError):
            self.svc.create_stage_run(
                run_id,
                CreateStageRunRequest(stage_code="05", executor_family="llama"),
            )
        # cancel is a *valid* transition (requested -> cancelled); assert it succeeds.
        run = self.svc.cancel_workflow_run(run_id)
        self.assertEqual(run.status, WorkflowStatus.CANCELLED)

    def test_stage_state_machine_and_event_emission(self) -> None:
        run_id = self._create_run()
        self.svc.start_workflow_run(run_id)
        stage = self.svc.create_stage_run(
            run_id,
            CreateStageRunRequest(stage_code="10", executor_family="llama", reviewer_family="gpt"),
        )
        self.assertEqual(stage.status, StageStatus.QUEUED)

        stage = self.svc.update_stage_status(
            stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.RUNNING)
        )
        self.assertEqual(stage.status, StageStatus.RUNNING)

        stage = self.svc.update_stage_status(
            stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.SUCCEEDED)
        )
        self.assertEqual(stage.status, StageStatus.SUCCEEDED)

        types = [e["event_type"] for e in self.svc.list_events(run_id)]
        self.assertIn("stage_queued", types)
        self.assertIn("stage_started", types)
        self.assertIn("gate_passed", types)

    def test_invalid_stage_transition_is_rejected(self) -> None:
        run_id = self._create_run()
        self.svc.start_workflow_run(run_id)
        stage = self.svc.create_stage_run(
            run_id, CreateStageRunRequest(stage_code="10", executor_family="llama")
        )
        # queued -> succeeded skips running: not an allowed transition
        with self.assertRaises(ConflictError):
            self.svc.update_stage_status(
                stage.stage_run_id, UpdateStageStatusRequest(to_status=StageStatus.SUCCEEDED)
            )

    def test_approval_gate_flow(self) -> None:
        run_id = self._create_run()
        self.svc.start_workflow_run(run_id)
        _run, approval_id = self.svc.request_approval(
            run_id,
            RequestApprovalRequest(
                subject_type="attack_campaign",
                reason="high_asr",
                policy_ref="policy.redteam",
                required_roles=["governance"],
                risk_tier="critical",
            ),
        )
        self.assertEqual(self.svc.get_workflow_run(run_id).status, WorkflowStatus.WAITING_APPROVAL)

        _run, _aid = self.svc.resolve_approval(
            run_id, ResolveApprovalRequest(resolution="approved", resolved_by="gov-1")
        )
        self.assertEqual(self.svc.get_workflow_run(run_id).status, WorkflowStatus.RUNNING)
        self.assertEqual(approval_id, _aid)

        types = [e["event_type"] for e in self.svc.list_events(run_id)]
        self.assertIn("approval_required", types)
        self.assertIn("approval_resolved", types)

    def test_rejected_approval_fails_workflow(self) -> None:
        run_id = self._create_run()
        self.svc.start_workflow_run(run_id)
        self.svc.request_approval(
            run_id,
            RequestApprovalRequest(subject_type="x", reason="r", policy_ref="p"),
        )
        self.svc.resolve_approval(
            run_id, ResolveApprovalRequest(resolution="rejected", resolved_by="gov-1")
        )
        self.assertEqual(self.svc.get_workflow_run(run_id).status, WorkflowStatus.FAILED)

    def test_decision_record_contract(self) -> None:
        run_id = self._create_run()
        decision = self.svc.record_decision(
            run_id,
            RecordDecisionRequest(
                decision_type=DecisionType.REVISIT,
                target_stage="05_data_evaluation_cleaning",
                reason_codes=["high_asr"],
            ),
        )
        self.assertEqual(decision.decision_type, DecisionType.REVISIT)
        # revisit requires target_stage; exit_success must not set it
        with self.assertRaises(ValueError):
            self.svc.record_decision(
                run_id,
                RecordDecisionRequest(
                    decision_type=DecisionType.EXIT_SUCCESS,
                    target_stage="05",
                ),
            )

    def test_not_found_errors(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.get_workflow_run("run-missing")
        with self.assertRaises(NotFoundError):
            self.svc.get_stage_run("stage-missing")


if __name__ == "__main__":
    unittest.main()
