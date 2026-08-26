from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EventBus:
    max_queue: int = 250
    _clients: set[asyncio.Queue] = field(default_factory=set)
    _seq: int = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    async def publish(self, event: str, payload: Any = None) -> dict[str, Any]:
        self._seq += 1
        item = {"id": self._seq, "event": str(event), "ts": time.time(), "data": payload if payload is not None else {}}
        for q in list(self._clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass
        return item

    @staticmethod
    def sse(item: dict[str, Any]) -> bytes:
        return (
            f"id: {item['id']}\n"
            f"event: {item['event']}\n"
            f"data: {json.dumps(item['data'], separators=(',', ':'))}\n\n"
        ).encode()
