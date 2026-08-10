"""Application event history.

Events are the human-readable story of what the system did: connections,
homing, detections, engagements, faults. They go to three places at once — the
structured log (journal), the SQLite ``events`` table, and any browser
listening on the telemetry WebSocket.

Retention is enforced on a timer so storage cannot grow without bound.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.database.db import run_db
from app.database.models import Event
from app.logging_config import get_logger

log = get_logger(__name__)

EventListener = Callable[[dict[str, Any]], Awaitable[None]]

# Categories used across the application. Kept as constants so the UI filter
# and the emitters cannot drift apart.
CAT_SYSTEM = "system"
CAT_CAMERA = "camera"
CAT_CONTROLLER = "controller"
CAT_DETECTION = "detection"
CAT_TARGETING = "targeting"
CAT_SPRAY = "spray"
CAT_MOTION = "motion"
CAT_SECURITY = "security"

CATEGORIES = (
    CAT_SYSTEM,
    CAT_CAMERA,
    CAT_CONTROLLER,
    CAT_DETECTION,
    CAT_TARGETING,
    CAT_SPRAY,
    CAT_MOTION,
    CAT_SECURITY,
)


class EventLog:
    def __init__(self, recent_size: int = 200) -> None:
        self._recent: deque[dict[str, Any]] = deque(maxlen=recent_size)
        self._listeners: list[EventListener] = []
        #: Suppression window for repeating events, keyed by (category, message).
        self._last_emit: dict[tuple[str, str], float] = {}

    def subscribe(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    @property
    def recent(self) -> list[dict[str, Any]]:
        return list(self._recent)

    async def emit(
        self,
        category: str,
        message: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
        snapshot_path: str | None = None,
        dedupe_s: float = 0.0,
    ) -> None:
        """Record an event. Never raises — logging must not break the caller."""
        if dedupe_s > 0:
            key = (category, message)
            now = time.monotonic()
            if now - self._last_emit.get(key, -1e9) < dedupe_s:
                return
            self._last_emit[key] = now

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "category": category,
            "message": message,
            "data": data,
            "snapshot": snapshot_path,
        }
        self._recent.append(record)

        log_fn = getattr(log, level if level in {"debug", "info", "warning", "error"} else "info")
        log_fn(message, extra={"ctx": {"category": category, **(data or {})}})

        try:
            await run_db(
                lambda session: session.add(
                    Event(
                        level=level,
                        category=category,
                        message=message,
                        data=data,
                        snapshot_path=snapshot_path,
                    )
                )
            )
        except Exception:
            log.exception("failed to persist event")

        for listener in list(self._listeners):
            try:
                await listener(record)
            except Exception:
                log.exception("event listener failed")

    # -- queries ---------------------------------------------------------
    async def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
        level: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))

        def _query(session: Any) -> list[dict[str, Any]]:
            stmt = select(Event).order_by(Event.id.desc())
            if category:
                stmt = stmt.where(Event.category == category)
            if level:
                stmt = stmt.where(Event.level == level)
            if since:
                stmt = stmt.where(Event.ts >= since)
            stmt = stmt.limit(limit).offset(max(0, offset))
            return [row.as_dict() for row in session.scalars(stmt).all()]

        return await run_db(_query)

    async def prune(self, retention_days: int, max_events: int) -> int:
        """Delete old events. Returns the number of rows removed."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        def _prune(session: Any) -> int:
            removed = session.execute(delete(Event).where(Event.ts < cutoff)).rowcount or 0
            total = session.scalar(select(Event.id).order_by(Event.id.desc()).limit(1))
            count = session.query(Event).count()
            if total is not None and count > max_events:
                keep_ids = session.scalars(
                    select(Event.id).order_by(Event.id.desc()).limit(max_events)
                ).all()
                if keep_ids:
                    oldest_kept = min(keep_ids)
                    removed += (
                        session.execute(delete(Event).where(Event.id < oldest_kept)).rowcount or 0
                    )
            return removed

        return await run_db(_prune)


async def prune_snapshots(directory: Path, retention_days: int, max_mb: int) -> int:
    """Delete snapshots older than the retention window, then oldest-first
    until the directory fits the size budget. Returns files removed."""

    def _prune() -> int:
        if not directory.exists():
            return 0
        cutoff = time.time() - retention_days * 86400
        files = sorted(
            (p for p in directory.glob("**/*.jpg") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        removed = 0
        for path in list(files):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                files.remove(path)
                removed += 1
        budget = max_mb * 1024 * 1024
        total = sum(p.stat().st_size for p in files if p.exists())
        for path in files:
            if total <= budget:
                break
            try:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:  # pragma: no cover - race with another writer
                continue
        return removed

    return await asyncio.to_thread(_prune)
