"""Runtime: the object that owns and wires every subsystem.

This is the only module that knows about all of them. Routes, WebSockets and
background loops all go through it, which keeps the arming rules, the safety
checks and the event logging in one place instead of scattered across handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.camera.credentials import CameraCredentialStore
from app.camera.manager import CameraManager
from app.camera.rtsp import encode_jpeg, safe_filename
from app.config import DeploymentConfig
from app.database.db import database_status
from app.logging_config import get_logger
from app.services import event_log as ev
from app.services.event_log import EventLog, prune_snapshots
from app.services.settings import SettingsStore
from app.services.settings_schema import AppSettings
from app.services.telemetry import TelemetryHub
from app.targeting.calibration import CalibrationService
from app.targeting.mapping import AimSolution
from app.targeting.spray_guard import SprayGuard
from app.targeting.state_machine import (
    Action,
    ActionKind,
    AutoState,
    TargetingStateMachine,
    TickContext,
)
from app.targeting.target_selector import SelectionResult, TargetSelector
from app.targeting.zones import ZoneService
from app.turret.manager import TurretManager
from app.turret.models import TurretError
from app.turret.simulator import SimulatedController
from app.version import version_info
from app.vision.overlays import render_overlay
from app.vision.pipeline import VisionPipeline

log = get_logger(__name__)


class Runtime:
    #: Targeting decisions per second. Faster than the detector on purpose:
    #: timeouts and aim convergence are checked against the clock, not frames.
    TARGETING_HZ = 10.0

    def __init__(self, config: DeploymentConfig) -> None:
        self.config = config
        self.settings_store = SettingsStore()
        self.events = EventLog()
        self.telemetry = TelemetryHub()

        settings = self.settings_store.current
        self.camera_credentials = CameraCredentialStore(config.data_dir / "camera_credentials.json")
        self.cameras = CameraManager(
            force_simulated=config.force_simulated_camera,
            credential_store=self.camera_credentials,
        )
        self.vision = VisionPipeline(
            self.cameras,
            settings,
            config.resolved_models_dir,
            force_mock=config.force_mock_detector,
        )
        self.calibration = CalibrationService()
        self.zones = ZoneService()
        self.selector = TargetSelector(settings.targeting)
        self.spray_guard = SprayGuard(settings.spray)
        self.state_machine = TargetingStateMachine(settings.targeting, self.spray_guard)
        self.turret = TurretManager(settings.controller, settings.motion)
        self.simulated_controller: SimulatedController | None = None

        #: System-level arm. Automatic engagement and water output both
        #: require it; it is never restored automatically on restart.
        self.armed = False
        self.started_at = time.monotonic()
        self._tasks: list[asyncio.Task[Any]] = []
        self._last_selection: SelectionResult | None = None
        self._stopping = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        settings = await self.settings_store.load()
        self.settings_store.subscribe(self._on_settings_changed)

        await self.calibration.refresh()
        await self.zones.refresh()

        self.selector.update_settings(settings.targeting)
        self.spray_guard.update_settings(settings.spray)
        self.state_machine.update_settings(settings.targeting)
        self.turret.update_settings(settings.controller, settings.motion)
        self.turret.subscribe_events(self._on_controller_event)

        await self.cameras.apply(settings.cameras)
        await self.vision.apply_settings(settings, {"detector", "tracker"})
        await self.vision.start()

        self._tasks = [
            asyncio.create_task(self._targeting_loop(), name="targeting"),
            asyncio.create_task(self._telemetry_loop(), name="telemetry"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]
        await self._sync_controller_mode()
        await self.events.emit(
            ev.CAT_SYSTEM, "server started", data={"version": version_info()["server_version"]}
        )

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        # Best effort: leave the hardware safe even on an abrupt shutdown.
        if self.turret.connected:
            with contextlib.suppress(Exception):
                await self.turret.arm_output(False)
            with contextlib.suppress(Exception):
                await self.turret.stop(emergency=False)
        await self._stop_simulated_controller(notify=False)

        await self.vision.stop()
        await self.cameras.stop_all()
        await self.telemetry.close_all()
        await self.events.emit(ev.CAT_SYSTEM, "server stopped")

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    async def _on_settings_changed(self, settings: AppSettings, changed: set[str]) -> None:
        if "cameras" in changed:
            self.camera_credentials.retain({camera.id for camera in settings.cameras.sources})
            await self.cameras.apply(settings.cameras)
        if changed & {"detector", "tracker", "cameras"}:
            await self.vision.apply_settings(settings, changed)
        if "targeting" in changed:
            self.selector.update_settings(settings.targeting)
            self.state_machine.update_settings(settings.targeting)
        if "spray" in changed:
            self.spray_guard.update_settings(settings.spray)
            if not settings.spray.enabled and self.turret.connected:
                with contextlib.suppress(TurretError):
                    await self.turret.arm_output(False)
        if changed & {"controller", "motion"}:
            self.turret.update_settings(settings.controller, settings.motion)
        if "controller" in changed:
            await self._sync_controller_mode()

    @property
    def settings(self) -> AppSettings:
        return self.settings_store.current

    async def _sync_controller_mode(self) -> None:
        if self.settings.controller.mode == "simulated":
            if self.simulated_controller is not None:
                if self.turret.info.controller_id == self.settings.controller.controller_id:
                    return
                await self._stop_simulated_controller(notify=False)
            simulator = SimulatedController(self.turret)
            self.simulated_controller = simulator
            if await simulator.start():
                await self.on_controller_connected()
            else:
                self.simulated_controller = None
        else:
            await self._stop_simulated_controller()

    async def _stop_simulated_controller(self, *, notify: bool = True) -> None:
        simulator = self.simulated_controller
        if simulator is None:
            return
        self.simulated_controller = None
        detached = await simulator.stop()
        if detached and notify:
            await self.on_controller_disconnected()

    # ------------------------------------------------------------------
    # controller events
    # ------------------------------------------------------------------
    async def on_controller_connected(self) -> None:
        settings = self.settings
        if settings.controller.push_config_on_connect:
            try:
                await self.turret.push_config()
            except TurretError as exc:
                await self.events.emit(
                    ev.CAT_CONTROLLER,
                    f"pushing controller configuration failed: {exc}",
                    level="warning",
                )
        # A fresh link never inherits a previous arm state.
        self.armed = False
        with contextlib.suppress(TurretError):
            await self.turret.arm_output(False)

        await self.events.emit(
            ev.CAT_CONTROLLER,
            "controller connected",
            data={
                "controller_id": self.turret.info.controller_id,
                "firmware": self.turret.info.firmware_version,
            },
        )
        if settings.motion.auto_home_on_connect:
            # Held in _tasks so the task is not garbage collected mid-homing.
            task = asyncio.create_task(self._auto_home(), name="auto-home")
            self._tasks.append(task)
            task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    async def _auto_home(self) -> None:
        try:
            await self.turret.home("both")
            await self.events.emit(ev.CAT_MOTION, "homing completed (automatic)")
        except TurretError as exc:
            await self.events.emit(ev.CAT_MOTION, f"automatic homing failed: {exc}", level="error")

    async def on_controller_disconnected(self) -> None:
        self.armed = False
        await self.events.emit(ev.CAT_CONTROLLER, "controller disconnected", level="warning")

    async def _on_controller_event(self, event: str, detail: dict[str, Any]) -> None:
        level = "error" if event in {"homing_failed", "fault", "estop"} else "info"
        await self.events.emit(
            ev.CAT_CONTROLLER, f"controller: {event}", level=level, data=detail or None
        )
        if event == "estop":
            self.armed = False

    # ------------------------------------------------------------------
    # operator actions
    # ------------------------------------------------------------------
    async def set_armed(self, armed: bool, source: str = "ui") -> dict[str, Any]:
        """Arm or disarm the system.

        Arming requires a healthy, homed controller: an armed system that
        cannot actually be stopped is worse than a disarmed one.
        """
        if armed:
            if not self.turret.connected:
                raise TurretError("cannot arm: controller not connected")
            if not self.turret.state.homed:
                raise TurretError("cannot arm: turret is not homed")
            if self.turret.state.estop:
                raise TurretError("cannot arm: emergency stop is active")

        await self.turret.arm_output(armed and self.settings.spray.enabled)
        self.armed = armed
        await self.events.emit(
            ev.CAT_SYSTEM,
            f"system {'armed' if armed else 'disarmed'}",
            level="warning" if armed else "info",
            data={"source": source},
        )
        return {"armed": self.armed}

    async def emergency_stop(self, source: str = "ui") -> dict[str, Any]:
        self.armed = False
        error: str | None = None
        try:
            await self.turret.stop(emergency=True)
        except TurretError as exc:
            error = str(exc)
        await self.events.emit(
            ev.CAT_SYSTEM,
            "EMERGENCY STOP",
            level="error",
            data={"source": source, "error": error},
        )
        return {"armed": False, "error": error}

    async def clear_emergency_stop(self) -> dict[str, Any]:
        """Clear the controller's latched e-stop (a plain stop command does it).

        Homing is cleared by an emergency stop, so the turret must be re-homed
        before absolute motion is accepted again.
        """
        await self.turret.stop(emergency=False)
        await self.events.emit(ev.CAT_SYSTEM, "emergency stop cleared", level="warning")
        return {"ok": True}

    async def home(self, axes: str = "both") -> dict[str, Any]:
        await self.events.emit(ev.CAT_MOTION, "homing started", data={"axes": axes})
        try:
            await self.turret.home(axes)
        except TurretError as exc:
            await self.events.emit(ev.CAT_MOTION, f"homing failed: {exc}", level="error")
            raise
        # The ack arrives before the next status frame, so the cached state is
        # still stale here. Wait for the hardware to actually say "homed"
        # rather than reporting a flag we have not been told yet.
        homed = await self.turret.wait_for_state(lambda state: state.homed, timeout=2.0)
        await self.events.emit(ev.CAT_MOTION, "homing completed")
        return {"homed": homed}

    async def center(self) -> dict[str, Any]:
        motion = self.settings.motion
        await self.turret.move_absolute(motion.park_pan_deg, motion.park_tilt_deg)
        return {"pan_deg": motion.park_pan_deg, "tilt_deg": motion.park_tilt_deg}

    async def move_to(
        self, pan_deg: float, tilt_deg: float, speed_deg_s: float | None = None
    ) -> dict[str, Any]:
        ack = await self.turret.move_absolute(pan_deg, tilt_deg, speed_deg_s)
        return {"ok": True, "clamped": ack.clamped}

    async def move_relative(
        self, pan_delta_deg: float, tilt_delta_deg: float, speed_deg_s: float | None = None
    ) -> dict[str, Any]:
        ack = await self.turret.move_relative(pan_delta_deg, tilt_delta_deg, speed_deg_s)
        return {"ok": True, "clamped": ack.clamped}

    async def jog(self, pan_fraction: float, tilt_fraction: float) -> dict[str, Any]:
        """Joystick input in [-1, 1] per axis, scaled by the manual speed."""
        speed = self.settings.motion.manual_speed_deg_s
        pan_rate = max(-1.0, min(1.0, pan_fraction)) * speed
        tilt_rate = max(-1.0, min(1.0, tilt_fraction)) * speed
        await self.turret.jog(pan_rate, tilt_rate)
        return {"pan_rate_deg_s": pan_rate, "tilt_rate_deg_s": tilt_rate}

    async def stop_motion(self) -> dict[str, Any]:
        await self.turret.stop(emergency=False)
        return {"ok": True}

    async def manual_spray(self, duration_ms: int | None = None) -> dict[str, Any]:
        """Operator-triggered spray. Subject to exactly the same budget as automatic."""
        if not self.armed:
            raise TurretError("cannot spray: system is disarmed")
        decision = self.spray_guard.check(duration_ms)
        if not decision.allowed:
            raise TurretError(f"spray refused: {decision.reason}")
        await self.turret.spray(decision.duration_ms)
        self.spray_guard.record(decision.duration_ms)
        await self.events.emit(
            ev.CAT_SPRAY,
            "manual spray activated",
            level="warning",
            data={
                "duration_ms": decision.duration_ms,
                "pan_deg": round(self.turret.state.pan_deg, 2),
                "tilt_deg": round(self.turret.state.tilt_deg, 2),
            },
        )
        return {"duration_ms": decision.duration_ms}

    async def spray_stop(self) -> dict[str, Any]:
        await self.turret.spray_stop()
        return {"ok": True}

    # ------------------------------------------------------------------
    # aiming helpers
    # ------------------------------------------------------------------
    def solve_image_point(
        self, x: float, y: float, camera_id: str | None = None, surface: str | None = None
    ) -> AimSolution | None:
        camera = camera_id or self.settings.cameras.primary_id
        return self.calibration.solve(
            self.calibration_camera_id(camera),
            x,
            y,
            zones=self.zones.for_camera(camera),
            surface=surface,
        )

    def calibration_camera_id(self, camera_id: str | None = None) -> str:
        """Storage key for the active physical/simulated calibration profile."""
        camera = camera_id or self.settings.cameras.primary_id
        if self.settings.controller.mode != "simulated":
            return camera
        digest = hashlib.sha256(camera.encode("utf-8")).hexdigest()[:10]
        return f"sim-{digest}-{camera[:49]}"[:64]

    def calibration_description(self, camera_id: str | None = None) -> dict[str, Any]:
        camera = camera_id or self.settings.cameras.primary_id
        result = self.calibration.describe(self.calibration_camera_id(camera))
        result["camera_id"] = camera
        result["controller_mode"] = self.settings.controller.mode
        return result

    async def aim_at_image_point(
        self,
        x: float,
        y: float,
        camera_id: str | None = None,
        surface: str | None = None,
        allow_extrapolation: bool = True,
    ) -> dict[str, Any]:
        """Click-to-aim. Pure motion — never triggers water."""
        solution = self.solve_image_point(x, y, camera_id, surface)
        if solution is None:
            raise TurretError("no calibration available for this camera")
        if solution.extrapolated and not allow_extrapolation:
            raise TurretError("point is outside the calibrated region")
        await self.turret.move_absolute(solution.pan_deg, solution.tilt_deg)
        await self.events.emit(
            ev.CAT_MOTION,
            "click-to-aim",
            data={
                "x": round(x, 4),
                "y": round(y, 4),
                "pan_deg": round(solution.pan_deg, 2),
                "tilt_deg": round(solution.tilt_deg, 2),
                "extrapolated": solution.extrapolated,
            },
        )
        return solution.as_dict()

    def turret_image_point(self) -> tuple[float, float] | None:
        """Where the turret is currently pointing, in normalised image coords."""
        camera = self.settings.cameras.primary_id
        if not self.turret.connected:
            return None
        if self.simulated_controller is not None:
            return self.simulated_controller.image_point(
                self.turret.state.pan_deg, self.turret.state.tilt_deg
            )
        return self.calibration.angles_to_image(
            camera, self.turret.state.pan_deg, self.turret.state.tilt_deg
        )

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------
    def render_preview(self, camera_id: str | None = None, overlays: bool | None = None) -> bytes:
        """Encode the newest frame (optionally with overlays) as JPEG."""
        ui = self.settings.ui
        camera = camera_id or self.settings.cameras.primary_id
        frame = self.cameras.latest(camera)
        if frame is None:
            raise LookupError(f"no frame available from camera '{camera}'")

        draw = ui.draw_overlays if overlays is None else overlays
        image = frame.image
        if draw:
            result = self.vision.latest
            tracks = result.tracks if result and result.camera_id == camera else []
            is_primary = camera == self.settings.cameras.primary_id
            turret_point = self.turret_image_point() if is_primary else None
            aim_point = None
            if (
                is_primary
                and self._last_selection
                and self.state_machine.target_track_id is not None
            ):
                candidate = self._last_selection.find(self.state_machine.target_track_id)
                if candidate is not None:
                    aim_point = candidate.aim_px
            image = render_overlay(
                image,
                tracks=tracks,
                zones=self.zones.for_camera(camera).as_dicts() if ui.draw_zones else (),
                target_track_id=self.state_machine.target_track_id,
                aim_point=aim_point,
                turret_point=(
                    (turret_point[0] * frame.width, turret_point[1] * frame.height)
                    if turret_point
                    else None
                ),
                hud_lines=self._hud_lines(),
            )
        return encode_jpeg(image, ui.preview_quality, ui.preview_width)

    def _hud_lines(self) -> list[str]:
        turret = self.turret.state
        # Two different facts share the word "disarmed": whether the *system*
        # is armed, and which state the *automatic* machine is in. Label them,
        # and put the safety-relevant one first.
        return [
            f"{'SIMULATED   ' if self.simulated_controller else ''}"
            f"{'ARMED' if self.armed else 'SAFE'}   AUTO {self.state_machine.state.value}",
            f"pan {turret.pan_deg:7.2f}  tilt {turret.tilt_deg:7.2f}"
            f"{'  MOVING' if turret.moving else ''}",
        ]

    async def save_snapshot(self, tag: str) -> str | None:
        """Write the current frame to the snapshot directory. Returns a path."""
        try:
            data = self.render_preview()
        except LookupError:
            return None
        directory = self.config.resolved_snapshot_dir
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_filename(tag)}.jpg"
        path = directory / name
        await asyncio.to_thread(path.write_bytes, data)
        return str(path)

    # ------------------------------------------------------------------
    # loops
    # ------------------------------------------------------------------
    async def _targeting_loop(self) -> None:
        interval = 1.0 / self.TARGETING_HZ
        while not self._stopping:
            try:
                await self._targeting_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("targeting tick failed")
            await asyncio.sleep(interval)

    async def _targeting_tick(self) -> None:
        settings = self.settings
        camera = settings.cameras.primary_id
        result = self.vision.latest

        selection: SelectionResult | None = None
        if result is not None and result.camera_id == camera:
            zones = self.zones.for_camera(camera)
            selection = self.selector.evaluate(
                result.tracks,
                result.frame_width,
                result.frame_height,
                zones,
                lambda x, y, surface: self.calibration.solve(
                    self.calibration_camera_id(camera),
                    x,
                    y,
                    zones=zones,
                    surface=surface,
                ),
                now=time.time(),
            )
            self._last_selection = selection

        turret = self.turret.state
        ctx = TickContext(
            now=time.monotonic(),
            armed=self.armed,
            auto_enabled=settings.targeting.auto_enabled,
            controller_connected=self.turret.connected,
            homed=turret.homed,
            moving=turret.moving,
            pan_deg=turret.pan_deg,
            tilt_deg=turret.tilt_deg,
            selection=selection,
            aim_tolerance_deg=settings.motion.aim_tolerance_deg,
            fault=self.turret.fault_reason(),
        )
        outcome = self.state_machine.step(ctx)

        for action in outcome.actions:
            await self._execute(action)

        for message, data in outcome.events:
            level = "warning" if "spray" in message or "blocked" in message else "info"
            snapshot = None
            if settings.targeting.snapshot_on_engage and "spray" in message:
                snapshot = await self.save_snapshot(message.replace(" ", "-"))
            await self.events.emit(
                ev.CAT_TARGETING, message, level=level, data=data or None, snapshot_path=snapshot
            )

        if outcome.changed:
            log.debug(
                "targeting state",
                extra={
                    "ctx": {
                        "from": outcome.previous_state.value,
                        "to": outcome.state.value,
                        "reason": outcome.reason,
                    }
                },
            )

    async def _execute(self, action: Action) -> None:
        try:
            if action.kind is ActionKind.MOVE and action.pan_deg is not None:
                await self.turret.move_absolute(
                    action.pan_deg, action.tilt_deg or 0.0, action.max_speed_deg_s
                )
            elif action.kind is ActionKind.SPRAY and action.duration_ms:
                await self.turret.spray(action.duration_ms)
            elif action.kind is ActionKind.STOP:
                await self.turret.stop(emergency=False)
        except TurretError as exc:
            await self.events.emit(
                ev.CAT_TARGETING,
                f"turret command failed: {exc}",
                level="error",
                data={"action": action.kind.value},
                dedupe_s=5.0,
            )

    async def _telemetry_loop(self) -> None:
        while not self._stopping:
            interval = 1.0 / max(1.0, self.settings.ui.telemetry_hz)
            try:
                await self.telemetry.broadcast(
                    {"type": "telemetry", "data": self.telemetry_snapshot()}
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telemetry broadcast failed")
            await asyncio.sleep(interval)

    async def _maintenance_loop(self) -> None:
        # First run shortly after start so a full disk is noticed early.
        await asyncio.sleep(60)
        while not self._stopping:
            try:
                system = self.settings.system
                removed = await self.events.prune(system.event_retention_days, system.max_events)
                files = await prune_snapshots(
                    self.config.resolved_snapshot_dir,
                    system.snapshot_retention_days,
                    system.max_snapshot_mb,
                )
                if removed or files:
                    log.info(
                        "retention cleanup",
                        extra={"ctx": {"events_removed": removed, "snapshots_removed": files}},
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("maintenance failed")
            await asyncio.sleep(3600)

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def telemetry_snapshot(self) -> dict[str, Any]:
        turret = self.turret.state
        result = self.vision.latest
        target = None
        if self._last_selection and self.state_machine.target_track_id is not None:
            candidate = self._last_selection.find(self.state_machine.target_track_id)
            if candidate is not None:
                target = {
                    "track_id": candidate.track.track_id,
                    "class": candidate.track.class_name,
                    "confidence": round(candidate.track.confidence, 3),
                    "aim_norm": [round(candidate.aim_norm[0], 4), round(candidate.aim_norm[1], 4)],
                    "solution": candidate.solution.as_dict() if candidate.solution else None,
                }

        turret_point = self.turret_image_point()
        return {
            "ts": time.time(),
            "system_state": self.state_machine.state.value,
            "state_reason": self.state_machine.reason,
            "armed": self.armed,
            "auto_enabled": self.settings.targeting.auto_enabled,
            "detection_enabled": self.settings.detector.enabled,
            "spray_enabled": self.settings.spray.enabled,
            "camera_connected": self.cameras.status_dict()["connected"],
            "controller_connected": self.turret.connected,
            "controller_mode": self.settings.controller.mode,
            "controller_simulated": bool(self.turret.info.hardware.get("simulated")),
            "controller_fault": self.turret.fault_reason(),
            "pan_deg": round(turret.pan_deg, 3),
            "tilt_deg": round(turret.tilt_deg, 3),
            "moving": turret.moving,
            "homed": turret.homed,
            "valve_open": turret.valve_open,
            "estop": turret.estop,
            "limits": {
                "pan_min": turret.limit_pan_min,
                "pan_max": turret.limit_pan_max,
                "tilt_min": turret.limit_tilt_min,
                "tilt_max": turret.limit_tilt_max,
            },
            "turret_point": (
                [round(turret_point[0], 4), round(turret_point[1], 4)] if turret_point else None
            ),
            "target": target,
            "tracks": [t.as_dict() for t in (result.tracks if result else [])],
            "frame": (
                {
                    "width": result.frame_width,
                    "height": result.frame_height,
                    "seq": result.frame_seq,
                    "inference_ms": round(result.inference_ms, 1),
                }
                if result
                else None
            ),
            "spray": self.spray_guard.status(),
            "targeting": self.state_machine.status(time.monotonic()),
        }

    def health(self) -> dict[str, Any]:
        vision = self.vision.status()
        database = database_status()
        camera_ok = self.cameras.any_connected or not any(
            c.enabled for c in self.settings.cameras.sources
        )
        checks = {
            "database": database["ok"],
            "camera": camera_ok,
            "controller": self.turret.connected,
            "ai": self.vision.healthy,
        }
        return {
            "status": "ok" if all(checks.values()) else "degraded",
            "checks": checks,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "version": version_info(),
            "armed": self.armed,
            "system_state": self.state_machine.state.value,
            "camera": self.cameras.status_dict(),
            "controller": self.turret.status_dict(),
            "vision": vision,
            "database": database,
            "calibration": self.calibration_description(),
            "telemetry_clients": self.telemetry.client_count,
        }

    def system_info(self) -> dict[str, Any]:
        from app.vision.yolo_detector import gpu_info

        return {
            "version": version_info(),
            "paths": {
                "data_dir": str(self.config.data_dir),
                "models_dir": str(self.config.resolved_models_dir),
                "snapshots_dir": str(self.config.resolved_snapshot_dir),
                "database": str(self.config.database_path),
            },
            "gpu": gpu_info(),
            "auth_enabled": self.config.auth_enabled,
            "controller_token_configured": bool(self.config.controller_token),
            "states": [s.value for s in AutoState],
        }


def snapshot_url(path: str | None, snapshot_dir: Path) -> str | None:
    """Map an absolute snapshot path to its served URL, if it is inside the dir."""
    if not path:
        return None
    try:
        relative = Path(path).resolve().relative_to(snapshot_dir.resolve())
    except (ValueError, OSError):
        return None
    return f"/api/snapshots/{relative.as_posix()}"
