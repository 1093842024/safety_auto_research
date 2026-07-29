"""RunExecutor tools — dispatch Research Agents in isolated git worktrees.

Each executor gets its own worktree (branched from current trunk HEAD),
so multiple executors can run in parallel without interfering with each
other or the main working directory.
"""

# pylint: disable=broad-exception-caught,protected-access

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ...core.tools.base import Tool
from ..hitl import await_user_decision
from .tree_ops import propagate_insights
from .git_ops import _run_git
from .worktree import (
    _compute_branch_name,
    _create_worktree,
    _finalize_worktree,
    _remove_worktree,
)
from .integrity import (
    apply_readonly,
    build_protected_manifest,
    clear_readonly,
    verify_protected_manifest,
)
from .protected import resolve_protected_paths
from .executor_io import (
    _build_executor_prompt,
    _build_resume_context,
    _gather_ancestor_insights,
    _get_eval_info,
    _parse_executor_report,
)

if TYPE_CHECKING:
    from ...core.agent import Agent
    from ..config import CoordinatorConfig
    from ..idea_tree import IdeaTree, Node
    from ...core.llm.base import LLMProvider

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Experiment artifact persistence
# ---------------------------------------------------------------------------


_CYCLE_STATUSES = {"done", "merged", "pruned", "failed", "needs_retry"}


def _classify_executor_outcome(
    *,
    score: Any,
    eval_status: str | None,
    stop_reason: str | None,
    raw_report: str,
) -> str:
    """Decide a node's terminal status from an executor run's outcome.

    A node is only "done" when it produced a real metric, or when eval was
    *intentionally* skipped on otherwise-complete work. Turn-cap / timeout /
    error / eval-crash exits become "needs_retry" — an incomplete-but-not-
    abandoned state that is excluded from every "completed experiment" filter
    (best-node, convergence, reports) and can be resumed via ResumeExecutor.
    """
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return "done"  # produced a metric — trust it even on a late stop
    if raw_report.startswith("[Timed out") or raw_report.startswith("[Error:"):
        return "needs_retry"
    if stop_reason == "max_turns":
        return "needs_retry"
    if eval_status == "skipped":
        return "done"  # intentional no-eval on solid work — acceptable
    return "needs_retry"  # failed_to_run / unparseable report


def _completed_cycles(tree: "IdeaTree") -> int:
    """Count nodes that consume a cycle budget.

    A cycle is consumed once a executor finishes (regardless of outcome) or
    once a branch is pruned/merged. Failed runs are counted on purpose — they
    spent compute, so they spend budget.
    """
    return sum(
        1 for n in tree.get_all_nodes()
        if n.id != tree.root_id and n.status in _CYCLE_STATUSES
    )


async def _save_experiment_artifacts(
    *,
    config: "CoordinatorConfig",
    node_id: str,
    hypothesis: str,
    raw_report: str,
    parsed: dict[str, Any],
    actual_branch: str,
    agent_turns: int,
    status: str = "done",
    eval_status: str | None = None,
    stop_reason: str | None = None,
    attempt: int = 1,
) -> None:
    """Save per-experiment artifacts to the workspace experiments/ directory."""
    workspace = config.workspace_dir
    if not workspace:
        return

    exp_dir = Path(workspace) / "experiments" / node_id
    exp_dir.mkdir(parents=True, exist_ok=True)

    report_md = (
        f"# Experiment {node_id}\n\n"
        f"**Hypothesis**: {hypothesis}\n"
        f"**Branch**: `{actual_branch}`\n"
        f"**Attempt**: {attempt}\n"
        f"**Status**: {status}\n"
        f"**Eval status**: {eval_status or 'unknown'}"
        + (f" (stop_reason={stop_reason})" if stop_reason else "")
        + "\n"
        f"**Turns**: {agent_turns}\n\n"
        f"---\n\n"
        f"{raw_report}\n"
    )
    (exp_dir / "report.md").write_text(report_md, encoding="utf-8")

    metrics = {
        "node_id": node_id,
        "hypothesis": hypothesis,
        "score": parsed.get("score"),
        "insight": parsed.get("insight", ""),
        "result": parsed.get("result", ""),
        "branch": actual_branch,
        "turns": agent_turns,
        "status": status,
        "eval_status": eval_status,
        "stop_reason": stop_reason,
        "attempt": attempt,
    }
    (exp_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8",
    )

    trunk = config.trunk_branch or "HEAD"
    diff_out, rc = await _run_git(
        f"git diff {shlex.quote(trunk)}...{shlex.quote(actual_branch)} --stat",
        config.cwd,
    )
    if rc == 0 and diff_out.strip():
        full_diff, _ = await _run_git(
            f"git diff {shlex.quote(trunk)}...{shlex.quote(actual_branch)}",
            config.cwd,
        )
        (exp_dir / "diff.patch").write_text(full_diff, encoding="utf-8")


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


