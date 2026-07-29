"""Shared engine-side human handshake for HITL (#2 idea gating, #10 ask-back).

The engine emits ``AWAIT_USER`` and blocks until a human reply arrives on the
bus as ``USER_INPUT_RECEIVED`` — it talks to the UI *only* through events (member
B renders the prompt and emits the reply; the engine never imports B). If no
reply arrives within the window, a ``value=None`` fallback is emitted so an
unattended run never hangs.

The reply is emitted from the UI's stdin thread, so the awaiting ``Future`` is
resolved via ``loop.call_soon_threadsafe`` rather than set directly in the bus
subscriber (which runs in the emitting thread).
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..events.types import AWAIT_USER, USER_INPUT_RECEIVED


async def await_user_decision(
    bus: Any,
    *,
    kind: str,
    prompt: str,
    node_id: str = "",
    options: list[str] | None = None,
    timeout: int,
) -> str | None:
    """Emit ``AWAIT_USER`` and block until the human replies (or ``timeout``).

    Returns the reply ``value`` (a string the user typed), or ``None`` on
    timeout / empty reply. Correlates by ``node_id`` when both sides carry one.
    """
    options = list(options or [])
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _resolve(value: Any) -> None:
        if not fut.done():
            fut.set_result(value)

    def _on_reply(event: Any) -> None:
        if fut.done():
            return
        data = getattr(event, "data", None) or {}
        reply_node = data.get("node_id", "")
        # Correlate by node_id when both sides carry one; otherwise accept.
        if node_id and reply_node and reply_node != node_id:
            return
        # The reply is emitted from the UI's stdin thread — marshal onto our loop.
        loop.call_soon_threadsafe(_resolve, data.get("value"))

    bus.on(USER_INPUT_RECEIVED, _on_reply)
    try:
        bus.emit(AWAIT_USER, {
            "kind": kind,
            "prompt": prompt,
            "node_id": node_id,
            "options": options,
        })
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # Unsubscribe before the fallback emit so it can't re-enter _on_reply.
            bus.off(USER_INPUT_RECEIVED, _on_reply)
            # Resolve the await for other observers (orchestrator clears the
            # pending checkpoint, dashboard closes the gate). value=None = no reply.
            bus.emit(USER_INPUT_RECEIVED, {"node_id": node_id, "value": None})
            return None
    finally:
        bus.off(USER_INPUT_RECEIVED, _on_reply)
