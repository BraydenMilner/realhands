"""EventBroker — fans out executor events to /events SSE subscribers.

In-memory only. Keeps the last 1000 events in a ring buffer so a client that
reconnects with ?last_id=N can replay anything it missed since.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import AsyncIterator, Optional

log = logging.getLogger("agent_bridge.broker")

BUFFER_SIZE = 1000
QUEUE_MAX = 256  # Per-subscriber backlog; drop oldest if slow consumer.


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._buffer: collections.deque[tuple[int, dict]] = collections.deque(
            maxlen=BUFFER_SIZE
        )
        self._seq = 0
        self._lock = asyncio.Lock()

    async def publish(self, event: dict) -> None:
        async with self._lock:
            self._seq += 1
            seq = self._seq
            envelope = {"_seq": seq, **event}
            self._buffer.append((seq, envelope))
            stale: list[asyncio.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(envelope)
                except asyncio.QueueFull:
                    # Drop oldest, push new — never block publish.
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(envelope)
                    except asyncio.QueueFull:
                        stale.append(q)
            for q in stale:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    async def subscribe(
        self, last_id: Optional[int] = None
    ) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        # Snapshot replay first, then add to live subscriber set under the lock
        # so we don't miss events that fire mid-snapshot.
        async with self._lock:
            replay = [env for seq, env in self._buffer if last_id is None or seq > last_id]
            self._subscribers.append(q)
        try:
            for env in replay:
                yield env
            while True:
                env = await q.get()
                yield env
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    @property
    def last_seq(self) -> int:
        return self._seq
