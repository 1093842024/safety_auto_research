"""SearchAgent — per-idea related-work / novelty annotation executor.

A thin specialization of ``core.Agent`` with a small toolset
(``web_search``, ``web_visit``, ``Read``) and a custom system prompt that
implements an iterative search → reason → visit → synthesize loop and emits
a single final JSON object describing related work + a novelty assessment.

The SearchAgent is *not* invoked directly. The coordinator dispatches it via
the ``SearchIdeaContext`` / ``SearchIdeaContextParallel`` tools defined in
``arbor.coordinator.tools.search_ctx``.
"""

from .agent import build_search_agent
from .prompts import SEARCH_AGENT_SYSTEM_PROMPT, build_search_user_prompt

__all__ = [
    "build_search_agent",
    "SEARCH_AGENT_SYSTEM_PROMPT",
    "build_search_user_prompt",
]
