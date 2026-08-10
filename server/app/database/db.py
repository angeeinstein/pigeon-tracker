"""Database engine, session management and migration bootstrap.

SQLite is used synchronously. Traffic is low (settings, calibration, events) and
a synchronous session is far easier to reason about than an async one; any call
made from the event loop goes through :func:`run_db` which hops to a worker
thread so the loop is never blocked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.database.migrations import LATEST_VERSION, MIGRATIONS
from app.database.models import Base
from app.logging_config import get_logger

log = get_logger(__name__)

T = TypeVar("T")

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        # WAL keeps readers (UI polling events) from blocking the writer.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def init_engine(database_path: Path) -> Engine:
    """Create the engine and session factory, and bring the schema up to date."""
    global _engine, _session_factory

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{database_path}",
        future=True,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    _configure_sqlite(engine)
    run_migrations(engine)

    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    log.info("database ready", extra={"ctx": {"path": str(database_path)}})
    return engine


def run_migrations(engine: Engine) -> int:
    """Bring the schema to :data:`LATEST_VERSION`. Returns the resulting version."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"))
        row = conn.execute(text("SELECT version FROM schema_version LIMIT 1")).fetchone()
        current = int(row[0]) if row else 0

        if current == 0:
            # Fresh database (or one predating versioning): build from metadata.
            Base.metadata.create_all(bind=conn)
            conn.execute(text("DELETE FROM schema_version"))
            conn.execute(
                text("INSERT INTO schema_version (version) VALUES (:v)"),
                {"v": LATEST_VERSION},
            )
            log.info("database created", extra={"ctx": {"version": LATEST_VERSION}})
            return LATEST_VERSION

        applied = current
        for migration in sorted(MIGRATIONS, key=lambda m: m.version):
            if migration.version <= current:
                continue
            log.info(
                "applying migration",
                extra={"ctx": {"version": migration.version, "desc": migration.description}},
            )
            for statement in migration.statements:
                conn.execute(text(statement))
            applied = migration.version

        # New tables added by a later release are created here; existing tables
        # are left untouched by create_all.
        Base.metadata.create_all(bind=conn)

        if applied != current:
            conn.execute(text("UPDATE schema_version SET version = :v"), {"v": applied})
        return applied


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("database not initialised - call init_engine() first")
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope. Commits on success, rolls back on error."""
    if _session_factory is None:
        raise RuntimeError("database not initialised - call init_engine() first")
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def run_db(fn: Callable[[Session], T]) -> T:
    """Run a database callable in a worker thread. Use this from async code."""

    def _call() -> T:
        with session_scope() as session:
            return fn(session)

    return await asyncio.to_thread(_call)


def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def database_status() -> dict[str, Any]:
    """Cheap health probe used by ``/api/health``."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version FROM schema_version")).scalar()
        return {"ok": True, "schema_version": int(version or 0), "url": str(engine.url)}
    except Exception as exc:  # pragma: no cover - only on a broken deployment
        return {"ok": False, "error": str(exc)}
