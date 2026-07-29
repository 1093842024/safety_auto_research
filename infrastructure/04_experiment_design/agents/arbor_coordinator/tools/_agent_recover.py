"""Shared helpers for recovering structured output from a finished/cut-short Agent run.

The search and research lanes both drive a sub-agent that must emit a final
JSON object. Two failure modes make the agent's *return value* unreliable:

- The premature-stop nudge (``Agent._looks_like_premature_no_tool_stop``) can
  push the agent past a perfectly good final JSON because the JSON prose
  contains future-intent words; if a later turn then times out or hits
  max_turns, the good answer is lost from the return value.
- On ``max_turns`` the agent returns a placeholder string, not the last text.

Both are recovered the same way: the Agent now records a normalized,
provider-agnostic transcript (``agent.assistant_texts`` / ``agent.tool_uses``),
so we can scan back for the last assistant message that parses as JSON, and
cross-check cited sources against URLs actually visited.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Pull the first balanced JSON object out of ``text``.

    Tries a direct ``json.loads`` (after stripping a code fence) first, then
    falls back to scanning for the largest balanced ``{ ... }`` block.
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Try to find the largest balanced { ... } block.
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in starts:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
    return None


def recover_json(agent: Any, raw: str) -> dict[str, Any] | None:
    """Best-effort recovery of the agent's final JSON.

    Tries ``raw`` (the value ``agent.run`` returned) first, then scans the
    normalized assistant transcript in reverse for the most recent message that
    parses as a JSON object. This rescues a valid final JSON that the loop
    nudged past, or that was lost to a max_turns placeholder / timeout.
    """
    parsed = _extract_json_block(raw or "")
    if parsed is not None:
        return parsed
    for text in reversed(getattr(agent, "assistant_texts", []) or []):
        parsed = _extract_json_block(text)
        if parsed is not None:
            return parsed
    return None


def _normalize_url(url: str) -> str:
    """Loose URL key for matching: host + path stem, lowercased, no scheme/query
    and no trailing ``vN`` version suffix (so ``…/2203.11171v4`` == ``…/2203.11171``)."""
    if not url:
        return ""
    s = url.strip()
    if "//" not in s and not s.startswith("http"):
        s = "//" + s
    try:
        p = urlparse(s if "//" in s else "//" + s)
        host = (p.netloc or "").lower().lstrip("www.")
        path = (p.path or "").rstrip("/").lower()
    except Exception:
        host, path = "", s.lower()
    path = re.sub(r"v\d+$", "", path)  # drop arxiv-style version suffix
    return f"{host}{path}"


def visited_urls(agent: Any) -> set[str]:
    """Normalized set of URLs the agent actually passed to a visit tool."""
    out: set[str] = set()
    for tu in getattr(agent, "tool_uses", []) or []:
        if tu.get("name") not in ("web_visit", "visit"):
            continue
        url = (tu.get("input") or {}).get("url")
        items = url if isinstance(url, list) else [url]
        for u in items:
            if isinstance(u, str) and u.strip():
                key = _normalize_url(u)
                if key:
                    out.add(key)
    return out


def filter_sources_to_visited(
    sources: list[Any], visited: set[str]
) -> tuple[list[Any], int]:
    """Drop sources whose URL was never visited. Returns (kept, dropped_count).

    No-op (keeps everything) when ``visited`` is empty — e.g. an answer drawn
    purely from search snippets with no page visits — to avoid over-filtering.
    """
    if not visited:
        return list(sources), 0
    kept: list[Any] = []
    dropped = 0
    for s in sources:
        url = s.get("url", "") if isinstance(s, dict) else ""
        if not url or _normalize_url(url) in visited:
            kept.append(s)
        else:
            dropped += 1
    return kept, dropped
