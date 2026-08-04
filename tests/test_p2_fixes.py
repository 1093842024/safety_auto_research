"""Regression tests for the P2 (secondary) defect fixes from the 2026-08-04 review.

Each test pins the *behavioral* contract of a fix so a future refactor cannot
silently revert it (the review's whole point: these were correctness bugs that
tests did not catch).
"""

import random
import unittest

from safety_auto_research.control_plane.evolution import Candidate, mutate
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.control_plane.store import Repository
from safety_auto_research.openmle_integration.inner_capability import (
    OperatorAuditViolation,
    assert_operator_inner_only,
)
from safety_auto_research.openmle_integration.reward_bridge import RewardConfig, reward_func
from safety_auto_research.platform_contracts.enums import ArtifactType, EventType, WorkflowStatus
from safety_auto_research.platform_contracts.objects import Artifact, StageRun


class P2_2_GuardAllowlistTest(unittest.TestCase):
    def test_none_caller_rejected(self):
        # P2-2: a missing caller_stage must fail-closed, not pass silently.
        with self.assertRaises(OperatorAuditViolation):
            assert_operator_inner_only("draft", None)

    def test_unknown_caller_rejected(self):
        with self.assertRaises(OperatorAuditViolation):
            assert_operator_inner_only("draft", "totally_unknown_stage")

    def test_allowed_inner_stages_pass(self):
        # Both canonical inner-loop stage names must be accepted.
        assert_operator_inner_only("draft", "inner")
        assert_operator_inner_only("draft", "inner_program_evolution")

    def test_forbidden_outer_stages_rejected(self):
        for stage in ("layer_11_external_audit", "layer_09_self_iterative_evolution"):
            with self.assertRaises(OperatorAuditViolation):
                assert_operator_inner_only("draft", stage)


class P2_1_RewardDirectionTest(unittest.TestCase):
    def test_lower_is_better_rewards_decrease(self):
        # P2-1: with maximize=False a decrease (0.5 < 0.8) is a real improvement.
        rc = reward_func(0.5, valid=True, prev_fitness=0.8, config=RewardConfig(maximize=False))
        self.assertGreater(rc.improvement, 0.0)

    def test_lower_is_better_no_reward_for_increase(self):
        rc = reward_func(0.8, valid=True, prev_fitness=0.5, config=RewardConfig(maximize=False))
        self.assertEqual(rc.improvement, 0.0)

    def test_default_maximize_preserved(self):
        # Default behavior (higher-is-better) must be unchanged.
        rc = reward_func(0.8, valid=True, prev_fitness=0.5, config=RewardConfig(maximize=True))
        self.assertGreater(rc.improvement, 0.0)
        rc2 = reward_func(0.5, valid=True, prev_fitness=0.8, config=RewardConfig(maximize=True))
        self.assertEqual(rc2.improvement, 0.0)


class P2_5_EditableSurfaceTest(unittest.TestCase):
    def test_only_params_keys_are_editable(self):
        # P2-5: the `or True` dead code used to include keys absent from params.
        parent = Candidate(
            candidate_id="c1", run_id="r", params={"a": 1}, generation=0
        )
        surface = {"a": (1, 2), "b": (3, 4)}  # "b" is NOT in parent.params
        child = mutate(parent, surface=surface, run_id="r", generation=1, rng=random.Random(0))
        self.assertIn("a", child.params)
        # The spurious key must never be injected.
        self.assertNotIn("b", child.params)


class P2_8_StatusEventTypeTest(unittest.TestCase):
    def test_terminal_status_maps_to_finished(self):
        # P2-8: terminal states must emit WORKFLOW_FINISHED, not REQUESTED.
        for st in (
            WorkflowStatus.FAILED,
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.EXITED_BUDGET,
            WorkflowStatus.EXITED_CONVERGED,
            WorkflowStatus.CANCELLED,
        ):
            self.assertEqual(
                ControlPlaneService._event_type_for_status(st), EventType.WORKFLOW_FINISHED
            )

    def test_running_maps_to_started(self):
        self.assertEqual(
            ControlPlaneService._event_type_for_status(WorkflowStatus.RUNNING),
            EventType.WORKFLOW_STARTED,
        )

    def test_requested_maps_to_requested(self):
        self.assertEqual(
            ControlPlaneService._event_type_for_status(WorkflowStatus.REQUESTED),
            EventType.WORKFLOW_REQUESTED,
        )


class P2_9_ArtifactNoSubstringLeakTest(unittest.TestCase):
    def _repo(self) -> Repository:
        repo = Repository()
        # A stage belonging to run "run_10" whose stage_run_id contains "run_1"
        # as a substring -- the exact shape that the old `in str(...)` scan leaked.
        stage = StageRun(
            stage_run_id="run_10_stage_a",
            run_id="run_10",
            stage_code="10_evolve",
            input_refs=[],
            output_refs=[],
            executor_family="local",
            reviewer_family="local",
            gate_result="passed",
            status="succeeded",
            retry_count=0,
        )
        repo.put_stage_run(stage)
        artifact = Artifact(
            artifact_id="artifact-001",
            artifact_type=ArtifactType.EVAL_REPORT,
            uri="s3://x/artifact-001.json",
            schema_version="1.0.0",
            producer_ref=stage.stage_run_id,
            lineage_parent_ids=["run_10"],
            integrity_hash="sha256:abc",
            visibility="restricted",
            compliance_tags=[],
        )
        repo.put_artifact(artifact)
        return repo

    def test_no_leak_to_substring_run(self):
        repo = self._repo()
        # run_1 is a substring of run_10 / run_10_stage_a, but it is NOT the owner.
        self.assertEqual(repo.list_artifacts("run_1"), [])

    def test_owner_run_sees_artifact(self):
        repo = self._repo()
        found = repo.list_artifacts("run_10")
        self.assertEqual([a.artifact_id for a in found], ["artifact-001"])


if __name__ == "__main__":
    unittest.main()
