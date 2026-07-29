"""CLI entry point for the coordinator."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import CoordinatorConfig
from .orchestrator import CoordinatorOrchestrator


_GITIGNORE_ENTRIES = (
    ".arbor/",
    ".autoresearch/",
    ".coordinator/",
    "results/",
    "cache_old_*/",
)


def _git_output(cwd: str, *args: str) -> str | None:
    """Return git command output, or None when cwd is not a usable git repo."""
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def _validate_start_branch(cwd: str, base_branch: str | None, allow_non_base: bool) -> None:
    """Fail before creating a trunk branch from the wrong HEAD."""
    if allow_non_base:
        return
    inside = _git_output(cwd, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return
    current = _git_output(cwd, "branch", "--show-current") or "(detached HEAD)"
    allowed = [base_branch] if base_branch else ["main", "master"]
    if current in allowed:
        return

    expected = base_branch or "main/master"
    print(
        f"Error: refusing to start from branch '{current}'. Expected clean base branch {expected}.\n"
        f"Checkout the base branch first, or pass --allow-non-base-branch if this is intentional.",
        file=sys.stderr,
    )
    sys.exit(1)


def _ensure_gitignore_before_branch(cwd: str) -> None:
    """Commit agent artifact ignores before creating the trunk branch."""
    inside = _git_output(cwd, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return

    gitignore = Path(cwd) / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    existing_lines = set(existing.splitlines())
    missing = [entry for entry in _GITIGNORE_ENTRIES if entry not in existing_lines]
    if not missing:
        return

    addition = "\n".join(missing)
    new_content = existing.rstrip("\n") + "\n" + addition + "\n" if existing else addition + "\n"
    gitignore.write_text(new_content, encoding="utf-8")

    try:
        subprocess.check_call(
            ["git", "add", ".gitignore"],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["git", "-c", "user.name=AutoResearch", "-c", "user.email=autoresearch@example.com",
             "commit", "-m", "chore: gitignore research agent artifacts"],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Warning: failed to commit .gitignore update before branch creation: {exc}", file=sys.stderr)


def create_provider(config: CoordinatorConfig):
    """Create an LLM provider based on configuration."""
    from ..core import create_provider as _create_provider
    from ..core.config import AgentConfig

    agent_config = AgentConfig(
        provider=config.provider,
        model=config.effective_meta_model,
        api_key=config.api_key,
        base_url=config.base_url,
        openai_api=config.openai_api,
        reasoning_effort=config.reasoning_effort,
        reasoning_summary=config.reasoning_summary,
        text_verbosity=config.text_verbosity,
        parallel_tool_calls=config.parallel_tool_calls,
        thinking_budget_tokens=config.thinking_budget_tokens,
        llm_timeout=config.llm_timeout,
        llm_provider_retries=config.llm_provider_retries,
    )
    return _create_provider(agent_config)


async def async_main(config: CoordinatorConfig) -> None:
    """Main async entry point."""
    _write_config_snapshot(config)
    provider = create_provider(config)
    orchestrator = CoordinatorOrchestrator(config=config, provider=provider)
    report = await orchestrator.run()
    print("\n" + report)


def _write_config_snapshot(config: CoordinatorConfig) -> None:
    """Persist the fully-resolved, secret-redacted config for this run (C5).

    Unlike copying the user's YAML, this captures the merged result of every
    layer (plugin/profile/YAML/CLI) and masks secrets — it is the structure
    checkpoint/resume and the WebUI consume.
    """
    import yaml

    from ..core.config_schema import redacted_snapshot

    try:
        import yaml

        from ..core.config_schema import redacted_snapshot

        path = config.coordinator_dir / "config_snapshot.yaml"
        path.write_text(
            yaml.safe_dump(redacted_snapshot(config), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except (OSError, ImportError) as exc:
        print(f"Warning: failed to write config snapshot: {exc}", file=sys.stderr)


def cli() -> None:
    """Command-line interface for the coordinator."""
    parser = argparse.ArgumentParser(
        description="Coordinator — arbor-guided research orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  coordinator --cwd ./project --task "Optimize accuracy"

  coordinator --config research_config.yaml --cwd ./project

  coordinator --cwd ./project --resume

  coordinator --cwd ./project --task "Improve harness" \\
    --max-depth 3 --max-turns 300
""",
    )
    from ..core.config_cli import add_config_arguments

    # ── Control flags (drive imperative setup; not plain config values) ──
    parser.add_argument("--config", default=None,
                        help="Path to YAML config file (values are overridden by CLI args)")
    parser.add_argument("--cwd", required=True,
                        help="Path to the target codebase directory")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from existing tree in .coordinator/")
    parser.add_argument("--no-git", action="store_true",
                        help="Disable automatic git management")
    parser.add_argument("--branch-prefix", default=None,
                        help="Git branch prefix for executor branches (default: research/run_<timestamp>)")
    parser.add_argument("--trunk-branch", default=None,
                        help="Working trunk branch (executors branch from here, merges go here; keeps main clean)")
    parser.add_argument("--base-branch", default=None,
                        help="Expected clean base branch for new runs (default: auto-detect main/master)")
    parser.add_argument("--allow-non-base-branch", action="store_true",
                        help="Allow creating the trunk from the current non-base branch (unsafe for benchmarks)")

    # ── Config-value flags (auto-generated from the single field registry) ──
    # Replaces ~40 hand-written add_argument calls; see core/config_cli.py.
    add_config_arguments(parser)

    args = parser.parse_args()

    # ── Setup logging ────────────────────────────────────────────────
    from ..core.logging_setup import setup_logging
    setup_logging(verbose=getattr(args, "verbose", False))

    cwd_abs = os.path.abspath(args.cwd)
    if not os.path.isdir(cwd_abs):
        print(f"Error: {cwd_abs} is not a directory", file=sys.stderr)
        sys.exit(1)

    # ── Resolve config (pydantic defaults < plugin < profile < YAML < CLI) ──
    from ..core.config_cli import cli_overrides
    from ..core.config_resolve import resolve_config

    overrides = cli_overrides(args)
    overrides["cwd"] = cwd_abs
    overrides["auto_git"] = not args.no_git
    overrides["require_base_branch"] = not args.allow_non_base_branch
    if args.base_branch is not None:
        overrides["base_branch"] = args.base_branch
    config = resolve_config(yaml_path=args.config, cli_overrides=overrides, role="coordinator")
    config.resume = args.resume

    # ── Imperative git / workspace / branch setup (uses resolved values) ──
    if not args.no_git:
        _validate_start_branch(cwd_abs, config.base_branch, args.allow_non_base_branch)
        _ensure_gitignore_before_branch(cwd_abs)

    if config.workspace_dir is None:
        cwd_name = os.path.basename(cwd_abs)
        config.workspace_dir = os.path.join(os.path.dirname(cwd_abs), f"{cwd_name}_workspace")

    branch_prefix = args.branch_prefix
    if branch_prefix is None:
        branch_prefix = f"research/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    config.git_branch_prefix = branch_prefix

    trunk_branch = args.trunk_branch
    if trunk_branch in {"main", "master"}:
        print("Error: --trunk-branch cannot be main/master", file=sys.stderr)
        sys.exit(1)
    if trunk_branch is None and not args.no_git:
        trunk_branch = f"{branch_prefix}/trunk"
        try:
            subprocess.check_call(
                ["git", "branch", trunk_branch],
                cwd=cwd_abs,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError):
            # Branch may already exist, or cwd may not be a git repo. The
            # orchestrator preflight will surface the latter clearly.
            pass
    config.trunk_branch = trunk_branch

    # ── Run ──────────────────────────────────────────────────────────
    asyncio.run(async_main(config))



if __name__ == "__main__":
    cli()
