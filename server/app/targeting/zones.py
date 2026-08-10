"""Zones: polygons on the camera image that change what the system may do.

Two kinds of zone, deliberately separated:

* **Rules** — ``active`` (engage only here), ``no_target`` (never engage) and
  ``no_spray`` (never open the valve). A rule zone answers "is this allowed?".
* **Surfaces** — ``railing``, ``planter``, ``floor``. A surface zone answers
  "which calibration surface does this pixel belong to?", which is what makes
  the pixel→angle mapping correct when the balcony is not a single plane.

All coordinates are normalised to ``[0, 1]`` so zones survive resolution and
preview-scale changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.targeting.geometry import Point, point_in_polygon, polygon_area, polygon_centroid


class ZoneType(str, Enum):
    ACTIVE = "active"
    NO_TARGET = "no_target"
    NO_SPRAY = "no_spray"
    RAILING = "railing"
    PLANTER = "planter"
    FLOOR = "floor"

    @property
    def is_surface(self) -> bool:
        return self in _SURFACE_TYPES


_SURFACE_TYPES = {ZoneType.RAILING, ZoneType.PLANTER, ZoneType.FLOOR}

#: Surface zone type -> calibration surface name. They match today, but the
#: indirection keeps zone naming (a UI concern) separate from calibration
#: surface naming (a geometry concern).
SURFACE_NAMES: dict[ZoneType, str] = {
    ZoneType.RAILING: "railing",
    ZoneType.PLANTER: "planter",
    ZoneType.FLOOR: "floor",
}


@dataclass(frozen=True)
class Zone:
    id: int
    name: str
    zone_type: ZoneType
    points: tuple[Point, ...]
    enabled: bool = True
    priority: int = 0
    camera_id: str = "overview"

    @property
    def valid(self) -> bool:
        return len(self.points) >= 3 and polygon_area(self.points) > 1e-6

    @property
    def centroid(self) -> Point:
        return polygon_centroid(self.points)

    def contains(self, point: Point) -> bool:
        return self.valid and point_in_polygon(point, self.points)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "zone_type": self.zone_type.value,
            "points": [[round(x, 5), round(y, 5)] for x, y in self.points],
            "enabled": self.enabled,
            "priority": self.priority,
            "camera_id": self.camera_id,
        }

    @classmethod
    def from_record(cls, record: Any) -> Zone:
        """Build from a :class:`app.database.models.Zone` row."""
        return cls(
            id=int(record.id),
            name=str(record.name),
            zone_type=ZoneType(record.zone_type),
            points=tuple((float(p[0]), float(p[1])) for p in (record.points or [])),
            enabled=bool(record.enabled),
            priority=int(record.priority),
            camera_id=str(record.camera_id),
        )


@dataclass(frozen=True)
class ZoneVerdict:
    """What the zone layer says about one image point."""

    in_active: bool
    in_no_target: bool
    in_no_spray: bool
    surface: str | None
    matched: tuple[str, ...] = field(default_factory=tuple)

    #: True when the point may be *engaged* (tracked and aimed at).
    def targetable(self, require_active_zone: bool, active_zones_exist: bool) -> bool:
        if self.in_no_target:
            return False
        return not (require_active_zone and active_zones_exist and not self.in_active)

    @property
    def spray_allowed(self) -> bool:
        return not self.in_no_spray

    def as_dict(self) -> dict[str, Any]:
        return {
            "in_active": self.in_active,
            "in_no_target": self.in_no_target,
            "in_no_spray": self.in_no_spray,
            "surface": self.surface,
            "matched": list(self.matched),
        }


class ZoneSet:
    """Evaluates a set of zones for one camera."""

    def __init__(self, zones: Iterable[Zone] = ()) -> None:
        self.zones: list[Zone] = [z for z in zones if z.enabled and z.valid]
        self._active = [z for z in self.zones if z.zone_type is ZoneType.ACTIVE]
        self._no_target = [z for z in self.zones if z.zone_type is ZoneType.NO_TARGET]
        self._no_spray = [z for z in self.zones if z.zone_type is ZoneType.NO_SPRAY]
        # Highest priority first, then smallest area: a small planter drawn
        # inside a big floor zone should win.
        self._surfaces = sorted(
            (z for z in self.zones if z.zone_type.is_surface),
            key=lambda z: (-z.priority, polygon_area(z.points)),
        )

    def __len__(self) -> int:
        return len(self.zones)

    @property
    def has_active_zones(self) -> bool:
        return bool(self._active)

    def surface_at(self, point: Point) -> str | None:
        for zone in self._surfaces:
            if zone.contains(point):
                return SURFACE_NAMES.get(zone.zone_type, zone.zone_type.value)
        return None

    def evaluate(self, point: Point) -> ZoneVerdict:
        matched = tuple(z.name for z in self.zones if z.contains(point))
        return ZoneVerdict(
            in_active=any(z.contains(point) for z in self._active),
            in_no_target=any(z.contains(point) for z in self._no_target),
            in_no_spray=any(z.contains(point) for z in self._no_spray),
            surface=self.surface_at(point),
            matched=matched,
        )

    def as_dicts(self) -> list[dict[str, Any]]:
        return [z.as_dict() for z in self.zones]


class ZoneService:
    """Persistence-backed zone store with an in-memory :class:`ZoneSet` cache.

    Kept in this module (rather than in a generic repository) so the polygon
    rules and their storage stay next to each other; everything above this line
    is pure and unit-tested without a database.
    """

    def __init__(self) -> None:
        self._by_camera: dict[str, ZoneSet] = {}

    async def refresh(self) -> None:
        from app.database.db import run_db
        from app.database.models import Zone as ZoneRecord

        def _load(session: Any) -> list[Zone]:
            return [Zone.from_record(row) for row in session.query(ZoneRecord).all()]

        zones = await run_db(_load)
        grouped: dict[str, list[Zone]] = {}
        for zone in zones:
            grouped.setdefault(zone.camera_id, []).append(zone)
        self._by_camera = {camera: ZoneSet(items) for camera, items in grouped.items()}

    def for_camera(self, camera_id: str) -> ZoneSet:
        return self._by_camera.get(camera_id, ZoneSet())

    async def list(self, camera_id: str | None = None) -> list[dict[str, Any]]:
        from app.database.db import run_db
        from app.database.models import Zone as ZoneRecord

        def _list(session: Any) -> list[dict[str, Any]]:
            query = session.query(ZoneRecord).order_by(ZoneRecord.id)
            if camera_id:
                query = query.filter(ZoneRecord.camera_id == camera_id)
            return [row.as_dict() for row in query.all()]

        return await run_db(_list)

    async def create(
        self,
        *,
        name: str,
        zone_type: str,
        points: Sequence[Sequence[float]],
        camera_id: str = "overview",
        priority: int = 0,
        enabled: bool = True,
    ) -> dict[str, Any]:
        from app.database.db import run_db
        from app.database.models import Zone as ZoneRecord

        cleaned = validate_points(points)
        parsed_type = ZoneType(zone_type)

        def _create(session: Any) -> dict[str, Any]:
            record = ZoneRecord(
                name=name.strip() or parsed_type.value,
                zone_type=parsed_type.value,
                points=cleaned,
                camera_id=camera_id,
                priority=priority,
                enabled=enabled,
            )
            session.add(record)
            session.flush()
            return record.as_dict()

        created = await run_db(_create)
        await self.refresh()
        return created

    async def update(self, zone_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        from app.database.db import run_db
        from app.database.models import Zone as ZoneRecord

        payload = dict(changes)
        if "points" in payload:
            payload["points"] = validate_points(payload["points"])
        if "zone_type" in payload:
            payload["zone_type"] = ZoneType(payload["zone_type"]).value

        def _update(session: Any) -> dict[str, Any]:
            record = session.get(ZoneRecord, zone_id)
            if record is None:
                raise KeyError(zone_id)
            for key in ("name", "zone_type", "points", "priority", "enabled", "camera_id"):
                if key in payload:
                    setattr(record, key, payload[key])
            session.flush()
            return record.as_dict()

        updated = await run_db(_update)
        await self.refresh()
        return updated

    async def delete(self, zone_id: int) -> None:
        from app.database.db import run_db
        from app.database.models import Zone as ZoneRecord

        def _delete(session: Any) -> None:
            record = session.get(ZoneRecord, zone_id)
            if record is None:
                raise KeyError(zone_id)
            session.delete(record)

        await run_db(_delete)
        await self.refresh()


def validate_points(points: Sequence[Sequence[float]]) -> list[list[float]]:
    """Validate and normalise polygon points coming from the UI.

    Rejects anything that is not a simple list of at least three in-range
    coordinate pairs — this data ends up deciding whether water is allowed to
    flow, so it does not get the benefit of the doubt.
    """
    if len(points) < 3:
        raise ValueError("a zone needs at least 3 points")
    if len(points) > 128:
        raise ValueError("a zone may not have more than 128 points")
    cleaned: list[list[float]] = []
    for point in points:
        if len(point) != 2:
            raise ValueError("each point must be [x, y]")
        x, y = float(point[0]), float(point[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("zone points must be normalised to [0, 1]")
        cleaned.append([x, y])
    if polygon_area([(p[0], p[1]) for p in cleaned]) <= 1e-6:
        raise ValueError("zone polygon has (nearly) zero area")
    return cleaned