async def _run_after_executor_hook(
    config: "CoordinatorConfig",
    worktree_path: Path,
    node_id: str,
) -> None:
    """Run the after_executor lifecycle hook — snapshot submission if it exists."""
    plugin = config.plugin
    if not plugin or not plugin.lifecycle_hooks:
        return

    hook = plugin.lifecycle_hooks.get("after_executor")
    if not hook:
        return

    submission_rel = plugin.eval_contract.get("submission_path", "submission.csv")
    submission = worktree_path / submission_rel
    if submission.exists():
        workspace_root = Path(config.workspace_dir) if config.workspace_dir else Path(config.cwd)
        snapshot_dir = workspace_root / "submissions"
        snapshot_dir.mkdir(exist_ok=True)
        ext = Path(submission_rel).suffix or ".csv"
        snapshot_name = f"{node_id}{ext}"
        shutil.copy2(submission, snapshot_dir / snapshot_name)
        log.info("Snapshot submission for %s -> submissions/%s", node_id, snapshot_name)


# ---------------------------------------------------------------------------
# Protected-path tamper guard
# ---------------------------------------------------------------------------

def _guard_protected(config: Any, tree: Any, worktree_path: Path) -> tuple[list[str], dict[str, str]]:
    """Snapshot + lock protected paths in a fresh worktree.

    Returns ``(resolved_paths, manifest)``. Both empty when enforcement is off
    or no protected paths are configured. Never raises.
    """
    if not getattr(config, "enforce_protected", True):
        return [], {}
    paths = resolve_protected_paths(config, getattr(config, "plugin", None), tree.meta)
    if not paths:
        return [], {}
    try:
        manifest = build_protected_manifest(worktree_path, paths)
        apply_readonly(worktree_path, paths)
        return paths, manifest
    except Exception as exc:  # noqa: BLE001
        log.warning("integrity: failed to guard protected paths: %s", exc)
        return [], {}


def _check_tamper(
    config: Any,
    tree: Any,
    worktree_path: Path,
    paths: list[str],
    manifest: dict[str, str],
    node_id: str,
    branch: str,
) -> list[Any]:
    """Clear read-only + verify the manifest. Emits PROTECTED_TAMPER on change."""
    if not paths:
        return []
    from ...events import types as ev

    clear_readonly(worktree_path, paths)
    try:
        changes = verify_protected_manifest(worktree_path, paths, manifest)
    except Exception as exc:  # noqa: BLE001
        log.warning("integrity: verify failed (treating as clean): %s", exc)
        return []
    if changes:
        tree.bus.emit(ev.PROTECTED_TAMPER, {
            "node_id": node_id,
            "branch": branch,
            "changes": [{"path": c.path, "kind": c.kind} for c in changes],
        })
        log.warning(
            "integrity: node %s tampered with protected paths: %s",
            node_id, ", ".join(f"{c.path}({c.kind})" for c in changes),
        )
    return changes


# ---------------------------------------------------------------------------
# Core: run a single executor in an isolated worktree
# ---------------------------------------------------------------------------

def _format_executor_summary(
    *,
    node_id: str,
    hypothesis: str,
    new_status: str,
    attempt: int,
    score: float | None,
    insight: str,
    code_ref: str,
    agent_turns: int,
    propagation_result: str,
    raw_report: str,
    eval_status: str | None,
    stop_reason: str | None,
) -> str:
    """Render the human-facing result string returned by a executor run."""
    score_str = f"{score:.1f}%" if score is not None else "N/A"

    # Include a reasonable excerpt of the raw report
    report_excerpt = raw_report
    if len(raw_report) > 8000:
        report_excerpt = (
            raw_report[:4000]
            + f"\n\n[... middle truncated, full report was {len(raw_report)} chars ...]\n\n"
            + raw_report[-4000:]
        )

    retry_hint = ""
    if new_status == "needs_retry":
        retry_hint = (
            "\n\n> This node is **needs_retry** (no score — "
            f"{eval_status}"
            + (f", stop_reason={stop_reason}" if stop_reason else "")
            + "). The branch above preserves its committed work. To continue it "
            "with extra turns and the prior report injected, call "
            f"`ResumeExecutor(node_id={node_id!r})`; or `RunExecutor` to retry "
            "from trunk, or `TreePrune` to abandon."
        )

    return (
        f"## Executor Result for {node_id}\n\n"
        f"**Hypothesis**: {hypothesis}\n"
        f"**Status**: {new_status} (attempt {attempt})\n"
        f"**Score**: {score_str}\n"
        f"**Insight**: {insight}\n"
        f"**Branch**: `{code_ref}`\n"
        f"**Turns**: {agent_turns}\n\n"
        f"### Propagation\n{propagation_result}\n\n"
        f"### Report Excerpt\n\n{report_excerpt}"
        f"{retry_hint}"
    )


