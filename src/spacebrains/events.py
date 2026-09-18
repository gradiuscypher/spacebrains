"""Event bus: persists events to the DB and fans them out to live subscribers (SSE)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from spacebrains.db import Database

log = logging.getLogger("spacebrains.events")


class EventBus:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    async def emit(
        self,
        kind: str,
        message: str,
        *,
        agent: str | None = None,
        ship: str | None = None,
        data: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "ts": time.time(),
            "agent": agent,
            "ship": ship,
            "kind": kind,
            "message": message,
            "data": data or {},
        }
        log.info("[%s] %s %s", agent or "-", kind, message)
        if persist:
            payload["id"] = await self._db.add_event(
                kind, message, agent=agent, ship=ship, data=data or {}
            )
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                self._subscribers.discard(q)
