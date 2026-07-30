"""Async-aware progress-event bus for Server-Sent Events (SSE).

Background threads (orchestrator _drive) push progress events via ``emit()``.
SSE endpoints iterate via ``subscribe()`` — an async generator that yields
events as they arrive. Built on ``asyncio.Queue``; bridges sync→async with
``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from typing import Any, AsyncGenerator

_log = logging.getLogger(__name__)

# How long a subscriber's queue can grow before we drop the oldest event.
_MAX_QUEUE_SIZE = 128


class ProgressBus:
    """Per-run event bus: one ``asyncio.Queue`` per active run_id.

    Thread-safe: ``emit()`` can be called from any thread (sync or async); it
    finds the running event loop and enqueues via ``call_soon_threadsafe``.
    """

    def __init__(self) -> None:
        # P1 fix: a SET of queues per run — previously a second subscriber overwrote
        # the first subscriber's queue, silently starving it.
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._conn_counts: dict[str, int] = {}  # active SSE subscribers per run
        # F1 fix: the serving event loop, captured by the first async request handler
        # (which always runs inside the loop). Background threads can NEVER discover
        # this loop themselves — ``asyncio.all_tasks()`` raises RuntimeError in a
        # thread without a running loop, so the old fallback silently dropped every
        # progress event emitted from orchestrator background threads.
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the serving event loop (called from async request handlers)."""

        if self._loop is None or self._loop.is_closed():
            self._loop = loop

    # ----------------------------------------------------------- public API
    def emit(self, run_id: str, event: dict[str, Any]) -> None:
        """Push a progress event to all subscribers of *run_id*.

        Safe to call from any thread.  If no subscriber is listening the event
        is silently dropped.
        """
        q_exists = bool(self._queues.get(run_id))
        if not q_exists:
            return  # no subscribers — drop silently
        # Stamp the event with a server-side timestamp.
        event.setdefault("ts", _time.time())
        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _log.warning("ProgressBus.emit: no event loop available — dropping event")
                return
        loop.call_soon_threadsafe(self._enqueue, run_id, event)

    def _enqueue(self, run_id: str, event: dict[str, Any]) -> None:
        for q in list(self._queues.get(run_id, ())):
            # Don't let unbounded queues build up if the client is slow.
            while q.qsize() >= _MAX_QUEUE_SIZE:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, run_id: str) -> AsyncGenerator[str, None]:
        """Async generator that yields SSE-formatted event strings.

        Usage in FastAPI::

            async def stream(run_id):
                return StreamingResponse(
                    progress_bus.subscribe(run_id),
                    media_type="text/event-stream",
                )
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._queues.setdefault(run_id, set()).add(q)
        self._conn_counts[run_id] = self._conn_counts.get(run_id, 0) + 1

        # Send an initial "connected" event so the client knows the stream is alive.
        yield self._sse_line("connected", {"run_id": run_id, "msg": "stream started"})

        try:
            while True:
                event = await q.get()
                yield self._sse_line("progress", event)
        except asyncio.CancelledError:
            pass
        finally:
            qs = self._queues.get(run_id)
            if qs is not None:
                qs.discard(q)
                if not qs:
                    self._queues.pop(run_id, None)
            self._conn_counts[run_id] = max(0, self._conn_counts.get(run_id, 1) - 1)
            if self._conn_counts.get(run_id, 0) <= 0:
                self._conn_counts.pop(run_id, None)

    @staticmethod
    def _sse_line(event: str, data: dict[str, Any]) -> str:
        payload = json.dumps(data, ensure_ascii=False, default=str)
        return f"event: {event}\ndata: {payload}\n\n"

    def has_subscribers(self, run_id: str) -> bool:
        return self._conn_counts.get(run_id, 0) > 0


# Singleton shared across the process.
progress_bus = ProgressBus()
