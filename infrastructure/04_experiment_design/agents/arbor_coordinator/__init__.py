"""Coordinator — arbor-guided research orchestrator with Idea Tree.

The coordinator orchestrates automated research through an Idea Tree.
It dispatches executors (Research Agents) to implement and test ideas,
learns from results, and systematically explores promising directions.
"""

from typing import TYPE_CHECKING, Any

from .config import CoordinatorConfig
from .idea_tree import IdeaTree, Node

if TYPE_CHECKING:  # for type checkers / IDEs only — no runtime import cost
    from .orchestrator import CoordinatorOrchestrator

__all__ = [
    "CoordinatorConfig",
    "IdeaTree",
    "Node",
    "CoordinatorOrchestrator",
]


def __getattr__(name: str) -> Any:
    """Lazily expose the heavy orchestrator (PEP 562).

    ``CoordinatorOrchestrator`` pulls in the agent + LLM provider stack, so we
    import it only on first access. This keeps lightweight consumers — e.g. the
    keyless ``arbor mcp`` server, which only needs ``IdeaTree`` — from loading
    any LLM code. ``from arbor.coordinator import CoordinatorOrchestrator`` still
    works exactly as before.
    """
    if name == "CoordinatorOrchestrator":
        from .orchestrator import CoordinatorOrchestrator

        return CoordinatorOrchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
