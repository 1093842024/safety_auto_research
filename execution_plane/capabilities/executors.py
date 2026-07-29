"""Stub executor for infrastructure layers that do not yet have a real adapter.

Produces a real, audited artifact (mapped to the layer's canonical ``ArtifactType``) and
a metric, so an agent invoking the capability gets a tangible, traceable result. Replace
``execute`` with the real skill / agent / dataset / benchmark integration when available,
keeping the same event + artifact contract the rest of the platform relies on.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from ...platform_contracts.enums import GateResult
from ...platform_contracts.enums import StageStatus
from ...platform_contracts.objects import StageRun
from ..base import ExecResult
from ..base import StageExecutor
from ..sdk import PlatformSDK


class StubCapabilityExecutor(StageExecutor):
    """Deterministic placeholder that publishes an artifact for the layer."""

    def __init__(self, layer_code: str, artifact_type: str) -> None:
        self.layer_code = layer_code
        self.artifact_type = artifact_type
        self.stage_codes = (layer_code,)

    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        run = sdk.load_object(f"run:{stage_run.run_id}")
        obj = run.objective_snapshot or {}
        seed = int(hashlib.sha256(stage_run.run_id.encode()).hexdigest(), 16) % 1000
        rng = random.Random(seed)
        quality = round(rng.uniform(0.55, 0.95), 4)

        label = params.get("label", self.layer_code)
        artifact = sdk.publish_artifact(
            payload={
                "run_id": stage_run.run_id,
                "artifact_type": self.artifact_type,
                "uri": f"stub://{self.layer_code}/{stage_run.stage_run_id}",
                "producer_ref": stage_run.stage_run_id,
                "lineage_parent_ids": stage_run.input_refs,
            },
            schema_version="1.0.0",
            metadata={"layer": self.layer_code, "label": label, "quality": quality},
        )
        sdk.record_metric(
            stage_run.run_id, f"capability.{self.layer_code}.quality", quality
        )
        return ExecResult(
            final_status=StageStatus.SUCCEEDED,
            gate_result=GateResult.PASSED,
            event=None,
            output_refs=[artifact.artifact_id],
            detail=(
                f"stub capability {self.layer_code} produced "
                f"{self.artifact_type} (quality={quality})"
            ),
        )
