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

import numpy as np

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
    #: Low-confidence proposals retained for evidence collection.
    proposals: list[Detection] = field(default_factory=list)
    #: Proposals above the operational threshold, used by the tracker.
    detections: list[Detection] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    inference_ms: float = 0.0
    total_ms: float = 0.0
    #: The exact source frame. Kept out of serialised payloads and used by
    #: evidence listeners immediately after inference.
    image: np.ndarray | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "frame_seq": self.frame_seq,
            "wall_ts": self.wall_ts,
            "width": self.frame_width,
            "height": self.frame_height,
            "proposals": len(self.proposals),
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
        self._reloading = False
        self._reload_error: str | None = None
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
            self._reload_error = None
            self._reload.set()
        if "tracker" in changed:
            self._tracker = create_tracker(settings.tracker)
        if "cameras" in changed:
            # Never carry detections or track history across a primary-camera
            # switch or source restart.
            self._invalidate_result()
            self._last_frame_seq = -1

    def subscribe(self, listener: ResultListener) -> None:
        self._listeners.append(listener)

    def _invalidate_result(self) -> None:
        """Discard detections and tracking history after an input interruption."""
        if self._latest is None:
            return
        self._latest = None
        self._tracker.reset()

    # -- access ----------------------------------------------------------
    @property
    def latest(self) -> VisionResult | None:
        result = self._latest
        if result is None:
            return None

        # A vision result is only actionable while it still represents the
        # current, connected primary camera.  Camera buffers intentionally keep
        # their most recent frame, so without this check a frozen stream would
        # otherwise leave its last detections visible to the targeting loop
        # indefinitely.
        if result.camera_id != self._settings.cameras.primary_id:
            return None
        camera = self._settings.cameras.get(result.camera_id)
        source = self._cameras.get(result.camera_id)
        if camera is None or source is None or not source.status.connected:
            return None
        if time.monotonic() - result.frame_ts > camera.stall_timeout_s:
            return None
        return result

    @property
    def tracks(self) -> list[Track]:
        result = self.latest
        return list(result.tracks) if result else []

    def status(self) -> dict[str, Any]:
        detector_status = (
            self._detector.status.as_dict()
            if self._detector
            else {
                "backend": "none",
                "loaded": False,
            }
        )
        raw_result = self._latest
        result = self.latest
        active_settings = self._detector.settings if self._detector is not None else None
        catalog_current = bool(
            self._detector
            and self._detector.status.loaded
            and active_settings
            and active_settings.backend == self._settings.detector.backend
            and active_settings.model_path == self._settings.detector.model_path
        )
        detector_status.update(
            {
                "configured_backend": self._settings.detector.backend,
                "configured_model": self._settings.detector.model_path,
                "active_backend": active_settings.backend if active_settings else None,
                "active_model": active_settings.model_path if active_settings else None,
                "catalog_current": catalog_current,
                "reload_pending": self._reload.is_set() or self._reloading,
                "reload_error": self._reload_error,
            }
        )
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
            "last_result_age_s": (
                round(time.monotonic() - raw_result.frame_ts, 2) if raw_result else None
            ),
            "result_stale": raw_result is not None and result is None,
        }

    @property
    def healthy(self) -> bool:
        if not self._settings.detector.enabled:
            return True
        return (
            self._detector is not None
            and self._detector.status.loaded
            and self._error is None
            and self._reload_error is None
        )

    # -- worker ----------------------------------------------------------
    async def _ensure_detector(self) -> None:
        if self._detector is not None and not self._reload.is_set():
            return
        self._reload.clear()
        candidate_settings = self._settings.detector.model_copy(deep=True)
        candidate = create_detector(
            candidate_settings, self._models_dir, force_mock=self._force_mock
        )
        self._reloading = True
        try:
            await asyncio.to_thread(candidate.load)
        except Exception as exc:
            if candidate_settings != self._settings.detector:
                await asyncio.to_thread(candidate.close)
                self._reload.set()
                return
            message = f"detector load failed: {exc}"
            self._reload_error = message
            log.error("detector replacement unavailable", extra={"ctx": {"error": str(exc)}})

            # With no working detector, retain the failed candidate so its
            # detailed status remains visible. When replacing a working model,
            # keep that model alive and only report the failed replacement.
            if self._detector is None or not self._detector.status.loaded:
                previous = self._detector
                self._detector = candidate
                self._error = message
                if previous is not None:
                    await asyncio.to_thread(previous.close)
            else:
                await asyncio.to_thread(candidate.close)
            return
        finally:
            self._reloading = False

        # Settings may have changed again while a large model was loading.
        # Never activate a candidate for a configuration that is already stale.
        if candidate_settings != self._settings.detector:
            await asyncio.to_thread(candidate.close)
            self._reload.set()
            return

        previous = self._detector
        self._detector = candidate
        self._error = candidate.status.error
        self._reload_error = None
        if previous is not None:
            await asyncio.to_thread(previous.close)

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
                self._invalidate_result()
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
            self._invalidate_result()
            await asyncio.sleep(0.2)
            return

        await self._ensure_detector()
        detector = self._detector
        if detector is None or not detector.status.loaded:
            self._invalidate_result()
            await asyncio.sleep(0.5)
            return

        camera_id = self._settings.cameras.primary_id
        frame = self._cameras.latest(camera_id)
        if frame is None or frame.seq == self._last_frame_seq:
            # No camera, or no new frame since last tick: nothing to do.
            if self._latest is not None and self.latest is None:
                self._invalidate_result()
            await asyncio.sleep(0.02)
            return
        self._last_frame_seq = frame.seq

        tick_started = time.perf_counter()
        raw_detections = await asyncio.to_thread(detector.infer, frame.image)
        proposals = detector.capturable(raw_detections)
        detections = detector.operational(raw_detections)
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
            proposals=proposals,
            detections=detections,
            tracks=tracks,
            inference_ms=inference_ms,
            total_ms=(time.perf_counter() - tick_started) * 1000.0,
            image=frame.image,
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