def _validate_dispatch(
    tree: "IdeaTree", node_id: str, resume: bool
) -> tuple["Node | None", int, str | None]:
    """Pre-flight checks for a dispatch.

    Returns ``(node, attempt, error)``. When ``error`` is non-None the dispatch
    must abort and return that message to the caller; ``node`` is None then.
    """
    # ── Early stop: gold already achieved ──────────────────────────
    if tree.meta.get("achieved_medal") == "gold":
        return None, 0, (
            f"Early stop: Gold medal already achieved on trunk. "
            f"No further experiments needed. Node {node_id} was NOT dispatched."
        )

    node = tree.get_node(node_id)
    if node is None:
        return None, 0, f"Error: Node {node_id!r} not found in the idea tree."
    if node.status not in ("pending", "running", "needs_retry"):
        return None, 0, (
            f"Error: Node {node_id} has status={node.status!r}. "
            f"Only 'pending' or 'needs_retry' nodes can be dispatched."
        )

    # Attempt number for this dispatch (1 for the first run, +1 per resume).
    attempt = node.attempt + 1 if resume else node.attempt

    # ── Enforce leaf-only dispatch when max_depth is set ───────────
    if tree.max_depth is not None and node.depth < tree.max_depth:
        return None, attempt, (
            f"Error: Node {node_id} is at depth {node.depth}, but max_depth "
            f"is {tree.max_depth}. Only leaf nodes (depth={tree.max_depth}) "
            f"can be dispatched for experiments. Please refine this idea into "
            f"more specific sub-ideas using TreeAddNode before dispatching."
        )

    return node, attempt, None


async def _build_and_run_executor_agent(
    *,
    tree: "IdeaTree",
    config: "CoordinatorConfig",
    provider: "LLMProvider",
    node: "Node",
    node_id: str,
    worktree_path: Path,
    actual_branch: str,
    attempt: int,
    resume: bool,
    extra_turns: int,
    additional_context: str | None,
) -> tuple[str, int, str | None, "Agent | None"]:
    """Build the executor Agent, run it under timeout, and capture its outcome.

    Returns ``(raw_report, agent_turns, stop_reason, agent)``. Timeout and
    unexpected errors are caught and reflected in ``raw_report`` (so the caller
    can classify the outcome) rather than propagated.
    """
    from ...core.agent import Agent
    from ...core.tools import get_all_tools
    from ...core.tools.executor_tool import ExecutorTool
    from ...executor.prompts import build_system_prompt

    raw_report = ""
    agent_turns = 0
    stop_reason: str | None = None
    agent: Agent | None = None

    try:
        executor_config = config.to_executor_config(node_id, node.hypothesis)
        executor_config.cwd = str(worktree_path)
        executor_config.event_bus = tree.bus
        if resume and extra_turns:
            executor_config.max_turns += extra_turns

        system_prompt = build_system_prompt(executor_config, plugin=config.plugin)
        tools = get_all_tools(
            cwd=str(worktree_path),
            workspace_dir=executor_config.workspace_dir,
            config=executor_config,
        )

        agent = Agent(
            provider=provider,
            tools=tools,
            system_prompt=system_prompt,
            config=executor_config,
        )

        # Pre-initialize git manager — worktree already has the correct branch
        agent.git_manager._initialized = True
        agent.git_manager.branch_name = actual_branch
        agent.git_manager.cwd = str(worktree_path)

        # Add Executor tool for nested delegation
        executor_tool = ExecutorTool(cwd=str(worktree_path), parent_agent=agent, workspace_dir=executor_config.workspace_dir)
        agent.tools[executor_tool.name] = executor_tool

        # ── Build prompt with auto-injected eval info ───────────────────
        ancestor_insights = _gather_ancestor_insights(tree, node_id)
        eval_info = _get_eval_info(
            tree,
            worktree_cwd=str(worktree_path),
            node_id=node_id,
        )
        merged_context = additional_context
        if resume:
            prior = _build_resume_context(config, node, attempt)
            merged_context = "\n\n".join(c for c in (prior, additional_context) if c)
        prompt = _build_executor_prompt(
            worktree_path=worktree_path,
            node=node,
            ancestor_insights=ancestor_insights,
            eval_info=eval_info,
            additional_context=merged_context,
        )

        log.info(
            "Dispatching executor for %s in worktree %s (branch=%s, timeout=%ds)",
            node_id, worktree_path, actual_branch, config.executor_timeout,
        )

        # ── Run executor ────────────────────────────────────────────────
        result = await asyncio.wait_for(
            agent.run(prompt),
            timeout=config.executor_timeout,
        )
        raw_report = result
        agent_turns = agent.total_turns
        stop_reason = agent.stop_reason

    except asyncio.TimeoutError:
        agent_turns = agent.total_turns if agent is not None else 0
        stop_reason = agent.stop_reason if agent is not None else None
        raw_report = f"[Timed out after {config.executor_timeout}s]"
        log.warning("Executor for %s timed out after %ds", node_id, config.executor_timeout)

    except Exception as e:
        agent_turns = agent.total_turns if agent is not None else 0
        stop_reason = agent.stop_reason if agent is not None else None
        raw_report = f"[Error: {e}]"
        log.error("Executor for %s failed: %s", node_id, e)

    return raw_report, agent_turns, stop_reason, agent


