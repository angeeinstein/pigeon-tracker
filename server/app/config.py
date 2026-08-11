"""Deployment configuration.

This module holds settings that describe *where the application runs*: paths,
bind address, secrets, feature switches. It is read from the environment (and
an optional protected env file) once at startup and never changes at runtime.

Everything a user can change from the web UI lives in
:mod:`app.services.settings` instead, backed by the database.
"""

from __future__ import annotations

import contextlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ENV_FILES = (
    "/etc/turret-control/turret.env",
    str(Path(__file__).resolve().parents[1] / ".env"),
)


class DeploymentConfig(BaseSettings):
    """Environment-driven configuration. Prefix every variable with ``TURRET_``."""

    model_config = SettingsConfigDict(
        env_prefix="TURRET_",
        env_file=DEFAULT_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- HTTP server -----------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    root_path: str = ""
    cors_origins: list[str] = Field(default_factory=list)

    # --- Paths -----------------------------------------------------------
    data_dir: Path = Path("/var/lib/turret-control")
    models_dir: Path | None = None
    snapshot_dir: Path | None = None
    detection_dir: Path | None = None
    static_dir: Path | None = None

    # --- Logging ---------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # --- Security --------------------------------------------------------
    #: When empty, the web UI is open. Intended for trusted LAN use only.
    auth_enabled: bool = False
    auth_username: str = "admin"
    auth_password: str = ""
    #: HMAC key for session cookies. Auto-generated into the data dir if unset.
    secret_key: str = ""
    #: Pre-shared token the ESP32 must present. Empty disables the check.
    controller_token: str = ""
    #: Largest accepted WebSocket/JSON payload, bytes.
    max_payload_bytes: int = 16 * 1024

    # --- Feature switches (development without hardware) -----------------
    #: Force the simulated video source regardless of camera settings.
    force_simulated_camera: bool = False
    #: Force the mock detector (no model download, deterministic output).
    force_mock_detector: bool = False
    #: Serve the frontend dev server's assets instead of the built bundle.
    dev_mode: bool = False

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    # --- Derived paths ---------------------------------------------------
    @property
    def database_path(self) -> Path:
        return self.data_dir / "turret.db"

    @property
    def resolved_models_dir(self) -> Path:
        return self.models_dir or (self.data_dir / "models")

    @property
    def resolved_snapshot_dir(self) -> Path:
        return self.snapshot_dir or (self.data_dir / "snapshots")

    @property
    def resolved_detection_dir(self) -> Path:
        return self.detection_dir or (self.data_dir / "detections")

    @property
    def resolved_static_dir(self) -> Path:
        return self.static_dir or (Path(__file__).resolve().parent / "static")

    def ensure_directories(self) -> None:
        """Create the runtime directories. Safe to call repeatedly."""
        for path in (
            self.data_dir,
            self.resolved_models_dir,
            self.resolved_snapshot_dir,
            self.resolved_detection_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def resolve_secret_key(self) -> str:
        """Return the session key, generating and persisting one if needed.

        The generated key is written with 0600 permissions inside the data
        directory so sessions survive a restart without ever entering git.
        """
        if self.secret_key:
            return self.secret_key
        key_file = self.data_dir / "secret_key"
        if key_file.exists():
            return key_file.read_text(encoding="utf-8").strip()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32).hex()
        key_file.write_text(key, encoding="utf-8")
        with contextlib.suppress(OSError):  # pragma: no cover - non-POSIX filesystems
            key_file.chmod(0o600)
        return key


@lru_cache(maxsize=1)
def get_config() -> DeploymentConfig:
    """Process-wide deployment configuration (cached)."""
    return DeploymentConfig()
