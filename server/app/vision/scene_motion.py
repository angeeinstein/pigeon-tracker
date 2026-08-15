"""Adaptive foreground masks and bounded motion-event tracking.

The detector in this module does not assign semantic classes. It only answers
"which pixels differ from the learned static scene?" and turns connected
foreground components into regions that can request a higher-resolution model
rescan. Slow background changes are absorbed by MOG2; global changes and sparse
compression noise are explicitly rejected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np

from app.services.settings_schema import SceneMotionSettings


@dataclass(frozen=True)
class MotionRegion:
    x1: float
    y1: float
    x2: float
    y2: float
    area_ratio: float
    fill_ratio: float
    speed_ratio_s: float
    score: float
    event_id: int

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    def as_dict(self) -> dict[str, object]:
        return {
            "bbox": [round(self.x1, 1), round(self.y1, 1), round(self.x2, 1), round(self.y2, 1)],
            "area_ratio": round(self.area_ratio, 6),
            "fill_ratio": round(self.fill_ratio, 3),
            "speed_ratio_s": round(self.speed_ratio_s, 4),
            "score": round(self.score, 3),
            "event_id": self.event_id,
        }


@dataclass(frozen=True)
class MotionAnalysis:
    regions: tuple[MotionRegion, ...]
    changed_ratio: float
    warming_up: bool = False
    global_change: bool = False


@dataclass
class _Event:
    event_id: int
    bbox: tuple[float, float, float, float]
    center: tuple[float, float]
    first_seen: float
    last_seen: float
    frames: int = 1
    rescans: int = 0
    last_rescan: float = float("-inf")


class SceneMotionDetector:
    """MOG2 foreground mask plus per-region rearm/rate limiting."""

    def __init__(self, settings: SceneMotionSettings) -> None:
        self._settings = settings
        self._subtractor: cv2.BackgroundSubtractor | None = None
        self._started_at: float | None = None
        self._previous_regions: list[
            tuple[tuple[float, float, float, float], tuple[float, float]]
        ] = []
        self._previous_ts: float | None = None
        self._events: dict[int, _Event] = {}
        self._next_event_id = 1
        self._mask: np.ndarray | None = None
        self._analysis = MotionAnalysis((), 0.0, warming_up=True)
        self._frames = 0
        self._rescans = 0
        self._detections = 0
        self._last_error: str | None = None
        self._create_subtractor()

    def _create_subtractor(self) -> None:
        cfg = self._settings
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=cfg.history_frames,
            varThreshold=cfg.variance_threshold,
            detectShadows=cfg.detect_shadows,
        )

    def update_settings(self, settings: SceneMotionSettings) -> None:
        if settings == self._settings:
            return
        self._settings = settings
        self.reset()

    def reset(self) -> None:
        self._started_at = None
        self._previous_regions.clear()
        self._previous_ts = None
        self._events.clear()
        self._next_event_id = 1
        self._mask = None
        self._analysis = MotionAnalysis((), 0.0, warming_up=True)
        self._last_error = None
        self._create_subtractor()

    def update(self, image: np.ndarray, now: float | None = None) -> MotionAnalysis:
        now = time.monotonic() if now is None else now
        cfg = self._settings
        if not cfg.enabled:
            self._analysis = MotionAnalysis((), 0.0)
            return self._analysis
        if self._started_at is None:
            self._started_at = now

        try:
            height, width = image.shape[:2]
            scale = min(1.0, cfg.processing_width / float(max(1, width)))
            work = image
            if scale < 1.0:
                work = cv2.resize(
                    image,
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            assert self._subtractor is not None
            raw = self._subtractor.apply(gray)
            # With shadow detection enabled, MOG2 uses 127 for shadows and 255
            # for definite foreground. Keep only the latter.
            _, mask = cv2.threshold(raw, 200, 255, cv2.THRESH_BINARY)
            kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_open)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_close)
            self._mask = mask
            self._frames += 1

            changed_ratio = float(np.count_nonzero(mask)) / float(max(1, mask.size))
            warming = now - self._started_at < cfg.warmup_s
            global_change = changed_ratio >= cfg.max_frame_change_ratio
            if warming or global_change:
                self._expire_events(now)
                self._analysis = MotionAnalysis(
                    (), changed_ratio, warming_up=warming, global_change=global_change
                )
                self._previous_regions.clear()
                self._previous_ts = now
                self._last_error = None
                return self._analysis

            regions = self._extract_regions(mask, width, height, now)
            regions = self._assign_events(regions, now)
            self._analysis = MotionAnalysis(tuple(regions), changed_ratio)
            self._last_error = None
            return self._analysis
        except Exception as exc:
            self._last_error = str(exc)
            self._analysis = MotionAnalysis((), 0.0)
            return self._analysis

    def _extract_regions(
        self, mask: np.ndarray, output_width: int, output_height: int, now: float
    ) -> list[MotionRegion]:
        cfg = self._settings
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask_height, mask_width = mask.shape[:2]
        mask_area = float(max(1, mask_width * mask_height))
        sx = output_width / float(max(1, mask_width))
        sy = output_height / float(max(1, mask_height))
        dt = max(1e-3, now - self._previous_ts) if self._previous_ts is not None else 0.0
        candidates: list[MotionRegion] = []
        next_previous: list[tuple[tuple[float, float, float, float], tuple[float, float]]] = []

        for contour in contours:
            area = float(cv2.contourArea(contour))
            area_ratio = area / mask_area
            if area_ratio < cfg.min_area_ratio or area_ratio > cfg.max_area_ratio:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            fill = area / float(max(1, w * h))
            if fill < cfg.min_fill_ratio:
                continue
            bbox = (x * sx, y * sy, (x + w) * sx, (y + h) * sy)
            center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
            speed = self._estimate_speed(center, output_width, dt)
            if cfg.min_speed_ratio_s and speed < cfg.min_speed_ratio_s:
                continue
            if cfg.max_speed_ratio_s and speed > cfg.max_speed_ratio_s:
                continue
            score = min(1.0, area_ratio / max(cfg.min_area_ratio * 20.0, 1e-9))
            candidates.append(
                MotionRegion(
                    *bbox,
                    area_ratio=area_ratio,
                    fill_ratio=fill,
                    speed_ratio_s=speed,
                    score=score,
                    event_id=0,
                )
            )
            next_previous.append((bbox, center))

        self._previous_regions = next_previous
        self._previous_ts = now
        candidates.sort(key=lambda item: item.area_ratio, reverse=True)
        return candidates[: cfg.max_regions]

    def _estimate_speed(self, center: tuple[float, float], width: int, dt: float) -> float:
        if not self._previous_regions or dt <= 0.0:
            return 0.0
        previous = min(
            self._previous_regions,
            key=lambda item: (item[1][0] - center[0]) ** 2 + (item[1][1] - center[1]) ** 2,
        )[1]
        distance = ((previous[0] - center[0]) ** 2 + (previous[1] - center[1]) ** 2) ** 0.5
        return distance / float(max(1, width)) / dt

    def _assign_events(self, regions: list[MotionRegion], now: float) -> list[MotionRegion]:
        self._expire_events(now)
        assigned: list[MotionRegion] = []
        used: set[int] = set()
        for region in regions:
            event = self._best_event(region, used)
            if event is None:
                event = _Event(
                    event_id=self._next_event_id,
                    bbox=(region.x1, region.y1, region.x2, region.y2),
                    center=region.center,
                    first_seen=now,
                    last_seen=now,
                )
                self._events[event.event_id] = event
                self._next_event_id += 1
            else:
                event.bbox = (region.x1, region.y1, region.x2, region.y2)
                event.center = region.center
                event.last_seen = now
                event.frames += 1
            used.add(event.event_id)
            assigned.append(
                MotionRegion(
                    x1=region.x1,
                    y1=region.y1,
                    x2=region.x2,
                    y2=region.y2,
                    area_ratio=region.area_ratio,
                    fill_ratio=region.fill_ratio,
                    speed_ratio_s=region.speed_ratio_s,
                    score=region.score,
                    event_id=event.event_id,
                )
            )
        return assigned

    def _best_event(self, region: MotionRegion, used: set[int]) -> _Event | None:
        best: tuple[float, _Event] | None = None
        for event in self._events.values():
            if event.event_id in used:
                continue
            overlap = _intersection_over_union(
                (region.x1, region.y1, region.x2, region.y2), event.bbox
            )
            ew = max(1.0, event.bbox[2] - event.bbox[0])
            eh = max(1.0, event.bbox[3] - event.bbox[1])
            distance = (
                (region.center[0] - event.center[0]) ** 2
                + (region.center[1] - event.center[1]) ** 2
            ) ** 0.5
            proximity = 1.0 - min(1.0, distance / max(ew, eh, region.width, region.height))
            match = max(overlap, proximity * 0.5)
            if match < 0.15:
                continue
            if best is None or match > best[0]:
                best = (match, event)
        return best[1] if best else None

    def _expire_events(self, now: float) -> None:
        quiet = self._settings.event_rearm_s
        self._events = {
            event_id: event
            for event_id, event in self._events.items()
            if now - event.last_seen <= quiet
        }

    def claim_rescans(self, regions: list[MotionRegion], now: float) -> list[MotionRegion]:
        """Rate-limit and claim regions that should receive model crop inference."""
        cfg = self._settings
        due: list[MotionRegion] = []
        for region in regions:
            event = self._events.get(region.event_id)
            if event is None or event.frames < cfg.min_persistence_frames:
                continue
            if event.rescans >= cfg.max_rescans_per_event:
                continue
            if now - event.last_rescan < cfg.rescan_interval_s:
                continue
            event.last_rescan = now
            event.rescans += 1
            self._rescans += 1
            due.append(region)
        return due

    def note_detection(self, count: int) -> None:
        self._detections += max(0, count)

    def mask_image(self) -> np.ndarray | None:
        return self._mask.copy() if self._mask is not None else None

    def status(self) -> dict[str, object]:
        analysis = self._analysis
        return {
            "enabled": self._settings.enabled,
            "warming_up": analysis.warming_up,
            "global_change": analysis.global_change,
            "changed_ratio": round(analysis.changed_ratio, 5),
            "regions": [region.as_dict() for region in analysis.regions],
            "active_events": len(self._events),
            "frames": self._frames,
            "rescans": self._rescans,
            "rescan_detections": self._detections,
            "error": self._last_error,
        }


def _intersection_over_union(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if intersection <= 0.0:
        return 0.0
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    return intersection / max(1e-9, left_area + right_area - intersection)
