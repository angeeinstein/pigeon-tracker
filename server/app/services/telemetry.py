"""Telemetry fan-out to browser WebSocket clients.

One producer (the runtime's telemetry loop) and N consumers. A client that
cannot keep up is disconnected rather than allowed to apply back-pressure to
the rest of the system — a slow phone must never slow down the turret.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from app.logging_config import get_logger

log = get_logger(__name__)


class BrowserConnection(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class TelemetryHub:
    #: Frames queued per client before it is considered too slow.
    QUEUE_LIMIT = 8

    def __init__(self) -> None:
        self._clients: dict[BrowserConnection, asyncio.Queue[str]] = {}
        self._tasks: dict[BrowserConnection, asyncio.Task[None]] = {}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def register(self, connection: BrowserConnection) -> None:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.QUEUE_LIMIT)
        self._clients[connection] = queue
        self._tasks[connection] = asyncio.create_task(
            self._writer(connection, queue), name="telemetry-writer"
        )

    async def unregister(self, connection: BrowserConnection) -> None:
        self._clients.pop(connection, None)
        task = self._tasks.pop(connection, None)
        if task is not None:
            task.cancel()

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        message = json.dumps(payload, default=str)
        for connection, queue in list(self._clients.items()):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                log.info("dropping slow telemetry client")
                await self.unregister(connection)

    async def send_to(self, connection: BrowserConnection, payload: dict[str, Any]) -> None:
        queue = self._clients.get(connection)
        if queue is None:
            return
        try:
            queue.put_nowait(json.dumps(payload, default=str))
        except asyncio.QueueFull:
            await self.unregister(connection)

    async def _writer(self, connection: BrowserConnection, queue: asyncio.Queue[str]) -> None:
        try:
            while True:
                message = await queue.get()
                await connection.send_text(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Normal on tab close; the route's finally block unregisters.
            self._clients.pop(connection, None)

    async def close_all(self) -> None:
        for connection in list(self._clients):
            await self.unregister(connection)
