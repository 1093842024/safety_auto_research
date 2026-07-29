"""Coordinator tool: an on-demand external-knowledge / research assistant.

This is the grounded-ideation lane (roadmap 1.1). It is a general
external-knowledge tool the coordinator can call AT ANY TIME — not a mandatory
IDEATE step. Ideas can come from experiment results, internal reasoning, or the
literature; this tool is the optional input the coordinator reaches for when
EXTERNAL knowledge would help. It adapts to an ``intent``:

- ``related_work`` — find + assess prior work for a draft idea
- ``survey``       — organize how a field/problem is currently solved
- ``lookup``       — answer a specific factual question (method/dataset/number)
- ``explore``      — open-ended scan of a direction for gaps / open problems

It is deliberately SEPARATE from the post-experiment novelty audit in
:mod:`search_ctx`:

- **Novelty audit** (``SearchIdeaContext``) runs AFTER an experiment, on a
  validated node, in the background, and writes ``node.related_work``.
- **ResearchSearch** (here) runs on demand and **blocks** — its digest enters
  the coordinator's context so it can act on the knowledge. When a returned
  source shaped an idea, the citation is recorded on ``node.grounding`` (via
  ``TreeAddNode``), a field distinct from ``related_work``.

The two lanes never share fetched text: each runs its own fresh search in its
own isolated context. That is the "separation, not prohibition" integrity
guarantee — benchmark fairness comes from the ``grounded_ideation`` master
switch being OFF by default, not from muzzling what this agent may return.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from ...core.tools.base import Tool
from ._agent_recover import (
    filter_sources_to_visited,
    recover_json,
    visited_urls,
)

if TYPE_CHECKING:
    from ..config import CoordinatorConfig
    from ...core.llm.base import LLMProvider

log = logging.getLogger(__name__)

_INTENTS = ("related_work", "survey", "lookup", "explore")

# Soft wall-clock cap for the research lane when the operator left
# ``search.agent_timeout`` unset. The agent is still bounded by
# ``agent_max_turns``; this just keeps a blocking GroundIdea call from stalling
# the coordinator for minutes. On timeout we recover the last valid JSON.
_RESEARCH_SOFT_TIMEOUT = 200


def _render_digest(parsed: dict[str, Any], dropped: int = 0) -> str:
    """Render the research assistant's JSON into a digest the coordinator reads.

    Sources are numbered so the coordinator can cite them by index when it
    calls ``TreeAddNode(grounding=...)``. ``dropped`` is the number of cited
    sources that were discarded because they were never actually visited.
    """
    summary = str(parsed.get("summary", "")).strip()
    details = str(parsed.get("details", "")).strip()
    sources = parsed.get("sources") or []

    lines: list[str] = []
    if summary:
        lines.append("### Summary")
        lines.append(summary)
        lines.append("")

    if details:
        lines.append("### Findings")
        lines.append(details)
        lines.append("")

    lines.append("### Sources")
    if isinstance(sources, list) and sources:
        for i, s in enumerate(sources, 1):
            if not isinstance(s, dict):
                continue
            title = str(s.get("title", "")).strip() or "(untitled)"
            url = str(s.get("url", "")).strip()
            note = str(s.get("note", "")).strip()
            head = f"[{title}]({url})" if url else title
            lines.append(f"[{i}] {head} — {note}" if note else f"[{i}] {head}")
    else:
        lines.append("- (none found)")

    if dropped:
        lines.append("")
        lines.append(
            f"_Note: {dropped} cited source(s) were dropped because they were "
            f"never opened during the search._"
        )

    return "\n".join(lines).rstrip() + "\n"


async def _run_research(
    *,
    config: "CoordinatorConfig",
    provider: "LLMProvider",
    query: str,
    intent: str | None = None,
    context: str = "",
    focus: str | None = None,
) -> str:
    """Build + run the research assistant for one query. Best-effort, never raises.

    Returns a Markdown digest (summary + findings + numbered sources), or a
    ``[research-failed: ...]`` marker.
    """
    from ...search_agent.agent import build_search_agent
    from ...search_agent.prompts import (
        RESEARCH_AGENT_SYSTEM_PROMPT,
        build_research_user_prompt,
    )

    sc = config.search
    if not (sc and sc.has_backend):
        return "[research-failed: no search backend configured]"
    if not query.strip():
        return "[research-failed: empty query]"

    user_prompt = build_research_user_prompt(
        query=query,
        intent=intent,
        context=context,
        focus=focus,
    )

    raw = ""
    agent = None
    timeout = sc.agent_timeout
    if timeout is None or timeout <= 0:
        timeout = _RESEARCH_SOFT_TIMEOUT
    # Grounded ideation reads for depth: give the visit tool a larger token
    # budget so paper method/results sections survive truncation (roadmap 1.1d).
    sc_research = sc.model_copy(update={
        "visit_max_content_tokens": max(
            getattr(sc, "research_visit_tokens", 0) or 0, sc.visit_max_content_tokens
        ),
    })
    try:
        agent = build_search_agent(
            provider=provider,
            search_config=sc_research,
            cwd=config.cwd,
            meta_config=config,
            context_window=config.context_window,
            system_prompt=RESEARCH_AGENT_SYSTEM_PROMPT,
        )
        raw = await asyncio.wait_for(agent.run(user_prompt), timeout=timeout)
    except asyncio.TimeoutError:
        # The agent often emits a valid final JSON before the deadline (the
        # premature-stop nudge can push it past a good answer); recover it
        # rather than discarding the work.
        recovered = recover_json(agent, "") if agent is not None else None
        if recovered is None:
            log.warning("research assistant for %r timed out", query[:60])
            return f"[research-failed: timed out after {timeout}s]"
        log.info("research assistant for %r timed out but a JSON was recovered", query[:60])
        parsed = recovered
    except Exception as exc:  # noqa: BLE001
        log.warning("research assistant for %r failed: %s", query[:60], exc)
        return f"[research-failed: {type(exc).__name__}: {exc}]"
    else:
        parsed = recover_json(agent, raw)

    if parsed is None:
        snippet = raw.strip()
        if len(snippet) > 4000:
            snippet = snippet[:4000] + "\n\n[... truncated ...]"
        return f"[research: JSON could not be parsed — raw output]\n\n{snippet}"

    # Integrity: drop cited sources that were never actually opened.
    sources = parsed.get("sources")
    dropped = 0
    if isinstance(sources, list):
        kept, dropped = filter_sources_to_visited(sources, visited_urls(agent))
        parsed["sources"] = kept

    return _render_digest(parsed, dropped=dropped)


class ResearchSearchTool(Tool):
    """On-demand external-knowledge assistant (web search + alphaXiv).

    Blocks until the search completes and returns a digest to the coordinator
    so it can act on the knowledge. Does NOT write to any node — when a returned
    source shapes an idea, the coordinator records the citation via
    ``TreeAddNode(grounding=...)``.
    """

    name = "ResearchSearch"
    description = (
        "Search external sources (web + alphaXiv) for knowledge that would help "
        "your research — an OPTIONAL input you can call any time, not a "
        "required step. Ideas may come from experiment results or your own "
        "reasoning; reach for this when EXTERNAL knowledge would help.\n\n"
        "Set `intent` to shape the search:\n"
        "- `related_work` — you have a draft idea; find + assess prior work "
        "(overlap / difference / gap).\n"
        "- `survey` — organize how a field/problem is currently solved "
        "(approaches + trade-offs).\n"
        "- `lookup` — answer a specific factual question (a method detail, "
        "dataset, benchmark number, API).\n"
        "- `explore` — open-ended scan of a direction for gaps / open problems.\n\n"
        "Runs in an ISOLATED context (verbose SERP / page text never enters "
        "yours) and BLOCKS, returning a digest: summary + findings + numbered "
        "sources. It does NOT write to the tree. When a source shaped an idea, "
        "record it via TreeAddNode(grounding=...). This is a SEPARATE lane from "
        "SearchIdeaContext (post-experiment novelty audit). Failures never "
        "raise — they return a `[research-failed: ...]` marker."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What you want to find out — a draft idea to ground, a "
                    "problem/area to survey, a specific question to look up, or "
                    "a direction to explore."
                ),
            },
            "intent": {
                "type": "string",
                "enum": list(_INTENTS),
                "description": (
                    "How to shape the search (see tool overview). Optional — "
                    "omit to let the assistant infer the most useful intent."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional background — a draft idea or experiment finding "
                    "that prompted this query. Used as context only; the "
                    "assistant searches for `query`, not this."
                ),
            },
            "focus": {
                "type": "string",
                "description": (
                    "Optional one-line directive biasing the search "
                    "(e.g. 'prefer arxiv 2024+', 'emphasize efficiency')."
                ),
            },
        },
        "required": ["query"],
    }
    is_read_only = True  # does not mutate the tree

    def __init__(
        self,
        *,
        cwd: str,
        config: "CoordinatorConfig",
        provider: "LLMProvider",
        **kwargs: Any,
    ):
        super().__init__(cwd=cwd, **kwargs)
        self._config = config
        self._provider = provider

    async def execute(self, **kwargs: Any) -> str:
        query: str = (kwargs.get("query") or "").strip()
        intent: str | None = (kwargs.get("intent") or "").strip() or None
        context: str = (kwargs.get("context") or "").strip()
        focus: str | None = kwargs.get("focus") or None
        if not query:
            return "Error: query is required (what you want to find out)."
        if intent is not None and intent not in _INTENTS:
            return f"Error: intent must be one of {_INTENTS} (or omitted)."
        if not self._config.search.has_backend:
            return "[research-failed: no search backend configured]"

        return await _run_research(
            config=self._config,
            provider=self._provider,
            query=query,
            intent=intent,
            context=context,
            focus=focus,
        )
