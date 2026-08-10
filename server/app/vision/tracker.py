"""Multi-object tracking (ByteTrack).

ByteTrack's idea in one line: associate high-confidence detections first, then
give the *low*-confidence leftovers a second chance to match tracks that are
still unmatched. Birds that briefly turn side-on or get partly occluded keep
their id instead of being re-numbered — which is what makes "this bird has been
sitting there for 3 seconds" a meaningful statement for the state machine.

Implemented here rather than pulled in from a tracking package: it is ~200
lines, it removes a heavy dependency, and it keeps the track lifetime semantics
(``first_seen`` / ``hits`` / ``lost``) under our control and unit-testable.

Optimal assignment uses SciPy when available and falls back to a greedy matcher
otherwise; with the handful of objects in this scene the two agree in practice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from app.services.settings_schema import TrackerSettings
from app.vision.detector import Detection

try:  # pragma: no cover - exercised implicitly by whichever branch is installed
    from scipy.optimize import linear_sum_assignment as _scipy_lsa

    _HAS_SCIPY = True
except Exception:  # pragma: no cover
    _HAS_SCIPY = False


# --------------------------------------------------------------------------
# Kalman filter (constant velocity on centre-x, centre-y, aspect, height)
# --------------------------------------------------------------------------


class KalmanFilter:
    """8-state constant-velocity filter over ``(x, y, aspect, height)``."""

    def __init__(self) -> None:
        ndim, dt = 4, 1.0
        self._motion_mat = np.eye(2 * ndim, 2 * ndim)
        for i in range(ndim):
            self._motion_mat[i, ndim + i] = dt
        self._update_mat = np.eye(ndim, 2 * ndim)
        # Uncertainty is proportional to object size: a 20 px bird may move a
        # pixel, a 200 px one may move ten.
        self._std_weight_position = 1.0 / 20
        self._std_weight_velocity = 1.0 / 160

    def initiate(self, measurement: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mean = np.r_[measurement, np.zeros(4)]
        h = measurement[3]
        std = [
            2 * self._std_weight_position * h,
            2 * self._std_weight_position * h,
            1e-2,
            2 * self._std_weight_position * h,
            10 * self._std_weight_velocity * h,
            10 * self._std_weight_velocity * h,
            1e-5,
            10 * self._std_weight_velocity * h,
        ]
        return mean, np.diag(np.square(std))

    def predict(self, mean: np.ndarray, covariance: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = mean[3]
        std = [
            self._std_weight_position * h,
            self._std_weight_position * h,
            1e-2,
            self._std_weight_position * h,
            self._std_weight_velocity * h,
            self._std_weight_velocity * h,
            1e-5,
            self._std_weight_velocity * h,
        ]
        motion_cov = np.diag(np.square(std))
        mean = self._motion_mat @ mean
        covariance = self._motion_mat @ covariance @ self._motion_mat.T + motion_cov
        return mean, covariance

    def project(self, mean: np.ndarray, covariance: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = mean[3]
        std = [
            self._std_weight_position * h,
            self._std_weight_position * h,
            1e-1,
            self._std_weight_position * h,
        ]
        innovation_cov = np.diag(np.square(std))
        projected_mean = self._update_mat @ mean
        projected_cov = self._update_mat @ covariance @ self._update_mat.T + innovation_cov
        return projected_mean, projected_cov

    def update(
        self, mean: np.ndarray, covariance: np.ndarray, measurement: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        projected_mean, projected_cov = self.project(mean, covariance)
        kalman_gain = covariance @ self._update_mat.T @ np.linalg.inv(projected_cov)
        innovation = measurement - projected_mean
        new_mean = mean + kalman_gain @ innovation
        new_covariance = covariance - kalman_gain @ projected_cov @ kalman_gain.T
        return new_mean, new_covariance


# --------------------------------------------------------------------------
# Association helpers
# --------------------------------------------------------------------------


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU between two sets of ``[x1, y1, x2, y2]`` boxes."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    area_a = np.maximum(0.0, a[:, 2] - a[:, 0]) * np.maximum(0.0, a[:, 3] - a[:, 1])
    area_b = np.maximum(0.0, b[:, 2] - b[:, 0]) * np.maximum(0.0, b[:, 3] - b[:, 1])

    lt = np.maximum(a[:, None, :2], b[None, :, :2])
    rb = np.minimum(a[:, None, 2:], b[None, :, 2:])
    wh = np.clip(rb - lt, 0.0, None)
    inter = wh[:, :, 0] * wh[:, :, 1]
    union = area_a[:, None] + area_b[None, :] - inter
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(union > 0, inter / union, 0.0).astype(np.float32)


def linear_assignment(
    cost: np.ndarray, threshold: float
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Match rows to columns while cost <= threshold.

    Returns ``(matches, unmatched_rows, unmatched_cols)``.
    """
    rows, cols = cost.shape
    if rows == 0 or cols == 0:
        return [], list(range(rows)), list(range(cols))

    if _HAS_SCIPY:
        row_idx, col_idx = _scipy_lsa(cost)
        pairs = list(zip(row_idx.tolist(), col_idx.tolist(), strict=True))
    else:
        # Greedy: repeatedly take the cheapest remaining pair.
        pairs = []
        work = cost.copy()
        for _ in range(min(rows, cols)):
            r, c = np.unravel_index(np.argmin(work), work.shape)
            if not np.isfinite(work[r, c]):
                break
            pairs.append((int(r), int(c)))
            work[r, :] = np.inf
            work[:, c] = np.inf

    matches = [(r, c) for r, c in pairs if cost[r, c] <= threshold]
    matched_rows = {r for r, _ in matches}
    matched_cols = {c for _, c in matches}
    return (
        matches,
        [r for r in range(rows) if r not in matched_rows],
        [c for c in range(cols) if c not in matched_cols],
    )


