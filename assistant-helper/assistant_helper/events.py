"""Fan-out of helper events to every connected /events client. Thread-safe publish."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.history: list[dict] = []  # last few events, for debugging / tests

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _deliver(self, event: dict) -> None:
        if event.get("type") != "levels":  # 10 Hz meter traffic would push out everything else
            self.history.append(event)
            del self.history[:-200]
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropping event for a slow /events client")

    def publish(self, event: dict[str, Any]) -> None:
        """Callable from any thread."""
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop:
            self._deliver(event)
        else:
            loop.call_soon_threadsafe(self._deliver, event)
