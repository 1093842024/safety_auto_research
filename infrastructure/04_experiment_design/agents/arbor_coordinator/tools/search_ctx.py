"""Coordinator tool: dispatch a SearchAgent to annotate an idea node.

By default the SearchAgent runs **in the background** — the coordinator's
``SearchIdeaContext`` call returns immediately with a "dispatched" message,
and the actual search work proceeds concurrently with the coordinator's other
work (more IDEATE rounds, RunExecutor dispatches, etc.). When the search
finishes, the result is written to ``node.related_work`` via the tree's
async-safe update.

The orchestrator calls :func:`wait_for_pending_searches` after the coordinator
loop ends, so any in-flight searches get a chance to flush before exit.

Failure is *non-blocking*: any exception or parse failure leaves a
``[search-failed: <reason>]`` marker on the node. The coordinator's prompt
mentions that a failed annotation never gates dispatch.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TYPE_CHECKING

from ...core.tools.base import Tool

if TYPE_CHECKING:
    from ..config import CoordinatorConfig
    from ..idea_tree import IdeaTree
    from ...core.llm.base import LLMProvider

log = logging.getLogger(__name__)

_MAX_PARALLEL = 4
_ANCESTOR_INSIGHT_CHARS = 600

# Module-level registry of in-flight background search tasks. The orchestrator
# awaits these on shutdown so background searches get a chance to flush their
# results to the tree before the event loop closes.
_BG_TASKS: set[asyncio.Task] = set()


def _validation_gate(
    tree: "IdeaTree", node, require_validated: bool
) -> str | None:
    """Return None if the node is allowed to be searched; else an error string.

    "Validated effective" = status in {done, merged} AND score is not None
    AND score > current trunk_score (or > baseline_score if no trunk yet,
    or > 0 if neither is recorded). Rationale: only spend search budget on
    nodes that are merge-worthy.
    """
    if not require_validated:
        return None
    if node.status not in ("done", "merged"):
        return (
            f"[skipped: node status={node.status!r} — SearchIdeaContext "
            f"only runs on validated nodes (done/merged). Run RunExecutor "
            f"first, or set search.require_validated=False to override.]"
        )
    if node.score is None:
        return (
            "[skipped: node has no score recorded — cannot confirm it beat "
            "the trunk. Re-extract the score via TreeUpdateNode if it ran.]"
        )
    trunk = tree.meta.get("trunk_score")
    baseline = tree.meta.get("baseline_score")
    threshold = trunk if trunk is not None else (baseline if baseline is not None else 0.0)
    if node.score <= threshold:
        ref = "trunk" if trunk is not None else ("baseline" if baseline is not None else "0")
        return (
            f"[skipped: node score {node.score:.2f} <= {ref} {threshold:.2f} "
            f"— SearchIdeaContext only runs on nodes that beat trunk. Set "
            f"search.require_validated=False to override.]"
        )
    return None


def _track(task: asyncio.Task) -> asyncio.Task:
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


def dispatch_auto_search(
    tree: "IdeaTree",
    config: "CoordinatorConfig",
    provider: "LLMProvider",
    node_id: str,
    focus: str | None = None,
) -> bool:
    """Fire-and-forget a *pre-experiment* novelty check on a freshly-added node.

    Unlike ``SearchIdeaContext``, this bypasses ``_validation_gate`` (it calls
    ``_run_one`` directly), so a pending/unexecuted node is searched on its
    hypothesis alone. The verdict is written to ``node.related_work``; it is
    advisory and never blocks the coordinator.

    No-op (returns False) unless ``search.enabled``, a backend is configured,
    and ``search.auto_search_on_add`` is set. The task is tracked in
    ``_BG_TASKS`` so the orchestrator's shutdown flush awaits it.
    """
    sc = getattr(config, "search", None)
    if not (sc and sc.enabled and sc.has_backend and sc.auto_search_on_add):
        return False
    if focus is None:
        focus = sc.auto_search_focus
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop — cannot dispatch a background task.
        log.warning("dispatch_auto_search: no running event loop for %s", node_id)
        return False
    _track(
        loop.create_task(
            _run_one(
                tree=tree,
                config=config,
                provider=provider,
                node_id=node_id,
                focus=focus,
            ),
            name=f"search-agent(pre):{node_id}",
        )
    )
    return True


def pending_search_count() -> int:
    """Return the number of in-flight background SearchAgent tasks."""
    return sum(1 for t in _BG_TASKS if not t.done())


async def wait_for_pending_searches(timeout: float | None = None) -> int:
    """Wait until all in-flight background SearchAgent tasks complete.

    Returns the number of tasks that were awaited. Safe to call when no
    searches are in flight (returns 0). Exceptions inside the tasks are
    swallowed (they were already converted to ``[search-failed: ...]``
    markers by ``_run_one``); this helper just guarantees the tasks finished.
    """
    pending = [t for t in _BG_TASKS if not t.done()]
    if not pending:
        return 0
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning(
            "wait_for_pending_searches timed out with %d tasks still running",
            sum(1 for t in pending if not t.done()),
        )
    return len(pending)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

# Moved to _agent_recover (shared with the research lane). Re-exported here so
# existing importers (e.g. cli/commands/idea_check_cmd.py) keep working.
from ._agent_recover import _extract_json_block, recover_json  # noqa: E402,F401


def _render_markdown(parsed: dict[str, Any]) -> str:
    """Render the SearchAgent's JSON into the documented Markdown shape."""
    summary = str(parsed.get("summary", "")).strip()
    novelty = str(parsed.get("novelty_assessment", "")).strip()
    overlap = str(parsed.get("overlap_risks", "")).strip()
    papers = parsed.get("related_papers") or []

    lines: list[str] = []
    if summary:
        lines.append("### Summary")
        lines.append(summary)
        lines.append("")

    lines.append("### Related Papers")
    if isinstance(papers, list) and papers:
        for p in papers:
            if not isinstance(p, dict):
                continue
            title = str(p.get("title", "")).strip() or "(untitled)"
            url = str(p.get("url", "")).strip()
            rel = str(p.get("one_line_relevance", "")).strip()
            if url:
                lines.append(f"- [{title}]({url}) — {rel}" if rel else f"- [{title}]({url})")
            else:
                lines.append(f"- {title} — {rel}" if rel else f"- {title}")
    else:
        lines.append("- (none found)")
    lines.append("")

    if novelty:
        lines.append("### Novelty")
        lines.append(novelty)
        lines.append("")

    if overlap:
        lines.append("### Overlap Risks")
        lines.append(overlap)

    return "\n".join(lines).rstrip() + "\n"