async def _run_single_executor(
    *,
    tree: "IdeaTree",
    config: "CoordinatorConfig",
    provider: "LLMProvider",
    node_id: str,
    additional_context: str | None = None,
    resume: bool = False,
    extra_turns: int = 0,
) -> str:
    """Run one executor in an isolated git worktree.

    Handles the full lifecycle: validate → worktree → run → parse → update tree.

    When ``resume`` is set, the worktree branches from the node's preserved
    ``code_ref`` (the prior attempt's committed work) instead of trunk, the turn
    budget is raised by ``extra_turns``, and the prior attempt's report/diff are
    injected as context (see ResumeExecutor).
    """
    # ── 1. Validate node & resolve attempt ─────────────────────────────
    node, attempt, error = _validate_dispatch(tree, node_id, resume)
    if error is not None:
        return error
    assert node is not None  # validated above; for type-checkers

    from ...events import types as ev

    # ── 2. Mark as running ──────────────────────────────────────────────
    await tree.async_update_node(node_id, status="running")
    cycle_num = _completed_cycles(tree) + 1
    tree.bus.emit(ev.CYCLE_START, {
        "cycle_num": cycle_num,
        "total_cycles": config.max_cycles,
        "node_id": node_id,
    })

    # ── 3. Create worktree ──────────────────────────────────────────────
    # On resume, continue the prior attempt's branch so its committed code is
    # the starting point; otherwise branch fresh from trunk. The attempt suffix
    # keeps each resume on its own auditable branch.
    resume_from = node.code_ref if (resume and node.code_ref) else None
    branch_name = _compute_branch_name(config, node_id, node.hypothesis)
    if resume_from:
        branch_name = f"{branch_name}-a{attempt}"
    start_point = resume_from or config.trunk_branch
    worktree_path: Path | None = None
    actual_branch = branch_name

    try:
        worktree_path, actual_branch = await _create_worktree(
            config.cwd, branch_name, start_point=start_point,
        )
    except RuntimeError as e:
        # Worktree setup failed before anything ran — no compute spent, so keep
        # it re-dispatchable (pending) rather than consuming a cycle as needs_retry.
        await tree.async_update_node(node_id, status="pending", result=f"Worktree creation failed: {e}")
        return f"Error creating worktree for {node_id}: {e}"
    tree.bus.emit(ev.EXECUTOR_START, {
        "node_id": node_id,
        "idea": node.hypothesis,
        "branch": actual_branch,
        "cycle_num": cycle_num,
    })

    # Snapshot + lock protected paths so any mid-run tampering is detectable.
    protected_paths, protected_manifest = _guard_protected(config, tree, worktree_path)

    # ── 4-6. Build the executor agent and run it under timeout ──────────
    executor_t0 = asyncio.get_running_loop().time()
    raw_report, agent_turns, stop_reason, agent = await _build_and_run_executor_agent(
        tree=tree,
        config=config,
        provider=provider,
        node=node,
        node_id=node_id,
        worktree_path=worktree_path,
        actual_branch=actual_branch,
        attempt=attempt,
        resume=resume,
        extra_turns=extra_turns,
        additional_context=additional_context,
    )

    # ── 7. Finalize & clean up worktree ─────────────────────────────────
    # Verify protected paths BEFORE finalize commits anything, so uncommitted
    # tampering is caught regardless of what gets committed to the branch.
    tamper_changes: list[Any] = []
    if worktree_path is not None:
        tamper_changes = _check_tamper(
            config, tree, worktree_path, protected_paths, protected_manifest,
            node_id, actual_branch,
        )

    if worktree_path is not None:
        try:
            await _finalize_worktree(worktree_path, node_id)
        except Exception as e:
            log.warning("Failed to finalize worktree for %s: %s", node_id, e)

        # ── 7b. after_executor lifecycle hook ─────────────────────────
        try:
            await _run_after_executor_hook(config, worktree_path, node_id)
        except Exception as e:
            log.warning("after_executor hook failed for %s: %s", node_id, e)

        await _remove_worktree(config.cwd, worktree_path)

    # ── 8. Parse report ─────────────────────────────────────────────────
    try:
        parsed = await _parse_executor_report(
            provider,
            raw_report,
            node.hypothesis,
            bus=tree.bus,
            cwd=str(worktree_path) if worktree_path is not None else config.cwd,
        )
    except Exception as e:
        log.warning("Failed to parse report for %s: %s", node_id, e)
        parsed = {}

    score = parsed.get("score")
    insight = parsed.get("insight", "")
    result_text = parsed.get("result", "")
    code_ref = parsed.get("code_ref") or actual_branch
    eval_status = parsed.get("eval_status", "failed_to_run")

    # A tampered protected path makes the dev score untrustworthy: discard it so
    # this node cannot be merged on an inflated number.
    insight_override = ""
    if tamper_changes:
        score = None
        eval_status = "tampered"
        insight_override = (
            "Protected path(s) modified during the run: "
            + ", ".join(f"{c.path}({c.kind})" for c in tamper_changes)
            + ". Dev score discarded; branch is not mergeable."
        )

    # ── 9. Update tree node ─────────────────────────────────────────────
    # Only a real score (or an intentionally-skipped eval on solid work) counts
    # as "done"; turn-cap / timeout / error / eval-crash become "needs_retry".
    new_status = _classify_executor_outcome(
        score=score,
        eval_status=eval_status,
        stop_reason=stop_reason,
        raw_report=raw_report,
    )
    await tree.async_update_node(
        node_id,
        status=new_status,
        score=score,
        score_split="dev",
        insight=insight_override or insight or ("Timed out" if raw_report.startswith("[Timed out") else ""),
        result=result_text or raw_report[:300],
        code_ref=code_ref,
        eval_status=eval_status,
        stop_reason=stop_reason,
        attempt=attempt,
    )
    duration = max(0.0, asyncio.get_running_loop().time() - executor_t0)
    tree.bus.emit(ev.EXECUTOR_END, {
        "node_id": node_id,
        "score": score,
        "duration": duration,
        "tokens": (
            (agent.total_input_tokens + agent.total_output_tokens)
            if agent is not None else None
        ),
        "turns": agent_turns,
        "branch": code_ref,
        "status": new_status,
    })
    # The evaluation phase concluded with this node's outcome. `error` marks an
    # eval that was attempted but produced no metric (vs. a clean score or an
    # intentional skip), which the stats collector counts as an eval failure.
    tree.bus.emit(ev.EVAL_END, {
        "node_id": node_id,
        "score": score,
        "duration": duration,
        "error": eval_status == "failed_to_run",
    })
    tree.bus.emit(ev.CYCLE_END, {
        "cycle_num": cycle_num,
        "total_cycles": config.max_cycles,
        "node_id": node_id,
        "duration": duration,
    })

    # ── 9b. Save experiment artifacts to workspace ─────────────────────
    try:
        await _save_experiment_artifacts(
            config=config,
            node_id=node_id,
            hypothesis=node.hypothesis,
            raw_report=raw_report,
            parsed=parsed,
            actual_branch=actual_branch,
            agent_turns=agent_turns,
            status=new_status,
            eval_status=eval_status,
            stop_reason=stop_reason,
            attempt=attempt,
        )
    except Exception as e:
        log.warning("Failed to save experiment artifacts for %s: %s", node_id, e)

    # ── 10. Propagate insights upward ───────────────────────────────────
    propagation_result = ""
    try:
        propagation_result = await propagate_insights(tree, provider, node_id)
    except Exception as e:
        log.warning("Propagation failed for %s: %s", node_id, e)
        propagation_result = f"Propagation failed: {e}"

    # ── 11. Format summary ──────────────────────────────────────────────
    return _format_executor_summary(
        node_id=node_id,
        hypothesis=node.hypothesis,
        new_status=new_status,
        attempt=attempt,
        score=score,
        insight=insight,
        code_ref=code_ref,
        agent_turns=agent_turns,
        propagation_result=propagation_result,
        raw_report=raw_report,
        eval_status=eval_status,
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------------
# HITL review gate (#2)
# ---------------------------------------------------------------------------

async def _review_gate(
    tree: "IdeaTree", config: "CoordinatorConfig", node_id: str, hypothesis: str,
) -> tuple[str, str | None]:
    """In ``review`` mode, ask the human before exploring a node.

    Returns ``(action, note)`` where ``action`` is ``"approve"`` or ``"skip"``
    and ``note`` is an optional free-text edit/comment to fold into the
    executor's context. No-op (auto-approve) in ``auto`` mode or on timeout.
    """
    interaction_mode = (getattr(config.ui, "interaction_mode", "auto") or "auto").lower()
    if interaction_mode not in ("review", "collaborative"):
        return ("approve", None)
    reply = await await_user_decision(
        tree.bus,
        kind="idea_review",
        prompt=f"Explore idea {node_id}: {hypothesis}",
        node_id=node_id,
        options=["approve", "skip", "edit <note>"],
        timeout=max(1, int(config.ui.review_timeout)),
    )
    if reply is None:
        log.info("review gate: node %s auto-approved (no review in window)", node_id)
        return ("approve", None)
    text = reply.strip()
    low = text.lower()
    if low in ("", "approve", "approved", "yes", "y", "ok", "go"):
        return ("approve", None)
    if low in ("skip", "no", "n", "reject"):
        return ("skip", None)
    if low.startswith("edit "):
        text = text[5:].strip()
    return ("approve", text or None)


def _react_convergence(
    detector: Any,
    tree: "IdeaTree",
    config: "CoordinatorConfig",
    result: str,
    signal: Any,
) -> str:
    """React to a convergence ``signal`` after experiment(s) complete.

    Appends the intervention text to ``result`` and, on a hard ``stop``, writes
    the stop signal and emits ``CONVERGENCE_REACHED`` so the dashboard/log/report
    learn why the run is winding down. No-op when there is no signal. Shared by
    the single and parallel executor tools so the reaction lives in one place.
    """
    if not signal:
        return result
    result += f"\n\n---\n{detector.format_intervention(signal)}\n---"
    if signal.level == "stop":
        detector.write_stop_signal(config.workspace_dir)
        from ...events import types as ev
        best = tree.get_best_done_node()
        tree.bus.emit(ev.CONVERGENCE_REACHED, {
            "reason": signal.reason,
            "final_score": best.score if best is not None else tree.meta.get("trunk_score"),
        })
    return result


# ---------------------------------------------------------------------------
# Tool: RunExecutor (single dispatch)
# ---------------------------------------------------------------------------

class RunExecutorTool(Tool):
    """Dispatch a single executor to implement and test a specific idea."""

    name = "RunExecutor"
    description = (
        "Dispatch a executor to implement and test a specific idea from the tree.\n\n"
        "The executor runs in an isolated git worktree branched from current trunk.\n"
        "It will:\n"
        "1. Implement the idea on an isolated branch\n"
        "2. Run evaluation (eval_cmd is auto-injected from tree metadata)\n"
        "3. Report results and insights\n\n"
        "The tree node is auto-updated with results and insights are propagated.\n\n"
        "For running multiple ideas at once, use RunExecutorParallel instead."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": (
                    "The idea node ID to implement (must be 'pending' or "
                    "'needs_retry'; a 'needs_retry' node restarts from trunk — "
                    "use ResumeExecutor to continue its preserved branch instead)."
                ),
            },
            "additional_context": {
                "type": "string",
                "description": (
                    "Extra context: relevant file paths, implementation hints, "
                    "insights from the tree. (Eval info is auto-injected — "
                    "no need to repeat it here.)"
                ),
            },
        },
        "required": ["node_id"],
    }
    is_read_only = False
    max_result_chars = 100_000

    def __init__(
        self,
        *,
        cwd: str,
        tree: "IdeaTree",
        config: "CoordinatorConfig",
        provider: "LLMProvider",
        convergence_detector: Any | None = None,
        **kwargs: Any,
    ):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree
        self._config = config
        self._provider = provider
        self._convergence_detector = convergence_detector

    async def execute(self, **kwargs: Any) -> str:
        done = _completed_cycles(self._tree)
        cap = self._config.max_cycles
        if done >= cap:
            return (
                f"HARD LIMIT REACHED: {done}/{cap} cycles already consumed "
                f"(counting done/merged/pruned/failed/needs_retry). RunExecutor is disabled. "
                f"Finalize now: merge the best branch if it beats the threshold, "
                f"otherwise stop and report."
            )
        # ── HITL review gate (#2): approve / skip / edit before spending compute ──
        node = self._tree.get_node(kwargs["node_id"])
        if node is not None:
            action, note = await _review_gate(
                self._tree, self._config, kwargs["node_id"], node.hypothesis)
            if action == "skip":
                self._tree.prune_node(kwargs["node_id"], reason="skipped by user in review")
                return (f"Idea {kwargs['node_id']} was skipped by the user (review mode); "
                        f"pruned, not explored. Propose or dispatch another idea.")
            if note:
                ctx = kwargs.get("additional_context")
                kwargs["additional_context"] = (
                    (f"{ctx}\n\n" if ctx else "") + f"User review note: {note}")
        result = await _run_single_executor(
            tree=self._tree,
            config=self._config,
            provider=self._provider,
            node_id=kwargs["node_id"],
            additional_context=kwargs.get("additional_context"),
        )
        # Check convergence after experiment completion
        if self._convergence_detector:
            signal = self._convergence_detector.on_experiment_complete(kwargs["node_id"])
            result = _react_convergence(
                self._convergence_detector, self._tree, self._config, result, signal
            )
        return result


