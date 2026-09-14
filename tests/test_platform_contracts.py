from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from safety_auto_research.platform_contracts.models import Artifact
from safety_auto_research.platform_contracts.models import ArtifactType
from safety_auto_research.platform_contracts.models import ArtifactPublishedEvent
from safety_auto_research.platform_contracts.models import ApprovalRequiredEvent
from safety_auto_research.platform_contracts.models import ApprovalResolvedEvent
from safety_auto_research.platform_contracts.models import AttackCampaign
from safety_auto_research.platform_contracts.models import AttackCompletedEvent
from safety_auto_research.platform_contracts.models import BasePlatformEvent
from safety_auto_research.platform_contracts.models import DatasetRelease
from safety_auto_research.platform_contracts.models import DecisionRecord
from safety_auto_research.platform_contracts.models import DecisionType
from safety_auto_research.platform_contracts.models import DecisionRecordedEvent
from safety_auto_research.platform_contracts.models import EvalCompletedEvent
from safety_auto_research.platform_contracts.models import EvalSuite
from safety_auto_research.platform_contracts.models import EventType
from safety_auto_research.platform_contracts.models import LessonCard
from safety_auto_research.platform_contracts.models import LessonPromotedEvent
from safety_auto_research.platform_contracts.models import ModelVersion
from safety_auto_research.platform_contracts.models import PolicyPack
from safety_auto_research.platform_contracts.models import ResearchProgram
from safety_auto_research.platform_contracts.models import SafetyTarget
from safety_auto_research.platform_contracts.models import StageRun
from safety_auto_research.platform_contracts.models import StageStatus
from safety_auto_research.platform_contracts.models import StageStatusChangedEvent
from safety_auto_research.platform_contracts.models import WorkflowRun
from safety_auto_research.platform_contracts.models import WorkflowStatus
from safety_auto_research.platform_contracts.models import WorkflowStatusChangedEvent
from safety_auto_research.platform_contracts.models import export_contract_schema_files
from safety_auto_research.platform_contracts.models import export_contract_schemas
from safety_auto_research.platform_contracts.models import validate_decision_record_contract
from safety_auto_research.platform_contracts.models import validate_stage_status_transition
from safety_auto_research.platform_contracts.models import validate_workflow_status_transition