# --------------------------------------------------------------------------
# Tracks
# --------------------------------------------------------------------------


class TrackState(str, Enum):
    #: Matched in the current frame. A brand-new track starts here too - being
    #: *tracked* and being *confirmed* are separate questions, and confirmation
    #: is decided by `hits >= min_hits`.
    TRACKED = "tracked"
    #: Not matched recently; kept around for `track_buffer` frames so a bird
    #: that reappears keeps its id.
    LOST = "lost"


@dataclass
class Track:
    """Public, serialisable view of a track."""

    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    class_name: str
    class_id: int
    hits: int
    age_frames: int
    #: Wall-clock time the track was first confirmed.
    first_seen: float
    last_seen: float
    lost: bool
    confirmed: bool

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    def duration_s(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.first_seen

    def aim_point(self, x_ratio: float = 0.5, y_ratio: float = 0.65) -> tuple[float, float]:
        """Point inside the box to aim at, in pixels."""
        return (self.x1 + self.width * x_ratio, self.y1 + self.height * y_ratio)

    def as_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "bbox": [round(self.x1, 1), round(self.y1, 1), round(self.x2, 1), round(self.y2, 1)],
            "confidence": round(self.confidence, 3),
            "class_name": self.class_name,
            "hits": self.hits,
            "lost": self.lost,
            "confirmed": self.confirmed,
            "age_s": round(time.time() - self.first_seen, 2),
        }


class _STrack:
    """Internal track state (Kalman + bookkeeping)."""

    _kf = KalmanFilter()

    def __init__(self, detection: Detection, track_id: int, frame_id: int, now: float) -> None:
        self.track_id = track_id
        self.class_name = detection.class_name
        self.class_id = detection.class_id
        self.confidence = detection.confidence
        self.state = TrackState.TRACKED
        self.hits = 1
        self.frame_id = frame_id
        self.start_frame = frame_id
        self.first_seen = now
        self.last_seen = now
        self.time_since_update = 0
        self.mean, self.covariance = self._kf.initiate(self._to_xyah(detection.as_tlbr()))

    # -- geometry --------------------------------------------------------
    @staticmethod
    def _to_xyah(tlbr: np.ndarray) -> np.ndarray:
        x1, y1, x2, y2 = tlbr
        w, h = max(1e-3, x2 - x1), max(1e-3, y2 - y1)
        return np.array([x1 + w / 2.0, y1 + h / 2.0, w / h, h], dtype=np.float64)

    @property
    def tlbr(self) -> np.ndarray:
        x, y, a, h = self.mean[:4]
        w = a * h
        return np.array([x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0])

    # -- lifecycle -------------------------------------------------------
    def predict(self) -> None:
        mean = self.mean.copy()
        if self.state is not TrackState.TRACKED:
            # A lost object is not assumed to keep changing shape.
            mean[7] = 0.0
        self.mean, self.covariance = self._kf.predict(mean, self.covariance)
        self.time_since_update += 1

    def update(self, detection: Detection, frame_id: int, now: float, min_hits: int) -> None:
        self.mean, self.covariance = self._kf.update(
            self.mean, self.covariance, self._to_xyah(detection.as_tlbr())
        )
        self.confidence = detection.confidence
        self.class_name = detection.class_name
        self.class_id = detection.class_id
        self.hits += 1
        self.frame_id = frame_id
        self.last_seen = now
        self.time_since_update = 0
        if self.hits >= min_hits or self.state is TrackState.LOST:
            self.state = TrackState.TRACKED

    def mark_lost(self) -> None:
        self.state = TrackState.LOST

    def to_public(self, min_hits: int) -> Track:
        x1, y1, x2, y2 = self.tlbr
        return Track(
            track_id=self.track_id,
            x1=float(x1),
            y1=float(y1),
            x2=float(x2),
            y2=float(y2),
            confidence=float(self.confidence),
            class_name=self.class_name,
            class_id=self.class_id,
            hits=self.hits,
            age_frames=self.frame_id - self.start_frame,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            lost=self.state is TrackState.LOST,
            confirmed=self.hits >= min_hits,
        )


