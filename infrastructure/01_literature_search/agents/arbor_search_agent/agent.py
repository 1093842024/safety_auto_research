"""Build a SearchAgent — a thin Agent specialization for related-work search."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..core.agent import Agent
from ..core.config import AgentConfig
from .prompts import SEARCH_AGENT_SYSTEM_PROMPT

if TYPE_CHECKING:
    from ..core.llm.base import LLMProvider
    from ..coordinator.config import CoordinatorConfig, SearchConfig


_PROVIDER_NAME_BY_CLASS = {
    "ClaudeProvider": "claude",
    "OpenAICompatProvider": "openai",
}


def _maybe_override_provider(
    provider: "LLMProvider",
    meta_config: "CoordinatorConfig | None",
    override_model: str | None,
) -> "LLMProvider":
    """Return a fresh provider on ``override_model`` if it differs from
    ``provider``'s current model. Otherwise return ``provider`` unchanged.
    """
    if not override_model:
        return provider
    current = getattr(provider, "model", None)
    if current == override_model:
        return provider
    if meta_config is None:
        # We don't have api_key/base_url to build a new provider — fall back.
        return provider
    from ..core import create_provider as _create_provider

    fresh_cfg = AgentConfig(
        provider=meta_config.provider,
        model=override_model,
        api_key=meta_config.api_key,
        base_url=meta_config.base_url,
        openai_api=meta_config.openai_api,
        reasoning_effort=meta_config.reasoning_effort,
        reasoning_summary=meta_config.reasoning_summary,
        text_verbosity=meta_config.text_verbosity,
        parallel_tool_calls=meta_config.parallel_tool_calls,
        thinking_budget_tokens=meta_config.thinking_budget_tokens,
        llm_timeout=meta_config.llm_timeout,
        llm_provider_retries=meta_config.llm_provider_retries,
    )
    return _create_provider(fresh_cfg)


def build_search_agent(
    *,
    provider: "LLMProvider",
    search_config: "SearchConfig",
    cwd: str,
    meta_config: "CoordinatorConfig | None" = None,
    event_bus: Any | None = None,
    max_tokens: int = 8192,
    context_window: int = 200_000,
    system_prompt: str | None = None,
) -> Agent:
    """Construct a SearchAgent.

    Parameters
    ----------
    provider:
        LLM provider used for the agent loop. May be replaced internally by a
        fresh provider on ``search_config.agent_model`` if that field is set
        and differs (requires ``meta_config`` for api_key/base_url).
    search_config:
        ``SearchConfig`` from the coordinator. Must have ``web_search_endpoint``
        set; ``web_browse_endpoint`` is recommended.
    cwd:
        Working directory.
    meta_config:
        Optional ``CoordinatorConfig`` — only consulted if a model override is
        configured and a fresh provider needs to be built.
    system_prompt:
        Optional system-prompt override. Defaults to the novelty-scout prompt
        (``SEARCH_AGENT_SYSTEM_PROMPT``). The grounded-ideation lane passes the
        grounding-scout prompt here to reuse the same backend wiring.
    """
    if not search_config.has_backend:
        raise ValueError(
            "build_search_agent requires a search backend: either set "
            "search.web_search_endpoint (WEB_SEARCH_ENDPOINT) for the HTTP "
            "backend, or set search.builtin_backend='alphaxiv' for the "
            "zero-config alphaXiv backend (bundled on Python >= 3.12)."
        )

    provider = _maybe_override_provider(
        provider, meta_config, search_config.agent_model,
    )

    # AgentConfig.provider/model are only carried for logging / branch-naming.
    provider_name = _PROVIDER_NAME_BY_CLASS.get(
        provider.__class__.__name__, "claude"
    )

    agent_config = AgentConfig(
        cwd=cwd,
        provider=provider_name,
        model=getattr(provider, "model", "claude-sonnet-4-20250514"),
        max_tokens=max_tokens,
        max_turns=search_config.agent_max_turns,
        max_tool_concurrency=3,
        context_window=context_window,
        auto_git=False,
        idea="search-agent",
        event_bus=event_bus,
    )
    if meta_config is not None:
        agent_config.llm_timeout = meta_config.llm_timeout
        agent_config.llm_provider_retries = meta_config.llm_provider_retries
        agent_config.llm_retry_attempts = meta_config.llm_retry_attempts
        agent_config.llm_retry_base_delay = meta_config.llm_retry_base_delay
        agent_config.llm_retry_max_delay = meta_config.llm_retry_max_delay

    # Backend selection (alphaXiv / Jina / Serper / Exa / endpoint search +
    # keyless or endpoint visit) is centralized in the web-tools factory.
    from ..core.tools.web.factory import (
        build_web_search_tool,
        build_web_visit_tool,
    )

    tools: list = []
    search_tool = build_web_search_tool(search_config, cwd=cwd)
    if search_tool is not None:
        tools.append(search_tool)
    visit_tool = build_web_visit_tool(search_config, cwd=cwd)
    if visit_tool is not None:
        tools.append(visit_tool)

    agent = Agent(
        provider=provider,
        tools=tools,
        system_prompt=system_prompt or SEARCH_AGENT_SYSTEM_PROMPT,
        config=agent_config,
    )
    # SearchAgent never touches git.
    agent.git_manager.enabled = False
    return agent
