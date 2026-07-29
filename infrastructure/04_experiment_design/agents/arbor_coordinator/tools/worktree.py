"""Git worktree lifecycle for executor experiments.

Each executor runs in its own git worktree, branched from the current trunk
HEAD, so multiple executors can run in parallel without touching each other or
the main working directory. This module owns the create / finalize / remove
lifecycle and the branch/directory naming that supports it.

Extracted from ``executor_run.py`` to keep that module focused on the executor
run loop and tool surface.
"""

from __future__ import annotations

import hashlib
import logging
import shlex
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.git_artifacts import filter_commit_paths
from .git_ops import _run_git, _user_token

if TYPE_CHECKING:
    from ..config import CoordinatorConfig

log = logging.getLogger(__name__)


# ── Naming ───────────────────────────────────────────────────────────


def _compute_branch_name(config: "CoordinatorConfig", node_id: str, hypothesis: str) -> str:
    """Derive the git branch name for a executor experiment."""
    from ...core.experiment import _slugify

    hypothesis_text = hypothesis or "unnamed"
    node_slug = _slugify(str(node_id), max_len=16)
    hypothesis_slug = _slugify(hypothesis_text, max_len=32)
    digest = hashlib.sha1(hypothesis_text.encode("utf-8")).hexdigest()[:8]
    return f"{config.git_branch_prefix}/n{node_slug}-{hypothesis_slug}-{digest}"


def _worktree_dir_name(branch_name: str) -> str:
    """Build a filesystem-safe, bounded worktree directory name."""
    safe = branch_name.replace("/", "__").replace(".", "_")
    if len(safe) <= 180:
        return safe
    digest = hashlib.sha1(branch_name.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:160]}__{digest}"


# ── Lifecycle ────────────────────────────────────────────────────────


async def _create_worktree(cwd: str, branch_name: str, start_point: str | None = None) -> tuple[Path, str]:
    """Create an isolated git worktree for a executor.

    Returns (worktree_path, actual_branch_name).
    The worktree is placed in a temp directory so it doesn't pollute the repo.

    If start_point is provided, the worktree branches from that ref
    (e.g. a working trunk branch) instead of HEAD.
    """
    worktree_base = Path(tempfile.gettempdir()) / f"coordinator-worktrees-{_user_token()}"
    worktree_base.mkdir(parents=True, exist_ok=True)

    dir_name = _worktree_dir_name(branch_name)
    worktree_path = worktree_base / dir_name

    # Clean up if exists from a previous failed run
    if worktree_path.exists():
        await _run_git(
            f"git worktree remove --force {shlex.quote(str(worktree_path))}", cwd
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Create worktree with new branch from start_point (or HEAD if not specified)
    start_ref = shlex.quote(start_point) if start_point else ""
    out, rc = await _run_git(
        f"git worktree add -b {shlex.quote(branch_name)} {shlex.quote(str(worktree_path))} {start_ref}".strip(),
        cwd,
    )
    if rc != 0:
        # Branch might already exist from a previous run — add timestamp suffix
        ts = datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
        branch_name = f"{branch_name}-{ts}"
        dir_name = _worktree_dir_name(branch_name)
        worktree_path = worktree_base / dir_name

        out, rc = await _run_git(
            f"git worktree add -b {shlex.quote(branch_name)} {shlex.quote(str(worktree_path))} {start_ref}".strip(),
            cwd,
        )
        if rc != 0:
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
            raise RuntimeError(f"Failed to create git worktree: {out}")

    return worktree_path, branch_name


async def _finalize_worktree(worktree_path: Path, node_id: str) -> None:
    """Commit useful code changes in the worktree before removal."""
    wt = str(worktree_path)
    await _run_git("git reset --", wt)
    diff, _ = await _run_git("git diff --name-only", wt)
    untracked, _ = await _run_git("git ls-files --others --exclude-standard", wt)
    changed_paths = [line.strip() for line in (diff + "\n" + untracked).splitlines() if line.strip()]
    commit_paths, artifact_paths = filter_commit_paths(changed_paths)

    if artifact_paths:
        log.info("Skipping generated artifacts for %s: %s", node_id, ", ".join(artifact_paths[:20]))

    if not commit_paths:
        return

    quoted_paths = " ".join(shlex.quote(path) for path in commit_paths)
    await _run_git(f"git add -- {quoted_paths}", wt)
    await _run_git(
        f"git commit -m {shlex.quote(f'coordinator: finalize {node_id}')}",
        wt,
    )


async def _remove_worktree(cwd: str, worktree_path: Path) -> None:
    """Remove a git worktree (the branch is preserved for later merging)."""
    try:
        await _run_git(
            f"git worktree remove --force {shlex.quote(str(worktree_path))}", cwd
        )
    except Exception as e:
        log.warning("Failed to remove worktree %s: %s", worktree_path, e)
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
