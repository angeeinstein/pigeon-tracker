"""Vision pipeline: newest frame -> detector -> tracker -> result.

Runs as one asyncio task ticking at the configured detector rate. Inference
happens in a worker thread (``asyncio.to_thread``) so a 300 ms model never
stalls telemetry, the web UI, or the controller link.

If the detector cannot keep up with its configured rate the loop simply runs
slower — it always grabs the *current* frame, so it degrades into "as fast as
possible on fresh data" rather than falling behind on stale data.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.camera.manager import CameraManager
from app.logging_config import get_logger
from app.services.settings_schema import AppSettings
from app.vision.detector import Detection, Detector, create_detector
from app.vision.tracker import ByteTracker, Track, create_tracker

log = get_logger(__name__)

ResultListener = Callable[["VisionResult"], Awaitable[None]]


@dataclass
class VisionResult:
    """Output of one pipeline tick."""

    camera_id: str
    frame_seq: int
    #: ``time.monotonic()`` of the frame this result came from.
    frame_ts: float
    wall_ts: float
    frame_width: int
    frame_height: int
    detections: list[Detection] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    inference_ms: float = 0.0
    total_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "frame_seq": self.frame_seq,
            "wall_ts": self.wall_ts,
            "width": self.frame_width,
            "height": self.frame_height,
            "detections": len(self.detections),
            "tracks": [t.as_dict() for t in self.tracks],
            "inference_ms": round(self.inference_ms, 1),
            "total_ms": round(self.total_ms, 1),
        }


class VisionPipeline:
    def __init__(
        self,
        cameras: CameraManager,
        settings: AppSettings,
        models_dir: Path,
        *,
        force_mock: bool = False,
    ) -> None:
        self._cameras = cameras
        self._settings = settings
        self._models_dir = models_dir
        self._force_mock = force_mock

        self._detector: Detector | None = None
        self._tracker: ByteTracker = create_tracker(settings.tracker)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._reload = asyncio.Event()
        self._listeners: list[ResultListener] = []

        self._latest: VisionResult | None = None
        self._last_frame_seq = -1
        self._error: str | None = None
        self._ticks = 0
        self._effective_fps = 0.0

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="vision-pipeline")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._detector is not None:
            await asyncio.to_thread(self._detector.close)
            self._detector = None

    async def apply_settings(self, settings: AppSettings, changed: set[str]) -> None:
        self._settings = settings
        if "detector" in changed:
            self._reload.set()
        if "tracker" in changed:
            self._tracker = create_tracker(settings.tracker)

    def subscribe(self, listener: ResultListener) -> None:
        self._listeners.append(listener)

    # -- access ----------------------------------------------------------
    @property
    def latest(self) -> VisionResult | None:
        return self._latest

    @property
    def tracks(self) -> list[Track]:
        return list(self._latest.tracks) if self._latest else []

    def status(self) -> dict[str, Any]:
        detector_status = (
            self._detector.status.as_dict()
            if self._detector
            else {
                "backend": "none",
                "loaded": False,
            }
        )
        result = self._latest
        return {
            "enabled": self._settings.detector.enabled,
            "running": self._task is not None and not self._task.done(),
            "detector": detector_status,
            "tracker": {
                "enabled": self._settings.tracker.enabled,
                "algorithm": self._settings.tracker.algorithm,
                "active_tracks": len(result.tracks) if result else 0,
            },
            "target_fps": self._settings.detector.fps,
            "effective_fps": round(self._effective_fps, 2),
            "ticks": self._ticks,
            "error": self._error,
            "last_result_age_s": (round(time.monotonic() - result.frame_ts, 2) if result else None),
        }

    @property
    def healthy(self) -> bool:
        if not self._settings.detector.enabled:
            return True
        return self._detector is not None and self._detector.status.loaded and self._error is None

    # -- worker ----------------------------------------------------------
    async def _ensure_detector(self) -> None:
        if self._detector is not None and not self._reload.is_set():
            return
        self._reload.clear()
        if self._detector is not None:
            await asyncio.to_thread(self._detector.close)
            self._detector = None

        detector = create_detector(
            self._settings.detector, self._models_dir, force_mock=self._force_mock
        )
        try:
            await asyncio.to_thread(detector.load)
            self._detector = detector
            self._error = detector.status.error
        except Exception as exc:
            self._error = f"detector load failed: {exc}"
            self._detector = detector  # keep it so the status carries the error
            log.error("detector unavailable", extra={"ctx": {"error": str(exc)}})

    async def _run(self) -> None:
        log.info("vision pipeline started")
        recent: list[float] = []
        while not self._stop.is_set():
            settings = self._settings.detector
            interval = 1.0 / max(0.2, settings.fps)
            started = time.monotonic()

            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = str(exc)
                log.exception("vision tick failed")
                await asyncio.sleep(1.0)
                continue

            now = time.monotonic()
            recent.append(now)
            if len(recent) > 20:
                del recent[: len(recent) - 20]
            if len(recent) > 1 and recent[-1] > recent[0]:
                self._effective_fps = (len(recent) - 1) / (recent[-1] - recent[0])

            await asyncio.sleep(max(0.0, interval - (now - started)))
        log.info("vision pipeline stopped")

    async def _tick(self) -> None:
        if not self._settings.detector.enabled:
            self._latest = None
            await asyncio.sleep(0.2)
            return

        await self._ensure_detector()
        detector = self._detector
        if detector is None or not detector.status.loaded:
            await asyncio.sleep(0.5)
            return

        camera_id = self._settings.cameras.primary_id
        frame = self._cameras.latest(camera_id)
        if frame is None or frame.seq == self._last_frame_seq:
            # No camera, or no new frame since last tick: nothing to do.
            await asyncio.sleep(0.02)
            return
        self._last_frame_seq = frame.seq

        tick_started = time.perf_counter()
        detections = await asyncio.to_thread(detector.infer, frame.image)
        inference_ms = detector.status.last_inference_ms

        if self._settings.tracker.enabled:
            tracks = self._tracker.update(detections, now=frame.wall_ts)
        else:
            tracks = _tracks_from_detections(detections, frame.wall_ts)

        result = VisionResult(
            camera_id=frame.camera_id,
            frame_seq=frame.seq,
            frame_ts=frame.ts,
            wall_ts=frame.wall_ts,
            frame_width=frame.width,
            frame_height=frame.height,
            detections=detections,
            tracks=tracks,
            inference_ms=inference_ms,
            total_ms=(time.perf_counter() - tick_started) * 1000.0,
        )
        self._latest = result
        self._ticks += 1
        self._error = detector.status.error

        for listener in list(self._listeners):
            try:
                await listener(result)
            except Exception:
                log.exception("vision listener failed")


def _tracks_from_detections(detections: list[Detection], now: float) -> list[Track]:
    """Detections as pseudo-tracks when tracking is disabled.

    Ids are per-frame indices, so they are *not* persistent — the targeting
    logic treats an unconfirmed track as ineligible, which keeps automatic mode
    from engaging when tracking is off.
    """
    return [
        Track(
            track_id=-(index + 1),
            x1=d.x1,
            y1=d.y1,
            x2=d.x2,
            y2=d.y2,
            confidence=d.confidence,
            class_name=d.class_name,
            class_id=d.class_id,
            hits=1,
            age_frames=0,
            first_seen=now,
            last_seen=now,
            lost=False,
            confirmed=False,
        )
        for index, d in enumerate(detections)
    ]
