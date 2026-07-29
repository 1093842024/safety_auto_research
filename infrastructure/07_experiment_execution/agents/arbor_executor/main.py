"""CLI entry point for the research agent."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from ..core import Agent, AgentConfig, create_provider
from ..core.tools import get_all_tools
from .prompts import build_system_prompt


_GITIGNORE_ENTRIES = (
    ".arbor/",
    ".coordinator/",
    "results/",
)


def _ensure_gitignore(cwd: str) -> None:
    """Ensure target repo .gitignore excludes agent artifacts (no commit)."""
    gi_path = Path(cwd) / ".gitignore"
    existing = gi_path.read_text(encoding="utf-8") if gi_path.exists() else ""
    existing_lines = set(existing.splitlines())

    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing_lines]
    if not missing:
        return

    addition = "\n".join(missing)
    new_content = existing.rstrip("\n") + "\n" + addition + "\n" if existing else addition + "\n"
    gi_path.write_text(new_content, encoding="utf-8")

    try:
        subprocess.check_call(
            ["git", "add", ".gitignore"], cwd=cwd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["git", "commit", "-m", "chore: gitignore research agent artifacts"],
            cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


async def async_main(config: AgentConfig, idea: str) -> None:
    """Main async entry point."""
    # Setup
    provider = create_provider(config)
    tools = get_all_tools(cwd=config.cwd, workspace_dir=config.workspace_dir, config=config)
    system_prompt = build_system_prompt(config)

    agent = Agent(
        provider=provider,
        tools=tools,
        system_prompt=system_prompt,
        config=config,
    )

    # Add Executor tool (needs reference to the parent agent)
    from ..core.tools.executor_tool import ExecutorTool
    executor_tool = ExecutorTool(cwd=config.cwd, parent_agent=agent, workspace_dir=config.workspace_dir)
    agent.tools[executor_tool.name] = executor_tool

    # Build the initial user message
    parts = [
        f"## Codebase\n\nWorking directory: {os.path.abspath(config.cwd)}",
        f"## Research Idea\n\n{idea}",
    ]
    if config.experiment_cmd:
        parts.append(f"## Experiment Command\n\n```\n{config.experiment_cmd}\n```")

    parts.append(
        "## Instructions\n\n"
        "Please analyze the codebase, implement the research idea accurately, "
        "run the experiment to verify your implementation works, and report "
        "the results (baseline vs post-implementation metrics).\n\n"
        "**Branch & results convention**:\n"
        "- You are on a dedicated experiment branch (created from main).\n"
        "- If `results/init/` does not exist, run the baseline first and "
        "save to `results/init/` on your current experiment branch.\n"
        "- Save your experiment results to `results/<descriptive-name>/` on your branch. "
        "You may commit selected small result files if they are useful for comparing "
        "the experiment; use `git add -f` when the target repo ignores results, and "
        "avoid bulky logs/caches/raw traces.\n\n"
        f"**Timeout reminder**: Use a generous timeout or `run_in_background=true` "
        f"for any experiment/eval command. The configured Bash default "
        f"is {config.bash_timeout_default}s and RunTraining supports up to "
        f"{config.run_training_timeout_max}s.\n\n"
        "Do not iterate to optimize metrics — just implement the idea faithfully "
        "and report what happened. Keep the final report concise."
    )

    user_message = "\n\n".join(parts)

    # Run
    result = await agent.run(user_message)

    # Output final result
    print("\n" + "=" * 60)
    print("FINAL RESULT")
    print("=" * 60)
    print(result)

    # Show experiment log summary
    summary = agent.experiment_tracker.get_summary()
    if summary != "No experiments recorded yet.":
        print("\n" + "=" * 60)
        print("EXPERIMENT LOG")
        print("=" * 60)
        print(summary)

    print(f"\n(Total: {agent.total_turns} turns, "
          f"{agent.total_input_tokens} input tokens, "
          f"{agent.total_output_tokens} output tokens)")


def cli() -> None:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description="Arbor — AI-powered codebase optimization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  executor --cwd ./my-project --idea "Add dropout regularization to prevent overfitting"
  executor --cwd ./ml-repo --idea "Switch optimizer from SGD to AdamW" --experiment-cmd "python train.py"
  executor --config research_config.yaml --cwd ./project --idea "Improve inference speed"
  executor --cwd ./project --idea "Add caching" --provider openai --model gpt-4o
""",
    )

    # ── Control flags (drive imperative setup) ───────────────────────
    parser.add_argument("--config", default=None,
                        help="Path to YAML config file (values are overridden by CLI args)")
    parser.add_argument("--cwd", required=True,
                        help="Path to the codebase directory")
    parser.add_argument("--no-git", action="store_true",
                        help="Disable automatic git commit/branch management")

    # ── Config-value flags (auto-generated from the shared field registry) ──
    from ..core.config_cli import EXECUTOR_CLI_FLAGS, add_arguments
    add_arguments(parser, EXECUTOR_CLI_FLAGS)

    args = parser.parse_args()

    # ── Setup logging ────────────────────────────────────────────────
    from ..core.logging_setup import setup_logging
    setup_logging(verbose=getattr(args, "verbose", False))

    cwd_abs = os.path.abspath(args.cwd)

    # ── Resolve config (pydantic defaults < plugin < profile < YAML < CLI) ──
    from ..core.config_cli import cli_overrides
    from ..core.config_resolve import resolve_config

    overrides = cli_overrides(args, EXECUTOR_CLI_FLAGS)
    overrides["cwd"] = cwd_abs
    overrides["auto_git"] = not args.no_git
    config = resolve_config(yaml_path=args.config, cli_overrides=overrides, role="executor")

    if config.workspace_dir is None:
        cwd_name = os.path.basename(cwd_abs)
        config.workspace_dir = os.path.join(os.path.dirname(cwd_abs), f"{cwd_name}_workspace")

    # Validate cwd
    if not os.path.isdir(config.cwd):
        print(f"Error: {config.cwd} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Ensure target repo .gitignore excludes agent artifacts
    _ensure_gitignore(config.cwd)

    # Run
    asyncio.run(async_main(config, config.idea))


if __name__ == "__main__":
    cli()
