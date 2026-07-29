"""Git operations tool for the coordinator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ...core.tools.base import Tool

if TYPE_CHECKING:
    from ..config import CoordinatorConfig
    from ..idea_tree import IdeaTree
    from ...core.llm.base import LLMProvider


def _user_token() -> str:
    """Per-user suffix for temp worktree dirs, safe where os.getuid() is absent.

    Uses the numeric uid on POSIX, falls back to a sanitized login name on
    platforms (e.g. Windows) without os.getuid(), and never raises.
    """
    import getpass

    try:
        raw = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
    except Exception:
        raw = "user"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw) or "user"

log = logging.getLogger(__name__)

# Branches that must never be a merge target.
_PROTECTED_BRANCHES = frozenset({"main", "master"})


async def _run_git(cmd: str, cwd: str, timeout: int = 60) -> tuple[str, int]:
    """Run a git command and return (stdout, exit_code)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace").strip(), proc.returncode or 0
    except asyncio.TimeoutError:
        proc.kill()
        return f"[timed out after {timeout}s]", -1


def _parse_eval_json(eval_output: str) -> dict[str, Any] | None:
    """Try to extract the JSON result block from eval output.

    Returns a dict with keys like score, gold_medal, silver_medal, etc.
    Returns None if no valid JSON block is found.
    """
    for m in re.finditer(r"\{[^{}]+\}", eval_output):
        try:
            obj = json.loads(m.group())
            if "score" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


async def _parse_eval_score(
    provider: "LLMProvider",
    eval_output: str,
    *,
    bus: Any | None = None,
    cwd: str | None = None,
) -> float | None:
    """Extract the primary metric score from evaluation command output."""
    max_chars = 8000
    if len(eval_output) > max_chars:
        excerpt = eval_output[:max_chars // 2] + "\n...\n" + eval_output[-(max_chars // 2):]
    else:
        excerpt = eval_output

    response = await provider.create(
        system=(
            "You extract the primary evaluation metric from command output. "
            "Return ONLY a JSON object: {\"score\": <number or null>}. "
            "The score should be the main accuracy/performance metric as a percentage "
            "(e.g. 45.2 means 45.2%). If multiple metrics exist, pick the primary one. "
            "If no score is found, return {\"score\": null}. "
            "No markdown fencing. Just raw JSON."
        ),
        messages=[{"role": "user", "content": f"Eval output:\n{excerpt}"}],
        max_tokens=256,
    )
    try:
        from ...core.agent import record_llm_usage
        record_llm_usage(
            response,
            bus=bus,
            model=getattr(provider, "model", None),
            source="parse_eval_score",
            agent_cwd=cwd,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    text = response.get_text().strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text).get("score")
    except (json.JSONDecodeError, AttributeError):
        log.warning("Failed to parse eval score from LLM response: %s", text[:200])
        return None


async def _run_eval_in_worktree(
    *,
    cwd: str,
    source_branch: str,
    node_id: str,
    eval_cmd_test: str,
    provider: "LLMProvider",
    bus: Any | None = None,
    timeout: int = 7200,
    retries: int = 1,
    retry_base_delay: float = 5.0,
    retry_max_delay: float = 30.0,
) -> tuple[float | None, str, dict[str, Any] | None]:
    """Run eval_cmd_test on source_branch in a temporary worktree.

    Returns (score, detail_message, medal_info).
    score is None if evaluation failed.
    medal_info is a dict with gold_medal/silver_medal/bronze_medal bools, or None.
    """
    import tempfile

    worktree_base = Path(tempfile.gettempdir()) / f"merge-eval-worktrees-{_user_token()}"
    worktree_base.mkdir(parents=True, exist_ok=True)

    dir_name = source_branch.replace("/", "__").replace(".", "_")
    worktree_path = worktree_base / dir_name

    try:
        # Clean up stale worktree
        if worktree_path.exists():
            await _run_git(f"git worktree remove --force {shlex.quote(str(worktree_path))}", cwd)
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)

        # Create detached worktree at source branch
        out, rc = await _run_git(
            f"git worktree add --detach {shlex.quote(str(worktree_path))} {shlex.quote(source_branch)}",
            cwd,
        )
        if rc != 0:
            return None, f"Failed to create eval worktree: {out}", None

        # Substitute template variables
        cmd = eval_cmd_test.replace("{cwd}", str(worktree_path)).replace("{node_id}", node_id)

        log.info("Running B_test in worktree %s: %s", worktree_path, cmd)

        max_attempts = max(1, retries + 1)
        failures: list[str] = []

        for attempt in range(1, max_attempts + 1):
            eval_output, returncode, timed_out = await _run_eval_command(
                cmd=cmd,
                cwd=str(worktree_path),
                timeout=timeout,
            )
            excerpt = eval_output[-2000:] if len(eval_output) > 2000 else eval_output

            if timed_out:
                failures.append(
                    f"Attempt {attempt}/{max_attempts}: B_test evaluation timed out after {timeout}s."
                )
                if attempt < max_attempts:
                    await asyncio.sleep(_eval_retry_delay(attempt, retry_base_delay, retry_max_delay))
                    continue
                return None, "\n".join(failures), None

            if returncode != 0:
                failures.append(
                    f"Attempt {attempt}/{max_attempts}: B_test exited with code {returncode}.\n{excerpt}"
                )
                if attempt < max_attempts and _is_transient_eval_failure(eval_output):
                    await asyncio.sleep(_eval_retry_delay(attempt, retry_base_delay, retry_max_delay))
                    continue
                return None, "\n\n".join(failures), None

            # Parse score and medal info from output
            medal_info = _parse_eval_json(eval_output)
            if medal_info is not None:
                score = medal_info.get("score")
            else:
                score = await _parse_eval_score(
                    provider,
                    eval_output,
                    bus=bus,
                    cwd=cwd,
                )

            if score is None:
                failures.append(
                    f"Attempt {attempt}/{max_attempts}: Could not extract score from B_test output.\n{excerpt}"
                )
                if attempt < max_attempts and _is_transient_eval_failure(eval_output):
                    await asyncio.sleep(_eval_retry_delay(attempt, retry_base_delay, retry_max_delay))
                    continue
                return None, "\n\n".join(failures), None

            retry_note = "" if attempt == 1 else f"\n\nRecovered after {attempt} attempts."
            return score, f"B_test score: {score}{retry_note}\n\nOutput (tail):\n{excerpt}", medal_info

    finally:
        # Always clean up worktree
        if worktree_path.exists():
            await _run_git(f"git worktree remove --force {shlex.quote(str(worktree_path))}", cwd)
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)


