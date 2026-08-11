"""Integration tests for the F6 audit follow-up interaction protocol.

Covers the new endpoints in ``control_plane/api.py``:

  * ``POST /workflow-runs/{run_id}/audits/{audit_id}/followup``
  * ``GET  /workflow-runs/{run_id}/audits/{audit_id}/followups``

and the supporting contracts (``AuditFollowupEvent`` / ``AuditFollowupRequest`` /
``AuditReport.followups``). The follow-up is a *supplementary* event: it never mutates
the original ``AuditCompletedEvent`` and v1 does not auto-feed the result into
``IterationRouter``.

Acceptance cases:
  1. valid clarification re-scores the constraint -> resolved + 200
  2. unknown run -> 404
  3. unknown audit_id -> 404
  4. unknown constraint_id -> 400
  5. question-only follow-up is recorded but does not resolve
  6. GET followups returns the emitted AuditFollowupEvent(s)
"""

from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from safety_auto_research.control_plane.api import create_app
from safety_auto_research.control_plane.schemas import AuditFollowupRequest
from safety_auto_research.control_plane.schemas import CreateWorkflowRunRequest
from safety_auto_research.control_plane.service import ControlPlaneService
from safety_auto_research.platform_contracts.enums import RunType
from safety_auto_research.platform_contracts.events import AuditCompletedEvent


class _EnvMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._files = []
        for _ in range(3):
            t = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
            t.close()
            os.remove(t.name)
            self._files.append(t.name)
        self._old = {
            k: os.environ.get(k)
            for k in ("CUSTOM_TASKS_STORE", "CONTROL_PLANE_STORE", "RESEARCH_STATE_DB")
        }
        os.environ["CUSTOM_TASKS_STORE"] = self._files[0]
        os.environ["CONTROL_PLANE_STORE"] = self._files[1]
        os.environ["RESEARCH_STATE_DB"] = self._files[2]
        self.svc = ControlPlaneService()
        self.client = TestClient(create_app(self.svc))

    def tearDown(self) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for f in self._files:
            if os.path.exists(f):
                os.remove(f)

    def _make_run(self, target_id: str = "platform.titanic") -> str:
        req = CreateWorkflowRunRequest(
            program_id="benchmark",
            run_type=RunType.STANDARD_RESEARCH,
            entry_stage="inner_research",
            target_id=target_id,
            objective_snapshot={
                "benchmark_task_id": target_id,
                "name": "titanic",
                "eval_metric": "accuracy",
                "direction": "higher",
                "config": {"inner_loop": {"model": "gbm"}},
            },
            requested_outcomes=["x"],
        )
        r = self.client.post("/workflow-runs", json=req.model_dump(mode="json"))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["run_id"]

    def _seed_audit(self, run_id: str, audit_id: str = "audit-seeded") -> None:
        """Inject a synthetic AuditCompletedEvent so the follow-up endpoint has a target."""
        event = AuditCompletedEvent(
            run_id=run_id,
            audit_id=audit_id,
            audited_target="titanic accuracy",
            constraints=[
                {
                    "id": "primary_metric",
                    "description": "primary metric meets threshold",
                    "status": "conflict",
                    "score": 0.3,
                    "note": "cv accuracy=0.81",
                },
                {
                    "id": "claims_supported",
                    "description": "inner-loop claims are supported by evidence",
                    "status": "partial",
                    "score": 0.5,
                    "note": "judge(score)=0.5",
                },
            ],
            unresolved_claims=["primary metric meets threshold"],
            rejected_candidates=[],
            confidence=0.55,
            recoverable=True,
            audit_confidence=0.9,
            gate_passed=False,
            report_ref="audit-report://seeded",
        )
        self.svc.append_event(event)


