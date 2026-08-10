"""Zone rules.

The no-spray rule is the one that stops water going somewhere it must not, so
it gets tested from several directions.
"""

from __future__ import annotations

import pytest

from app.targeting.zones import ZoneSet, ZoneType, validate_points
from tests.conftest import make_zone, make_zone_set

SQUARE = [(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)]
RIGHT_SQUARE = [(0.5, 0.0), (1.0, 0.0), (1.0, 0.5), (0.5, 0.5)]


class TestVerdicts:
    def test_empty_zone_set_allows_everything(self) -> None:
        zones = ZoneSet()
        verdict = zones.evaluate((0.5, 0.5))
        assert verdict.spray_allowed is True
        assert verdict.targetable(require_active_zone=True, active_zones_exist=False) is True

    def test_no_spray_zone_blocks_water(self) -> None:
        zones = make_zone_set(make_zone(ZoneType.NO_SPRAY, SQUARE))
        assert zones.evaluate((0.25, 0.25)).spray_allowed is False
        assert zones.evaluate((0.75, 0.25)).spray_allowed is True

    def test_no_target_zone_blocks_engagement(self) -> None:
        zones = make_zone_set(make_zone(ZoneType.NO_TARGET, SQUARE))
        verdict = zones.evaluate((0.25, 0.25))
        assert verdict.targetable(require_active_zone=False, active_zones_exist=False) is False

    def test_active_zone_is_required_when_one_exists(self) -> None:
        zones = make_zone_set(make_zone(ZoneType.ACTIVE, SQUARE))
        assert zones.has_active_zones is True
        inside = zones.evaluate((0.25, 0.25))
        outside = zones.evaluate((0.75, 0.25))
        assert inside.targetable(True, True) is True
        assert outside.targetable(True, True) is False
        # ...but only when the operator asked for that rule.
        assert outside.targetable(False, True) is True

    def test_no_target_beats_active(self) -> None:
        zones = make_zone_set(
            make_zone(ZoneType.ACTIVE, SQUARE, zone_id=1),
            make_zone(ZoneType.NO_TARGET, SQUARE, zone_id=2),
        )
        assert zones.evaluate((0.25, 0.25)).targetable(True, True) is False

    def test_a_no_spray_zone_still_allows_aiming(self) -> None:
        # Aiming is not spraying: the turret may track a bird on the neighbour's
        # window sill, it just may not water it.
        zones = make_zone_set(make_zone(ZoneType.NO_SPRAY, SQUARE))
        verdict = zones.evaluate((0.25, 0.25))
        assert verdict.targetable(False, False) is True
        assert verdict.spray_allowed is False


class TestSurfaces:
    def test_surface_lookup(self) -> None:
        zones = make_zone_set(make_zone(ZoneType.RAILING, SQUARE, name="rail"))
        assert zones.surface_at((0.25, 0.25)) == "railing"
        assert zones.surface_at((0.75, 0.25)) is None

    def test_smaller_zone_wins_when_nested(self) -> None:
        big = make_zone(ZoneType.FLOOR, [(0, 0), (1, 0), (1, 1), (0, 1)], zone_id=1)
        small = make_zone(ZoneType.PLANTER, SQUARE, zone_id=2)
        zones = make_zone_set(big, small)
        assert zones.surface_at((0.25, 0.25)) == "planter"
        assert zones.surface_at((0.75, 0.75)) == "floor"

    def test_priority_overrides_size(self) -> None:
        big = make_zone(ZoneType.FLOOR, [(0, 0), (1, 0), (1, 1), (0, 1)], zone_id=1, priority=10)
        small = make_zone(ZoneType.PLANTER, SQUARE, zone_id=2, priority=0)
        zones = make_zone_set(big, small)
        assert zones.surface_at((0.25, 0.25)) == "floor"


class TestValidation:
    def test_accepts_a_normal_polygon(self) -> None:
        assert validate_points(SQUARE) == [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]

    @pytest.mark.parametrize(
        "points, message",
        [
            ([[0, 0], [1, 1]], "at least 3"),
            ([[0, 0], [0.5, 0], [1, 0]], "zero area"),
            ([[0, 0], [1, 0], [1.5, 1]], "normalised"),
            ([[0, 0], [1, 0], [0, 1, 2]], "each point"),
        ],
    )
    def test_rejects_bad_input(self, points: list, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            validate_points(points)

    def test_rejects_absurdly_large_polygons(self) -> None:
        points = [[i / 200, (i % 2) / 2] for i in range(200)]
        with pytest.raises(ValueError, match="more than 128"):
            validate_points(points)


class TestDisabledZones:
    def test_disabled_zones_are_ignored(self) -> None:
        zone = make_zone(ZoneType.NO_SPRAY, SQUARE)
        enabled = ZoneSet([zone])
        disabled = ZoneSet([zone.__class__(**{**zone.__dict__, "enabled": False})])
        assert enabled.evaluate((0.25, 0.25)).spray_allowed is False
        assert disabled.evaluate((0.25, 0.25)).spray_allowed is True