def _gather_ancestor_insights(tree: "IdeaTree", node_id: str) -> str:
    """Return parent + grandparent insight excerpts as background context."""
    path = tree.get_path_to_root(node_id)
    ancestors = path[1:]  # parent → root
    chunks: list[str] = []
    for n in ancestors:
        if n.insight:
            ins = n.insight.strip()
            if len(ins) > _ANCESTOR_INSIGHT_CHARS:
                ins = ins[: _ANCESTOR_INSIGHT_CHARS - 3] + "..."
            chunks.append(f"- [{n.id}]: {ins}")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Core: run one SearchAgent
# ---------------------------------------------------------------------------

async def _run_one(
    *,
    tree: "IdeaTree",
    config: "CoordinatorConfig",
    provider: "LLMProvider",
    node_id: str,
    focus: str | None,
) -> str:
    """Build + run a SearchAgent for a single node. Always succeeds (best-effort).

    Returns a short summary string for the coordinator (≤ ~1KB).
    """
    from ...search_agent.agent import build_search_agent
    from ...search_agent.prompts import build_search_user_prompt

    sc = config.search

    node = tree.get_node(node_id)
    if node is None:
        return f"Error: node {node_id!r} not found."
    if not node.hypothesis.strip():
        return f"Error: node {node_id} has no hypothesis to search for."

    if not sc.has_backend:
        marker = "[search-failed: no search backend configured]"
        await tree.async_update_node(node_id, related_work=marker)
        return f"{node_id}: {marker}"

    ancestor_insights = _gather_ancestor_insights(tree, node_id)
    user_prompt = build_search_user_prompt(
        hypothesis=node.hypothesis,
        ancestor_insights=ancestor_insights,
        focus=focus,
    )

    raw = ""
    agent = None
    try:
        agent = build_search_agent(
            provider=provider,
            search_config=sc,
            cwd=config.cwd,
            meta_config=config,
            event_bus=tree.bus,
            context_window=config.context_window,
        )
        if sc.agent_timeout is None or sc.agent_timeout <= 0:
            # Unlimited — let the SearchAgent run to completion
            # (still bounded by agent_max_turns).
            raw = await agent.run(user_prompt)
        else:
            raw = await asyncio.wait_for(
                agent.run(user_prompt),
                timeout=sc.agent_timeout,
            )
    except asyncio.TimeoutError:
        # The agent may have emitted a valid final JSON before the deadline;
        # recover it from the transcript instead of discarding the work.
        recovered = recover_json(agent, "") if agent is not None else None
        if recovered is None:
            marker = f"[search-failed: timed out after {sc.agent_timeout}s]"
            await tree.async_update_node(node_id, related_work=marker)
            log.warning("SearchAgent for %s timed out", node_id)
            return f"{node_id}: {marker}"
        log.info("SearchAgent for %s timed out but a final JSON was recovered", node_id)
        parsed = recovered
    except Exception as exc:  # noqa: BLE001
        marker = f"[search-failed: {type(exc).__name__}: {exc}]"
        await tree.async_update_node(node_id, related_work=marker)
        log.warning("SearchAgent for %s failed: %s", node_id, exc)
        return f"{node_id}: {marker}"
    else:
        parsed = recover_json(agent, raw)

    if parsed is None:
        # Fallback: store the raw assistant text with an [unparsed] tag.
        snippet = raw.strip()
        if len(snippet) > 4000:
            snippet = snippet[:4000] + "\n\n[... truncated ...]"
        block = f"[unparsed JSON — raw output]\n\n{snippet}"
        await tree.async_update_node(node_id, related_work=block)
        return f"{node_id}: SearchAgent ran but JSON could not be parsed (raw text saved)."

    md = _render_markdown(parsed)
    await tree.async_update_node(node_id, related_work=md)

    summary_text = str(parsed.get("summary", "")).strip()
    assessment = str(parsed.get("novelty_assessment", "")).strip() or "?"
    n_papers = (
        len(parsed["related_papers"])
        if isinstance(parsed.get("related_papers"), list) else 0
    )
    short_summary = (
        summary_text[:300] + "..." if len(summary_text) > 300 else summary_text
    )
    return (
        f"{node_id}: novelty={assessment}, papers={n_papers}\n"
        f"summary: {short_summary}"
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class SearchIdeaContextTool(Tool):
    """Dispatch a SearchAgent to annotate one idea node with related work."""

    name = "SearchIdeaContext"
    description = (
        "Dispatch a SearchAgent to survey related work / prior art for a "
        "single idea node, and write the result back to the node's "
        "``related_work`` field.\n\n"
        "**Eligibility**: by default this only runs on **validated, "
        "effective** nodes — status in {done, merged} AND score > "
        "trunk_score. Calling it on a pending / unscored / underperforming "
        "node returns a ``[skipped: ...]`` message and does NOT spend any "
        "search budget. The intent is to spend novelty-check cost only on "
        "ideas that already proved out experimentally and are merge "
        "candidates. Override with ``search.require_validated=False``.\n\n"
        "By default this runs in the BACKGROUND: the call returns "
        "immediately with a 'dispatched' message, the SearchAgent runs "
        "concurrently with your other work (more IDEATE rounds, "
        "RunExecutor dispatches, etc.), and the result is written to the "
        "node's ``related_work`` field whenever the search completes. "
        "Check `SearchStatus()` for in-flight count, or `TreeView("
        "format=\"node\", node_id=...)` to read a finished annotation.\n\n"
        "Failures never raise — they leave a ``[search-failed: ...]`` tag "
        "on the node."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "Tree node id (e.g. '1.2.1'). Must already exist.",
            },
            "focus": {
                "type": "string",
                "description": (
                    "Optional one-line directive that biases the search "
                    "(e.g. 'prefer arxiv 2024+', 'focus on tool-use angle'). "
                    "Leave empty for a generic novelty survey."
                ),
            },
        },
        "required": ["node_id"],
    }
    is_read_only = False  # mutates tree

    def __init__(
        self,
        *,
        cwd: str,
        tree: "IdeaTree",
        config: "CoordinatorConfig",
        provider: "LLMProvider",
        **kwargs: Any,
    ):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree
        self._config = config
        self._provider = provider

    async def execute(self, **kwargs: Any) -> str:
        node_id: str = kwargs["node_id"]
        focus: str | None = kwargs.get("focus") or None

        # Pre-flight validation so the coordinator gets immediate feedback
        # on obvious errors (unknown node / missing endpoint / unvalidated
        # node) instead of discovering them only when the background task
        # finishes.
        node = self._tree.get_node(node_id)
        if node is None:
            return f"Error: node {node_id!r} not found."
        if not node.hypothesis.strip():
            return f"Error: node {node_id} has no hypothesis to search for."
        if not self._config.search.has_backend:
            marker = "[search-failed: no search backend configured]"
            await self._tree.async_update_node(node_id, related_work=marker)
            return f"{node_id}: {marker}"

        gate_msg = _validation_gate(
            self._tree, node, self._config.search.require_validated,
        )
        if gate_msg is not None:
            return f"{node_id}: {gate_msg}"

        if not self._config.search.background:
            return await _run_one(
                tree=self._tree,
                config=self._config,
                provider=self._provider,
                node_id=node_id,
                focus=focus,
            )

        _track(asyncio.create_task(
            _run_one(
                tree=self._tree,
                config=self._config,
                provider=self._provider,
                node_id=node_id,
                focus=focus,
            ),
            name=f"search-agent:{node_id}",
        ))
        return (
            f"{node_id}: SearchAgent dispatched in background "
            f"(pending searches: {pending_search_count()}). "
            f"Continue with your other work; the result will land on "
            f"node.related_work when the search completes."
        )