class AuditFollowupEndpointTest(_EnvMixin):
    def test_valid_clarification_resolves_constraint(self) -> None:
        run_id = self._make_run()
        self._seed_audit(run_id)

        # A clarification containing the claim's keywords should re-score to "verified".
        clarification = (
            "The inner-loop claims are supported by evidence because the held-out "
            "metrics confirm the reported accuracy."
        )
        body = AuditFollowupRequest(
            constraint_id="claims_supported",
            clarification=clarification,
        )
        r = self.client.post(
            f"/workflow-runs/{run_id}/audits/audit-seeded/followup",
            json=body.model_dump(mode="json"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["event_type"], "audit_followup")
        self.assertEqual(data["audit_id"], "audit-seeded")
        self.assertEqual(data["constraint_id"], "claims_supported")
        self.assertEqual(data["prior_status"], "partial")
        self.assertEqual(data["new_status"], "verified")
        self.assertTrue(data["resolved"])
        self.assertEqual(data["recommendation"], "accept")

        # The original audit verdict must stay immutable.
        audit = self.client.get(f"/workflow-runs/{run_id}/audit").json()
        self.assertEqual(len(audit), 1)
        seeded = next(c for c in audit[0]["constraints"] if c["id"] == "claims_supported")
        self.assertEqual(seeded["status"], "partial")

    def test_question_only_is_recorded_not_resolved(self) -> None:
        run_id = self._make_run()
        self._seed_audit(run_id)
        body = AuditFollowupRequest(
            constraint_id="claims_supported",
            question="why was this constraint only partial?",
        )
        r = self.client.post(
            f"/workflow-runs/{run_id}/audits/audit-seeded/followup",
            json=body.model_dump(mode="json"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertFalse(data["resolved"])
        self.assertEqual(data["new_status"], "partial")  # unchanged
        self.assertEqual(data["recommendation"], "revisit")
        self.assertEqual(data["question"], "why was this constraint only partial?")
        self.assertIsNone(data["clarification"])

    def test_unknown_run_is_404(self) -> None:
        body = AuditFollowupRequest(constraint_id="x", clarification="y")
        r = self.client.post(
            "/workflow-runs/nope/audits/audit-x/followup",
            json=body.model_dump(mode="json"),
        )
        self.assertEqual(r.status_code, 404)

    def test_unknown_audit_is_404(self) -> None:
        run_id = self._make_run()
        body = AuditFollowupRequest(constraint_id="claims_supported", clarification="y")
        r = self.client.post(
            f"/workflow-runs/{run_id}/audits/audit-missing/followup",
            json=body.model_dump(mode="json"),
        )
        self.assertEqual(r.status_code, 404)

    def test_unknown_constraint_is_400(self) -> None:
        run_id = self._make_run()
        self._seed_audit(run_id)
        body = AuditFollowupRequest(constraint_id="does_not_exist", clarification="y")
        r = self.client.post(
            f"/workflow-runs/{run_id}/audits/audit-seeded/followup",
            json=body.model_dump(mode="json"),
        )
        self.assertEqual(r.status_code, 400)


class AuditFollowupListTest(_EnvMixin):
    def test_get_followups_returns_emitted_events(self) -> None:
        run_id = self._make_run()
        self._seed_audit(run_id)
        body = AuditFollowupRequest(
            constraint_id="primary_metric",
            clarification="The primary metric meets threshold; cv=0.81 reported.",
        )
        post = self.client.post(
            f"/workflow-runs/{run_id}/audits/audit-seeded/followup",
            json=body.model_dump(mode="json"),
        )
        self.assertEqual(post.status_code, 200, post.text)

        listing = self.client.get(
            f"/workflow-runs/{run_id}/audits/audit-seeded/followups"
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        events = listing.json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "audit_followup")
        self.assertEqual(events[0]["constraint_id"], "primary_metric")

        # followups for a different audit are empty
        other = self.client.get(
            f"/workflow-runs/{run_id}/audits/audit-other/followups"
        )
        self.assertEqual(other.json(), [])


class AuditReportFollowupsFieldTest(unittest.TestCase):
    """The AuditReport.followups field exists and defaults to an empty list (back-compat)."""

    def test_followups_default_empty(self) -> None:
        from safety_auto_research.platform_contracts.objects import AuditReport

        rep = AuditReport(
            audit_id="a1",
            audited_target="t",
            constraints=[],
            unresolved_claims=[],
            rejected_candidates=[],
            confidence=0.5,
            recoverable=True,
            audit_confidence=0.9,
            recommendation="refine",
            report_ref="r",
        )
        self.assertEqual(rep.followups, [])

    def test_audit_followup_event_type_registered(self) -> None:
        from safety_auto_research.platform_contracts.enums import EventType
        from safety_auto_research.platform_contracts.events import ALL_EVENT_MODELS
        from safety_auto_research.platform_contracts.events import AuditFollowupEvent

        self.assertEqual(EventType.AUDIT_FOLLOWUP, "audit_followup")
        self.assertIn(AuditFollowupEvent, ALL_EVENT_MODELS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
