"""Resolve the effective protected-path set from config + plugin + tree meta."""

from __future__ import annotations

from typing import Any


def resolve_protected_paths(config: Any, plugin: Any, meta: dict[str, Any]) -> list[str]:
    """Union of config, plugin, and tree-meta protected globs (order-stable)."""
    out: list[str] = []
    seen: set[str] = set()
    sources = [
        getattr(config, "protected_paths", None) or [],
        (getattr(plugin, "protected_paths", None) or []) if plugin else [],
        meta.get("protected_paths") or [],
    ]
    for source in sources:
        for pattern in source:
            if pattern not in seen:
                seen.add(pattern)
                out.append(pattern)
    return out
