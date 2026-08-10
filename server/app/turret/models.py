"""Turret link and hardware state as seen by the server."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.turret.protocol import Status


class LinkState(str, Enum):
    DISCONNECTED = "disconnected"
    HANDSHAKE = "handshake"
    CONNECTED = "connected"
    #: Connected but speaking a protocol version we refuse to command.
    INCOMPATIBLE = "incompatible"


@dataclass
class ControllerInfo:
    controller_id: str = ""
    firmware_version: str = ""
    protocol_version: int = 0
    capabilities: list[str] = field(default_factory=list)
    hardware: dict[str, Any] = field(default_factory=dict)
    connected_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "controller_id": self.controller_id,
            "firmware_version": self.firmware_version,
            "protocol_version": self.protocol_version,
            "capabilities": self.capabilities,
            "hardware": self.hardware,
            "connected_since_s": (
                round(time.monotonic() - self.connected_at, 1) if self.connected_at else None
            ),
        }


@dataclass
class TurretState:
    """Latest hardware state. Never inferred — only ever set from a status frame."""

    pan_deg: float = 0.0
    tilt_deg: float = 0.0
    target_pan_deg: float | None = None
    target_tilt_deg: float | None = None
    pan_rate_deg_s: float = 0.0
    tilt_rate_deg_s: float = 0.0
    moving: bool = False
    homed: bool = False
    armed: bool = False
    valve_open: bool = False
    estop: bool = False
    limit_pan_min: bool = False
    limit_pan_max: bool = False
    limit_tilt_min: bool = False
    limit_tilt_max: bool = False
    state: str = "BOOT"
    error: str | None = None
    seq: int = 0
    uptime_ms: int = 0
    #: ``time.monotonic()`` of the last status frame.
    updated_at: float | None = None

    def apply(self, status: Status) -> None:
        self.pan_deg = status.pan_deg
        self.tilt_deg = status.tilt_deg
        self.target_pan_deg = status.target_pan_deg
        self.target_tilt_deg = status.target_tilt_deg
        self.pan_rate_deg_s = status.pan_rate_deg_s
        self.tilt_rate_deg_s = status.tilt_rate_deg_s
        self.moving = status.moving
        self.homed = status.homed
        self.armed = status.armed
        self.valve_open = status.valve_open
        self.estop = status.estop
        self.limit_pan_min = status.limit_pan_min
        self.limit_pan_max = status.limit_pan_max
        self.limit_tilt_min = status.limit_tilt_min
        self.limit_tilt_max = status.limit_tilt_max
        self.state = status.state
        self.error = status.error
        self.seq = status.seq
        self.uptime_ms = status.uptime_ms
        self.updated_at = time.monotonic()

    def reset(self) -> None:
        """Forget hardware state on disconnect.

        Deliberately clears ``homed`` and ``valve_open``: once the link is gone
        the server knows nothing, and pretending otherwise is how a UI ends up
        showing a closed valve that is actually open.
        """
        self.__init__()  # type: ignore[misc]

    def is_stale(self, timeout_s: float) -> bool:
        return self.updated_at is None or (time.monotonic() - self.updated_at) > timeout_s

    def age_s(self) -> float | None:
        return None if self.updated_at is None else time.monotonic() - self.updated_at

    def as_dict(self) -> dict[str, Any]:
        status_age_s = self.age_s()
        return {
            "pan_deg": round(self.pan_deg, 3),
            "tilt_deg": round(self.tilt_deg, 3),
            "target_pan_deg": self.target_pan_deg,
            "target_tilt_deg": self.target_tilt_deg,
            "pan_rate_deg_s": round(self.pan_rate_deg_s, 2),
            "tilt_rate_deg_s": round(self.tilt_rate_deg_s, 2),
            "moving": self.moving,
            "homed": self.homed,
            "armed": self.armed,
            "valve_open": self.valve_open,
            "estop": self.estop,
            "limits": {
                "pan_min": self.limit_pan_min,
                "pan_max": self.limit_pan_max,
                "tilt_min": self.limit_tilt_min,
                "tilt_max": self.limit_tilt_max,
            },
            "state": self.state,
            "error": self.error,
            "status_age_s": round(status_age_s, 2) if status_age_s is not None else None,
        }


class TurretError(RuntimeError):
    """A command was rejected, timed out, or the link was unavailable."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