async def _run_eval_command(*, cmd: str, cwd: str, timeout: int) -> tuple[str, int, bool]:
    """Run an evaluation command and return (output, exit_code, timed_out)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env={**os.environ, "TERM": "dumb"},
        start_new_session=True,
    )
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        eval_output = stdout_bytes.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            eval_output += f"\n[Exit code: {proc.returncode}]"
        return eval_output, proc.returncode or 0, False
    except asyncio.TimeoutError:
        _kill_process_tree(proc, signal.SIGTERM)
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            _kill_process_tree(proc, signal.SIGKILL)
            stdout_bytes, _ = await proc.communicate()
        eval_output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        return eval_output, -1, True


def _kill_process_tree(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Terminate the shell and any child processes it launched."""
    if proc.returncode is not None or proc.pid is None:
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.kill()
        except ProcessLookupError:
            return


def _is_transient_eval_failure(output: str) -> bool:
    lowered = output.lower()
    transient_markers = (
        "429",
        "rate limit",
        "too many requests",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "service unavailable",
        "gateway timeout",
        "apierror",
    )
    return any(marker in lowered for marker in transient_markers)


def _eval_retry_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    delay = min(max_delay, base_delay * attempt)
    return max(0.0, delay)


class GitMergeBranchTool(Tool):
    """Merge a verified experiment branch into the trunk branch.

    Safety gates enforced by this tool:
    - Merging into main/master is **always** rejected.
    - B_test evaluation is **automatically run** on the source branch to get
      a verified score. The LLM cannot bypass this.
    - If a trunk_score is known, the verified test_score must meet or exceed it.
    """

    name = "GitMergeBranch"
    description = (
        "Merge a verified experiment branch into the trunk branch.\n\n"
        "This tool AUTOMATICALLY runs eval_cmd_test on the source branch in "
        "an isolated worktree to independently verify the experiment score "
        "before merging. You do NOT need to run B_test yourself.\n\n"
        "The tool will:\n"
        "1. Create a temporary worktree for the source branch\n"
        "2. Run eval_cmd_test to get a verified score\n"
        "3. Verify the score meets the merge threshold\n"
        "4. Merge with --no-ff if the score passes\n\n"
        "Merging into main/master is not allowed — "
        "always merge into the working trunk branch."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "source_branch": {
                "type": "string",
                "description": "The experiment branch to merge from.",
            },
            "target_branch": {
                "type": "string",
                "description": (
                    "The target branch to merge into. "
                    "Defaults to the configured trunk branch. "
                    "Cannot be 'main' or 'master'."
                ),
            },
            "node_id": {
                "type": "string",
                "description": "The idea node ID being merged (for commit message).",
            },
            "test_score": {
                "type": "number",
                "description": (
                    "Optional: the B_test score you observed. The tool will "
                    "independently verify by running eval_cmd_test itself. "
                    "If provided, it is logged for comparison with the verified score."
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Custom merge commit message. Auto-generated if not provided.",
            },
        },
        "required": ["source_branch", "node_id"],
    }
    is_read_only = False

    def __init__(self, *, cwd: str, config: "CoordinatorConfig", tree: "IdeaTree", provider: "LLMProvider", **kwargs: Any):
        super().__init__(cwd=cwd, **kwargs)
        self._config = config
        self._tree = tree
        self._provider = provider

    async def execute(self, **kwargs: Any) -> str:
        source_branch: str = kwargs["source_branch"]
        node_id: str = kwargs["node_id"]
        llm_test_score: float | None = kwargs.get("test_score")
        commit_message: str = kwargs.get(
            "commit_message",
            f"coordinator: merge {node_id} from {source_branch}",
        )

        # ── Resolve target branch (never fall back to main) ────────────
        target_branch: str | None = (
            kwargs.get("target_branch") or self._config.trunk_branch
        )
        if not target_branch:
            return (
                "Error: No target branch specified and no trunk_branch is configured. "
                "Set --trunk-branch when launching the coordinator, or pass target_branch explicitly."
            )
        if target_branch in _PROTECTED_BRANCHES:
            return (
                f"Error: Refusing to merge into protected branch '{target_branch}'. "
                f"Merges must go into the working trunk branch, not main/master. "
                f"The configured trunk branch is: {self._config.trunk_branch or '(not set)'}."
            )

        # ── Auto-run B_test evaluation ─────────────────────────────────
        eval_cmd_test = self._tree.meta.get("eval_cmd_test")

        if eval_cmd_test:
            log.info("Auto-running B_test for node %s on branch %s", node_id, source_branch)
            eval_timeout_meta = self._tree.meta.get("eval_timeout")
            eval_retries_meta = self._tree.meta.get("eval_retries")
            eval_retry_base_meta = self._tree.meta.get("eval_retry_base_delay")
            eval_retry_max_meta = self._tree.meta.get("eval_retry_max_delay")
            eval_timeout = int(eval_timeout_meta if eval_timeout_meta is not None else self._config.eval_timeout)
            eval_retries = int(eval_retries_meta if eval_retries_meta is not None else self._config.eval_retries)
            eval_retry_base_delay = float(
                eval_retry_base_meta if eval_retry_base_meta is not None else self._config.eval_retry_base_delay
            )
            eval_retry_max_delay = float(
                eval_retry_max_meta if eval_retry_max_meta is not None else self._config.eval_retry_max_delay
            )
            verified_score, eval_detail, medal_info = await _run_eval_in_worktree(
                cwd=self.cwd,
                source_branch=source_branch,
                node_id=node_id,
                eval_cmd_test=eval_cmd_test,
                provider=self._provider,
                bus=self._tree.bus,
                timeout=eval_timeout,
                retries=eval_retries,
                retry_base_delay=eval_retry_base_delay,
                retry_max_delay=eval_retry_max_delay,
            )
            if verified_score is None:
                return (
                    f"Error: B_test evaluation failed on branch {source_branch}. "
                    f"Merge rejected.\n\nDetails:\n{eval_detail}"
                )
            if llm_test_score is not None:
                log.info(
                    "Score comparison for %s: LLM-reported=%.1f%%, verified=%.1f%%",
                    node_id, llm_test_score, verified_score,
                )
            test_score = verified_score
        elif llm_test_score is not None:
            medal_info = None
            log.warning(
                "No eval_cmd_test configured — falling back to LLM-reported "
                "test_score (%.1f%%) for node %s. This is NOT independently verified.",
                llm_test_score, node_id,
            )
            test_score = llm_test_score
        else:
            return (
                "Error: No eval_cmd_test configured in tree metadata and no "
                "test_score provided. Cannot verify experiment quality.\n\n"
                "Set eval_cmd_test via TreeSetMeta before merging, or provide "
                "a test_score parameter as a fallback."
            )

        # ── Verify test score against trunk ────────────────────────────
        test_trunk_score = (
            self._tree.meta.get("test_trunk_score")
            or self._tree.meta.get("test_baseline_score")
        )
        merge_threshold = self._config.merge_threshold

        if test_trunk_score is not None:
            if not self._tree.is_improvement(test_score, test_trunk_score):
                direction = self._tree.meta.get("metric_direction", "maximize")
                return (
                    f"Error: Test score ({test_score:.1f}%) is NOT an improvement over "
                    f"current test trunk score ({test_trunk_score:.1f}%) with direction={direction}. "
                    f"Merge rejected — the experiment must improve over the current trunk "
                    f"on the test set.\n\n"
                    f"If you believe this is correct, investigate the discrepancy between "
                    f"B_dev and B_test scores before retrying."
                )
            direction = self._tree.meta.get("metric_direction", "maximize")
            improvement = abs(test_score - test_trunk_score)
            if improvement < merge_threshold:
                log.warning(
                    "Test improvement (%.1f%%) is below merge_threshold (%.1f%%) for node %s, "
                    "but proceeding as test_score (%.1f%%) >= test_trunk_score (%.1f%%).",
                    improvement, merge_threshold, node_id, test_score, test_trunk_score,
                )

        log.info(
            "Merging %s into %s for node %s (test_score=%.1f%%)",
            source_branch, target_branch, node_id, test_score,
        )

        # ── Plugin merge guards ───────────────────────────────────────
        plugin = self._config.plugin
        if plugin:
            # Check protected paths
            if plugin.protected_paths:
                diff_out, diff_rc = await _run_git(
                    f"git diff --name-only {shlex.quote(target_branch)}...{shlex.quote(source_branch)}",
                    self.cwd,
                )
                if diff_rc == 0 and diff_out.strip():
                    changed_files = diff_out.strip().splitlines()
                    for pattern in plugin.protected_paths:
                        for f in changed_files:
                            if fnmatch(f, pattern):
                                return (
                                    f"Merge rejected: branch modifies protected path '{f}' "
                                    f"(matches pattern '{pattern}'). "
                                    f"Data and evaluation files must not be modified."
                                )

            # Check required outputs
            if plugin.required_outputs:
                for output in plugin.required_outputs:
                    check_cmd = f"git show {shlex.quote(source_branch)}:{shlex.quote(output)}"
                    _, check_rc = await _run_git(check_cmd, self.cwd)
                    if check_rc != 0:
                        return (
                            f"Merge rejected: required output '{output}' not found on "
                            f"branch {source_branch}. The branch must produce all "
                            f"required outputs before merging."
                        )

        # Save current branch
        current, _ = await _run_git("git branch --show-current", self.cwd)

        # Check source branch exists
        out, rc = await _run_git(f"git rev-parse --verify {shlex.quote(source_branch)}", self.cwd)
        if rc != 0:
            return f"Error: Source branch {source_branch!r} does not exist."

        # Checkout target branch
        out, rc = await _run_git(f"git checkout {shlex.quote(target_branch)}", self.cwd)
        if rc != 0:
            return f"Error: Could not checkout target branch {target_branch!r}: {out}"

        # Merge with --no-ff
        out, rc = await _run_git(
            f"git merge --no-ff -m {shlex.quote(commit_message)} {shlex.quote(source_branch)}",
            self.cwd,
            timeout=120,
        )

        if rc != 0:
            # Abort the merge on conflict
            await _run_git("git merge --abort", self.cwd)
            # Return to original branch
            await _run_git(f"git checkout {shlex.quote(current)}", self.cwd)
            return (
                f"Error: Merge conflict when merging {source_branch} into {target_branch}.\n"
                f"Merge aborted. Details:\n{out}\n\n"
                f"Consider resolving conflicts manually or trying a different approach."
            )

        # Get the merge commit hash
        merge_hash, _ = await _run_git("git rev-parse --short HEAD", self.cwd)

        # Get trunk score info
        trunk_log, _ = await _run_git("git log --oneline -3", self.cwd)

        # Return to original branch
        if current and current != target_branch:
            await _run_git(f"git checkout {shlex.quote(current)}", self.cwd)

        # Record the verified test score on the node + trunk meta directly, so
        # split provenance does not depend on the LLM following the instruction
        # in the result string below.
        try:
            await self._tree.async_update_node(node_id, test_score=test_score, status="merged")
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to record test_score on node %s: %s", node_id, exc)
        self._tree.meta["test_trunk_score"] = test_score

        verified_tag = " (independently verified)" if eval_cmd_test else " (LLM-reported, NOT verified)"
        result = (
            f"Successfully merged {source_branch} into {target_branch}.\n"
            f"  Merge commit: {merge_hash}\n"
            f"  Test score: {test_score}{verified_tag}\n"
            f"  Message: {commit_message}\n"
            f"  Recent trunk commits:\n{trunk_log}\n\n"
            f"IMPORTANT: Update test_trunk_score via TreeSetMeta to {test_score} "
            f"(the verified test score) and set the node status to 'merged'."
        )

        # Detect medal from eval JSON and record in tree.meta
        if medal_info:
            if medal_info.get("gold_medal"):
                self._tree.meta["achieved_medal"] = "gold"
                result += (
                    "\n\n** GOLD MEDAL ACHIEVED on the test set! "
                    "No further experiments are needed. "
                    "Call TreeSetMeta to record gold and STOP. **"
                )
            elif medal_info.get("silver_medal"):
                self._tree.meta["achieved_medal"] = "silver"
                result += "\n\nSilver medal achieved. Consider whether further improvement is worthwhile."
            elif medal_info.get("bronze_medal"):
                self._tree.meta["achieved_medal"] = "bronze"

        log.info("Merge complete: %s -> %s (%s)", source_branch, target_branch, merge_hash)
        return result
