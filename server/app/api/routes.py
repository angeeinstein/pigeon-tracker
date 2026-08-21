"""REST API.

Configuration and one-shot actions live here; anything that streams (telemetry,
video) is a WebSocket or an MJPEG response. Every request body is a Pydantic
model, so malformed input is rejected before it reaches the runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from app.api.auth import SESSION_COOKIE, check_credentials, create_token
from app.api.deps import AuthDep, ConfigDep, RuntimeDep
from app.camera.onvif import (
    OnvifError,
    discover_onvif,
    fetch_onvif_profiles,
    validate_private_device_url,
)
from app.services import event_log as ev
from app.services.settings import SettingsError
from app.services.settings_schema import SECTION_MODELS, CameraConfig
from app.targeting.zones import ZoneType
from app.turret.models import TurretError
from app.version import version_info

router = APIRouter(prefix="/api")
ONVIF_PROFILE_TIMEOUT_S = 20.0
DETECTOR_MODEL_UPLOAD_PATH = "/api/detector/models"
MAX_MODEL_UPLOAD_BYTES = 512 * 1024 * 1024
MODEL_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,122}\.pt", re.IGNORECASE)


def _turret_error(exc: TurretError) -> HTTPException:
    """Map a hardware refusal onto a meaningful HTTP status."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


# ==========================================================================
# health, version, system
# ==========================================================================


@router.get("/health", summary="Health probe used by the installer and systemd")
async def health(runtime: RuntimeDep) -> JSONResponse:
    payload = runtime.health()
    # 200 even when degraded: the process is alive and able to answer, which is
    # what a liveness check asks. Callers read `status` for readiness.
    return JSONResponse(payload)


@router.get("/version")
async def version() -> dict[str, Any]:
    return version_info()


@router.get("/system")
async def system(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    return runtime.system_info()


@router.get("/detector/catalog")
async def detector_catalog(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    """Loaded model classes plus warnings for incompatible saved filters."""
    return runtime.detector_catalog()


@router.post("/detector/models")
async def upload_detector_model(
    request: Request,
    runtime: RuntimeDep,
    _auth: AuthDep,
    filename: str = Query(min_length=4, max_length=128),
    overwrite: bool = Query(default=False),
) -> dict[str, Any]:
    """Install a locally trained PyTorch model without requiring server SSH."""
    if Path(filename).name != filename or not MODEL_FILENAME_RE.fullmatch(filename):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "model filename must use only letters, numbers, dots, dashes or underscores "
                "and end in .pt"
            ),
        )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared_size > MAX_MODEL_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="model exceeds the 512 MiB upload limit")

    models_dir = runtime.config.resolved_models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / filename
    configured = Path(runtime.settings.detector.model_path).name
    active_value = runtime.detector_catalog().get("active_model")
    protected_models = {configured}
    if active_value:
        protected_models.add(Path(str(active_value)).name)
    if target.exists() and not overwrite:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{filename} already exists; choose a versioned name or explicitly allow "
                "replacement"
            ),
        )
    if target.exists() and overwrite and filename in protected_models:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "the configured model cannot be replaced in place; upload it under a new "
                "versioned name"
            ),
        )

    temporary = models_dir / f".{filename}.{uuid4().hex}.upload"
    size = 0
    digest = hashlib.sha256()
    prefix = bytearray()
    try:
        with temporary.open("xb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_MODEL_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="model exceeds the 512 MiB upload limit",
                    )
                if len(prefix) < 4:
                    prefix.extend(chunk[: 4 - len(prefix)])
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0:
            raise HTTPException(status_code=422, detail="uploaded model is empty")
        if bytes(prefix) != b"PK\x03\x04":
            raise HTTPException(
                status_code=422,
                detail="file is not a modern PyTorch .pt checkpoint (ZIP signature missing)",
            )

        if overwrite:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"{filename} was installed by another request; choose a different name",
                ) from exc
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)

    await runtime.events.emit(
        ev.CAT_SYSTEM,
        "detector model uploaded",
        data={"filename": filename, "size_bytes": size, "sha256": digest.hexdigest()},
    )
    return {
        "filename": filename,
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "installed_models": runtime.installed_detector_models(),
    }


