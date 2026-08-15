"""Camera abstractions.

A camera source produces frames into a :class:`LatestFrameBuffer`. Sources are
named, so the same machinery serves the fixed overview camera today and a
turret-mounted camera later without any structural change.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.camera.frame_buffer import LatestFrameBuffer
from app.services.settings_schema import CameraConfig


@dataclass(frozen=True)
class Frame:
    """One decoded video frame.

    ``image`` is BGR uint8 (OpenCV convention) and must be treated as
    read-only: it is shared by every consumer. Copy before drawing on it.
    """

    image: np.ndarray
    seq: int
    #: ``time.monotonic()`` when the frame was decoded — use for age/latency.
    ts: float
    #: ``time.time()`` when the frame was decoded — use for display/filenames.
    wall_ts: float
    camera_id: str
    #: Native decoded image retained for short-lived high-resolution crops.
    #: It may be the same array as ``image`` when no camera downscale is set.
    native_image: np.ndarray | None = field(default=None, repr=False)

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.ts

    @property
    def native(self) -> np.ndarray:
        return self.native_image if self.native_image is not None else self.image


@dataclass
class CameraStatus:
    camera_id: str
    name: str = ""
    enabled: bool = True
    connected: bool = False
    backend: str = "none"
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frames: int = 0
    reconnects: int = 0
    last_frame_age_s: float | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "name": self.name,
            "enabled": self.enabled,
            "connected": self.connected,
            "backend": self.backend,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 2),
            "frames": self.frames,
            "reconnects": self.reconnects,
            "last_frame_age_s": (
                round(self.last_frame_age_s, 2) if self.last_frame_age_s is not None else None
            ),
            "error": self.error,
            **({"extra": self.extra} if self.extra else {}),
        }


class CameraSource(abc.ABC):
    """A video source that continuously fills a latest-frame buffer."""

    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self.buffer = LatestFrameBuffer(config.id)
        self.status = CameraStatus(camera_id=config.id, name=config.name, enabled=config.enabled)

    @property
    def camera_id(self) -> str:
        return self.config.id

    @abc.abstractmethod
    def start(self) -> None:
        """Start producing frames. Must return promptly (no blocking connect)."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop producing frames and release all resources."""

    def latest(self) -> Frame | None:
        return self.buffer.latest()

    def snapshot_status(self) -> CameraStatus:
        frame = self.buffer.latest()
        self.status.last_frame_age_s = frame.age_s if frame else None
        if frame is not None:
            self.status.width, self.status.height = frame.width, frame.height
        return self.status
