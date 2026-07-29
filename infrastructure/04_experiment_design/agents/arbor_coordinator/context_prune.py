"""Context pruning for the coordinator.

When a candidate is committed via ``TreeAddNode``, the scratch work that
produced it — the loaded skill bodies, the probe block, per-candidate
declarations, and final self-check notes — is no longer useful to the
coordinator. Only the committed hypothesis matters going forward.

This module rewrites that scratch work in place on the agent's message
list, leaving only the structural skeleton needed for tool_use /
tool_result pairing.

Anchor strategy
---------------
The most recent assistant turn that called ``TreeView(format="constraints")``
is treated as the start of the current IDEATE round (per the system
prompt, this is the mandatory first action of every IDEATE step).
Everything strictly after that anchor turn, up to and including the
current end of ``messages`` (the assistant turn carrying the just-
executed ``TreeAddNode``), is in-scope for rewriting.

Rewrite rules
-------------
* Assistant message: drop ``thinking`` / ``redacted_thinking`` blocks;
  collapse all ``text`` blocks into a single stub; preserve ``tool_use``
  blocks unchanged so their corresponding ``tool_result`` messages stay
  paired.
* User message (tool results): replace the ``content`` of any
  ``tool_result`` block whose ``tool_use_id`` matches an in-range
  ``LoadSkill`` call for one of the IDEATE skills.
* All other content (Bash output, TreeView snapshots, etc.) is left
  alone — the user only asked to elide skill bodies and reasoning.

The function is idempotent: re-running it after another candidate is
committed in the same IDEATE round only stubs newly-grown messages.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

REASONING_STUB = "[IDEATE reasoning elided post-commit]"
SKILL_STUB = "[skill body elided post-IDEATE]"
IDEATE_SKILL_NAMES = frozenset({
    "idea_drafting",
    "first_principles_probe",
})


def _content_blocks(msg: dict[str, Any]) -> list[Any] | None:
    content = msg.get("content")
    if isinstance(content, list):
        return content
    return None


def _find_constraints_anchor(messages: list[dict[str, Any]]) -> int | None:
    """Index of the most recent assistant turn calling TreeView(constraints)."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue
        blocks = _content_blocks(msg)
        if not blocks:
            continue
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") != "tool_use":
                continue
            if blk.get("name") != "TreeView":
                continue
            inp = blk.get("input")
            if isinstance(inp, dict) and inp.get("format") == "constraints":
                return i
    return None


def _collect_skill_tool_use_ids(
    messages: list[dict[str, Any]],
    start: int,
    end: int,
) -> set[str]:
    """Return tool_use IDs for in-range LoadSkill calls on IDEATE skills."""
    ids: set[str] = set()
    for i in range(start, end):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue
        blocks = _content_blocks(msg)
        if not blocks:
            continue
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") != "tool_use":
                continue
            if blk.get("name") != "LoadSkill":
                continue
            inp = blk.get("input") or {}
            skill = inp.get("skill_name") if isinstance(inp, dict) else None
            if skill in IDEATE_SKILL_NAMES:
                tool_id = blk.get("id")
                if isinstance(tool_id, str):
                    ids.add(tool_id)
    return ids


def _rewrite_assistant_message(msg: dict[str, Any]) -> bool:
    """Stub text blocks, drop thinking blocks, keep tool_use blocks.

    Returns True if any change was made.
    """
    blocks = _content_blocks(msg)
    if not blocks:
        return False

    new_blocks: list[Any] = []
    modified = False
    text_stub_emitted = False

    for blk in blocks:
        if not isinstance(blk, dict):
            new_blocks.append(blk)
            continue
        btype = blk.get("type")
        if btype in ("thinking", "redacted_thinking"):
            modified = True
            continue  # drop — past thinking blocks aren't required on subsequent requests
        if btype == "text":
            existing = blk.get("text", "")
            if existing == REASONING_STUB:
                if not text_stub_emitted:
                    new_blocks.append(blk)
                    text_stub_emitted = True
                else:
                    modified = True
                continue
            if not text_stub_emitted:
                new_blocks.append({"type": "text", "text": REASONING_STUB})
                text_stub_emitted = True
            modified = True
            continue
        # tool_use and anything else: preserve
        new_blocks.append(blk)

    if modified:
        msg["content"] = new_blocks
    return modified


def _rewrite_skill_tool_results(
    msg: dict[str, Any],
    skill_ids: set[str],
) -> int:
    """Replace tool_result content for in-range LoadSkill IDs. Returns count."""
    blocks = _content_blocks(msg)
    if not blocks:
        return 0
    stubbed = 0
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") != "tool_result":
            continue
        if blk.get("tool_use_id") not in skill_ids:
            continue
        if blk.get("content") == SKILL_STUB:
            continue
        blk["content"] = SKILL_STUB
        stubbed += 1
    return stubbed


def prune_ideate_context(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Prune IDEATE-round scratch work from ``messages`` in place.

    Safe to call repeatedly within the same IDEATE round (idempotent).
    No-op if no ``TreeView(constraints)`` anchor is found, so accidental
    invocation outside an IDEATE round cannot corrupt history.

    Returns a small stats dict ``{anchor, rewritten_assistants,
    stubbed_skill_results}`` suitable for logging.
    """
    anchor = _find_constraints_anchor(messages)
    if anchor is None:
        log.debug("prune_ideate_context: no TreeView(constraints) anchor — skip")
        return {"anchor": -1, "rewritten_assistants": 0, "stubbed_skill_results": 0}

    start = anchor + 1
    end = len(messages)
    if start >= end:
        return {"anchor": anchor, "rewritten_assistants": 0, "stubbed_skill_results": 0}

    skill_ids = _collect_skill_tool_use_ids(messages, start, end)

    rewritten = 0
    stubbed = 0
    for i in range(start, end):
        msg = messages[i]
        role = msg.get("role")
        if role == "assistant":
            if _rewrite_assistant_message(msg):
                rewritten += 1
        elif role == "user":
            stubbed += _rewrite_skill_tool_results(msg, skill_ids)

    log.info(
        "prune_ideate_context: anchor=%d rewrote=%d assistant turn(s), "
        "stubbed=%d skill result(s)",
        anchor, rewritten, stubbed,
    )
    return {
        "anchor": anchor,
        "rewritten_assistants": rewritten,
        "stubbed_skill_results": stubbed,
    }
