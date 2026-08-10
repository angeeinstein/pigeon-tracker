"""Target selection rules."""

from __future__ import annotations

import time

import pytest

from app.services.settings_schema import TargetingSettings
from app.targeting.mapping import AimSolution
from app.targeting.target_selector import TargetSelector
from app.targeting.zones import ZoneSet, ZoneType
from tests.conftest import make_track, make_zone, make_zone_set

NOW = time.time()


def solver(x: float, y: float, surface: str | None) -> AimSolution:
    return AimSolution(
        pan_deg=(x - 0.5) * 100,
        tilt_deg=(0.5 - y) * 50,
        extrapolated=False,
        surface=surface or "default",
        nearest_distance=0.01,
        strategy="local_linear",
    )


def no_calibration(_x: float, _y: float, _surface: str | None) -> None:
    return None


def selector(**kwargs) -> TargetSelector:
    defaults = {
        "min_track_duration_s": 0.0,
        "min_confidence": 0.5,
        "require_active_zone": False,
    }
    return TargetSelector(TargetingSettings(**{**defaults, **kwargs}))


class TestEligibility:
    def test_selects_a_valid_bird(self) -> None:
        result = selector().evaluate([make_track(now=NOW)], 1280, 720, ZoneSet(), solver, NOW)
        assert result.best is not None
        assert result.best.track.track_id == 1

    def test_rejects_the_wrong_class(self) -> None:
        result = selector().evaluate(
            [make_track(class_name="person", now=NOW)], 1280, 720, ZoneSet(), solver, NOW
        )
        assert result.best is None
        assert result.candidates[0].rejected == "class not targeted"

    def test_rejects_low_confidence(self) -> None:
        result = selector(min_confidence=0.8).evaluate(
            [make_track(confidence=0.6, now=NOW)], 1280, 720, ZoneSet(), solver, NOW
        )
        assert result.best is None
        assert "confidence" in (result.candidates[0].rejected or "")

    def test_rejects_unconfirmed_tracks(self) -> None:
        result = selector().evaluate(
            [make_track(confirmed=False, now=NOW)], 1280, 720, ZoneSet(), solver, NOW
        )
        assert result.candidates[0].rejected == "track not confirmed"

    def test_rejects_young_tracks(self) -> None:
        result = selector(min_track_duration_s=5.0).evaluate(
            [make_track(first_seen=NOW - 1.0, now=NOW)], 1280, 720, ZoneSet(), solver, NOW
        )
        assert result.best is None

    def test_rejects_when_calibration_is_missing(self) -> None:
        result = selector().evaluate(
            [make_track(now=NOW)], 1280, 720, ZoneSet(), no_calibration, NOW
        )
        assert result.best is None
        assert result.candidates[0].rejected == "no calibration for this point"


class TestZoneInteraction:
    def test_no_target_zone_excludes_the_track(self) -> None:
        zones = make_zone_set(make_zone(ZoneType.NO_TARGET, [(0, 0), (1, 0), (1, 1), (0, 1)]))
        result = selector().evaluate([make_track(now=NOW)], 1280, 720, zones, solver, NOW)
        assert result.best is None
        assert result.candidates[0].rejected == "in no-target zone"

    def test_active_zone_requirement(self) -> None:
        zones = make_zone_set(make_zone(ZoneType.ACTIVE, [(0, 0), (0.2, 0), (0.2, 0.2), (0, 0.2)]))
        result = selector(require_active_zone=True).evaluate(
            [make_track(x1=600, y1=300, x2=640, y2=340, now=NOW)], 1280, 720, zones, solver, NOW
        )
        assert result.candidates[0].rejected == "outside active zone"

    def test_no_spray_zone_still_yields_a_candidate(self) -> None:
        zones = make_zone_set(make_zone(ZoneType.NO_SPRAY, [(0, 0), (1, 0), (1, 1), (0, 1)]))
        result = selector().evaluate([make_track(now=NOW)], 1280, 720, zones, solver, NOW)
        assert result.best is not None
        assert result.best.verdict.spray_allowed is False


class TestAimPoint:
    def test_aim_ratios_are_applied(self) -> None:
        result = selector(aim_x_ratio=0.5, aim_y_ratio=1.0).evaluate(
            [make_track(x1=0, y1=0, x2=100, y2=200, now=NOW)],
            1000,
            1000,
            ZoneSet(),
            solver,
            NOW,
        )
        assert result.best is not None
        assert result.best.aim_px == (50.0, 200.0)
        assert result.best.aim_norm == (0.05, 0.2)

    def test_solution_comes_from_the_aim_point(self) -> None:
        result = selector().evaluate(
            [make_track(x1=1260, y1=340, x2=1280, y2=380, now=NOW)],
            1280,
            720,
            ZoneSet(),
            solver,
            NOW,
        )
        assert result.best is not None
        assert result.best.solution is not None
        assert result.best.solution.pan_deg == pytest.approx(49.2, abs=1.0)


class TestPolicies:
    def build(self, policy: str):
        tracks = [
            make_track(1, x1=0, y1=0, x2=20, y2=20, confidence=0.95, now=NOW),
            make_track(
                2, x1=600, y1=340, x2=700, y2=440, confidence=0.6, first_seen=NOW - 60, now=NOW
            ),
        ]
        return selector(selection=policy).evaluate(tracks, 1280, 720, ZoneSet(), solver, NOW)

    def test_highest_confidence(self) -> None:
        assert self.build("highest_confidence").best.track.track_id == 1

    def test_largest(self) -> None:
        assert self.build("largest").best.track.track_id == 2

    def test_oldest(self) -> None:
        assert self.build("oldest").best.track.track_id == 2

    def test_closest_to_center(self) -> None:
        assert self.build("closest_to_center").best.track.track_id == 2


class TestResultHelpers:
    def test_find_by_track_id(self) -> None:
        result = selector().evaluate([make_track(7, now=NOW)], 1280, 720, ZoneSet(), solver, NOW)
        assert result.find(7) is not None
        assert result.find(99) is None

    def test_serialisable(self) -> None:
        result = selector().evaluate([make_track(now=NOW)], 1280, 720, ZoneSet(), solver, NOW)
        payload = result.as_dict()
        assert payload["best"]["track_id"] == 1
        assert payload["candidates"][0]["solution"]["pan_deg"] is not None
