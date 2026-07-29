"""Wire protocol between the platform and an external agent (Codex / WorkBuddy).

The protocol is a small set of JSON messages exchanged over a :mod:`transport`.
Every message carries a ``msg_type`` discriminator so either side can route it.

Platform -> Agent
  * ``TaskDecide``       - "decide the next step" (run state + event + router suggestion)
  * ``TaskRunStage``     - "fulfill this stage task spec" (with available tools)
  * ``ToolResult``       - response to an agent ``ToolCall``

Agent -> Platform
  * ``AgentDecision``    - the chosen next step
  * ``AgentStageResult`` - the outcome of a stage run (incl. the platform event to emit)
  * ``ToolCall``         - request to invoke an SDK tool (load/publish/emit/approval/metric/lesson)

The six SDK tools an agent may call are listed in ``AGENT_TOOL_NAMES`` (harness.py).
See ``PROTOCOL.md`` for the human-readable contract and integration steps.
"""

from __future__ import annotations

import uuid
from typing import Any
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import Field


class _Base(BaseModel):
    """Allow extra fields so the protocol can grow without breaking old agents."""

    model_config = {"extra": "allow"}


# ----------------------------------------------------------------- Platform -> Agent
class RouteSuggestion(BaseModel):
    """The router's reference policy (R1-R10); the agent may accept or override it."""

    decision_type: str
    target_stage: Optional[str] = None
    reason_codes: list[str] = []
    evidence_refs: list[str] = []
    route_rule: str = ""
    summary: str = ""


class TaskDecide(_Base):
    msg_type: Literal["task.decide"] = "task.decide"
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    run: dict[str, Any]
    event: Optional[dict[str, Any]] = None
    suggestion: RouteSuggestion
    available_tools: list[str] = []


class TaskRunStage(_Base):
    msg_type: Literal["task.run_stage"] = "task.run_stage"
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    spec: dict[str, Any]
    available_tools: list[str] = []


class ToolResult(_Base):
    msg_type: Literal["platform.tool_result"] = "platform.tool_result"
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    call_id: str
    ok: bool = True
    result: Any = None
    error: Optional[str] = None


# ----------------------------------------------------------------- Agent -> Platform
class AgentDecision(_Base):
    msg_type: Literal["agent.decision"] = "agent.decision"
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    decision_type: str
    target_stage: Optional[str] = None
    reason_codes: list[str] = []
    evidence_refs: list[str] = []
    policy_ref: Optional[str] = None
    rationale: str = ""


class AgentStageResult(_Base):
    msg_type: Literal["agent.stage_result"] = "agent.stage_result"
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    event: Optional[dict[str, Any]] = None
    final_status: str = "succeeded"
    gate_result: str = "passed"
    output_refs: list[str] = []
    metrics: dict[str, Any] = {}
    detail: str = ""


class ToolCall(_Base):
    msg_type: Literal["agent.tool_call"] = "agent.tool_call"
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    call_id: str
    tool: str
    args: dict[str, Any] = {}


_MESSAGE_TYPES: dict[str, type[BaseModel]] = {
    "task.decide": TaskDecide,
    "task.run_stage": TaskRunStage,
    "platform.tool_result": ToolResult,
    "agent.decision": AgentDecision,
    "agent.stage_result": AgentStageResult,
    "agent.tool_call": ToolCall,
}
PLATFORM_TO_AGENT = {"task.decide", "task.run_stage", "platform.tool_result"}
AGENT_TO_PLATFORM = {"agent.decision", "agent.stage_result", "agent.tool_call"}
TERMINAL_TYPES = {"agent.decision", "agent.stage_result"}


def parse_message(data: dict[str, Any]) -> BaseModel:
    """Rebuild a typed message from a raw wire dict (after JSON decode)."""

    msg_type = data.get("msg_type")
    cls = _MESSAGE_TYPES.get(msg_type)
    if cls is None:
        raise ValueError(f"unknown agent message type: {msg_type!r}")
    return cls.model_validate(data)


def message_schema(msg_type: str) -> dict[str, Any]:
    cls = _MESSAGE_TYPES.get(msg_type)
    if cls is None:
        raise ValueError(f"unknown agent message type: {msg_type!r}")
    return cls.model_json_schema()


def all_schemas() -> dict[str, Any]:
    return {t: c.model_json_schema() for t, c in _MESSAGE_TYPES.items()}
