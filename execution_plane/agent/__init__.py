"""Agent harness seam — the integration point for an autonomous research agent.

The platform talks to an external agent (Codex / WorkBuddy) over a small JSON wire
protocol. ``protocol`` defines the message types, ``transport`` carries them, and
``harness`` wraps them behind the ``AgentHarness`` interface the orchestrator calls.

See ``PROTOCOL.md`` for the human-readable contract and integration steps.
"""

from .harness import AGENT_TOOL_NAMES
from .harness import AgentHarness
from .harness import LocalAgentHarness
from .harness import RemoteAgentHarness
from .protocol import AgentDecision
from .protocol import AgentStageResult
from .protocol import RouteSuggestion
from .protocol import TaskDecide
from .protocol import TaskRunStage
from .protocol import ToolCall
from .protocol import ToolResult
from .protocol import all_schemas
from .protocol import parse_message
from .transport import CallableTransport
from .transport import ClaudeCodeTransport
from .transport import CodexTransport
from .transport import HttpTransport
from .transport import SubprocessTransport
from .transport import Transport

__all__ = [
    "AgentHarness",
    "LocalAgentHarness",
    "RemoteAgentHarness",
    "AGENT_TOOL_NAMES",
    "TaskDecide",
    "TaskRunStage",
    "AgentDecision",
    "AgentStageResult",
    "ToolCall",
    "ToolResult",
    "RouteSuggestion",
    "parse_message",
    "all_schemas",
    "Transport",
    "CallableTransport",
    "CodexTransport",
    "ClaudeCodeTransport",
    "SubprocessTransport",
    "HttpTransport",
]
