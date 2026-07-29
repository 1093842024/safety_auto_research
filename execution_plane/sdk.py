"""PlatformSDK — the only interface an execution-plane adapter may use to talk
to the control plane (spec §9.4).

It wraps a ``ControlPlaneService`` and exposes the six canonical operations:
  - ``load_object(ref)``            read a canonical input object / collection
  - ``publish_artifact(...)``       publish an output object + emit event
  - ``emit_event(event)``           append a platform event to the log
  - ``request_approval(payload)``   open a HITL gate
  - ``record_metric(...)``          record an observation metric
  - ``register_lesson(payload)``    promote a LessonCard + emit reinjection event
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..control_plane.schemas import RequestApprovalRequest
from ..control_plane.service import ControlPlaneService
from ..control_plane.store_tree import ResearchStateStore
from ..platform_contracts.enums import ArtifactType
from ..platform_contracts.enums import Visibility
from ..platform_contracts.events import ApprovalRequiredEvent
from ..platform_contracts.events import ArtifactPublishedEvent
from ..platform_contracts.events import BasePlatformEvent
from ..platform_contracts.events import LessonPromotedEvent
from ..platform_contracts.objects import Artifact
from ..platform_contracts.objects import LessonCard


class PlatformSDK:
    """Thin, contract-bound façade over the control plane for adapters.

    Optionally carries a ``ResearchStateStore`` so adapters/agents can accumulate a
    HypothesisTree and an ExperienceBank across rounds (dual-loop Phase 3). When no store
    is attached, the cumulative-state methods are no-ops (backward compatible).
    """

    def __init__(
        self, service: ControlPlaneService, state_store: ResearchStateStore | None = None
    ) -> None:
        self.service = service
        self.state_store = state_store

    # ------------------------------------------------------------------ 1. load
    def load_object(self, ref: str) -> Any:
        """Load a canonical object by ``kind:id``.

        Supported kinds: ``run``, ``stage``, ``decision``, ``events:<run_id>``,
        ``decisions:<run_id>``, ``artifact``, ``lesson``.
        """

        if ":" not in ref:
            raise ValueError("ref must be 'kind:id' (e.g. 'run:run-abc')")
        kind, rid = ref.split(":", 1)
        svc = self.service
        if kind == "run":
            return svc.get_workflow_run(rid)
        if kind == "stage":
            return svc.get_stage_run(rid)
        if kind == "decision":
            return svc.get_decision(rid)
        if kind == "events":
            return svc.list_events(rid)
        if kind == "decisions":
            return svc.list_decisions(rid)
        if kind == "artifact":
            return svc._repo.get_artifact(rid)
        if kind == "lesson":
            return svc._repo.get_lesson(rid)
        raise ValueError(f"unknown object kind: {kind}")

    # ------------------------------------------------------------------ 3. publish
    def publish_artifact(
        self,
        payload: dict[str, Any],
        schema_version: str,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """Create a canonical ``Artifact`` and emit ``ArtifactPublishedEvent``."""

        run_id = payload.get("run_id", "")
        artifact_id = self.service._repo.next_id("artifact")
        artifact = Artifact(
            artifact_id=artifact_id,
            artifact_type=ArtifactType(payload["artifact_type"]),
            uri=payload.get("uri", f"artifact://{artifact_id}"),
            schema_version=schema_version,
            producer_ref=payload.get("producer_ref", ""),
            lineage_parent_ids=payload.get("lineage_parent_ids", []),
            integrity_hash=payload.get(
                "integrity_hash",
                "sha256:" + hashlib.sha256(artifact_id.encode()).hexdigest()[:16],
            ),
            visibility=Visibility(payload.get("visibility", "internal")),
            compliance_tags=payload.get("compliance_tags", []),
        )
        self.service._repo.put_artifact(artifact)
        self.service.append_event(
            ArtifactPublishedEvent(
                run_id=run_id,
                artifact_id=artifact_id,
                artifact_type=artifact.artifact_type,
            )
        )
        return artifact

    # ------------------------------------------------------------------ 4. emit
    def emit_event(self, event: BasePlatformEvent) -> None:
        """Append a platform event to the durable event log."""

        self.service.append_event(event)

    # ------------------------------------------------------------------ approval
    def request_approval(self, run_id: str, payload: dict[str, Any]) -> tuple[Any, str]:
        """Open a HITL gate (e.g. high ASR red-team). Returns (run, approval_id)."""

        return self.service.request_approval(run_id, RequestApprovalRequest(**payload))

    # ------------------------------------------------------------------ metrics
    def record_metric(
        self,
        run_id: str,
        name: str,
        value: float,
        tags: dict[str, Any] | None = None,
    ) -> None:
        """Record an observation metric (spec §9.4 record_metric)."""

        self.service._repo.record_metric(run_id, name, value, tags)

    # ------------------------------------------------------------------ lesson
    def register_lesson(self, payload: dict[str, Any]) -> LessonPromotedEvent:
        """Promote a transient observation into a platform ``LessonCard``.

        Emits ``LessonPromotedEvent`` (the reinjection signal, spec §4 经验回注)
        and returns the event so the caller can chain the next decision.
        """

        lesson_id = self.service._repo.next_id("lesson")
        lesson = LessonCard(
            lesson_id=lesson_id,
            scope=payload["scope"],
            source_run_id=payload["source_run_id"],
            pattern_type=payload["pattern_type"],
            applicable_stages=payload.get("applicable_stages", []),
            confidence=payload.get("confidence", 0.5),
            payload_ref=payload.get("payload_ref", f"lesson-payload://{lesson_id}"),
            prm_score=payload.get("prm_score", 0.5),
        )
        self.service._repo.put_lesson(lesson)
        event = LessonPromotedEvent(
            run_id=payload["source_run_id"],
            lesson_id=lesson_id,
            source_run_id=payload["source_run_id"],
            scope=payload["scope"],
            pattern_type=payload["pattern_type"],
            prm_score=lesson.prm_score,
            confidence=lesson.confidence,
            applicable_stages=lesson.applicable_stages,
            payload_ref=lesson.payload_ref,
        )
        self.service.append_event(event)
        return event

    # ------------------------------------------------------------------ cumulative state
    def observe_hypothesis(
        self,
        hypothesis: str,
        evidence_refs: list[str] | None = None,
        parent_id: str | None = None,
        artifact_ref: str | None = None,
        branch: str = "main",
        score: float = 0.0,
        run_id: str | None = None,
    ) -> str | None:
        """Add / extend a node in the cumulative HypothesisTree (Arbor-style)."""

        if self.state_store is None:
            return None
        node = self.state_store.hypo_tree.observe(
            hypothesis=hypothesis,
            evidence_refs=evidence_refs or [],
            parent_id=parent_id,
            artifact_ref=artifact_ref,
            branch=branch,
            score=score,
            run_id=run_id,
        )
        return node.node_id

    def backpropagate_insight(self, node_id: str, insight: str, score: float | None = None) -> None:
        """Attach an insight to a hypothesis node and propagate the score upward."""

        if self.state_store is None:
            return
        self.state_store.hypo_tree.backpropagate(node_id, insight, score)

    def mark_pruned(self, node_id: str) -> None:
        if self.state_store is not None:
            self.state_store.hypo_tree.mark_pruned(node_id)

    def mark_merged(self, node_id: str) -> None:
        if self.state_store is not None:
            self.state_store.hypo_tree.mark_merged(node_id)

    def register_experience(
        self,
        kind: str,
        context: str,
        lesson: str,
        applicable_stages: list[str] | None = None,
        confidence: float = 0.5,
    ) -> str | None:
        """Record a pass/fail lesson in the cross-run ExperienceBank (training-free replay)."""

        if self.state_store is None:
            return None
        e = self.state_store.experience_bank.add(
            kind=kind,
            context=context,
            lesson=lesson,
            applicable_stages=applicable_stages or [],
            confidence=confidence,
        )
        return e.entry_id

    def query_experiences(self, stage: str | None = None, k: int = 5) -> list[dict[str, object]]:
        """Retrieve up to ``k`` cross-run experiences (reinjection context)."""

        if self.state_store is None:
            return []
        return [e.model_dump(mode="json") for e in self.state_store.experience_bank.query(stage, k)]

    def compact_research_state(
        self,
        keep: list[str] | None = None,
        unresolved: list[str] | None = None,
        rejected_candidates: list[str] | None = None,
        next_plan: str = "",
        run_id: str | None = None,
    ) -> dict[str, object]:
        """AREX ``update_context``: compress the turning point into an improvement_state.

        Preserves verified findings, unresolved constraints, and **rejected candidates**
        (the anti-local-optimum safeguard). No-op (empty dict) without a state store.
        """

        if self.state_store is None:
            return {}
        return self.state_store.hypo_tree.compact(
            keep=keep,
            unresolved=unresolved,
            rejected_candidates=rejected_candidates,
            next_plan=next_plan,
            run_id=run_id,
        )
