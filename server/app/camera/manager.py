"""Camera manager: owns every named camera source and reacts to settings changes."""

from __future__ import annotations

import asyncio
from typing import Any

from app.camera.base import CameraSource, CameraStatus, Frame
from app.camera.rtsp import RtspCameraSource
from app.camera.simulated import SimulatedCameraSource
from app.logging_config import get_logger
from app.services.settings_schema import CameraConfig, CamerasSettings

log = get_logger(__name__)


def create_source(config: CameraConfig, *, force_simulated: bool = False) -> CameraSource:
    """Build the source implementation for a camera configuration.

    A camera with no URL falls back to the simulated source instead of failing:
    a fresh install should show *something* in the preview so the rest of the
    UI can be set up before the camera exists.
    """
    if force_simulated or config.backend == "simulated" or not config.url.strip():
        return SimulatedCameraSource(config)
    return RtspCameraSource(config)


class CameraManager:
    def __init__(self, *, force_simulated: bool = False) -> None:
        self._sources: dict[str, CameraSource] = {}
        self._settings = CamerasSettings()
        self._force_simulated = force_simulated
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------
    async def apply(self, settings: CamerasSettings) -> None:
        """Reconcile running sources with the configuration.

        Only cameras whose configuration actually changed are restarted, so
        editing an unrelated setting never interrupts the live view.
        """
        async with self._lock:
            self._settings = settings
            wanted = {c.id: c for c in settings.sources if c.enabled}

            for camera_id in list(self._sources):
                if camera_id not in wanted:
                    await self._stop_source(camera_id)

            for camera_id, config in wanted.items():
                existing = self._sources.get(camera_id)
                if existing is not None and existing.config == config:
                    continue
                if existing is not None:
                    await self._stop_source(camera_id)
                source = create_source(config, force_simulated=self._force_simulated)
                await asyncio.to_thread(source.start)
                self._sources[camera_id] = source

    async def stop_all(self) -> None:
        async with self._lock:
            for camera_id in list(self._sources):
                await self._stop_source(camera_id)

    async def _stop_source(self, camera_id: str) -> None:
        source = self._sources.pop(camera_id, None)
        if source is not None:
            await asyncio.to_thread(source.stop)

    # -- access ----------------------------------------------------------
    @property
    def primary_id(self) -> str:
        return self._settings.primary_id

    def get(self, camera_id: str | None = None) -> CameraSource | None:
        if camera_id is None:
            camera_id = self._settings.primary_id
        return self._sources.get(camera_id)

    def latest(self, camera_id: str | None = None) -> Frame | None:
        source = self.get(camera_id)
        return source.latest() if source else None

    def statuses(self) -> list[CameraStatus]:
        configured = {c.id: c for c in self._settings.sources}
        result: list[CameraStatus] = []
        for camera_id, config in configured.items():
            source = self._sources.get(camera_id)
            if source is not None:
                result.append(source.snapshot_status())
            else:
                result.append(
                    CameraStatus(
                        camera_id=camera_id,
                        name=config.name,
                        enabled=config.enabled,
                        connected=False,
                        error=None if config.enabled else "disabled",
                    )
                )
        return result

    def status_dict(self) -> dict[str, Any]:
        statuses = [s.as_dict() for s in self.statuses()]
        primary = next((s for s in statuses if s["camera_id"] == self.primary_id), None)
        return {
            "primary_id": self.primary_id,
            "connected": bool(primary and primary["connected"]),
            "cameras": statuses,
        }

    @property
    def any_connected(self) -> bool:
        return any(source.status.connected for source in self._sources.values())
