"""Infrastructure-layer capabilities exposed to an agent as callable tools.

Each ``InfraCapability`` is one registered, auditable action an agent may invoke through
the ``run_capability`` tool (e.g. literature search, idea generation, evaluation,
adversarial hardening). The capability is bound to a ``StageExecutor`` (real or stub) so
invoking it creates a real ``StageRun`` + events — keeping every agent action on the
audited, guard-railed path. This is the missing layer that lets an agent orchestrate the
ten infrastructure layers end-to-end instead of being confined to one assigned stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ..base import StageExecutor


@dataclass
class InfraCapability:
    """One infrastructure-layer capability an agent can call as a tool."""

    capability_id: str  # stable id, e.g. "layer_03_eval"
    layer_code: str  # maps to KNOWN_STAGE_CODES / a stage run
    layer_name: str  # human label of the infrastructure layer
    title: str  # short action label
    description: str  # what the agent should expect
    param_schema: dict[str, Any] = field(default_factory=dict)  # JSON schema for args
    executor: StageExecutor | None = None  # bound implementation (real or stub)
    artifact_type: str | None = None  # default artifact produced (used by stubs)
    is_infra: bool = True  # False for extra (non-infra-layer) capabilities like kaggle_eval

    def to_catalog_entry(self) -> dict[str, Any]:
        """Machine-readable description the agent discovers via ``/agent/protocol``."""

        return {
            "capability_id": self.capability_id,
            "layer_code": self.layer_code,
            "layer_name": self.layer_name,
            "title": self.title,
            "description": self.description,
            "param_schema": self.param_schema,
            "bound": self.executor is not None,
            "is_infra": self.is_infra,
        }