class ByteTracker:
    """ByteTrack association over :class:`Detection` inputs."""

    def __init__(self, settings: TrackerSettings) -> None:
        self.settings = settings
        self._tracked: list[_STrack] = []
        self._lost: list[_STrack] = []
        self._next_id = 1
        self._frame_id = 0

    def reset(self) -> None:
        self._tracked.clear()
        self._lost.clear()
        self._frame_id = 0
        self._next_id = 1

    def update(self, detections: list[Detection], now: float | None = None) -> list[Track]:
        now = time.time() if now is None else now
        self._frame_id += 1
        cfg = self.settings

        high = [d for d in detections if d.confidence >= cfg.track_thresh]
        low = [d for d in detections if cfg.low_thresh <= d.confidence < cfg.track_thresh]

        for track in [*self._tracked, *self._lost]:
            track.predict()

        # --- first association: confident detections vs tracked + lost ---
        pool = [*self._tracked, *self._lost]
        matches, unmatched_tracks, unmatched_dets = self._associate(pool, high, cfg.match_thresh)
        activated: list[_STrack] = []
        for track_idx, det_idx in matches:
            track = pool[track_idx]
            track.update(high[det_idx], self._frame_id, now, cfg.min_hits)
            activated.append(track)

        # --- second association: leftovers vs low-confidence detections ---
        remaining = [pool[i] for i in unmatched_tracks if pool[i].state is TrackState.TRACKED]
        matches_low, unmatched_low_tracks, _ = self._associate(remaining, low, 0.5)
        for track_idx, det_idx in matches_low:
            track = remaining[track_idx]
            track.update(low[det_idx], self._frame_id, now, cfg.min_hits)
            activated.append(track)

        still_unmatched = {id(remaining[i]) for i in unmatched_low_tracks}
        lost_now = [
            pool[i]
            for i in unmatched_tracks
            if pool[i].state is not TrackState.TRACKED or id(pool[i]) in still_unmatched
        ]
        for track in lost_now:
            track.mark_lost()

        # --- new tracks from unmatched confident detections ---
        for det_idx in unmatched_dets:
            detection = high[det_idx]
            if detection.confidence < cfg.track_thresh:
                continue
            track = _STrack(detection, self._next_id, self._frame_id, now)
            self._next_id += 1
            activated.append(track)

        # --- bookkeeping -------------------------------------------------
        active_ids = {id(t) for t in activated}
        self._tracked = [t for t in activated if t.state is not TrackState.LOST]
        self._lost = [
            t
            for t in [*lost_now, *(t for t in self._lost if id(t) not in active_ids)]
            if self._frame_id - t.frame_id <= cfg.track_buffer
        ]
        # De-duplicate (a track can appear in both lists after re-finding).
        seen: set[int] = set()
        self._lost = [t for t in self._lost if not (id(t) in seen or seen.add(id(t)))]
        tracked_ids = {id(t) for t in self._tracked}
        self._lost = [t for t in self._lost if id(t) not in tracked_ids]

        return [t.to_public(cfg.min_hits) for t in self._tracked]

    def _associate(
        self, tracks: list[_STrack], detections: list[Detection], threshold: float
    ) -> tuple[list[tuple[int, int]], list[int], list[int]]:
        if not tracks or not detections:
            return [], list(range(len(tracks))), list(range(len(detections)))
        track_boxes = np.array([t.tlbr for t in tracks], dtype=np.float32)
        det_boxes = np.array([d.as_tlbr() for d in detections], dtype=np.float32)
        cost = 1.0 - iou_matrix(track_boxes, det_boxes)
        # Never associate across classes - a bird is not a person.
        for i, track in enumerate(tracks):
            for j, detection in enumerate(detections):
                if track.class_name != detection.class_name:
                    cost[i, j] = 1.0
        return linear_assignment(cost, threshold)


def create_tracker(settings: TrackerSettings) -> ByteTracker:
    return ByteTracker(settings)
