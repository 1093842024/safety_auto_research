"""CLI entry point for the SearchAgent — standalone testing only.

Example:

    search-agent --hypothesis "Tree-of-thought planning over web search results" \\
        --search-endpoint http://example/search --browse-endpoint http://example/browse
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from ..core import create_provider
from ..core.config import AgentConfig
from ..coordinator.config import SearchConfig
from .agent import build_search_agent
from .prompts import build_search_user_prompt


async def async_main(
    hypothesis: str,
    *,
    search_config: SearchConfig,
    provider_name: str,
    model: str,
    api_key: str | None,
    base_url: str | None,
    cwd: str,
    focus: str | None,
) -> None:
    provider = create_provider(
        AgentConfig(
            provider=provider_name,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
    )
    agent = build_search_agent(
        provider=provider,
        search_config=search_config,
        cwd=cwd,
    )
    user_msg = build_search_user_prompt(hypothesis=hypothesis, focus=focus)
    result = await agent.run(user_msg)
    print("\n" + "=" * 60)
    print("FINAL JSON")
    print("=" * 60)
    print(result)
    print(f"\n(Total: {agent.total_turns} turns, "
          f"{agent.total_input_tokens} in / {agent.total_output_tokens} out)")


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="SearchAgent — related-work / novelty assessment for one hypothesis",
    )
    parser.add_argument("--hypothesis", required=True,
                        help="The research hypothesis to investigate.")
    parser.add_argument("--focus", default=None,
                        help="Optional focus directive (e.g. 'prefer arxiv 2024').")
    parser.add_argument("--cwd", default=".",
                        help="Working directory for FileReadTool (default: cwd).")

    parser.add_argument("--search-endpoint", default=None,
                        help="Web search endpoint URL (or set WEB_SEARCH_ENDPOINT).")
    parser.add_argument("--browse-endpoint", default=None,
                        help="Web browse endpoint URL (or set WEB_BROWSE_ENDPOINT).")
    parser.add_argument("--search-provider", default="google",
                        help="Backend search provider name (default: google).")
    parser.add_argument("--visit-max-tokens", type=int, default=2048,
                        help="Per-page truncation token budget (default: 2048).")

    parser.add_argument("--provider", choices=["claude", "openai"], default="claude")
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    from ..core.logging_setup import setup_logging
    setup_logging(verbose=args.verbose)

    model = args.model or (
        "claude-sonnet-4-20250514" if args.provider == "claude" else "gpt-4o"
    )

    search_config = SearchConfig(
        web_search_endpoint=args.search_endpoint,
        web_browse_endpoint=args.browse_endpoint,
        web_search_provider=args.search_provider,
        visit_max_content_tokens=args.visit_max_tokens,
        agent_max_turns=args.max_turns,
    )
    if not search_config.web_search_endpoint:
        print("Error: --search-endpoint or WEB_SEARCH_ENDPOINT must be set.",
              file=sys.stderr)
        sys.exit(1)

    asyncio.run(async_main(
        args.hypothesis,
        search_config=search_config,
        provider_name=args.provider,
        model=model,
        api_key=args.api_key,
        base_url=args.base_url,
        cwd=os.path.abspath(args.cwd),
        focus=args.focus,
    ))


if __name__ == "__main__":
    cli()