class PlatformContractsTest(unittest.TestCase):
    def test_export_contract_schemas_contains_all_core_objects(self) -> None:
        schemas = export_contract_schemas()

        self.assertEqual(
            set(schemas["objects"]),
            {
                "ResearchProgram",
                "SafetyTarget",
                "WorkflowRun",
                "StageRun",
                "Artifact",
                "DatasetRelease",
                "ModelVersion",
                "EvalSuite",
                "AttackCampaign",
                "DecisionRecord",
                "LessonCard",
                "PolicyPack",
                "AuditReport",
                "ImprovementProposal",
                "HypothesisNode",
                "ExperienceEntry",
                # Rubric stage (layer_12): the run's executable grading contract and the
                # three-dimensional review of a task's declared evaluation standard.
                "RubricGoal",
                "RubricCriterion",
                "RubricFinding",
                "RubricReview",
                "ExecutableRubric",
            },
        )
        self.assertEqual(set(schemas["events"]), {
            "BasePlatformEvent",
            "WorkflowStatusChangedEvent",
            "StageStatusChangedEvent",
            "DecisionRecordedEvent",
            "ArtifactPublishedEvent",
            "ApprovalRequiredEvent",
            "ApprovalResolvedEvent",
            "EvalCompletedEvent",
            "AttackCompletedEvent",
            "LessonPromotedEvent",
            "AuditCompletedEvent",
            "AuditFollowupEvent",
            "RubricSynthesizedEvent",
            "ImprovementAppliedEvent",
            "AgentStepEvent",
            "DebugEvent",
        })
        self.assertEqual(schemas["objects"]["Artifact"]["title"], "Artifact")

    def test_export_contract_schema_files_writes_shared_json_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            manifest = export_contract_schema_files(Path(tmpdir))

            self.assertTrue((Path(tmpdir) / "manifest.json").exists())
            self.assertTrue((Path(tmpdir) / "objects" / "Artifact.json").exists())
            self.assertTrue((Path(tmpdir) / "events" / "WorkflowStatusChangedEvent.json").exists())
            self.assertIn("objects", manifest)
            self.assertIn("events", manifest)

    def test_core_objects_can_be_instantiated_with_minimal_valid_payloads(self) -> None:
        now = datetime.now(timezone.utc)

        program = ResearchProgram(
            program_id="prog-001",
            name="Safety RnD Core",
            domain="general-ai-safety",
            owner="research-platform",
            goal_statement="Improve safety robustness under adversarial pressure.",
            risk_tier="high",
            budget_policy_ref="budget.default",
            default_policy_pack_ref="policy.default",
            status="active",
        )
        target = SafetyTarget(
            target_id="target-001",
            target_type="agent_guard",
            modality="text",
            task_family="prompt-injection-defense",
            capabilities=["injection-detection", "tool-guard"],
            threat_model_refs=["threat.prompt-injection.v1"],
            acceptance_policy_ref="accept.default",
        )
        run = WorkflowRun(
            run_id="run-001",
            program_id=program.program_id,
            run_type="adversarial_hardening",
            entry_stage="10_adversarial_data_generation",
            target_id=target.target_id,
            objective_snapshot={"metric": "robustness"},
            requested_outcomes=["reduce_asr", "preserve_retention"],
            status="running",
            started_at=now,
        )
        stage = StageRun(
            stage_run_id="stage-001",
            run_id=run.run_id,
            stage_code="10_adversarial_data_generation",
            input_refs=["model:model-001"],
            output_refs=[],
            executor_family="llama",
            reviewer_family="gpt",
            gate_result="pending",
            status="queued",
            retry_count=0,
        )
        artifact = Artifact(
            artifact_id="artifact-001",
            artifact_type=ArtifactType.ATTACK_REPORT,
            uri="s3://contracts/artifact-001.json",
            schema_version="1.0.0",
            producer_ref=stage.stage_run_id,
            lineage_parent_ids=[run.run_id],
            integrity_hash="sha256:abc123",
            visibility="restricted",
            compliance_tags=["fingerprinted", "redteam"],
        )
        dataset = DatasetRelease(
            dataset_id="dataset-001",
            artifact_ref=artifact.artifact_id,
            source_mix={"badcase": 0.4, "replay": 0.6},
            label_schema_version="safety-labels.v2",
            pii_status="redacted",
            split_policy="group_time_isolation",
            retention_policy="retain_core_capabilities",
            quality_report_ref="artifact:quality-001",
            leakage_report_ref="artifact:leak-001",
        )
        model = ModelVersion(
            model_id="model-001",
            base_model="qwen3-8b",
            adapter_stack=["lora:safety-v2"],
            training_recipe_ref="recipe:adv-hardening-v1",
            safety_capabilities=["refusal", "tool-abuse-detection"],
            artifact_ref="artifact:model-001",
            registry_status="candidate",
        )
        suite = EvalSuite(
            eval_suite_id="eval-001",
            suite_type="robustness",
            task_refs=["task:jailbreak", "task:prompt-injection"],
            metric_defs={"asr": "lower_better", "retention": "higher_better"},
            pass_thresholds={"asr": 0.08, "retention": 0.95},
            sandbox_policy_ref="sandbox.harbor",
            canary_policy_ref="canary.default",
        )
        decision = DecisionRecord(
            decision_id="decision-001",
            run_id=run.run_id,
            decision_type=DecisionType.REVISIT,
            target_stage="05_data_evaluation_cleaning",
            reason_codes=["high_asr", "low_retention"],
            evidence_refs=[artifact.artifact_id, suite.eval_suite_id],
            policy_hits=["cross_model_review_required"],
            approved_by=["policy-engine"],
        )
        lesson = LessonCard(
            lesson_id="lesson-001",
            scope="platform",
            source_run_id=run.run_id,
            pattern_type="attack_pattern",
            applicable_stages=["10_adversarial_data_generation", "05_data_evaluation_cleaning"],
            confidence=0.83,
            payload_ref=artifact.artifact_id,
            prm_score=0.91,
        )
        policy = PolicyPack(
            policy_pack_id="policy.default",
            domain="general-ai-safety",
            rules={"cross_model_required": True},
            required_approvals=["high_risk_redteam"],
            model_separation_policy="executor_reviewer_distinct_family",
            data_compliance_policy="pii_redaction_required",
            redteam_constraints="fingerprint_only_for_dangerous_payloads",
        )

        self.assertEqual(program.program_id, "prog-001")
        self.assertEqual(target.target_type, "agent_guard")
        self.assertEqual(run.run_type, "adversarial_hardening")
        self.assertEqual(stage.gate_result, "pending")
        self.assertEqual(artifact.artifact_type, ArtifactType.ATTACK_REPORT)
        self.assertEqual(dataset.pii_status, "redacted")
        self.assertEqual(model.registry_status, "candidate")
        self.assertEqual(suite.suite_type, "robustness")
        self.assertEqual(decision.decision_type, DecisionType.REVISIT)
        self.assertAlmostEqual(lesson.prm_score, 0.91)
        self.assertEqual(policy.policy_pack_id, "policy.default")

    def test_unified_event_models_can_be_instantiated(self) -> None:
        now = datetime.now(timezone.utc)

        base_event = BasePlatformEvent(
            event_id="evt-001",
            event_type=EventType.ARTIFACT_PUBLISHED,
            run_id="run-001",
            occurred_at=now,
        )
        workflow_event = WorkflowStatusChangedEvent(
            event_id="evt-002",
            event_type=EventType.WORKFLOW_STARTED,
            run_id="run-001",
            occurred_at=now,
            from_status=WorkflowStatus.REQUESTED,
            to_status=WorkflowStatus.RUNNING,
        )
        stage_event = StageStatusChangedEvent(
            event_id="evt-003",
            event_type=EventType.STAGE_STARTED,
            run_id="run-001",
            occurred_at=now,
            stage_run_id="stage-001",
            from_status=StageStatus.QUEUED,
            to_status=StageStatus.RUNNING,
        )
        decision_event = DecisionRecordedEvent(
            event_id="evt-004",
            event_type=EventType.DECISION_ISSUED,
            run_id="run-001",
            occurred_at=now,
            decision_id="decision-001",
            decision_type=DecisionType.REVISIT,
        )
        artifact_event = ArtifactPublishedEvent(
            event_id="evt-005",
            event_type=EventType.ARTIFACT_PUBLISHED,
            run_id="run-001",
            occurred_at=now,
            artifact_id="artifact-001",
            artifact_type=ArtifactType.EVAL_REPORT,
        )

        self.assertEqual(base_event.event_type, EventType.ARTIFACT_PUBLISHED)
        self.assertEqual(workflow_event.to_status, WorkflowStatus.RUNNING)
        self.assertEqual(stage_event.to_status, StageStatus.RUNNING)
        self.assertEqual(decision_event.decision_type, DecisionType.REVISIT)
        self.assertEqual(artifact_event.artifact_type, ArtifactType.EVAL_REPORT)

    def test_new_event_models_can_be_instantiated(self) -> None:
        now = datetime.now(timezone.utc)

        approval_required = ApprovalRequiredEvent(
            event_id="evt-100",
            event_type=EventType.APPROVAL_REQUIRED,
            run_id="run-001",
            occurred_at=now,
            approval_id="apr-001",
            subject_type="attack_campaign",
            subject_ref="campaign-001",
            reason="high_asr_success_rate",
            policy_ref="policy.redteam.high_risk",
            required_roles=["safety_engineer", "governance_owner"],
            risk_tier="critical",
        )
        approval_resolved = ApprovalResolvedEvent(
            event_id="evt-101",
            event_type=EventType.APPROVAL_RESOLVED,
            run_id="run-001",
            occurred_at=now,
            approval_id="apr-001",
            resolution="approved",
            resolved_by="governance_owner",
            decision_ref="decision-020",
            note="sandbox isolated, fingerprint only",
        )
        eval_completed = EvalCompletedEvent(
            event_id="evt-102",
            event_type=EventType.EVAL_COMPLETED,
            run_id="run-001",
            occurred_at=now,
            eval_suite_id="eval-001",
            stage_run_id="stage-001",
            passed=True,
            metrics={"asr": 0.05, "retention": 0.97},
            gate_passed=True,
            report_ref="artifact:eval-report-001",
        )
        attack_completed = AttackCompletedEvent(
            event_id="evt-103",
            event_type=EventType.ATTACK_COMPLETED,
            run_id="run-001",
            occurred_at=now,
            campaign_id="campaign-001",
            target_model_id="model-001",
            success_rate=0.12,
            total_attempts=500,
            successful_attempts=60,
            retention_rate=0.96,
            forgetting_rate=0.04,
            vulnerability_patterns=["prompt_injection", "jailbreak"],
            report_ref="artifact:attack-report-001",
        )
        lesson_promoted = LessonPromotedEvent(
            event_id="evt-104",
            event_type=EventType.LESSON_PROMOTED,
            run_id="run-001",
            occurred_at=now,
            lesson_id="lesson-002",
            source_run_id="run-001",
            scope="platform",
            pattern_type="attack_pattern",
            prm_score=0.9,
            confidence=0.85,
            applicable_stages=["10_adversarial_data_generation", "05_data_evaluation_cleaning"],
            payload_ref="artifact:lesson-002",
        )

        self.assertEqual(approval_required.risk_tier, "critical")
        self.assertEqual(approval_resolved.resolution, "approved")
        self.assertTrue(eval_completed.gate_passed)
        self.assertEqual(attack_completed.successful_attempts, 60)
        self.assertEqual(lesson_promoted.pattern_type, "attack_pattern")

    def test_workflow_and_stage_transition_contracts_validate(self) -> None:
        self.assertTrue(
            validate_workflow_status_transition(
                WorkflowStatus.REQUESTED,
                WorkflowStatus.RUNNING,
            )
        )
        self.assertTrue(
            validate_stage_status_transition(
                StageStatus.QUEUED,
                StageStatus.RUNNING,
            )
        )

        with self.assertRaises(ValueError):
            validate_workflow_status_transition(
                WorkflowStatus.SUCCEEDED,
                WorkflowStatus.RUNNING,
            )

        with self.assertRaises(ValueError):
            validate_stage_status_transition(
                StageStatus.SUCCEEDED,
                StageStatus.QUEUED,
            )

    def test_decision_record_contract_rules_are_enforced(self) -> None:
        revisit = DecisionRecord(
            decision_id="decision-010",
            run_id="run-010",
            decision_type=DecisionType.REVISIT,
            target_stage="05_data_evaluation_cleaning",
            reason_codes=["metric_regression"],
            evidence_refs=[],
            policy_hits=[],
            approved_by=[],
        )
        validate_decision_record_contract(revisit)

        invalid_exit = DecisionRecord(
            decision_id="decision-011",
            run_id="run-011",
            decision_type=DecisionType.EXIT_SUCCESS,
            target_stage="05_data_evaluation_cleaning",
            reason_codes=["done"],
            evidence_refs=[],
            policy_hits=[],
            approved_by=[],
        )
        with self.assertRaises(ValueError):
            validate_decision_record_contract(invalid_exit)

    def test_invalid_decision_type_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            DecisionRecord(
                decision_id="decision-002",
                run_id="run-002",
                decision_type="reroute",
                target_stage="09_self_iterative_evolution",
                reason_codes=["invalid_decision"],
                evidence_refs=[],
                policy_hits=[],
                approved_by=[],
            )


if __name__ == "__main__":
    unittest.main()