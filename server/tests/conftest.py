"""Shared test fixtures and builders.

The point of the builders below is that the targeting logic is pure: given
tracks, zones and a calibration solver, it produces decisions. None of these
tests need a camera, a model, a database or hardware.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.services.settings_schema import TargetingSettings
from app.targeting.mapping import AimSolution
from app.targeting.target_selector import Candidate, SelectionResult
from app.targeting.zones import Zone, ZoneSet, ZoneType, ZoneVerdict
from app.vision.detector import Detection
from app.vision.tracker import Track


@pytest.fixture()
def temp_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialised, empty database in a temporary directory."""
    from app.database.db import dispose_engine, init_engine

    path = tmp_path / "test.db"
    init_engine(path)
    yield path
    dispose_engine()


def make_track(
    track_id: int = 1,
    *,
    x1: float = 100,
    y1: float = 100,
    x2: float = 140,
    y2: float = 140,
    confidence: float = 0.9,
    class_name: str = "bird",
    confirmed: bool = True,
    first_seen: float | None = None,
    now: float | None = None,
) -> Track:
    now = time.time() if now is None else now
    return Track(
        track_id=track_id,
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        confidence=confidence,
        class_name=class_name,
        class_id=14,
        hits=10,
        age_frames=10,
        first_seen=first_seen if first_seen is not None else now - 5.0,
        last_seen=now,
        lost=False,
        confirmed=confirmed,
    )


def make_detection(
    x1: float = 100,
    y1: float = 100,
    x2: float = 140,
    y2: float = 140,
    confidence: float = 0.9,
    class_name: str = "bird",
) -> Detection:
    return Detection(
        x1=x1, y1=y1, x2=x2, y2=y2, confidence=confidence, class_id=14, class_name=class_name
    )


def make_candidate(
    track: Track | None = None,
    *,
    pan: float = 10.0,
    tilt: float = -5.0,
    spray_allowed: bool = True,
    rejected: str | None = None,
) -> Candidate:
    track = track or make_track()
    verdict = ZoneVerdict(
        in_active=True,
        in_no_target=False,
        in_no_spray=not spray_allowed,
        surface=None,
        matched=(),
    )
    return Candidate(
        track=track,
        aim_norm=(0.5, 0.5),
        aim_px=(640.0, 360.0),
        verdict=verdict,
        solution=AimSolution(
            pan_deg=pan,
            tilt_deg=tilt,
            extrapolated=False,
            surface="default",
            nearest_distance=0.01,
            strategy="local_linear",
        ),
        score=track.confidence,
        rejected=rejected,
    )


def make_selection(*candidates: Candidate) -> SelectionResult:
    eligible = [c for c in candidates if c.eligible]
    best = max(eligible, key=lambda c: c.score) if eligible else None
    return SelectionResult(best=best, candidates=list(candidates))


def make_zone(
    zone_type: ZoneType,
    points: list[tuple[float, float]],
    *,
    zone_id: int = 1,
    name: str = "zone",
    priority: int = 0,
) -> Zone:
    return Zone(
        id=zone_id,
        name=name,
        zone_type=zone_type,
        points=tuple(points),
        enabled=True,
        priority=priority,
    )


def make_zone_set(*zones: Zone) -> ZoneSet:
    return ZoneSet(zones)


@pytest.fixture()
def targeting_settings() -> TargetingSettings:
    """Fast timings so a full engagement can be simulated in a few ticks."""
    return TargetingSettings(
        auto_enabled=True,
        min_track_duration_s=0.0,
        detect_stability_s=0.0,
        verify_duration_s=0.0,
        aim_timeout_s=5.0,
        lost_grace_s=0.5,
        result_window_s=1.0,
        cooldown_s=2.0,
        max_retries=1,
        require_active_zone=False,
    )
