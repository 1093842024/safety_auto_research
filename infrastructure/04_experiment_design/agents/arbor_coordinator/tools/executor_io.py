"""Executor I/O — prompt construction and report parsing.

The text boundary around a executor run: assembling the user prompt it sees
(codebase/eval/insight context) and parsing the free-form report it returns
into structured fields. Extracted from ``executor_run.py`` to separate this
I/O layer from the run loop and tool surface.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...core.llm.base import LLMProvider
    from ..config import CoordinatorConfig
    from ..idea_tree import IdeaTree, Node

log = logging.getLogger(__name__)


def _tail(text: str, limit: int) -> str:
    """Return the last ``limit`` chars of ``text`` with a truncation marker."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"[... truncated to last {limit} chars ...]\n" + text[-limit:]


def _gather_ancestor_insights(tree: "IdeaTree", node_id: str) -> str:
    """Collect insights from ancestor nodes (root -> parent order)."""
    path = tree.get_path_to_root(node_id)
    parts: list[str] = []
    for ancestor in reversed(path[1:]):  # skip the node itself; root first
        if ancestor.insight:
            parts.append(f"- {ancestor.id}: {ancestor.insight}")
    if not parts:
        return ""
    return "## Insights from Prior Experiments\n\n" + "\n".join(parts)


def _substitute_eval_templates(cmd: str, cwd: str, node_id: str) -> str:
    """Replace template variables in eval_cmd.

    Supported variables:
      {cwd}     - working directory (worktree path for executors)
      {node_id} - tree node ID (e.g. "1.2.1") for unique result naming
    """
    return cmd.replace("{cwd}", cwd).replace("{node_id}", node_id)


def _get_eval_info(tree: "IdeaTree", *, worktree_cwd: str | None = None, node_id: str | None = None) -> str:
    """Read evaluation info from tree.meta and format for executor injection.

    If worktree_cwd and node_id are provided, template variables in eval_cmd
    are substituted so the executor gets ready-to-run commands.
    """
    meta = tree.meta
    parts: list[str] = []

    def _sub(cmd: str) -> str:
        """Apply template substitution if context is available."""
        if worktree_cwd and node_id:
            return _substitute_eval_templates(cmd, worktree_cwd, node_id)
        return cmd

    if meta.get("eval_cmd"):
        parts.append(f"- **Evaluation command (B_dev)**: `{_sub(meta['eval_cmd'])}`")
    if meta.get("eval_cmd_test"):
        parts.append(
            f"- **Evaluation command (B_test)** "
            f"(DO NOT use for routine experiments): `{_sub(meta['eval_cmd_test'])}`"
        )
    if meta.get("dataset_info"):
        parts.append(f"- **Dataset info**: {meta['dataset_info']}")
    if meta.get("baseline_score") is not None:
        parts.append(f"- **Baseline score**: {meta['baseline_score']}")
    if meta.get("trunk_score") is not None:
        parts.append(f"- **Current trunk score**: {meta['trunk_score']}")

    if not parts:
        return ""
    return (
        "## Evaluation Info\n\n"
        + "\n".join(parts)
        + "\n\nUse the B_dev evaluation command above to measure your final results. "
        "Do NOT use B_test.\n\n"
        "You may test on a subset of examples or individual cases first for "
        "quick validation before committing to a full evaluation run. Use your "
        "judgment — if a quick check shows the approach clearly isn't working, "
        "iterate on the implementation instead of running the full eval.\n\n"
        "**CRITICAL**: Run ALL commands from your working directory. "
        "Do NOT `cd` to any other directory, especially not the main repository. "
        "Your working directory is an isolated worktree with the correct code. "
        "Running eval elsewhere will evaluate the wrong code and corrupt results."
    )


def _build_executor_prompt(
    *,
    worktree_path: Path,
    node: Any,
    ancestor_insights: str,
    eval_info: str,
    additional_context: str | None,
) -> str:
    """Build the user message for a executor."""
    parts = [
        f"## Codebase\n\nWorking directory: {worktree_path}",
        (
            "## Git Isolation\n\n"
            "You are running in an isolated git worktree created from the current "
            "coordinator trunk. Do not checkout a different branch and do not `cd` "
            "back to the main workspace. If you need to sanity-check the setup, run "
            "`pwd` and `git branch --show-current`; both should point to this "
            "worktree/experiment branch."
        ),
        (
            f"## Research Idea\n\n"
            f"**ID**: {node.id}\n"
            f"**Hypothesis**: {node.hypothesis}"
        ),
    ]

    if eval_info:
        parts.append(eval_info)
    if ancestor_insights:
        parts.append(ancestor_insights)
    if additional_context:
        parts.append(f"## Additional Context\n\n{additional_context}")

    parts.append(
        "## Instructions\n\n"
        "Implement the research idea faithfully and thoroughly:\n\n"
        "1. **UNDERSTAND**: Read the codebase to understand the current "
        "implementation before making any changes.\n"
        "2. **IMPLEMENT**: Make the code changes to realize the idea.\n"
        "3. **VALIDATE IMPLEMENTATION**: Run on 2-3 representative examples "
        "to verify your code actually works — no crashes, the new logic is "
        "actually being reached, outputs look sane. If there are bugs or the "
        "idea isn't taking effect, **go back to step 2 and fix it**. Do NOT "
        "run full eval on a broken implementation.\n"
        "4. **ITERATE UNTIL SOLID**: Repeat steps 2-3 until you are confident "
        "the idea is implemented correctly and completely — the code runs "
        "without errors, the new logic is active, and the approach has been "
        "given a fair shot. The goal is to separate 'idea quality' from "
        "'implementation quality': a bad score should mean the idea itself "
        "didn't help, not that the code was buggy.\n"
        "5. **EVALUATE**: Once the implementation is solid, run the full "
        "evaluation command to get the definitive score. You may skip full "
        "eval ONLY if, after a thorough and correct implementation, quick "
        "checks already make the outcome unambiguous.\n"
        "6. **REPORT**: Provide:\n"
        "   - **Changes**: Files modified\n"
        "   - **Baseline vs Result**: Metrics comparison\n"
        "   - **Score**: The primary metric value achieved "
        "(report as absolute value, not delta; null only if eval was skipped)\n"
        "   - **Insight**: Why it worked or didn't\n\n"
        "**Key principle**: Give every idea a fair shot. A negative result is "
        "valuable — but only if the implementation was correct. Iterate on "
        "implementation bugs before concluding an idea doesn't work.\n\n"
        "**Do not stop after describing what you will do. If you say you will "
        "write, edit, train, or evaluate something, call the appropriate tool "
        "in that same turn or the next turn before reporting completion.**\n\n"
        f"**Result directory**: Save results to `results/{node.id}-<brief-description>/` "
        f"to avoid overwriting other experiments."
    )

    return "\n\n".join(parts)