class ResumeExecutorTool(RunExecutorTool):
    """Resume a ``needs_retry`` node, continuing its preserved branch.

    Unlike RunExecutor (which would restart from trunk), this continues from the
    node's ``code_ref`` branch — the prior attempt's committed work — with extra
    turns and the prior report/diff injected as context. Use it when an executor
    timed out, hit its turn cap, or failed to produce a score, and the partial
    work is worth finishing rather than discarding.
    """

    name = "ResumeExecutor"
    description = (
        "Resume a 'needs_retry' idea node: continue from its preserved branch "
        "(the prior attempt's committed work) with extra turns and the prior "
        "report/diff injected as context, so the executor finishes the work and "
        "produces a real score instead of starting over.\n\n"
        "Use when a node is 'needs_retry' (timed out / hit max turns / eval "
        "failed to run) and the partial work is worth continuing. To retry from "
        "scratch instead, use RunExecutor; to abandon, use TreePrune."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "The 'needs_retry' idea node ID to resume.",
            },
            "extra_turns": {
                "type": "integer",
                "description": "Extra turns added to the executor's budget (default 10).",
            },
            "additional_context": {
                "type": "string",
                "description": (
                    "Extra steering for the resumed attempt (optional). The prior "
                    "report/diff and eval info are injected automatically."
                ),
            },
        },
        "required": ["node_id"],
    }

    async def execute(self, **kwargs: Any) -> str:
        node_id = kwargs["node_id"]
        node = self._tree.get_node(node_id)
        if node is None:
            return f"Error: Node {node_id!r} not found in the idea tree."
        if node.status != "needs_retry":
            return (
                f"Error: ResumeExecutor only applies to 'needs_retry' nodes; "
                f"{node_id} is {node.status!r}. Use RunExecutor for a fresh dispatch."
            )
        if not node.code_ref:
            return (
                f"Error: Node {node_id} has no preserved branch (code_ref is None) — "
                f"the prior attempt likely crashed before committing any work, so "
                f"there is nothing to continue. Use RunExecutor to retry from trunk."
            )
        # node.attempt counts completed dispatches (1 = initial run); max_retries
        # is retries beyond that, so allow while attempt <= max_retries.
        max_retries = getattr(self._config, "max_retries", 3)
        if node.attempt > max_retries:
            return (
                f"Error: Node {node_id} has already used {node.attempt - 1} of "
                f"{max_retries} allowed retries. Prune it or accept the result "
                f"instead of resuming again."
            )
        done = _completed_cycles(self._tree)
        cap = self._config.max_cycles
        if done >= cap:
            return (
                f"HARD LIMIT REACHED: {done}/{cap} cycles already consumed. "
                f"ResumeExecutor is disabled. Finalize now."
            )
        result = await _run_single_executor(
            tree=self._tree,
            config=self._config,
            provider=self._provider,
            node_id=node_id,
            additional_context=kwargs.get("additional_context"),
            resume=True,
            extra_turns=int(kwargs.get("extra_turns", 10) or 10),
        )
        if self._convergence_detector:
            signal = self._convergence_detector.on_experiment_complete(node_id)
            result = _react_convergence(
                self._convergence_detector, self._tree, self._config, result, signal
            )
        return result

