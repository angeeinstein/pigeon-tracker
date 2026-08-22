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
from app.vision.scene_motion import MotionRegion, SceneMotionDetector
from app.vision.tracker import ByteTracker, Track, create_tracker

log = get_logger(__name__)

ResultListener = Callable[["VisionResult"], Awaitable[None]]


@dataclass
class MotionEvidence:
    """Native source frame and model boxes produced by a motion crop rescan."""

    image: np.ndarray = field(repr=False)
    detections: list[Detection] = field(default_factory=list)
    regions: list[MotionRegion] = field(default_factory=list)
    class_name: str = "motion"
    confidence: float | None = None
    rescan_ms: float = 0.0


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
    #: Optional native-resolution evidence from motion-guided crop inference.
    #: It is never supplied to the tracker or target selector.
    motion_evidence: MotionEvidence | None = field(default=None, repr=False)

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
            "motion_rescans": len(self.motion_evidence.regions) if self.motion_evidence else 0,
            "motion_rescan_ms": (
                round(self.motion_evidence.rescan_ms, 1) if self.motion_evidence else 0.0
            ),
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
        self._scene_motion = SceneMotionDetector(settings.scene_motion)
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
        if "scene_motion" in changed:
            self._scene_motion.update_settings(settings.scene_motion)
        if "cameras" in changed:
            # Never carry detections or track history across a primary-camera
            # switch or source restart.
            self._invalidate_result()
            self._last_frame_seq = -1
            self._scene_motion.reset()

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
            "scene_motion": self._scene_motion.status(),
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

        motion_evidence: MotionEvidence | None = None
        if self._settings.scene_motion.enabled:
            analysis = await asyncio.to_thread(self._scene_motion.update, frame.image, frame.ts)
            unexplained = [
                region
                for region in analysis.regions
                if not _motion_region_has_detection(
                    region,
                    proposals,
                    self._settings.scene_motion.rescan_classes,
                    min_confidence=self._settings.scene_motion.rescan_confidence,
                )
            ]
            due = self._scene_motion.claim_rescans(unexplained, frame.ts)
            if due:
                motion_evidence = await self._rescan_motion(detector, frame, due)

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
            motion_evidence=motion_evidence,
        )
        self._latest = result
        self._ticks += 1
        self._error = detector.status.error

        for listener in list(self._listeners):
            try:
                await listener(result)
            except Exception:
                log.exception("vision listener failed")

    async def _rescan_motion(
        self,
        detector: Detector,
        frame: Any,
        regions: list[MotionRegion],
    ) -> MotionEvidence | None:
        """Run evidence-only inference on native crops around motion regions."""
        cfg = self._settings.scene_motion
        native = frame.native
        wanted = {name.casefold() for name in cfg.rescan_classes}
        evidence_boxes: list[Detection] = []
        best_confidence: float | None = None
        best_class_name = ""
        elapsed_ms = 0.0

        crop_bounds = _merge_crop_bounds(
            [
                _native_crop_bounds(
                    region,
                    display_width=frame.width,
                    display_height=frame.height,
                    native_width=int(native.shape[1]),
                    native_height=int(native.shape[0]),
                    padding_ratio=cfg.crop_padding_ratio,
                    min_width_ratio=cfg.min_crop_width_ratio,
                )
                for region in regions
            ]
        )
        for x1, y1, x2, y2 in crop_bounds:
            crop = native[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            started = time.perf_counter()
            found = await asyncio.to_thread(
                detector.infer,
                crop,
                min_confidence=cfg.rescan_confidence,
            )
            elapsed_ms += (time.perf_counter() - started) * 1000.0
            if wanted:
                found = [item for item in found if item.class_name.casefold() in wanted]

            if found:
                self._scene_motion.note_detection(len(found))
                for item in found:
                    mapped = Detection(
                        x1=item.x1 + x1,
                        y1=item.y1 + y1,
                        x2=item.x2 + x1,
                        y2=item.y2 + y1,
                        confidence=item.confidence,
                        class_id=item.class_id,
                        class_name=item.class_name,
                        source="motion_rescan",
                    )
                    evidence_boxes.append(mapped)
                    if best_confidence is None or item.confidence > best_confidence:
                        best_confidence = item.confidence
                        best_class_name = item.class_name

        if not evidence_boxes:
            return None
        # Ultralytics applies NMS inside each crop. Overlapping motion crops
        # still need one final pass after their boxes share native coordinates.
        evidence_boxes = _deduplicate_detections(
            evidence_boxes, self._settings.detector.iou
        )[: self._settings.detector.max_detections]
        return MotionEvidence(
            image=native,
            detections=evidence_boxes,
            regions=regions,
            class_name=best_class_name,
            confidence=best_confidence,
            rescan_ms=elapsed_ms,
        )

    def motion_mask(self) -> np.ndarray | None:
        """Latest monochrome foreground mask for the optional web preview."""
        return self._scene_motion.mask_image()


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


def _detection_iou(left: Detection, right: Detection) -> float:
    x1 = max(left.x1, right.x1)
    y1 = max(left.y1, right.y1)
    x2 = min(left.x2, right.x2)
    y2 = min(left.y2, right.y2)
    overlap = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = left.area + right.area - overlap
    return overlap / union if union > 0.0 else 0.0


def _deduplicate_detections(
    detections: list[Detection], iou_threshold: float
) -> list[Detection]:
    """Class-aware NMS for boxes produced by separate motion crops."""
    kept: list[Detection] = []
    for candidate in sorted(detections, key=lambda item: item.confidence, reverse=True):
        duplicate = any(
            candidate.class_name.casefold() == existing.class_name.casefold()
            and _detection_iou(candidate, existing) >= iou_threshold
            for existing in kept
        )
        if not duplicate:
            kept.append(candidate)
    return kept


def _merge_crop_bounds(
    bounds: list[tuple[int, int, int, int]],
    overlap_threshold: float = 0.5,
) -> list[tuple[int, int, int, int]]:
    """Merge motion crops that substantially cover the same native pixels.

    Motion components can fragment one moving bird into several nearby regions.
    Padding those regions often creates nearly identical model inputs. Merging
    only when at least half of the smaller crop overlaps avoids redundant
    inference without combining separate objects that merely sit close by.
    """
    merged: list[tuple[int, int, int, int]] = []
    for candidate in bounds:
        current = candidate
        index = 0
        while index < len(merged):
            existing = merged[index]
            x1 = max(current[0], existing[0])
            y1 = max(current[1], existing[1])
            x2 = min(current[2], existing[2])
            y2 = min(current[3], existing[3])
            intersection = max(0, x2 - x1) * max(0, y2 - y1)
            current_area = max(1, current[2] - current[0]) * max(
                1, current[3] - current[1]
            )
            existing_area = max(1, existing[2] - existing[0]) * max(
                1, existing[3] - existing[1]
            )
            if intersection / min(current_area, existing_area) >= overlap_threshold:
                current = (
                    min(current[0], existing[0]),
                    min(current[1], existing[1]),
                    max(current[2], existing[2]),
                    max(current[3], existing[3]),
                )
                merged.pop(index)
                index = 0
                continue
            index += 1
        merged.append(current)
    return merged


def _motion_region_has_detection(
    region: MotionRegion,
    detections: list[Detection],
    class_names: list[str],
    *,
    min_confidence: float = 0.0,
) -> bool:
    wanted = {name.casefold() for name in class_names}
    region_area = max(1.0, region.width * region.height)
    for detection in detections:
        # A weak full-frame proposal is exactly where the native crop can add
        # useful resolution. Do not let it suppress the guided second pass.
        if detection.confidence < min_confidence:
            continue
        if wanted and detection.class_name.casefold() not in wanted:
            continue
        x1 = max(region.x1, detection.x1)
        y1 = max(region.y1, detection.y1)
        x2 = min(region.x2, detection.x2)
        y2 = min(region.y2, detection.y2)
        overlap = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        if overlap / region_area >= 0.15:
            return True
        cx, cy = region.center
        if detection.x1 <= cx <= detection.x2 and detection.y1 <= cy <= detection.y2:
            return True
    return False


def _map_region_to_native(
    region: MotionRegion,
    *,
    display_width: int,
    display_height: int,
    native_width: int,
    native_height: int,
) -> tuple[float, float, float, float]:
    sx = native_width / float(max(1, display_width))
    sy = native_height / float(max(1, display_height))
    return (region.x1 * sx, region.y1 * sy, region.x2 * sx, region.y2 * sy)


def _native_crop_bounds(
    region: MotionRegion,
    *,
    display_width: int,
    display_height: int,
    native_width: int,
    native_height: int,
    padding_ratio: float,
    min_width_ratio: float,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = _map_region_to_native(
        region,
        display_width=display_width,
        display_height=display_height,
        native_width=native_width,
        native_height=native_height,
    )
    width = max(1.0, x2 - x1)
    height = max(1.0, y2 - y1)
    center_x = (x1 + x2) / 2.0
    center_y = (y1 + y2) / 2.0
    wanted_width = max(width * (1.0 + 2.0 * padding_ratio), native_width * min_width_ratio)
    wanted_height = max(
        height * (1.0 + 2.0 * padding_ratio),
        wanted_width * native_height / float(max(1, native_width)),
    )
    wanted_width = min(float(native_width), wanted_width)
    wanted_height = min(float(native_height), wanted_height)
    left = min(max(0.0, center_x - wanted_width / 2.0), native_width - wanted_width)
    top = min(max(0.0, center_y - wanted_height / 2.0), native_height - wanted_height)
    return (
        int(left),
        int(top),
        max(int(left) + 1, min(native_width, round(left + wanted_width))),
        max(int(top) + 1, min(native_height, round(top + wanted_height))),
    )
