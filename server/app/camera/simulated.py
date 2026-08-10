"""Synthetic camera source for development without hardware.

Renders a balcony-ish scene with a couple of moving blobs that the mock
detector recognises as birds. Deterministic given a seed, so the targeting
state machine can be exercised end to end (and demoed) with no camera, no
network and no model.
"""

from __future__ import annotations

import math
import threading
import time

import cv2
import numpy as np

from app.camera.base import CameraSource, Frame
from app.logging_config import get_logger
from app.services.settings_schema import CameraConfig

log = get_logger(__name__)


class SimulatedBird:
    """A blob following a slow elliptical path with occasional pauses."""

    def __init__(self, seed: int, width: int, height: int) -> None:
        rng = np.random.default_rng(seed)
        self.width, self.height = width, height
        self.cx = float(rng.uniform(0.25, 0.75)) * width
        self.cy = float(rng.uniform(0.45, 0.8)) * height
        self.rx = float(rng.uniform(0.06, 0.18)) * width
        self.ry = float(rng.uniform(0.03, 0.10)) * height
        self.period = float(rng.uniform(14.0, 32.0))
        self.phase = float(rng.uniform(0.0, math.tau))
        self.size = int(rng.uniform(0.035, 0.06) * width)
        self.perch_ratio = float(rng.uniform(0.3, 0.6))

    def position(self, t: float) -> tuple[int, int, bool]:
        cycle = (t / self.period + self.phase / math.tau) % 1.0
        if cycle < self.perch_ratio:
            # Perched: sit still at the start of the path (this is what the
            # state machine's "stable for N seconds" rule needs to see).
            angle = self.phase
            moving = False
        else:
            angle = self.phase + math.tau * (cycle - self.perch_ratio) / (1 - self.perch_ratio)
            moving = True
        x = self.cx + self.rx * math.cos(angle)
        y = self.cy + self.ry * math.sin(angle)
        return int(x), int(y), moving


class SimulatedCameraSource(CameraSource):
    """Generates frames at a fixed rate without touching the network."""

    def __init__(self, config: CameraConfig, fps: float = 12.0, seed: int = 7) -> None:
        super().__init__(config)
        self.fps = fps
        self.width = config.target_width or 1280
        self.height = int(self.width * 9 / 16)
        self._birds = [SimulatedBird(seed + i, self.width, self.height) for i in range(2)]
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seq = 0
        self._t0 = time.monotonic()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"camera-sim-{self.camera_id}", daemon=True
        )
        self._thread.start()
        self.status.backend = "simulated"
        log.info("simulated camera started", extra={"ctx": {"camera": self.camera_id}})

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None
        self.status.connected = False
        self.buffer.clear()

    # -- rendering -------------------------------------------------------
    def bird_boxes(self, t: float) -> list[tuple[int, int, int, int]]:
        """Ground-truth boxes; the mock detector reads these directly."""
        boxes = []
        for bird in self._birds:
            x, y, _moving = bird.position(t)
            half = bird.size // 2
            boxes.append((x - half, y - half, x + half, y + half))
        return boxes

    def _render(self, t: float) -> np.ndarray:
        image = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        # Sky gradient.
        gradient = np.linspace(200, 120, self.height, dtype=np.uint8)
        image[:, :, 0] = gradient[:, None]
        image[:, :, 1] = (gradient * 0.85).astype(np.uint8)[:, None]
        image[:, :, 2] = (gradient * 0.7).astype(np.uint8)[:, None]

        floor_y = int(self.height * 0.78)
        cv2.rectangle(image, (0, floor_y), (self.width, self.height), (70, 80, 95), -1)
        # Railing.
        rail_y = int(self.height * 0.55)
        cv2.rectangle(image, (0, rail_y), (self.width, rail_y + 14), (90, 105, 120), -1)
        for x in range(40, self.width, 90):
            cv2.line(image, (x, rail_y), (x, floor_y), (85, 95, 110), 4)
        # Planter box.
        cv2.rectangle(
            image,
            (int(self.width * 0.62), floor_y - 60),
            (int(self.width * 0.92), floor_y + 10),
            (60, 90, 60),
            -1,
        )

        for bird in self._birds:
            x, y, moving = bird.position(t)
            half = bird.size // 2
            colour = (40, 40, 45) if not moving else (55, 55, 65)
            cv2.ellipse(image, (x, y), (half, int(half * 0.7)), 0, 0, 360, colour, -1)
            cv2.circle(image, (x + half - 2, y - half // 2), max(3, half // 3), colour, -1)

        cv2.putText(
            image,
            f"SIMULATED SOURCE  t={t:6.1f}s",
            (12, 26),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        return image

    def _run(self) -> None:
        interval = 1.0 / self.fps
        self.status.connected = True
        next_tick = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            image = self._render(now - self._t0)
            self._seq += 1
            self.buffer.publish(
                Frame(
                    image=image,
                    seq=self._seq,
                    ts=now,
                    wall_ts=time.time(),
                    camera_id=self.camera_id,
                )
            )
            self.status.frames = self._seq
            self.status.fps = self.buffer.fps()
            next_tick += interval
            self._stop.wait(max(0.0, next_tick - time.monotonic()))
        self.status.connected = False
