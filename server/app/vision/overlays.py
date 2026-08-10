"""Overlay rendering for the preview stream.

Drawing happens server-side so every client (phone, tablet, a plain
``<img>`` tag pointed at the MJPEG endpoint) shows the same picture without
reimplementing the geometry. The browser additionally draws interactive
overlays on a canvas for editing zones and calibration.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np

from app.vision.tracker import Track

# BGR, chosen to stay readable on both a bright sky and a dark floor.
COLOR_TRACK = (0, 200, 255)
COLOR_TARGET = (0, 80, 255)
COLOR_AIM = (0, 0, 255)
COLOR_TURRET = (255, 200, 0)
COLOR_TEXT = (255, 255, 255)
COLOR_SHADOW = (0, 0, 0)

ZONE_COLORS: dict[str, tuple[int, int, int]] = {
    "active": (80, 220, 80),
    "no_target": (60, 60, 220),
    "no_spray": (0, 140, 255),
    "railing": (200, 200, 80),
    "planter": (80, 200, 160),
    "floor": (180, 120, 220),
}

FONT = cv2.FONT_HERSHEY_SIMPLEX


def _text(
    image: np.ndarray,
    text: str,
    origin: tuple[int, int],
    scale: float = 0.5,
    color: tuple[int, int, int] = COLOR_TEXT,
) -> None:
    """Text with a 1 px shadow so it stays legible over any background."""
    cv2.putText(
        image, text, (origin[0] + 1, origin[1] + 1), FONT, scale, COLOR_SHADOW, 2, cv2.LINE_AA
    )
    cv2.putText(image, text, origin, FONT, scale, color, 1, cv2.LINE_AA)


def draw_zones(image: np.ndarray, zones: Sequence[dict[str, Any]], alpha: float = 0.18) -> None:
    """Draw normalised polygons. Mutates ``image``."""
    height, width = image.shape[:2]
    overlay = image.copy()
    for zone in zones:
        points = zone.get("points") or []
        if len(points) < 3:
            continue
        color = ZONE_COLORS.get(str(zone.get("zone_type")), (160, 160, 160))
        polygon = np.array(
            [[int(px * width), int(py * height)] for px, py in points], dtype=np.int32
        )
        cv2.fillPoly(overlay, [polygon], color)
        cv2.polylines(image, [polygon], True, color, 2, cv2.LINE_AA)
        _text(image, str(zone.get("name", "")), tuple(polygon[0] + np.array([4, -6])), 0.45, color)
    cv2.addWeighted(overlay, alpha, image, 1 - alpha, 0, dst=image)


def draw_tracks(
    image: np.ndarray,
    tracks: Sequence[Track],
    target_track_id: int | None = None,
) -> None:
    for track in tracks:
        is_target = track.track_id == target_track_id
        color = COLOR_TARGET if is_target else COLOR_TRACK
        thickness = 3 if is_target else 2
        p1 = (int(track.x1), int(track.y1))
        p2 = (int(track.x2), int(track.y2))
        cv2.rectangle(image, p1, p2, color, thickness, cv2.LINE_AA)
        label = f"#{track.track_id} {track.class_name} {track.confidence:.2f}"
        _text(image, label, (p1[0], max(12, p1[1] - 6)), 0.5, color)
        if is_target:
            _text(image, "TARGET", (p1[0], min(image.shape[0] - 4, p2[1] + 16)), 0.5, color)


def draw_crosshair(
    image: np.ndarray,
    point: tuple[float, float],
    color: tuple[int, int, int],
    label: str = "",
    size: int = 14,
) -> None:
    x, y = int(point[0]), int(point[1])
    cv2.line(image, (x - size, y), (x - 4, y), color, 2, cv2.LINE_AA)
    cv2.line(image, (x + 4, y), (x + size, y), color, 2, cv2.LINE_AA)
    cv2.line(image, (x, y - size), (x, y - 4), color, 2, cv2.LINE_AA)
    cv2.line(image, (x, y + 4), (x, y + size), color, 2, cv2.LINE_AA)
    cv2.circle(image, (x, y), size + 4, color, 1, cv2.LINE_AA)
    if label:
        _text(image, label, (x + size + 6, y + 4), 0.45, color)


def draw_hud(image: np.ndarray, lines: Sequence[str]) -> None:
    if not lines:
        return
    pad = 8
    height = 18 * len(lines) + pad
    width = max(140, int(max(len(line) for line in lines) * 7.2) + 2 * pad)
    box = image[0:height, 0:width]
    cv2.addWeighted(box, 0.35, np.zeros_like(box), 0.65, 0, dst=box)
    for index, line in enumerate(lines):
        _text(image, line, (pad, pad + 12 + index * 18), 0.48)


def render_overlay(
    image: np.ndarray,
    *,
    tracks: Sequence[Track] = (),
    zones: Sequence[dict[str, Any]] = (),
    target_track_id: int | None = None,
    aim_point: tuple[float, float] | None = None,
    turret_point: tuple[float, float] | None = None,
    hud_lines: Sequence[str] = (),
    draw_zone_layer: bool = True,
) -> np.ndarray:
    """Return a new image with all overlays drawn.

    The input frame is shared with every other consumer, so it is copied first
    and never modified.
    """
    canvas = image.copy()
    if draw_zone_layer and zones:
        draw_zones(canvas, zones)
    draw_tracks(canvas, tracks, target_track_id)
    if turret_point is not None:
        draw_crosshair(canvas, turret_point, COLOR_TURRET, "turret")
    if aim_point is not None:
        draw_crosshair(canvas, aim_point, COLOR_AIM, "aim")
    draw_hud(canvas, hud_lines)
    return canvas
