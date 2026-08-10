"""Camera pixel <-> turret angle mapping.

The overview camera and the turret are not co-located, so there is no closed
form from pixel to angle without knowing the 3-D geometry of the balcony. What
*is* cheap and reliable is measuring: aim the turret at a spot, click the spot
in the image, save the pair. Enough pairs define the surface implicitly.

Version 1 therefore interpolates between measured correspondences:

* ``local_linear`` (default) — a distance-weighted affine fit around the query
  point. Behaves like a plane fit near each measurement, so it follows a
  curved surface (railing vs. floor) far better than one global fit, and it
  reproduces the measurements exactly when you stand on one.
* ``affine`` — one global 2-D affine fit. Right answer if everything of
  interest lies on a single plane and the measurements are noisy.
* ``idw`` — plain inverse-distance weighting. No extrapolation ability, but it
  never does anything surprising.
* ``nearest`` — what you get with one or two points.

Everything below is deliberately dependency-light (numpy only) and pure: no I/O
and no database, so it is fully unit-testable. Swapping in a real 3-D model
later means implementing :class:`MappingModel` and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.targeting.geometry import Point, convex_hull, point_in_polygon

Strategy = Literal["local_linear", "affine", "idw", "nearest"]

#: Points closer than this (in normalised image units / degrees) count as the
#: same measurement.
_EPS = 1e-9


@dataclass(frozen=True)
class CalibrationSample:
    """One measured correspondence.

    Image coordinates are **normalised** to ``[0, 1]`` so a change of camera
    resolution or preview scaling does not invalidate hours of measuring.
    """

    cam_x: float
    cam_y: float
    pan_deg: float
    tilt_deg: float
    surface: str = "default"
    id: int | None = None

    @property
    def image_point(self) -> Point:
        return (self.cam_x, self.cam_y)

    @property
    def angles(self) -> Point:
        return (self.pan_deg, self.tilt_deg)


@dataclass(frozen=True)
class AimSolution:
    """Result of mapping an image point to turret angles."""

    pan_deg: float
    tilt_deg: float
    #: True when the query lies outside the convex hull of the calibration
    #: points, i.e. the answer is an extrapolation and should be treated with
    #: suspicion (the UI warns; automatic mode can be told to refuse).
    extrapolated: bool
    surface: str
    #: Distance to the nearest calibration point, normalised image units.
    nearest_distance: float
    strategy: str

    def as_dict(self) -> dict[str, object]:
        return {
            "pan_deg": round(self.pan_deg, 3),
            "tilt_deg": round(self.tilt_deg, 3),
            "extrapolated": self.extrapolated,
            "surface": self.surface,
            "nearest_distance": round(self.nearest_distance, 4),
            "strategy": self.strategy,
        }


class ScatteredMap2D:
    """Scattered-data interpolation from R² to R².

    One instance maps in one direction; a :class:`MappingModel` holds two so
    click-to-aim and "where is the turret pointing in the image" both work.
    """

    def __init__(
        self,
        src: np.ndarray,
        dst: np.ndarray,
        strategy: Strategy = "local_linear",
        neighbours: int = 8,
    ) -> None:
        if src.shape[0] != dst.shape[0]:
            raise ValueError("src and dst must have the same number of rows")
        self.src = np.asarray(src, dtype=np.float64).reshape(-1, 2)
        self.dst = np.asarray(dst, dtype=np.float64).reshape(-1, 2)
        self.neighbours = max(3, neighbours)
        self.strategy: Strategy = self._effective_strategy(strategy)
        self._affine = self._fit_affine() if self.strategy == "affine" else None

    @property
    def count(self) -> int:
        return int(self.src.shape[0])

    def _effective_strategy(self, requested: Strategy) -> Strategy:
        """Degrade gracefully when there are too few points for the request."""
        n = self.src.shape[0]
        if n == 0:
            raise ValueError("at least one calibration point is required")
        if n < 3:
            return "nearest"
        if requested in {"local_linear", "affine"} and self._is_degenerate():
            # All points on a line: an affine fit is under-determined.
            return "idw"
        return requested

    def _is_degenerate(self) -> bool:
        centred = self.src - self.src.mean(axis=0)
        # Rank of the centred coordinates: 2 means the points span a plane.
        return bool(np.linalg.matrix_rank(centred, tol=1e-9) < 2)

    def _fit_affine(self, weights: np.ndarray | None = None) -> np.ndarray:
        design = np.hstack([self.src, np.ones((self.src.shape[0], 1))])
        target = self.dst
        if weights is not None:
            root = np.sqrt(weights)[:, None]
            design = design * root
            target = target * root
        solution, *_ = np.linalg.lstsq(design, target, rcond=None)
        return solution  # (3, 2)

    def predict(self, x: float, y: float) -> tuple[float, float]:
        query = np.array([x, y], dtype=np.float64)
        deltas = self.src - query
        distances = np.sqrt(np.einsum("ij,ij->i", deltas, deltas))

        nearest_index = int(np.argmin(distances))
        if distances[nearest_index] < 1e-7:
            return float(self.dst[nearest_index, 0]), float(self.dst[nearest_index, 1])

        if self.strategy == "nearest":
            return float(self.dst[nearest_index, 0]), float(self.dst[nearest_index, 1])

        if self.strategy == "affine":
            assert self._affine is not None
            result = np.array([x, y, 1.0]) @ self._affine
            return float(result[0]), float(result[1])

        weights = 1.0 / (distances**2 + _EPS)

        if self.strategy == "idw":
            result = (weights[:, None] * self.dst).sum(axis=0) / weights.sum()
            return float(result[0]), float(result[1])

        # local_linear: weighted affine fit over the nearest neighbours.
        k = min(self.neighbours, self.src.shape[0])
        order = np.argsort(distances)[:k]
        local_src = self.src[order]
        local_dst = self.dst[order]
        local_w = weights[order]

        centred = local_src - local_src.mean(axis=0)
        if np.linalg.matrix_rank(centred, tol=1e-9) < 2:
            # Neighbourhood is collinear: fall back to IDW over it.
            result = (local_w[:, None] * local_dst).sum(axis=0) / local_w.sum()
            return float(result[0]), float(result[1])

        design = np.hstack([local_src, np.ones((k, 1))]) * np.sqrt(local_w)[:, None]
        target = local_dst * np.sqrt(local_w)[:, None]
        solution, *_ = np.linalg.lstsq(design, target, rcond=None)
        result = np.array([x, y, 1.0]) @ solution
        return float(result[0]), float(result[1])


class MappingModel:
    """Bidirectional mapping for one calibration surface."""

    def __init__(
        self,
        samples: Sequence[CalibrationSample],
        strategy: Strategy = "local_linear",
        surface: str = "default",
    ) -> None:
        if not samples:
            raise ValueError("a mapping needs at least one calibration point")
        self.samples = list(samples)
        self.surface = surface
        image = np.array([[s.cam_x, s.cam_y] for s in samples], dtype=np.float64)
        angles = np.array([[s.pan_deg, s.tilt_deg] for s in samples], dtype=np.float64)
        self._forward = ScatteredMap2D(image, angles, strategy)
        self._inverse = ScatteredMap2D(angles, image, strategy)
        self._hull = convex_hull([(float(p[0]), float(p[1])) for p in image])
        self._image_points = image

    @property
    def strategy(self) -> str:
        return self._forward.strategy

    @property
    def count(self) -> int:
        return len(self.samples)

    def contains(self, x: float, y: float) -> bool:
        """Is the image point inside the calibrated region?"""
        if len(self._hull) < 3:
            return False
        return point_in_polygon((x, y), self._hull)

    def nearest_distance(self, x: float, y: float) -> float:
        deltas = self._image_points - np.array([x, y])
        return float(np.sqrt(np.einsum("ij,ij->i", deltas, deltas)).min())

    def image_to_angles(self, x: float, y: float) -> AimSolution:
        pan, tilt = self._forward.predict(x, y)
        return AimSolution(
            pan_deg=pan,
            tilt_deg=tilt,
            extrapolated=not self.contains(x, y),
            surface=self.surface,
            nearest_distance=self.nearest_distance(x, y),
            strategy=self.strategy,
        )

    def angles_to_image(self, pan_deg: float, tilt_deg: float) -> tuple[float, float]:
        return self._inverse.predict(pan_deg, tilt_deg)

    def describe(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "points": self.count,
            "strategy": self.strategy,
            "hull": [[round(x, 4), round(y, 4)] for x, y in self._hull],
        }


class MappingSet:
    """All calibration surfaces for one camera.

    Surfaces exist because a balcony is not one plane: the railing, the planter
    boxes and the floor are at different distances, and a pixel near the
    boundary genuinely maps to two different angles depending on which surface
    the bird is standing on. The zone layer decides which surface a point
    belongs to; this class then asks that surface's model.
    """

    def __init__(self, models: dict[str, MappingModel] | None = None) -> None:
        self._models: dict[str, MappingModel] = models or {}

    @property
    def surfaces(self) -> list[str]:
        return sorted(self._models)

    @property
    def is_calibrated(self) -> bool:
        return bool(self._models)

    def get(self, surface: str | None = None) -> MappingModel | None:
        if surface and surface in self._models:
            return self._models[surface]
        if "default" in self._models:
            return self._models["default"]
        # Fall back to whichever surface has the most measurements.
        if not self._models:
            return None
        return max(self._models.values(), key=lambda m: m.count)

    def solve(self, x: float, y: float, surface: str | None = None) -> AimSolution | None:
        """Map a normalised image point to angles on the given surface.

        When no surface is requested, every surface is evaluated and the one
        whose calibrated region actually contains the point wins; ties break
        toward the closest measurement. That makes click-to-aim behave sanely
        before any zones have been drawn.
        """
        if surface is not None:
            model = self.get(surface)
            return model.image_to_angles(x, y) if model else None

        candidates = [
            (model, model.contains(x, y), model.nearest_distance(x, y))
            for model in self._models.values()
        ]
        if not candidates:
            return None
        inside = [c for c in candidates if c[1]]
        pool = inside or candidates
        model = min(pool, key=lambda c: c[2])[0]
        return model.image_to_angles(x, y)

    def describe(self) -> list[dict[str, object]]:
        return [model.describe() for model in self._models.values()]


def build_mapping_set(
    samples: Sequence[CalibrationSample], strategy: Strategy = "local_linear"
) -> MappingSet:
    """Group samples by surface and fit a model for each."""
    grouped: dict[str, list[CalibrationSample]] = {}
    for sample in samples:
        grouped.setdefault(sample.surface or "default", []).append(sample)

    models: dict[str, MappingModel] = {}
    for surface, group in grouped.items():
        try:
            models[surface] = MappingModel(group, strategy=strategy, surface=surface)
        except ValueError:
            continue
    return MappingSet(models)