def _build_resume_context(
    config: "CoordinatorConfig", node: "Node", attempt: int
) -> str:
    """Assemble context for a resumed attempt from the prior attempt's artifacts.

    The executor worktree is gone, but its branch (``node.code_ref``) is checked
    out as this attempt's start point, so the code is already present. This block
    re-grounds the model on what the last attempt did, where it stopped, and the
    shape of its diff — without replaying the (discarded) message history.
    """
    parts: list[str] = [
        f"## Resuming a prior attempt (attempt {attempt})",
        (
            f"A previous executor on this idea ended as `{node.status}` "
            f"(eval_status={node.eval_status or 'unknown'}"
            + (f", stop_reason={node.stop_reason}" if node.stop_reason else "")
            + "). "
            + (
                f"Its committed work is already on this worktree's branch "
                f"`{node.code_ref}` — continue from there; do NOT start over. "
                if node.code_ref
                else "Its work was not committed, so you are starting from trunk; "
                "use the prior report below to avoid repeating dead ends. "
            )
            + "Finish the implementation and run the evaluation so a real score "
            "is produced."
        ),
    ]
    if node.result:
        parts.append(f"### Prior result\n{node.result.strip()}")
    if node.insight:
        parts.append(f"### Prior insight\n{node.insight.strip()}")

    workspace = config.workspace_dir
    if workspace:
        exp_dir = Path(workspace) / "experiments" / node.id
        report = exp_dir / "report.md"
        diff = exp_dir / "diff.patch"
        if report.exists():
            text = report.read_text(encoding="utf-8", errors="replace")
            parts.append(f"### Prior report (truncated)\n{_tail(text, 6000)}")
        if diff.exists():
            text = diff.read_text(encoding="utf-8", errors="replace")
            parts.append(f"### Prior diff --stat / patch (truncated)\n{_tail(text, 4000)}")
    return "\n\n".join(parts)


async def _parse_executor_report(
    provider: "LLMProvider",
    report: str,
    hypothesis: str,
    *,
    bus: Any | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Use the LLM to extract structured fields from a executor report."""
    max_chars = 12000
    if len(report) <= max_chars:
        report_excerpt = report
    else:
        head = report[: max_chars // 2]
        tail = report[-(max_chars // 2) :]
        report_excerpt = f"{head}\n\n[... middle truncated ...]\n\n{tail}"

    response = await provider.create(
        system=(
            "You extract structured data from experiment reports. "
            "Return ONLY valid JSON with these fields:\n"
            '- "score": number or null (the absolute metric value achieved, '
            "e.g. 45.2 means 45.2%. This is NOT a delta.)\n"
            '- "insight": string (key learning, 1-3 sentences)\n'
            '- "result": string (factual description, 1-2 sentences)\n'
            '- "code_ref": string or null (git branch name if mentioned)\n'
            '- "eval_status": one of "scored" (a numeric metric was produced by '
            "running evaluation), \"skipped\" (implementation looks complete but "
            "evaluation was intentionally not run), or \"failed_to_run\" "
            "(evaluation was attempted but crashed/produced no parseable metric, "
            "or there was no working implementation)\n"
            "No markdown fencing. Just raw JSON."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Hypothesis: {hypothesis}\n\n"
                    f"Experiment report:\n{report_excerpt}\n\n"
                    "Extract the structured fields as JSON."
                ),
            }
        ],
        max_tokens=1024,
    )
    try:
        from ...core.agent import record_llm_usage
        record_llm_usage(
            response,
            bus=bus,
            model=getattr(provider, "model", None),
            source="parse_executor_report",
            agent_cwd=cwd,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    text = response.get_text().strip()

    # Strip markdown code fences if the LLM added them
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("Failed to parse JSON from LLM response: %s", text[:200])
        return {
            "score": None,
            "insight": "",
            "result": text[:500],
            "code_ref": None,
            "eval_status": "failed_to_run",
        }
