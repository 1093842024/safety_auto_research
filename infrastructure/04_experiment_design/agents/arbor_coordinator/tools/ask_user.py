"""AskUser — the engine-side ask-back hook (#10, A5.4).

When the coordinator is genuinely blocked on missing information it cannot
obtain itself, it calls this tool. The tool emits ``AWAIT_USER`` and suspends
until a human reply arrives on the bus as ``USER_INPUT_RECEIVED`` — the engine
talks to the UI *only* through events (member B renders the prompt and emits
the reply; the engine never imports B). If no reply arrives within the
configured window, the tool returns a non-blocking fallback so an unattended
run never hangs.

Registered only when ``ui.allow_agent_questions`` is set (see ``get_coordinator_tools``);
in the default unattended mode the agent has no way to ask and never stalls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.tools.base import Tool
from ..hitl import await_user_decision

if TYPE_CHECKING:
    from ..config import CoordinatorConfig
    from ..idea_tree import IdeaTree


class AskUserTool(Tool):
    """Ask the human for missing information and block until they answer."""

    name = "AskUser"
    description = (
        "Ask the human operator for information you genuinely cannot obtain "
        "yourself, then wait for their reply.\n\n"
        "Use this ONLY when truly blocked — e.g. an ambiguous objective, a "
        "missing dataset path or credential, or a decision that needs human "
        "judgment. First try to answer the question from the codebase, the "
        "task description, or your tools. Do NOT use it for routine progress "
        "updates or to confirm decisions you can make yourself.\n\n"
        "If no human answers within the wait window, you receive a note telling "
        "you to proceed on your best assumption — so never depend on a reply."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the human (be specific and self-contained).",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional suggested choices. Omit for a free-form answer.",
            },
            "node_id": {
                "type": "string",
                "description": "Related idea-tree node ID, if the question is about a specific idea.",
            },
            "kind": {
                "type": "string",
                "enum": ["ask_back", "idea_direction"],
                "description": "Prompt kind for the UI. Defaults to ask_back.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Optional per-question wait window. Omit to use ui.ask_user_timeout.",
            },
        },
        "required": ["question"],
    }
    # Not read-only: this serializes AskUser calls in the agent loop, so two
    # questions are never put to the human at once (and their replies can't
    # cross-talk). The coordinator runs with auto_git off, so no commit happens.
    is_read_only = False

    def __init__(
        self,
        *,
        cwd: str,
        tree: "IdeaTree",
        config: "CoordinatorConfig",
        workspace_dir: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(cwd=cwd, workspace_dir=workspace_dir, **kwargs)
        self._tree = tree
        self._config = config

    async def execute(self, **kwargs: Any) -> str:
        question = (kwargs.get("question") or "").strip()
        if not question:
            return "Error: 'question' is required."
        node_id = kwargs.get("node_id") or ""
        options = list(kwargs.get("options") or [])
        kind = str(kwargs.get("kind") or "ask_back").strip() or "ask_back"
        if kind not in {"ask_back", "idea_direction"}:
            kind = "ask_back"
        timeout_raw = kwargs.get("timeout_seconds")
        timeout = max(
            1,
            int(timeout_raw if timeout_raw is not None else self._config.ui.ask_user_timeout),
        )

        answer = await await_user_decision(
            self._tree.bus,
            kind=kind,
            prompt=question,
            node_id=node_id,
            options=options,
            timeout=timeout,
        )

        if answer is None or (isinstance(answer, str) and not answer.strip()):
            return (
                "No human reply arrived within the wait window. Proceed with your "
                "best assumption and state it explicitly — do not block on this."
            )
        return f"User replied: {answer}"
