"""Protected, file-backed camera credentials.

Camera URLs are ordinary runtime settings and therefore live in SQLite.  User
names and passwords do not: this small store keeps them in the protected data
directory and never exposes passwords through the API.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CameraCredentials:
    username: str
    password: str


class CameraCredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._items: dict[str, CameraCredentials] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
            for camera_id, item in payload.items():
                if not isinstance(camera_id, str) or not isinstance(item, dict):
                    continue
                username = item.get("username")
                password = item.get("password")
                if isinstance(username, str) and isinstance(password, str):
                    self._items[camera_id] = CameraCredentials(username, password)
        except (OSError, ValueError):
            # A corrupt credential file must not prevent the server starting.
            self._items = {}

    def get(self, camera_id: str) -> CameraCredentials | None:
        with self._lock:
            return self._items.get(camera_id)

    def status(self, camera_id: str | None = None) -> dict[str, Any]:
        """Return non-secret metadata only."""
        with self._lock:
            if camera_id is not None:
                item = self._items.get(camera_id)
                return {
                    "camera_id": camera_id,
                    "configured": item is not None,
                    "username": item.username if item else "",
                }
            return {
                key: {"camera_id": key, "configured": True, "username": value.username}
                for key, value in self._items.items()
            }

    def set(self, camera_id: str, username: str, password: str | None) -> None:
        self._validate_id(camera_id)
        with self._lock:
            current = self._items.get(camera_id)
            resolved_password = (
                password if password is not None else (current.password if current else "")
            )
            self._items[camera_id] = CameraCredentials(username.strip(), resolved_password)
            self._write()

    def remove(self, camera_id: str) -> bool:
        with self._lock:
            removed = self._items.pop(camera_id, None) is not None
            if removed:
                self._write()
            return removed

    def retain(self, camera_ids: AbstractSet[str]) -> None:
        with self._lock:
            if not (set(self._items) - camera_ids):
                return
            self._items = {key: value for key, value in self._items.items() if key in camera_ids}
            self._write()

    @staticmethod
    def _validate_id(camera_id: str) -> None:
        if not camera_id or not all(ch.isalnum() or ch in "-_" for ch in camera_id):
            raise ValueError("camera id may only contain letters, digits, '-' and '_'")

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            key: {"username": value.username, "password": value.password}
            for key, value in self._items.items()
        }
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, self.path)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
