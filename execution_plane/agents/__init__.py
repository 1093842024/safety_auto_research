"""Agent framework public surface.

The per-role agent adapters and the role->agent registry that let every step of the
autonomous-research harness run on its configured optimal backend (deterministic,
Codex, Claude Code, WorkBuddy, or an independent OpenAI-compatible LLM).
"""

from .adapter import AuditVerdict
from .adapter import ClaudeCodeAdapter
from .adapter import CodexAdapter
from .adapter import DeterministicAdapter
from .adapter import ExecOutput
from .adapter import IndependentAuditorAdapter
from .adapter import LLMApiAdapter
from .adapter import LocalExecutorAdapter
from .adapter import ManagerAdapter
from .adapter import MetaDeciderAdapter
from .adapter import RoleAgentAdapter
from .adapter import RoleBudget
from .adapter import TransportExecutorAdapter
from .adapter import WorkBuddyAdapter
from .registry import RoleAgentRegistry

__all__ = [
    "AuditVerdict",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "DeterministicAdapter",
    "ExecOutput",
    "IndependentAuditorAdapter",
    "LLMApiAdapter",
    "LocalExecutorAdapter",
    "RoleAgentAdapter",
    "RoleBudget",
    "TransportExecutorAdapter",
    "WorkBuddyAdapter",
    "RoleAgentRegistry",
    "ManagerAdapter",
    "MetaDeciderAdapter",
]