class SearchIdeaContextParallelTool(Tool):
    """Annotate up to 4 leaf nodes with related work in parallel."""

    name = "SearchIdeaContextParallel"
    description = (
        "Annotate multiple nodes with related-work surveys in parallel "
        "(up to 4 concurrent SearchAgents). Each annotation is written back "
        "to its node's ``related_work`` field independently; one failure "
        "does not affect the others.\n\n"
        "**Eligibility**: same gate as SearchIdeaContext — only "
        "done/merged nodes with score > trunk_score are admitted; others "
        "are reported as ``[skipped: ...]`` and do not consume a "
        "SearchAgent slot. Override with ``search.require_validated=False``.\n\n"
        "Use after several sibling branches have been executed and you "
        "want novelty annotations on the ones that beat trunk, before "
        "deciding which to merge."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "List of tree node ids to annotate (up to 4 run concurrently).",
            },
            "focus": {
                "type": "string",
                "description": "Optional shared focus directive applied to every node.",
            },
        },
        "required": ["node_ids"],
    }
    is_read_only = False

    def __init__(
        self,
        *,
        cwd: str,
        tree: "IdeaTree",
        config: "CoordinatorConfig",
        provider: "LLMProvider",
        **kwargs: Any,
    ):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree
        self._config = config
        self._provider = provider

    async def execute(self, **kwargs: Any) -> str:
        node_ids_raw = kwargs.get("node_ids") or []
        if isinstance(node_ids_raw, str):
            # Tolerate accidentally-passed JSON string.
            try:
                node_ids = json.loads(node_ids_raw)
            except Exception:
                node_ids = [node_ids_raw]
        else:
            node_ids = list(node_ids_raw)
        node_ids = [str(n) for n in node_ids if str(n).strip()]
        if not node_ids:
            return "Error: node_ids must be a non-empty list."

        focus: str | None = kwargs.get("focus") or None
        sem = asyncio.Semaphore(_MAX_PARALLEL)

        # Apply the validation gate up-front. Skipped nodes are reported
        # but do not consume a SearchAgent slot.
        skipped: list[str] = []
        admitted: list[str] = []
        for nid in node_ids:
            node = self._tree.get_node(nid)
            if node is None:
                skipped.append(f"{nid}: [skipped: node not found]")
                continue
            if not node.hypothesis.strip():
                skipped.append(f"{nid}: [skipped: no hypothesis]")
                continue
            gate_msg = _validation_gate(
                self._tree, node, self._config.search.require_validated,
            )
            if gate_msg is not None:
                skipped.append(f"{nid}: {gate_msg}")
                continue
            admitted.append(nid)

        async def _bounded(nid: str) -> str:
            async with sem:
                return await _run_one(
                    tree=self._tree,
                    config=self._config,
                    provider=self._provider,
                    node_id=nid,
                    focus=focus,
                )

        if not self._config.search.background:
            results = await asyncio.gather(
                *[_bounded(nid) for nid in admitted],
                return_exceptions=True,
            )
            lines: list[str] = list(skipped)
            for nid, res in zip(admitted, results):
                if isinstance(res, Exception):
                    lines.append(f"{nid}: [search-failed: {type(res).__name__}: {res}]")
                else:
                    lines.append(str(res))
            return "\n\n".join(lines) if lines else "No nodes to search."

        # Background: schedule each as its own task (still capped by sem).
        for nid in admitted:
            _track(asyncio.create_task(
                _bounded(nid),
                name=f"search-agent:{nid}",
            ))
        head = (
            f"Dispatched {len(admitted)} SearchAgent(s) in background "
            f"(pending: {pending_search_count()}, concurrency cap: "
            f"{_MAX_PARALLEL}). Results will land on each node's "
            f"related_work as searches complete."
        )
        if skipped:
            head += "\n\nSkipped nodes:\n" + "\n".join(skipped)
        return head


class SearchStatusTool(Tool):
    """Report how many background SearchAgents are currently running."""

    name = "SearchStatus"
    description = (
        "Report the number of SearchAgent annotations currently running in "
        "the background. Use this to check whether your earlier "
        "SearchIdeaContext / SearchIdeaContextParallel calls have finished "
        "writing to the tree before you read the related_work field."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
    }
    is_read_only = True

    async def execute(self, **kwargs: Any) -> str:  # noqa: ARG002
        n = pending_search_count()
        if n == 0:
            return "No background SearchAgents are currently running."
        names = sorted(
            t.get_name() for t in _BG_TASKS if not t.done()
        )
        return f"{n} background SearchAgent(s) running: {', '.join(names)}"