@router.get("/scene-motion/mask")
async def scene_motion_mask(runtime: RuntimeDep, _auth: AuthDep) -> Response:
    """Latest monochrome foreground mask used by motion-guided rescans."""
    try:
        data = runtime.render_motion_mask()
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ==========================================================================
# auth
# ==========================================================================


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@router.post("/auth/login")
async def login(payload: LoginRequest, response: Response, config: ConfigDep) -> dict[str, Any]:
    if not config.auth_enabled:
        return {"authenticated": True, "auth_enabled": False}
    if not config.auth_password:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="authentication is enabled but no password is configured",
        )
    if not check_credentials(
        config.auth_username, config.auth_password, payload.username, payload.password
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    token = create_token(config.resolve_secret_key(), payload.username)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 3600,
        path="/",
    )
    return {"authenticated": True, "auth_enabled": True, "token": token}


@router.post("/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request, config: ConfigDep) -> dict[str, Any]:
    """Whether the caller is authenticated — used by the UI to decide on a login screen."""
    if not config.auth_enabled:
        return {"auth_enabled": False, "authenticated": True, "username": None}
    from app.api.auth import verify_token
    from app.api.deps import _extract_token

    token = _extract_token(
        request.cookies, request.headers.get("authorization"), request.query_params.get("token")
    )
    session = verify_token(config.resolve_secret_key(), token or "")
    return {
        "auth_enabled": True,
        "authenticated": session is not None,
        "username": session.username if session else None,
    }


# ==========================================================================
# settings
# ==========================================================================


@router.get("/settings")
async def get_settings(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    return runtime.settings_store.as_dict()


@router.get("/settings-defaults")
async def get_settings_defaults(_auth: AuthDep) -> dict[str, Any]:
    """Return factory defaults separately from the currently saved values."""
    return {name: model().model_dump(mode="json") for name, model in SECTION_MODELS.items()}


@router.patch("/settings")
async def patch_settings(
    patch: dict[str, dict[str, Any]], runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    """Validate and save all edited sections as one atomic operation."""
    try:
        await runtime.settings_store.update(patch)
    except SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.events.emit(
        ev.CAT_SYSTEM,
        "settings updated",
        data={"sections": sorted(patch)},
    )
    return runtime.settings_store.as_dict()


@router.get("/settings/{section}")
async def get_settings_section(section: str, runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    if section not in SECTION_MODELS:
        raise HTTPException(status_code=404, detail=f"unknown settings section: {section}")
    return runtime.settings_store.section(section).model_dump(mode="json")


@router.patch("/settings/{section}")
async def patch_settings_section(
    section: str, patch: dict[str, Any], runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    try:
        updated = await runtime.settings_store.update_section(section, patch)
    except SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.events.emit(
        ev.CAT_SYSTEM, f"settings updated: {section}", data={"keys": sorted(patch)}
    )
    return updated.model_dump(mode="json")


@router.post("/settings/{section}/reset")
async def reset_settings_section(
    section: str, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    try:
        updated = await runtime.settings_store.reset_section(section)
    except SettingsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await runtime.events.emit(ev.CAT_SYSTEM, f"settings reset: {section}", level="warning")
    return updated.model_dump(mode="json")


@router.get("/settings-schema")
async def settings_schema(_auth: AuthDep) -> dict[str, Any]:
    """JSON Schema per section, so the UI can render forms generically."""
    return {name: model.model_json_schema() for name, model in SECTION_MODELS.items()}


# ==========================================================================
# control
# ==========================================================================


class ArmRequest(BaseModel):
    armed: bool


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pan_deg: float = Field(ge=-360, le=360)
    tilt_deg: float = Field(ge=-360, le=360)
    speed_deg_s: float | None = Field(default=None, gt=0, le=1000)


class MoveRelativeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pan_delta_deg: float = Field(ge=-360, le=360)
    tilt_delta_deg: float = Field(ge=-360, le=360)
    speed_deg_s: float | None = Field(default=None, gt=0, le=1000)


class JogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Joystick deflection per axis, -1 … 1.
    pan: float = Field(ge=-1.0, le=1.0)
    tilt: float = Field(ge=-1.0, le=1.0)


class HomeRequest(BaseModel):
    axes: Literal["both", "pan", "tilt"] = "both"


class SprayRequest(BaseModel):
    duration_ms: int | None = Field(default=None, ge=20, le=10_000)


class AimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Normalised image coordinates.
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    camera_id: str | None = None
    surface: str | None = None
    allow_extrapolation: bool = True


@router.post("/control/arm")
async def control_arm(payload: ArmRequest, runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.set_armed(payload.armed)
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/estop")
async def control_estop(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    # Never fails: an emergency stop that can return an error to the operator
    # instead of acting is not an emergency stop.
    return await runtime.emergency_stop()


@router.post("/control/estop/clear")
async def control_estop_clear(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.clear_emergency_stop()
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/home")
async def control_home(payload: HomeRequest, runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.home(payload.axes)
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/center")
async def control_center(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.center()
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/move")
async def control_move(payload: MoveRequest, runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.move_to(payload.pan_deg, payload.tilt_deg, payload.speed_deg_s)
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/move_relative")
async def control_move_relative(
    payload: MoveRelativeRequest, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    try:
        return await runtime.move_relative(
            payload.pan_delta_deg, payload.tilt_delta_deg, payload.speed_deg_s
        )
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/jog")
async def control_jog(payload: JogRequest, runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.jog(payload.pan, payload.tilt)
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/stop")
async def control_stop(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.stop_motion()
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/spray")
async def control_spray(
    payload: SprayRequest, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    try:
        return await runtime.manual_spray(payload.duration_ms)
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/spray/stop")
async def control_spray_stop(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.spray_stop()
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/aim")
async def control_aim(payload: AimRequest, runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        return await runtime.aim_at_image_point(
            payload.x,
            payload.y,
            payload.camera_id,
            payload.surface,
            payload.allow_extrapolation,
        )
    except TurretError as exc:
        raise _turret_error(exc) from exc


@router.post("/control/config/push")
async def control_push_config(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        await runtime.turret.push_config()
    except TurretError as exc:
        raise _turret_error(exc) from exc
    return {"ok": True}


@router.get("/control/status")
async def control_status(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    return runtime.telemetry_snapshot()


# ==========================================================================
# calibration
# ==========================================================================


class CalibrationPointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    #: Omit to capture the turret's current position (the normal workflow:
    #: jog until the nozzle points at the spot, then save).
    pan_deg: float | None = Field(default=None, ge=-360, le=360)
    tilt_deg: float | None = Field(default=None, ge=-360, le=360)
    surface: str = Field(default="default", max_length=64)
    label: str = Field(default="", max_length=128)
    camera_id: str | None = None


class CalibrationPointUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cam_x: float | None = Field(default=None, ge=0.0, le=1.0)
    cam_y: float | None = Field(default=None, ge=0.0, le=1.0)
    pan_deg: float | None = Field(default=None, ge=-360, le=360)
    tilt_deg: float | None = Field(default=None, ge=-360, le=360)
    surface: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None


@router.get("/calibration/points")
async def list_calibration_points(
    runtime: RuntimeDep, _auth: AuthDep, camera_id: str | None = None
) -> list[dict[str, Any]]:
    return await runtime.calibration.list_points(runtime.calibration_camera_id(camera_id))


@router.post("/calibration/points", status_code=201)
async def create_calibration_point(
    payload: CalibrationPointRequest, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    camera_id = payload.camera_id or runtime.settings.cameras.primary_id
    pan = payload.pan_deg
    tilt = payload.tilt_deg
    if pan is None or tilt is None:
        if not runtime.turret.connected:
            raise HTTPException(
                status_code=409,
                detail="controller not connected: supply pan_deg/tilt_deg explicitly",
            )
        pan = runtime.turret.state.pan_deg if pan is None else pan
        tilt = runtime.turret.state.tilt_deg if tilt is None else tilt

    created = await runtime.calibration.add_point(
        camera_id=runtime.calibration_camera_id(camera_id),
        cam_x=payload.x,
        cam_y=payload.y,
        pan_deg=pan,
        tilt_deg=tilt,
        surface=payload.surface,
        label=payload.label,
    )
    await runtime.events.emit(ev.CAT_TARGETING, "calibration point saved", data=created)
    return created


@router.patch("/calibration/points/{point_id}")
async def update_calibration_point(
    point_id: int, payload: CalibrationPointUpdate, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="no changes supplied")
    try:
        return await runtime.calibration.update_point(point_id, changes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="calibration point not found") from exc


@router.delete("/calibration/points/{point_id}", status_code=204)
async def delete_calibration_point(point_id: int, runtime: RuntimeDep, _auth: AuthDep) -> Response:
    try:
        await runtime.calibration.delete_point(point_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="calibration point not found") from exc
    return Response(status_code=204)


@router.delete("/calibration/points")
async def clear_calibration_points(
    runtime: RuntimeDep, _auth: AuthDep, camera_id: str | None = None
) -> dict[str, int]:
    removed = await runtime.calibration.clear(runtime.calibration_camera_id(camera_id))
    await runtime.events.emit(
        ev.CAT_TARGETING, "calibration cleared", level="warning", data={"removed": removed}
    )
    return {"removed": removed}


@router.get("/calibration/model")
async def calibration_model(
    runtime: RuntimeDep, _auth: AuthDep, camera_id: str | None = None
) -> dict[str, Any]:
    return runtime.calibration_description(camera_id)


@router.get("/calibration/solve")
async def calibration_solve(
    runtime: RuntimeDep,
    _auth: AuthDep,
    x: float = Query(ge=0.0, le=1.0),
    y: float = Query(ge=0.0, le=1.0),
    camera_id: str | None = None,
    surface: str | None = None,
) -> dict[str, Any]:
    """Preview where a click would aim, without moving anything."""
    solution = runtime.solve_image_point(x, y, camera_id, surface)
    if solution is None:
        raise HTTPException(status_code=409, detail="no calibration available")
    return solution.as_dict()


# ==========================================================================
# zones
# ==========================================================================


class ZoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=128)
    zone_type: str
    points: list[list[float]] = Field(min_length=3, max_length=128)
    camera_id: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    enabled: bool = True


class ZoneUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=128)
    zone_type: str | None = None
    points: list[list[float]] | None = Field(default=None, min_length=3, max_length=128)
    priority: int | None = Field(default=None, ge=-100, le=100)
    enabled: bool | None = None


@router.get("/zones/types")
async def zone_types() -> list[dict[str, Any]]:
    return [{"value": zone.value, "is_surface": zone.is_surface} for zone in ZoneType]


@router.get("/zones")
async def list_zones(
    runtime: RuntimeDep, _auth: AuthDep, camera_id: str | None = None
) -> list[dict[str, Any]]:
    return await runtime.zones.list(camera_id)


@router.post("/zones", status_code=201)
async def create_zone(payload: ZoneRequest, runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    try:
        created = await runtime.zones.create(
            name=payload.name,
            zone_type=payload.zone_type,
            points=payload.points,
            camera_id=payload.camera_id or runtime.settings.cameras.primary_id,
            priority=payload.priority,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.events.emit(
        ev.CAT_TARGETING, f"zone created: {payload.name}", data={"type": payload.zone_type}
    )
    return created


@router.patch("/zones/{zone_id}")
async def update_zone(
    zone_id: int, payload: ZoneUpdate, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="no changes supplied")
    try:
        return await runtime.zones.update(zone_id, changes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="zone not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/zones/{zone_id}", status_code=204)
async def delete_zone(zone_id: int, runtime: RuntimeDep, _auth: AuthDep) -> Response:
    try:
        await runtime.zones.delete(zone_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="zone not found") from exc
    await runtime.events.emit(ev.CAT_TARGETING, "zone deleted", data={"id": zone_id})
    return Response(status_code=204)


# ==========================================================================
# presets
# ==========================================================================


class PresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=128)
    #: Omit to capture the current position.
    pan_deg: float | None = Field(default=None, ge=-360, le=360)
    tilt_deg: float | None = Field(default=None, ge=-360, le=360)


@router.get("/presets")
async def list_presets(runtime: RuntimeDep, _auth: AuthDep) -> list[dict[str, Any]]:
    from app.database.db import run_db
    from app.database.models import Preset

    return await run_db(
        lambda session: [p.as_dict() for p in session.query(Preset).order_by(Preset.name).all()]
    )


@router.post("/presets", status_code=201)
async def create_preset(
    payload: PresetRequest, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    from app.database.db import run_db
    from app.database.models import Preset

    pan = payload.pan_deg if payload.pan_deg is not None else runtime.turret.state.pan_deg
    tilt = payload.tilt_deg if payload.tilt_deg is not None else runtime.turret.state.tilt_deg

    def _create(session: Any) -> dict[str, Any]:
        existing = session.query(Preset).filter(Preset.name == payload.name).one_or_none()
        if existing is not None:
            existing.pan_deg, existing.tilt_deg = pan, tilt
            session.flush()
            return existing.as_dict()
        preset = Preset(name=payload.name, pan_deg=pan, tilt_deg=tilt)
        session.add(preset)
        session.flush()
        return preset.as_dict()

    return await run_db(_create)


@router.delete("/presets/{preset_id}", status_code=204)
async def delete_preset(preset_id: int, runtime: RuntimeDep, _auth: AuthDep) -> Response:
    from app.database.db import run_db
    from app.database.models import Preset

    def _delete(session: Any) -> bool:
        preset = session.get(Preset, preset_id)
        if preset is None:
            return False
        session.delete(preset)
        return True

    if not await run_db(_delete):
        raise HTTPException(status_code=404, detail="preset not found")
    return Response(status_code=204)


@router.post("/presets/{preset_id}/goto")
async def goto_preset(preset_id: int, runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    from app.database.db import run_db
    from app.database.models import Preset

    def _get(session: Any) -> dict[str, Any] | None:
        preset = session.get(Preset, preset_id)
        return preset.as_dict() if preset is not None else None

    preset = await run_db(_get)
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    try:
        return await runtime.move_to(preset["pan_deg"], preset["tilt_deg"])
    except TurretError as exc:
        raise _turret_error(exc) from exc


# ==========================================================================
# detection captures
# ==========================================================================


class DetectionCaptureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_status: Literal["unreviewed", "training", "rejected"]
    review_label: str = Field(default="", max_length=128)


class DetectionAnnotationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_status: Literal["unreviewed", "accepted", "rejected"]
    review_label: str = Field(default="", max_length=128)


class DetectionAnnotationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bbox: tuple[float, float, float, float]
    class_name: str = Field(default="bird", min_length=1, max_length=128)


@router.get("/detection-captures")
async def list_detection_captures(
    runtime: RuntimeDep,
    _auth: AuthDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    review_status: Literal["unreviewed", "training", "rejected"] | None = None,
    class_name: str | None = Query(default=None, max_length=128),
) -> list[dict[str, object]]:
    return await runtime.detection_captures.list(
        limit=limit,
        offset=offset,
        review_status=review_status,
        class_name=class_name,
    )


@router.get("/detection-captures/page")
async def page_detection_captures(
    runtime: RuntimeDep,
    _auth: AuthDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    review_status: Literal["unreviewed", "training", "rejected"] | None = None,
    class_name: str | None = Query(default=None, max_length=128),
) -> dict[str, object]:
    return await runtime.detection_captures.page(
        limit=limit,
        offset=offset,
        review_status=review_status,
        class_name=class_name,
    )


@router.post("/detection-captures/manual", status_code=status.HTTP_201_CREATED)
async def save_manual_detection_capture(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, object]:
    capture = await runtime.save_manual_detection_capture()
    if capture is None:
        raise HTTPException(status_code=503, detail="primary camera has no frame")
    return capture


@router.patch("/detection-captures/{capture_id}")
async def review_detection_capture(
    capture_id: int,
    payload: DetectionCaptureReviewRequest,
    runtime: RuntimeDep,
    _auth: AuthDep,
) -> dict[str, object]:
    capture = await runtime.detection_captures.update_review(
        capture_id,
        review_status=payload.review_status,
        review_label=payload.review_label,
    )
    if capture is None:
        raise HTTPException(status_code=404, detail="detection capture not found")
    return capture


@router.get("/detection-captures/{capture_id}/navigate")
async def navigate_detection_captures(
    capture_id: int,
    runtime: RuntimeDep,
    _auth: AuthDep,
    direction: Literal["current", "previous", "next"] = "current",
    review_status: Literal["unreviewed", "training", "rejected"] | None = None,
    class_name: str | None = Query(default=None, max_length=128),
) -> dict[str, object]:
    result = await runtime.detection_captures.navigate(
        capture_id,
        direction=direction,
        review_status=review_status,
        class_name=class_name,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="no matching detection capture")
    return result


@router.patch("/detection-captures/{capture_id}/annotations/{annotation_index}")
async def review_detection_annotation(
    capture_id: int,
    annotation_index: int,
    payload: DetectionAnnotationReviewRequest,
    runtime: RuntimeDep,
    _auth: AuthDep,
) -> dict[str, object]:
    try:
        capture = await runtime.detection_captures.review_annotation(
            capture_id,
            annotation_index,
            review_status=payload.review_status,
            review_label=payload.review_label,
        )
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if capture is None:
        raise HTTPException(status_code=404, detail="detection capture not found")
    return capture


@router.post("/detection-captures/{capture_id}/annotations", status_code=status.HTTP_201_CREATED)
async def add_detection_annotation(
    capture_id: int,
    payload: DetectionAnnotationCreateRequest,
    runtime: RuntimeDep,
    _auth: AuthDep,
) -> dict[str, object]:
    try:
        capture = await runtime.detection_captures.add_annotation(
            capture_id,
            bbox=list(payload.bbox),
            class_name=payload.class_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if capture is None:
        raise HTTPException(status_code=404, detail="detection capture not found")
    return capture


@router.delete("/detection-captures/{capture_id}/annotations/{annotation_index}")
async def delete_detection_annotation(
    capture_id: int,
    annotation_index: int,
    runtime: RuntimeDep,
    _auth: AuthDep,
) -> dict[str, object]:
    try:
        capture = await runtime.detection_captures.delete_annotation(capture_id, annotation_index)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if capture is None:
        raise HTTPException(status_code=404, detail="detection capture not found")
    return capture


@router.post("/detection-captures/{capture_id}/annotations/reject-unreviewed")
async def reject_unreviewed_detection_annotations(
    capture_id: int, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, object]:
    capture = await runtime.detection_captures.reject_unreviewed_annotations(capture_id)
    if capture is None:
        raise HTTPException(status_code=404, detail="detection capture not found")
    return capture


@router.get("/detection-captures/export/yolo.zip")
async def export_detection_captures(runtime: RuntimeDep, _auth: AuthDep) -> FileResponse:
    try:
        path, summary = await runtime.detection_captures.export_yolo()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"pigeon-dataset-{time.strftime('%Y%m%d-%H%M%S')}.zip",
        headers={
            "X-Dataset-Images": str(summary["images"]),
            "X-Dataset-Boxes": str(summary["boxes"]),
        },
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.delete("/detection-captures/{capture_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_detection_capture(
    capture_id: int, runtime: RuntimeDep, _auth: AuthDep
) -> Response:
    if not await runtime.detection_captures.delete(capture_id):
        raise HTTPException(status_code=404, detail="detection capture not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/detection-captures/{capture_id}/image")
async def get_detection_capture_image(
    capture_id: int, runtime: RuntimeDep, _auth: AuthDep
) -> FileResponse:
    path = await runtime.detection_captures.image_for(capture_id)
    if path is None:
        raise HTTPException(status_code=404, detail="detection capture image not found")
    return FileResponse(path, media_type="image/jpeg")


# ==========================================================================
# events & snapshots
# ==========================================================================


@router.get("/events")
async def list_events(
    runtime: RuntimeDep,
    _auth: AuthDep,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    category: str | None = None,
    level: str | None = None,
) -> list[dict[str, Any]]:
    return await runtime.events.query(limit=limit, offset=offset, category=category, level=level)


@router.get("/events/categories")
async def event_categories() -> list[str]:
    return list(ev.CATEGORIES)


@router.get("/snapshots/{name}")
async def get_snapshot(name: str, runtime: RuntimeDep, _auth: AuthDep) -> FileResponse:
    directory = runtime.config.resolved_snapshot_dir.resolve()
    # Resolve and confine: the name comes from a URL and is never trusted.
    path = (directory / name).resolve()
    if not str(path).startswith(str(directory)) or not path.is_file():
        raise HTTPException(status_code=404, detail="snapshot not found")
    return FileResponse(path, media_type="image/jpeg")


# ==========================================================================
# camera / preview
# ==========================================================================


@router.get("/cameras")
async def cameras(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    return runtime.cameras.status_dict()


class OnvifProfilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    xaddr: str = Field(max_length=1024)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)


class CameraOnboardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=128)
    role: Literal["overview", "turret", "aux"] = "overview"
    uri: str = Field(max_length=1024)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)
    make_primary: bool = True


class CameraCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(default="", max_length=128)
    # null means preserve the current password; an empty string deliberately clears it.
    password: str | None = Field(default=None, max_length=256)


@router.post("/cameras/discover")
async def cameras_discover(
    runtime: RuntimeDep,
    _auth: AuthDep,
    timeout_s: int = Query(default=4, ge=1, le=10),
) -> dict[str, Any]:
    try:
        devices = await asyncio.to_thread(discover_onvif, timeout_s)
    except OnvifError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "devices": devices,
        "note": (
            "WS-Discovery only reaches the server's local network segment; "
            "enter a device service URL manually when cameras are on another subnet."
        ),
    }


@router.post("/cameras/onvif/profiles")
async def camera_onvif_profiles(
    payload: OnvifProfilesRequest, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                fetch_onvif_profiles, payload.xaddr, payload.username, payload.password
            ),
            timeout=ONVIF_PROFILE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"camera did not answer within {ONVIF_PROFILE_TIMEOUT_S:g} seconds; "
                "check the ONVIF address, "
                "port and network route"
            ),
        ) from exc
    except OnvifError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cameras/onboard", status_code=status.HTTP_201_CREATED)
async def camera_onboard(
    payload: CameraOnboardRequest, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    if runtime.settings.cameras.get(payload.id) is not None:
        raise HTTPException(status_code=409, detail=f"camera id already exists: {payload.id}")
    try:
        await asyncio.to_thread(validate_private_device_url, payload.uri, {"rtsp", "rtsps"})
        camera = CameraConfig(
            id=payload.id,
            name=payload.name,
            url=payload.uri,
            enabled=True,
            role=payload.role,
        )
    except (OnvifError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    factory_source = CameraConfig().model_dump(mode="json")
    sources = [
        item.model_dump(mode="json")
        for item in runtime.settings.cameras.sources
        if item.model_dump(mode="json") != factory_source
    ]
    if len(sources) >= 8:
        raise HTTPException(status_code=409, detail="at most 8 camera sources are supported")
    sources.append(camera.model_dump(mode="json"))
    previous = runtime.camera_credentials.get(payload.id)
    if payload.username or payload.password:
        runtime.camera_credentials.set(payload.id, payload.username, payload.password)
    patch: dict[str, Any] = {"sources": sources}
    if payload.make_primary:
        patch["primary_id"] = payload.id
    try:
        updated = await runtime.settings_store.update_section("cameras", patch)
    except SettingsError as exc:
        if previous is None:
            runtime.camera_credentials.remove(payload.id)
        else:
            runtime.camera_credentials.set(payload.id, previous.username, previous.password)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.events.emit(
        ev.CAT_SYSTEM,
        "camera added via ONVIF",
        data={"camera_id": payload.id, "role": payload.role},
    )
    return updated.model_dump(mode="json")


@router.get("/cameras/credentials")
async def camera_credentials(runtime: RuntimeDep, _auth: AuthDep) -> dict[str, Any]:
    return runtime.camera_credentials.status()


@router.put("/cameras/{camera_id}/credentials")
async def update_camera_credentials(
    camera_id: str,
    payload: CameraCredentialRequest,
    runtime: RuntimeDep,
    _auth: AuthDep,
) -> dict[str, Any]:
    if runtime.settings.cameras.get(camera_id) is None:
        raise HTTPException(status_code=404, detail="camera not found")
    try:
        runtime.camera_credentials.set(camera_id, payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.cameras.restart(camera_id)
    return runtime.camera_credentials.status(camera_id)


@router.delete("/cameras/{camera_id}/credentials")
async def delete_camera_credentials(
    camera_id: str, runtime: RuntimeDep, _auth: AuthDep
) -> dict[str, Any]:
    if runtime.settings.cameras.get(camera_id) is None:
        raise HTTPException(status_code=404, detail="camera not found")
    runtime.camera_credentials.remove(camera_id)
    await runtime.cameras.restart(camera_id)
    return runtime.camera_credentials.status(camera_id)


@router.get("/camera/snapshot.jpg")
async def camera_snapshot(
    runtime: RuntimeDep,
    _auth: AuthDep,
    camera_id: str | None = None,
    overlays: bool | None = None,
) -> Response:
    try:
        data = runtime.render_preview(camera_id, overlays)
    except LookupError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=data, media_type="image/jpeg")


@router.get("/camera/stream.mjpg")
async def camera_stream(
    runtime: RuntimeDep,
    _auth: AuthDep,
    camera_id: str | None = None,
    overlays: bool | None = None,
) -> StreamingResponse:
    """MJPEG preview.

    Works in any browser with a plain ``<img>`` tag and needs no JavaScript,
    which makes it the reliable fallback when a phone struggles with the
    WebSocket preview.
    """
    boundary = "turretframe"

    async def generate() -> Any:
        import asyncio

        last_seq = -1
        while True:
            ui = runtime.settings.ui
            interval = 1.0 / max(1.0, ui.preview_fps)
            started = time.monotonic()
            source = runtime.cameras.get(camera_id)
            frame = source.latest() if source else None
            if frame is not None and frame.seq != last_seq:
                last_seq = frame.seq
                try:
                    payload = await asyncio.to_thread(runtime.render_preview, camera_id, overlays)
                except LookupError:
                    payload = None
                if payload:
                    yield (
                        (
                            f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(payload)}\r\n\r\n"
                        ).encode()
                        + payload
                        + b"\r\n"
                    )
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )
