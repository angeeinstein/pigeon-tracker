"""Small 2-D geometry helpers shared by zones and calibration.

Pure Python and dependency-free on purpose: these are the pieces most worth
unit-testing, and they are used on every frame.
"""

from __future__ import annotations

from collections.abc import Sequence

Point = tuple[float, float]


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray-casting point-in-polygon test.

    Points exactly on an edge are not guaranteed to be classified one way or
    the other; that ambiguity is irrelevant at pixel scale and the alternative
    (an epsilon everywhere) hides real bugs.
    """
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Does the edge straddle the horizontal ray at y?
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def polygon_area(polygon: Sequence[Point]) -> float:
    """Absolute area via the shoelace formula."""
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        total += (xj + xi) * (yj - yi)
        j = i
    return abs(total) / 2.0


def polygon_centroid(polygon: Sequence[Point]) -> Point:
    """Area centroid, falling back to the vertex mean for degenerate shapes."""
    area = polygon_area(polygon)
    if area <= 1e-12:
        if not polygon:
            return (0.0, 0.0)
        return (
            sum(p[0] for p in polygon) / len(polygon),
            sum(p[1] for p in polygon) / len(polygon),
        )
    cx = cy = 0.0
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        cross = xj * yi - xi * yj
        cx += (xj + xi) * cross
        cy += (yj + yi) * cross
        j = i
    signed_area = 0.0
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        signed_area += xj * yi - xi * yj
        j = i
    signed_area /= 2.0
    factor = 1.0 / (6.0 * signed_area)
    return (cx * factor, cy * factor)


def convex_hull(points: Sequence[Point]) -> list[Point]:
    """Convex hull (Andrew's monotone chain), counter-clockwise.

    Used to tell whether a click is inside the calibrated region or whether the
    mapping would be extrapolating — which the UI shows as a warning instead of
    silently aiming somewhere invented.
    """
    unique = sorted(set(points))
    if len(unique) <= 2:
        return list(unique)

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: list[Point] = []
    for p in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def distance(a: Point, b: Point) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value
