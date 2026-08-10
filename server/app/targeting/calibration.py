"""Persistence-backed calibration service.

Owns the measured pixel↔angle correspondences, keeps a fitted
:class:`~app.targeting.mapping.MappingSet` per camera in memory, and answers
the one question the rest of the system asks: *given this point in the image,
where should the turret point?*

The fit is rebuilt only when points change, so the per-frame path is a couple
of small matrix solves.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.database.db import run_db
from app.database.models import CalibrationPoint
from app.logging_config import get_logger
from app.targeting.mapping import (
    AimSolution,
    CalibrationSample,
    MappingSet,
    Strategy,
    build_mapping_set,
)
from app.targeting.zones import ZoneSet

log = get_logger(__name__)


class CalibrationService:
    def __init__(self, strategy: Strategy = "local_linear") -> None:
        self.strategy: Strategy = strategy
        self._by_camera: dict[str, MappingSet] = {}
        self._lock = asyncio.Lock()

    # -- fitting ---------------------------------------------------------
    async def refresh(self) -> None:
        """Reload every calibration point and refit the models."""

        def _load(session: Any) -> list[dict[str, Any]]:
            stmt = select(CalibrationPoint).where(CalibrationPoint.enabled.is_(True))
            return [row.as_dict() for row in session.scalars(stmt).all()]

        rows = await run_db(_load)
        grouped: dict[str, list[CalibrationSample]] = {}
        for row in rows:
            grouped.setdefault(row["camera_id"], []).append(
                CalibrationSample(
                    cam_x=row["cam_x"],
                    cam_y=row["cam_y"],
                    pan_deg=row["pan_deg"],
                    tilt_deg=row["tilt_deg"],
                    surface=row["surface"] or "default",
                    id=row["id"],
                )
            )

        async with self._lock:
            self._by_camera = {
                camera_id: build_mapping_set(samples, self.strategy)
                for camera_id, samples in grouped.items()
            }
        log.info(
            "calibration refreshed",
            extra={
                "ctx": {
                    "cameras": len(self._by_camera),
                    "points": sum(len(v) for v in grouped.values()),
                }
            },
        )

    def mapping_for(self, camera_id: str) -> MappingSet | None:
        return self._by_camera.get(camera_id)

    def is_calibrated(self, camera_id: str) -> bool:
        mapping = self._by_camera.get(camera_id)
        return bool(mapping and mapping.is_calibrated)

    # -- queries ---------------------------------------------------------
    def solve(
        self,
        camera_id: str,
        x: float,
        y: float,
        *,
        zones: ZoneSet | None = None,
        surface: str | None = None,
    ) -> AimSolution | None:
        """Map a normalised image point to turret angles.

        The surface is taken from the argument, else from the zone the point
        falls in, else chosen by the mapping set itself.
        """
        mapping = self._by_camera.get(camera_id)
        if mapping is None or not mapping.is_calibrated:
            return None
        chosen = surface
        if chosen is None and zones is not None:
            chosen = zones.surface_at((x, y))
        if chosen is not None and chosen not in mapping.surfaces:
            # A zone exists for a surface that has no calibration points yet:
            # better to answer with the general fit than to refuse.
            chosen = None
        return mapping.solve(x, y, chosen)

    def angles_to_image(
        self, camera_id: str, pan_deg: float, tilt_deg: float
    ) -> tuple[float, float] | None:
        """Where the turret is pointing, in normalised image coordinates."""
        mapping = self._by_camera.get(camera_id)
        if mapping is None:
            return None
        model = mapping.get()
        if model is None:
            return None
        return model.angles_to_image(pan_deg, tilt_deg)

    def describe(self, camera_id: str) -> dict[str, Any]:
        mapping = self._by_camera.get(camera_id)
        return {
            "camera_id": camera_id,
            "calibrated": bool(mapping and mapping.is_calibrated),
            "strategy": self.strategy,
            "surfaces": mapping.describe() if mapping else [],
        }

    # -- CRUD ------------------------------------------------------------
    async def list_points(self, camera_id: str | None = None) -> list[dict[str, Any]]:
        def _list(session: Any) -> list[dict[str, Any]]:
            stmt = select(CalibrationPoint).order_by(CalibrationPoint.id)
            if camera_id:
                stmt = stmt.where(CalibrationPoint.camera_id == camera_id)
            return [row.as_dict() for row in session.scalars(stmt).all()]

        return await run_db(_list)

    async def add_point(
        self,
        *,
        camera_id: str,
        cam_x: float,
        cam_y: float,
        pan_deg: float,
        tilt_deg: float,
        surface: str = "default",
        label: str = "",
    ) -> dict[str, Any]:
        if not (0.0 <= cam_x <= 1.0 and 0.0 <= cam_y <= 1.0):
            raise ValueError("camera coordinates must be normalised to [0, 1]")

        def _add(session: Any) -> dict[str, Any]:
            point = CalibrationPoint(
                camera_id=camera_id,
                cam_x=cam_x,
                cam_y=cam_y,
                pan_deg=pan_deg,
                tilt_deg=tilt_deg,
                surface=surface or "default",
                label=label,
            )
            session.add(point)
            session.flush()
            return point.as_dict()

        created = await run_db(_add)
        await self.refresh()
        return created

    async def update_point(self, point_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"cam_x", "cam_y", "pan_deg", "tilt_deg", "surface", "label", "enabled"}

        def _update(session: Any) -> dict[str, Any]:
            point = session.get(CalibrationPoint, point_id)
            if point is None:
                raise KeyError(point_id)
            for key, value in changes.items():
                if key in allowed:
                    setattr(point, key, value)
            session.flush()
            return point.as_dict()

        updated = await run_db(_update)
        await self.refresh()
        return updated

    async def delete_point(self, point_id: int) -> None:
        def _delete(session: Any) -> None:
            point = session.get(CalibrationPoint, point_id)
            if point is None:
                raise KeyError(point_id)
            session.delete(point)

        await run_db(_delete)
        await self.refresh()

    async def clear(self, camera_id: str | None = None) -> int:
        def _clear(session: Any) -> int:
            stmt = select(CalibrationPoint)
            if camera_id:
                stmt = stmt.where(CalibrationPoint.camera_id == camera_id)
            rows = session.scalars(stmt).all()
            for row in rows:
                session.delete(row)
            return len(rows)

        removed = await run_db(_clear)
        await self.refresh()
        return removed