class RunExecutorParallelTool(Tool):
    """Dispatch multiple executors in parallel, each in its own git worktree."""

    name = "RunExecutorParallel"
    description = (
        "Dispatch 2-4 executors in parallel, each in its own isolated git worktree.\n\n"
        "Use this to explore multiple ideas simultaneously for faster iteration.\n"
        "Each executor gets its own copy of the trunk codebase and cannot interfere\n"
        "with others.\n\n"
        "All tree nodes are auto-updated with results and insights are propagated.\n"
        "Returns combined results for all dispatched executors."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "Idea node ID to implement.",
                        },
                        "additional_context": {
                            "type": "string",
                            "description": "Extra context for this specific executor.",
                        },
                    },
                    "required": ["node_id"],
                },
                "minItems": 2,
                "maxItems": 4,
                "description": "List of ideas to explore in parallel (2-4 items).",
            },
        },
        "required": ["tasks"],
    }
    is_read_only = False
    max_result_chars = 200_000

    def __init__(
        self,
        *,
        cwd: str,
        tree: "IdeaTree",
        config: "CoordinatorConfig",
        provider: "LLMProvider",
        convergence_detector: Any | None = None,
        **kwargs: Any,
    ):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree
        self._config = config
        self._provider = provider
        self._convergence_detector = convergence_detector

    async def execute(self, **kwargs: Any) -> str:
        tasks = kwargs["tasks"]

        max_parallel = self._config.budget_policy.max_parallel_executors
        if max_parallel is not None:
            if max_parallel <= 1:
                return (
                    "RunExecutorParallel is disabled by "
                    "budget_policy.max_parallel_executors=1. "
                    "Use RunExecutor(node_id=..., additional_context=...) and "
                    "wait for that single experiment to finish before starting another."
                )
            if len(tasks) > max_parallel:
                tasks = tasks[:max_parallel]
                kwargs["tasks"] = tasks
                log.warning(
                    "RunExecutorParallel: truncated to %d task(s) to respect "
                    "budget_policy.max_parallel_executors=%d",
                    len(tasks),
                    max_parallel,
                )

        # ── Early stop: gold already achieved ──────────────────────
        if self._tree.meta.get("achieved_medal") == "gold":
            return (
                "Early stop: Gold medal already achieved on trunk. "
                "No further experiments needed."
            )

        # ── Hard cycle cap ─────────────────────────────────────────
        done = _completed_cycles(self._tree)
        cap = self._config.max_cycles
        remaining = cap - done
        if remaining <= 0:
            return (
                f"HARD LIMIT REACHED: {done}/{cap} cycles already consumed "
                f"(counting done/merged/pruned/failed/needs_retry). RunExecutorParallel is "
                f"disabled. Finalize now: merge the best branch if it beats the "
                f"threshold, otherwise stop and report."
            )
        if len(tasks) > remaining:
            tasks = tasks[:remaining]
            kwargs["tasks"] = tasks
            log.warning(
                "RunExecutorParallel: truncated to %d task(s) to respect "
                "max_cycles=%d (already done=%d)", len(tasks), cap, done,
            )

        # ── Validate all nodes upfront ──────────────────────────────────
        errors: list[str] = []
        for task in tasks:
            node = self._tree.get_node(task["node_id"])
            if node is None:
                errors.append(f"Node {task['node_id']!r} not found.")
            elif node.status not in ("pending", "needs_retry"):
                errors.append(
                    f"Node {task['node_id']} has status={node.status!r}, "
                    f"expected 'pending' or 'needs_retry'."
                )
            elif (
                self._tree.max_depth is not None
                and node is not None
                and node.depth < self._tree.max_depth
            ):
                errors.append(
                    f"Node {task['node_id']} is at depth {node.depth}, but max_depth "
                    f"is {self._tree.max_depth}. Only leaf nodes (depth={self._tree.max_depth}) "
                    f"can be dispatched. Refine into sub-ideas first."
                )
        if errors:
            return "Validation errors:\n" + "\n".join(f"- {e}" for e in errors)

        # ── HITL review gate (#2): review each idea before compute; drop skips ──
        interaction_mode = (getattr(self._config.ui, "interaction_mode", "auto") or "auto").lower()
        if interaction_mode in ("review", "collaborative"):
            kept: list[dict] = []
            skipped: list[str] = []
            for task in tasks:
                node = self._tree.get_node(task["node_id"])
                action, note = await _review_gate(
                    self._tree, self._config, task["node_id"],
                    node.hypothesis if node else "")
                if action == "skip":
                    self._tree.prune_node(task["node_id"], reason="skipped by user in review")
                    skipped.append(task["node_id"])
                    continue
                if note:
                    ctx = task.get("additional_context")
                    task["additional_context"] = (
                        (f"{ctx}\n\n" if ctx else "") + f"User review note: {note}")
                kept.append(task)
            tasks = kept
            kwargs["tasks"] = tasks
            if not tasks:
                return (f"All proposed ideas ({', '.join(skipped)}) were skipped by the "
                        f"user (review mode). Nothing dispatched — propose different ideas.")

        # ── Dispatch all executors concurrently ─────────────────────────
        log.info("Dispatching %d executors in parallel", len(tasks))

        coroutines = [
            _run_single_executor(
                tree=self._tree,
                config=self._config,
                provider=self._provider,
                node_id=task["node_id"],
                additional_context=task.get("additional_context"),
            )
            for task in tasks
        ]

        results = await asyncio.gather(*coroutines, return_exceptions=True)

        # ── Format combined report ──────────────────────────────────────
        parts: list[str] = [f"# Parallel Executor Results ({len(tasks)} dispatched)\n"]

        completed = 0
        for i, (task, result) in enumerate(zip(tasks, results)):
            parts.append(f"---\n## Task {i + 1}: {task['node_id']}\n")
            if isinstance(result, Exception):
                parts.append(f"**Error**: {result}\n")
            else:
                completed += 1
                parts.append(result)

        parts.append(
            f"\n---\n**Summary**: {completed}/{len(tasks)} completed successfully."
        )

        # Check convergence after all parallel experiments complete
        combined = "\n\n".join(parts)
        if self._convergence_detector:
            signal = None
            for task in tasks:
                signal = self._convergence_detector.on_experiment_complete(task["node_id"])
            combined = _react_convergence(
                self._convergence_detector, self._tree, self._config, combined, signal
            )

        return combined
