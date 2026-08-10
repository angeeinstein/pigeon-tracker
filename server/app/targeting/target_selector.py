"""Target selection.

Turns "here are some tracks" into "this is the one, and here is where to point"
— or into a list of reasons why nothing qualifies. Pure logic: the caller
injects the zone set and the calibration solver, so this is fully testable
without a database, a camera, or a turret.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.settings_schema import TargetingSettings
from app.targeting.mapping import AimSolution
from app.targeting.zones import ZoneSet, ZoneVerdict
from app.vision.tracker import Track

#: ``(x_norm, y_norm, surface) -> AimSolution | None``
AimSolver = Callable[[float, float, str | None], AimSolution | None]


@dataclass
class Candidate:
    track: Track
    #: Aim point in normalised image coordinates.
    aim_norm: tuple[float, float]
    #: Aim point in pixels (for overlays).
    aim_px: tuple[float, float]
    verdict: ZoneVerdict
    solution: AimSolution | None
    score: float = 0.0
    rejected: str | None = None

    @property
    def eligible(self) -> bool:
        return self.rejected is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track.track_id,
            "class_name": self.track.class_name,
            "confidence": round(self.track.confidence, 3),
            "aim_norm": [round(self.aim_norm[0], 4), round(self.aim_norm[1], 4)],
            "score": round(self.score, 4),
            "rejected": self.rejected,
            "zones": self.verdict.as_dict(),
            "solution": self.solution.as_dict() if self.solution else None,
        }


@dataclass
class SelectionResult:
    best: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def eligible(self) -> list[Candidate]:
        return [c for c in self.candidates if c.eligible]

    def find(self, track_id: int) -> Candidate | None:
        return next((c for c in self.candidates if c.track.track_id == track_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "best": self.best.as_dict() if self.best else None,
            "candidates": [c.as_dict() for c in self.candidates],
        }


class TargetSelector:
    def __init__(self, settings: TargetingSettings) -> None:
        self.settings = settings

    def update_settings(self, settings: TargetingSettings) -> None:
        self.settings = settings

    def evaluate(
        self,
        tracks: list[Track],
        frame_width: int,
        frame_height: int,
        zones: ZoneSet,
        solve: AimSolver,
        now: float,
    ) -> SelectionResult:
        """Score every track and pick the best eligible one."""
        cfg = self.settings
        width = float(max(1, frame_width))
        height = float(max(1, frame_height))
        candidates: list[Candidate] = []

        for track in tracks:
            aim_px = track.aim_point(cfg.aim_x_ratio, cfg.aim_y_ratio)
            aim_norm = (aim_px[0] / width, aim_px[1] / height)
            verdict = zones.evaluate(aim_norm)
            solution = solve(aim_norm[0], aim_norm[1], verdict.surface)
            candidate = Candidate(
                track=track,
                aim_norm=aim_norm,
                aim_px=aim_px,
                verdict=verdict,
                solution=solution,
            )
            candidate.rejected = self._rejection_reason(candidate, now, zones)
            candidate.score = self._score(candidate, width, height, now)
            candidates.append(candidate)

        eligible = [c for c in candidates if c.eligible]
        best = max(eligible, key=lambda c: c.score) if eligible else None
        return SelectionResult(best=best, candidates=candidates)

    # -- rules -----------------------------------------------------------
    def _rejection_reason(self, candidate: Candidate, now: float, zones: ZoneSet) -> str | None:
        cfg = self.settings
        track = candidate.track

        if cfg.target_classes and track.class_name.lower() not in {
            c.lower() for c in cfg.target_classes
        }:
            return "class not targeted"
        if not track.confirmed:
            return "track not confirmed"
        if track.confidence < cfg.min_confidence:
            return f"confidence below {cfg.min_confidence:.2f}"
        if track.duration_s(now) < cfg.min_track_duration_s:
            return f"tracked for less than {cfg.min_track_duration_s:.1f}s"
        if not candidate.verdict.targetable(cfg.require_active_zone, zones.has_active_zones):
            if candidate.verdict.in_no_target:
                return "in no-target zone"
            return "outside active zone"
        if candidate.solution is None:
            return "no calibration for this point"
        return None

    def _score(self, candidate: Candidate, width: float, height: float, now: float) -> float:
        """Higher is better. Only compared between eligible candidates."""
        track = candidate.track
        policy = self.settings.selection
        if policy == "largest":
            return track.area / (width * height)
        if policy == "oldest":
            return track.duration_s(now)
        if policy == "closest_to_center":
            cx, cy = candidate.aim_norm
            return -(((cx - 0.5) ** 2 + (cy - 0.5) ** 2) ** 0.5)
        return track.confidence
