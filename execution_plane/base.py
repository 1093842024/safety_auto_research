"""Stage executor contract + execution result."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ..platform_contracts.enums import GateResult
from ..platform_contracts.enums import StageStatus
from ..platform_contracts.events import BasePlatformEvent
from ..platform_contracts.objects import StageRun
from .sdk import PlatformSDK


@dataclass
class ExecResult:
    """Outcome of running a stage executor.

    ``final_status`` is the *execution* status (the stage ran to completion);
    the gate outcome lives in ``gate_result`` and in the emitted event.
    """

    final_status: StageStatus
    gate_result: GateResult
    event: BasePlatformEvent | None = None
    output_refs: list[str] = field(default_factory=list)
    detail: str = ""
    # Optional structured output the orchestrator needs in-process and that does not fit
    # the event/artifact contracts (e.g. layer_12's frozen ExecutableRubric, which must
    # be handed to the inner loop and to layer_11 within the same run). Defaults to an
    # empty dict so every existing executor and caller is unaffected.
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageTaskSpec:
    """A stage, handed to an agent as a *task specification* rather than a fixed script.

    The agent fulfills this spec autonomously using the SDK tool surface
    (``available_tools``). In scripted mode — or as a fallback — the deterministic
    ``StageExecutor`` implements it instead.

    This is the pivot point of the agent-driven refactor: the platform no longer tells
    the agent *how* to do a stage; it hands over the goal, the tools, and the context.
    """

    stage_run: StageRun
    params: dict[str, Any] = field(default_factory=dict)
    available_tools: list[str] = field(default_factory=list)
    run_context: dict[str, Any] = field(default_factory=dict)
    # Open-goal orchestration: an agent may be handed a goal + candidate capabilities
    # and invoke ``run_capability`` for whichever layers it chooses (end-to-end autonomy).
    open_goal: str | None = None
    candidate_capabilities: list[str] = field(default_factory=list)
    # Inner-loop agent configuration (prompt / skills / tools / step_plan / data knobs).
    # Surfaced verbatim to a RemoteAgentHarness so the researcher's settings actually drive
    # the autonomous inner loop. Ignored by the deterministic executors / LocalAgentHarness.
    agent_config: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "stage_code": self.stage_run.stage_code,
            "stage_run_id": self.stage_run.stage_run_id,
            "params": self.params,
            "available_tools": self.available_tools,
            "run_context": self.run_context,
        }
        if self.open_goal is not None:
            d["open_goal"] = self.open_goal
        if self.candidate_capabilities:
            d["candidate_capabilities"] = self.candidate_capabilities
        if self.agent_config:
            d["agent_config"] = self.agent_config
        return d


class StageExecutor(ABC):
    """Default / fallback implementation of a stage's logic (spec §11).

    In agent mode the ``AgentHarness`` fulfills the ``StageTaskSpec`` autonomously; this
    executor is the deterministic implementation the agent (or the scripted autopilot)
    can fall back to. It is bound to one or more stage codes.
    """

    # Stage codes this executor can handle (prefix match is supported).
    stage_codes: tuple[str, ...] = ()

    def can_handle(self, stage_code: str) -> bool:
        norm = stage_code.lower().replace("_", "")
        return any(
            norm == c.lower().replace("_", "") or norm.startswith(c.lower().replace("_", ""))
            for c in self.stage_codes
        )

    @abstractmethod
    def execute(
        self,
        stage_run: StageRun,
        sdk: PlatformSDK,
        params: dict[str, Any],
    ) -> ExecResult:
        """Run the layer logic, publish outputs, emit events, return results."""
