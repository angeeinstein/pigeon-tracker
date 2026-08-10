"""Calibration mapping tests.

These matter more than they look: every automatic engagement and every
click-to-aim goes through this maths, and a silent error here points water at
the wrong place.
"""

from __future__ import annotations

import math

import pytest

from app.targeting.geometry import convex_hull, point_in_polygon, polygon_area
from app.targeting.mapping import (
    CalibrationSample,
    MappingModel,
    build_mapping_set,
)


def linear_samples(count: int = 5) -> list[CalibrationSample]:
    """A known affine relation: pan = 100x - 50, tilt = 40y - 20."""
    samples = []
    for i in range(count):
        for j in range(count):
            x = i / (count - 1)
            y = j / (count - 1)
            samples.append(
                CalibrationSample(cam_x=x, cam_y=y, pan_deg=100 * x - 50, tilt_deg=40 * y - 20)
            )
    return samples


class TestExactReproduction:
    def test_calibration_points_map_to_their_own_angles(self) -> None:
        samples = linear_samples()
        model = MappingModel(samples)
        for sample in samples:
            solution = model.image_to_angles(sample.cam_x, sample.cam_y)
            assert solution.pan_deg == pytest.approx(sample.pan_deg, abs=1e-6)
            assert solution.tilt_deg == pytest.approx(sample.tilt_deg, abs=1e-6)

    def test_interpolates_an_affine_field_exactly(self) -> None:
        model = MappingModel(linear_samples())
        solution = model.image_to_angles(0.37, 0.62)
        assert solution.pan_deg == pytest.approx(100 * 0.37 - 50, abs=1e-3)
        assert solution.tilt_deg == pytest.approx(40 * 0.62 - 20, abs=1e-3)

    @pytest.mark.parametrize("strategy", ["local_linear", "affine", "idw"])
    def test_every_strategy_is_close_on_a_plane(self, strategy: str) -> None:
        model = MappingModel(linear_samples(), strategy=strategy)  # type: ignore[arg-type]
        solution = model.image_to_angles(0.5, 0.5)
        assert solution.pan_deg == pytest.approx(0.0, abs=2.0)
        assert solution.tilt_deg == pytest.approx(0.0, abs=2.0)


class TestInverse:
    def test_angles_map_back_to_the_image(self) -> None:
        model = MappingModel(linear_samples())
        x, y = model.angles_to_image(0.0, 0.0)
        assert x == pytest.approx(0.5, abs=1e-3)
        assert y == pytest.approx(0.5, abs=1e-3)

    def test_round_trip(self) -> None:
        model = MappingModel(linear_samples())
        solution = model.image_to_angles(0.3, 0.7)
        x, y = model.angles_to_image(solution.pan_deg, solution.tilt_deg)
        assert x == pytest.approx(0.3, abs=1e-3)
        assert y == pytest.approx(0.7, abs=1e-3)


class TestDegradation:
    def test_single_point_falls_back_to_nearest(self) -> None:
        model = MappingModel([CalibrationSample(0.5, 0.5, 12.0, -3.0)])
        assert model.strategy == "nearest"
        solution = model.image_to_angles(0.1, 0.9)
        assert (solution.pan_deg, solution.tilt_deg) == (12.0, -3.0)

    def test_collinear_points_do_not_produce_a_degenerate_fit(self) -> None:
        # All points on one horizontal line: an affine fit is under-determined,
        # so the model must fall back rather than return nonsense.
        samples = [CalibrationSample(x / 4, 0.5, 20 * (x / 4), 0.0) for x in range(5)]
        model = MappingModel(samples, strategy="local_linear")
        assert model.strategy == "idw"
        solution = model.image_to_angles(0.5, 0.5)
        assert math.isfinite(solution.pan_deg)
        assert -1.0 <= solution.tilt_deg <= 1.0

    def test_empty_sample_set_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            MappingModel([])


class TestExtrapolationFlag:
    def test_inside_the_hull_is_not_extrapolated(self) -> None:
        model = MappingModel(linear_samples())
        assert model.image_to_angles(0.5, 0.5).extrapolated is False

    def test_outside_the_hull_is_flagged(self) -> None:
        model = MappingModel(linear_samples())
        solution = model.image_to_angles(1.4, 0.5)
        assert solution.extrapolated is True

    def test_nearest_distance_is_reported(self) -> None:
        model = MappingModel([CalibrationSample(0.5, 0.5, 0.0, 0.0), *linear_samples(3)])
        assert model.image_to_angles(0.5, 0.5).nearest_distance == pytest.approx(0.0, abs=1e-9)


class TestSurfaces:
    def test_each_surface_gets_its_own_model(self) -> None:
        samples = [
            *(CalibrationSample(x / 2, 0.2, 10 * x, 5.0, surface="railing") for x in range(3)),
            *(CalibrationSample(x / 2, 0.8, 10 * x, -15.0, surface="floor") for x in range(3)),
        ]
        mapping = build_mapping_set(samples)
        assert set(mapping.surfaces) == {"railing", "floor"}

        railing = mapping.solve(0.5, 0.2, "railing")
        floor = mapping.solve(0.5, 0.2, "floor")
        assert railing is not None and floor is not None
        # Same pixel, different surface -> genuinely different tilt.
        assert railing.tilt_deg == pytest.approx(5.0, abs=0.5)
        assert floor.tilt_deg == pytest.approx(-15.0, abs=0.5)

    def test_unspecified_surface_picks_the_containing_one(self) -> None:
        samples = [
            *(
                CalibrationSample(x / 2, 0.1 + y / 20, 10 * x, 5.0, surface="railing")
                for x in range(3)
                for y in range(2)
            ),
            *(
                CalibrationSample(x / 2, 0.8 + y / 20, 10 * x, -15.0, surface="floor")
                for x in range(3)
                for y in range(2)
            ),
        ]
        mapping = build_mapping_set(samples)
        solution = mapping.solve(0.5, 0.85)
        assert solution is not None
        assert solution.surface == "floor"

    def test_no_samples_means_no_solution(self) -> None:
        mapping = build_mapping_set([])
        assert mapping.is_calibrated is False
        assert mapping.solve(0.5, 0.5) is None


class TestGeometryHelpers:
    def test_point_in_polygon(self) -> None:
        square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        assert point_in_polygon((0.5, 0.5), square) is True
        assert point_in_polygon((1.5, 0.5), square) is False
        assert point_in_polygon((0.5, -0.1), square) is False

    def test_concave_polygon(self) -> None:
        # An L-shape: the notch must not count as inside.
        shape = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
        assert point_in_polygon((0.5, 1.5), shape) is True
        assert point_in_polygon((1.5, 1.5), shape) is False

    def test_degenerate_polygon_contains_nothing(self) -> None:
        assert point_in_polygon((0.5, 0.5), [(0, 0), (1, 1)]) is False

    def test_polygon_area(self) -> None:
        assert polygon_area([(0, 0), (2, 0), (2, 3), (0, 3)]) == pytest.approx(6.0)

    def test_convex_hull_drops_interior_points(self) -> None:
        hull = convex_hull([(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)])
        assert len(hull) == 4
        assert (0.5, 0.5) not in hull
