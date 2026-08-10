"""Settings store: validated runtime configuration backed by SQLite.

Subsystems never read the database directly. They read
:attr:`SettingsStore.current` and subscribe to changes, so a settings edit in
the UI reconfigures the camera, detector or targeting logic without a restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from app.database.db import run_db
from app.database.models import SettingsRecord
from app.logging_config import get_logger
from app.services.settings_schema import SECTION_MODELS, AppSettings

log = get_logger(__name__)

SettingsListener = Callable[[AppSettings, set[str]], Awaitable[None]]


class SettingsError(ValueError):
    """Raised when a settings update is rejected."""


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``patch`` into ``base`` (lists are replaced, not merged)."""
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class SettingsStore:
    def __init__(self) -> None:
        self._settings = AppSettings()
        self._listeners: list[SettingsListener] = []
        self._lock = asyncio.Lock()

    # -- access ----------------------------------------------------------
    @property
    def current(self) -> AppSettings:
        """The current, fully validated settings. Treat as immutable."""
        return self._settings

    def as_dict(self) -> dict[str, Any]:
        return self._settings.model_dump(mode="json")

    def section(self, name: str) -> BaseModel:
        if name not in SECTION_MODELS:
            raise SettingsError(f"unknown settings section: {name}")
        return getattr(self._settings, name)

    # -- lifecycle -------------------------------------------------------
    async def load(self) -> AppSettings:
        """Load all sections from the database, falling back to defaults.

        A section that fails validation (hand-edited database, or a downgrade)
        is logged and replaced with defaults rather than preventing startup —
        the turret must always come up in a safe, controllable state.
        """

        def _read(session: Any) -> dict[str, Any]:
            return {row.section: row.data for row in session.query(SettingsRecord).all()}

        stored = await run_db(_read)

        payload: dict[str, Any] = {}
        for name, model in SECTION_MODELS.items():
            data = stored.get(name)
            if not data:
                continue
            try:
                merged = deep_merge(model().model_dump(mode="json"), data)
                payload[name] = model.model_validate(merged).model_dump(mode="json")
            except ValidationError as exc:
                log.error(
                    "invalid stored settings section, using defaults",
                    extra={"ctx": {"section": name, "error": exc.error_count()}},
                )

        self._settings = AppSettings.model_validate(payload)
        log.info("settings loaded", extra={"ctx": {"sections": len(payload)}})
        return self._settings

    def subscribe(self, listener: SettingsListener) -> None:
        """Register an async callback invoked after every successful update."""
        self._listeners.append(listener)

    # -- mutation --------------------------------------------------------
    async def update_section(self, name: str, patch: dict[str, Any]) -> BaseModel:
        """Validate and persist a partial update to one section."""
        return (await self.update({name: patch}))[name]  # type: ignore[return-value]

    async def update(self, patch: dict[str, dict[str, Any]]) -> dict[str, BaseModel]:
        """Validate and persist a partial update spanning several sections.

        Validation happens against the *whole* settings object so cross-field
        rules still apply. Nothing is written unless everything validates.
        """
        unknown = set(patch) - set(SECTION_MODELS)
        if unknown:
            raise SettingsError(f"unknown settings section(s): {', '.join(sorted(unknown))}")

        async with self._lock:
            current = self._settings.model_dump(mode="json")
            candidate = deep_merge(current, patch)
            try:
                new_settings = AppSettings.model_validate(candidate)
            except ValidationError as exc:
                raise SettingsError(_format_validation_error(exc)) from exc

            changed = {
                name
                for name in patch
                if getattr(new_settings, name).model_dump(mode="json") != current.get(name)
            }
            if not changed:
                return {name: getattr(new_settings, name) for name in patch}

            dumped = new_settings.model_dump(mode="json")

            def _write(session: Any) -> None:
                for name in changed:
                    record = session.get(SettingsRecord, name)
                    if record is None:
                        session.add(SettingsRecord(section=name, data=dumped[name]))
                    else:
                        record.data = dumped[name]

            await run_db(_write)
            self._settings = new_settings

        log.info("settings updated", extra={"ctx": {"sections": ",".join(sorted(changed))}})
        await self._notify(changed)
        return {name: getattr(new_settings, name) for name in patch}

    async def reset_section(self, name: str) -> BaseModel:
        """Restore a section to its defaults."""
        if name not in SECTION_MODELS:
            raise SettingsError(f"unknown settings section: {name}")
        defaults = SECTION_MODELS[name]().model_dump(mode="json")
        return await self.update_section(name, defaults)

    async def _notify(self, changed: set[str]) -> None:
        for listener in list(self._listeners):
            try:
                await listener(self._settings, changed)
            except Exception:
                log.exception(
                    "settings listener failed",
                    extra={"ctx": {"listener": getattr(listener, "__qualname__", "?")}},
                )


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
