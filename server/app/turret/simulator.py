"""In-process ESP32 and two-axis turret emulator.

The simulator attaches to :class:`TurretManager` through the exact same
``ControllerConnection`` interface as the hardware WebSocket.  Commands are
acknowledged through the normal protocol path and state is reported with
normal status frames, so the rest of the application cannot accidentally grow
a separate "demo" control path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from typing import Any

from app.turret import protocol as proto
from app.turret.manager import TurretManager
from app.version import PROTOCOL_VERSION


class SimulatedController:
    STATUS_INTERVAL_S = 0.05
    HOMING_DURATION_S = 0.6

    def __init__(self, manager: TurretManager) -> None:
        self.manager = manager
        self.pan_deg = 0.0
        self.tilt_deg = 0.0
        self.pan_rate_deg_s = 0.0
        self.tilt_rate_deg_s = 0.0
        self.target_pan_deg: float | None = None
        self.target_tilt_deg: float | None = None
        self.homed = False
        self.armed = False
        self.valve_open = False
        self.estop = False
        self.error: str | None = None
        self.state = proto.ControllerState.BOOT
        self._closed = True
        self._task: asyncio.Task[None] | None = None
        self._started_at = time.monotonic()
        self._last_tick = self._started_at
        self._seq = 0
        self._jog_expires_at = 0.0
        self._homing_finishes_at = 0.0
        self._homing_axes = "both"
        self._valve_closes_at = 0.0
        self._max_speed_deg_s = manager.motion.max_speed_deg_s
        self._config: dict[str, Any] = {}
        self._pending_event: proto.Event | None = None

    async def start(self) -> bool:
        if not self._closed:
            return True
        self._closed = False
        self._started_at = time.monotonic()
        self._last_tick = self._started_at
        accepted = await self.manager.attach(
            self,
            proto.Hello(
                controller_id=self.manager.settings.controller_id,
                firmware_version="simulator-1.0",
                protocol_version=PROTOCOL_VERSION,
                capabilities=["motion", "homing", "jog", "spray", "config", "simulated"],
                hardware={
                    "board": "virtual-esp32",
                    "driver": "in-process",
                    "simulated": True,
                },
            ),
        )
        if not accepted:
            self._closed = True
            return False
        self.state = proto.ControllerState.IDLE
        await self._emit_status()
        self._task = asyncio.create_task(self._status_loop(), name="simulated-controller")
        return True

    async def stop(self) -> bool:
        await self.close()
        return await self.manager.detach(self)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self._closed = True
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def send_text(self, data: str) -> None:
        """Receive a server-to-controller protocol frame."""
        if self._closed:
            return
        payload = json.loads(data)
        kind = payload.get("type")
        command_id = payload.get("id")

        if kind == proto.MessageType.HELLO_ACK:
            if not payload.get("accepted", False):
                await self.close(reason=str(payload.get("reason", "rejected")))
            return
        if kind == proto.MessageType.PING:
            await self.manager.handle_message(
                proto.Pong(id=int(command_id), t_ms=int(payload.get("t_ms", 0)))
            )
            return

        if not isinstance(command_id, int):
            return
        if kind == proto.MessageType.MOVE_ABSOLUTE:
            await self._move_absolute(command_id, payload)
        elif kind == proto.MessageType.MOVE_RELATIVE:
            await self._move_relative(command_id, payload)
        elif kind == proto.MessageType.JOG:
            await self._jog(command_id, payload)
        elif kind == proto.MessageType.HOME:
            await self._home(command_id, str(payload.get("axes", "both")))
        elif kind == proto.MessageType.STOP:
            await self._stop_command(command_id, bool(payload.get("emergency", False)))
        elif kind == proto.MessageType.SPRAY:
            await self._spray(command_id, int(payload.get("duration_ms", 0)))
        elif kind == proto.MessageType.SPRAY_STOP:
            self.valve_open = False
            self._valve_closes_at = 0.0
            await self._ack(command_id)
        elif kind == proto.MessageType.ARM_OUTPUT:
            await self._arm(command_id, bool(payload.get("armed", False)))
        elif kind == proto.MessageType.SET_CONFIG:
            self._config = dict(payload.get("config") or {})
            await self._ack(command_id)
            await self.manager.handle_message(
                proto.ConfigReport(id=command_id, config=self._config)
            )
        elif kind == proto.MessageType.GET_CONFIG:
            await self._ack(command_id)
            await self.manager.handle_message(
                proto.ConfigReport(id=command_id, config=self._config)
            )
        elif kind == proto.MessageType.REBOOT:
            await self._ack(command_id)
            self._reset_state()
        else:
            await self._ack(
                command_id,
                ok=False,
                code=proto.ErrorCode.UNSUPPORTED,
                error=f"unsupported command: {kind}",
            )

    def image_point(self, pan_deg: float, tilt_deg: float) -> tuple[float, float]:
        """Ground-truth image position, independent of user calibration.

        This is the virtual equivalent of seeing where a physical nozzle points.
        It deliberately does not use the fitted calibration model: calibration
        and automatic aiming can therefore be tested for real error.
        """
        motion = self.manager.motion
        pan_span = max(1e-6, motion.pan_max_deg - motion.pan_min_deg)
        tilt_span = max(1e-6, motion.tilt_max_deg - motion.tilt_min_deg)
        x = (pan_deg - motion.pan_min_deg) / pan_span
        # Positive tilt is visually up, while image y grows downward.
        y = 1.0 - (tilt_deg - motion.tilt_min_deg) / tilt_span
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    async def _move_absolute(self, command_id: int, payload: dict[str, Any]) -> None:
        if not await self._motion_allowed(command_id):
            return
        motion = self.manager.motion
        requested_pan = float(payload.get("pan_deg", self.pan_deg))
        requested_tilt = float(payload.get("tilt_deg", self.tilt_deg))
        pan, tilt = motion.clamp(requested_pan, requested_tilt)
        self.target_pan_deg = pan
        self.target_tilt_deg = tilt
        self._max_speed_deg_s = min(
            float(payload.get("max_speed_deg_s") or motion.max_speed_deg_s),
            motion.max_speed_deg_s,
        )
        self._jog_expires_at = 0.0
        self.state = proto.ControllerState.MOVING
        await self._emit_status()
        await self._ack(command_id, clamped=(pan != requested_pan or tilt != requested_tilt))

    async def _move_relative(self, command_id: int, payload: dict[str, Any]) -> None:
        if not await self._motion_allowed(command_id):
            return
        motion = self.manager.motion
        requested_pan = self.pan_deg + float(payload.get("pan_delta_deg", 0.0))
        requested_tilt = self.tilt_deg + float(payload.get("tilt_delta_deg", 0.0))
        pan, tilt = motion.clamp(requested_pan, requested_tilt)
        self.target_pan_deg = pan
        self.target_tilt_deg = tilt
        self._max_speed_deg_s = min(
            float(payload.get("max_speed_deg_s") or motion.manual_speed_deg_s),
            motion.max_speed_deg_s,
        )
        self._jog_expires_at = 0.0
        self.state = proto.ControllerState.MOVING
        await self._emit_status()
        await self._ack(command_id, clamped=(pan != requested_pan or tilt != requested_tilt))

    async def _jog(self, command_id: int, payload: dict[str, Any]) -> None:
        if not await self._motion_allowed(command_id):
            return
        self.target_pan_deg = None
        self.target_tilt_deg = None
        self.pan_rate_deg_s = float(payload.get("pan_rate_deg_s", 0.0))
        self.tilt_rate_deg_s = float(payload.get("tilt_rate_deg_s", 0.0))
        ttl_ms = max(50, int(payload.get("ttl_ms", 400)))
        self._jog_expires_at = time.monotonic() + ttl_ms / 1000.0
        self.state = (
            proto.ControllerState.JOGGING
            if self.pan_rate_deg_s or self.tilt_rate_deg_s
            else proto.ControllerState.IDLE
        )
        await self._emit_status()
        await self._ack(command_id)

    async def _home(self, command_id: int, axes: str) -> None:
        if self.estop:
            await self._ack(
                command_id, ok=False, code=proto.ErrorCode.ESTOP, error="emergency stop active"
            )
            return
        self.armed = False
        self.valve_open = False
        self._valve_closes_at = 0.0
        self.homed = False
        self._homing_axes = axes if axes in {"both", "pan", "tilt"} else "both"
        self._homing_finishes_at = time.monotonic() + self.HOMING_DURATION_S
        self.target_pan_deg = None
        self.target_tilt_deg = None
        self.state = proto.ControllerState.HOMING
        await self._emit_status()
        await self._ack(command_id)
        await self.manager.handle_message(
            proto.Event(
                event=proto.ControllerEvent.HOMING_STARTED, detail={"axes": self._homing_axes}
            )
        )

    async def _stop_command(self, command_id: int, emergency: bool) -> None:
        self.target_pan_deg = None
        self.target_tilt_deg = None
        self.pan_rate_deg_s = 0.0
        self.tilt_rate_deg_s = 0.0
        self._jog_expires_at = 0.0
        self._homing_finishes_at = 0.0
        self.valve_open = False
        self._valve_closes_at = 0.0
        if emergency:
            self.estop = True
            self.homed = False
            self.armed = False
            self.state = proto.ControllerState.ESTOP
            await self.manager.handle_message(
                proto.Event(event=proto.ControllerEvent.ESTOP, detail={"simulated": True})
            )
        else:
            was_estop = self.estop
            self.estop = False
            self.state = proto.ControllerState.IDLE
            if was_estop:
                await self.manager.handle_message(
                    proto.Event(event=proto.ControllerEvent.ESTOP_CLEARED, detail={})
                )
        await self._emit_status()
        await self._ack(command_id)

    async def _spray(self, command_id: int, duration_ms: int) -> None:
        if not self.armed:
            await self._ack(
                command_id, ok=False, code=proto.ErrorCode.DISARMED, error="output is disarmed"
            )
            return
        self.valve_open = True
        self._valve_closes_at = time.monotonic() + max(1, duration_ms) / 1000.0
        await self._emit_status()
        await self._ack(command_id)

    async def _arm(self, command_id: int, armed: bool) -> None:
        if armed and self.estop:
            await self._ack(
                command_id, ok=False, code=proto.ErrorCode.ESTOP, error="emergency stop active"
            )
            return
        self.armed = armed
        if not armed:
            self.valve_open = False
            self._valve_closes_at = 0.0
        await self._emit_status()
        await self._ack(command_id)

    async def _motion_allowed(self, command_id: int) -> bool:
        if self.estop:
            await self._ack(
                command_id, ok=False, code=proto.ErrorCode.ESTOP, error="emergency stop active"
            )
            return False
        if not self.homed and not self.manager.settings.hardware.allow_unhomed_motion:
            await self._ack(
                command_id, ok=False, code=proto.ErrorCode.NOT_HOMED, error="turret is not homed"
            )
            return False
        return True

    async def _ack(
        self,
        command_id: int,
        *,
        ok: bool = True,
        clamped: bool = False,
        code: str | None = None,
        error: str | None = None,
    ) -> None:
        await self.manager.handle_message(
            proto.Ack(id=command_id, ok=ok, clamped=clamped, code=code, error=error)
        )

    async def _status_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.STATUS_INTERVAL_S)
                now = time.monotonic()
                self._advance(now, min(0.2, now - self._last_tick))
                self._last_tick = now
                if self._pending_event is not None:
                    event = self._pending_event
                    self._pending_event = None
                    await self.manager.handle_message(event)
                await self._emit_status()
        except asyncio.CancelledError:
            raise

    def _advance(self, now: float, dt: float) -> None:
        if self._homing_finishes_at:
            if now >= self._homing_finishes_at:
                hardware = self.manager.settings.hardware
                if self._homing_axes in {"both", "pan"}:
                    self.pan_deg = hardware.pan_home_offset_deg
                if self._homing_axes in {"both", "tilt"}:
                    self.tilt_deg = hardware.tilt_home_offset_deg
                self.pan_rate_deg_s = 0.0
                self.tilt_rate_deg_s = 0.0
                self._homing_finishes_at = 0.0
                self.homed = True
                self.state = proto.ControllerState.IDLE
                self._pending_event = proto.Event(
                    event=proto.ControllerEvent.HOMING_COMPLETED, detail={}
                )
            return

        if self._jog_expires_at:
            if now < self._jog_expires_at:
                self.pan_deg, self.tilt_deg = self.manager.motion.clamp(
                    self.pan_deg + self.pan_rate_deg_s * dt,
                    self.tilt_deg + self.tilt_rate_deg_s * dt,
                )
            else:
                self._jog_expires_at = 0.0
                self.pan_rate_deg_s = 0.0
                self.tilt_rate_deg_s = 0.0
                self.state = proto.ControllerState.IDLE
        elif self.target_pan_deg is not None and self.target_tilt_deg is not None:
            self.pan_deg, self.pan_rate_deg_s, pan_done = self._advance_axis(
                self.pan_deg, self.pan_rate_deg_s, self.target_pan_deg, dt
            )
            self.tilt_deg, self.tilt_rate_deg_s, tilt_done = self._advance_axis(
                self.tilt_deg, self.tilt_rate_deg_s, self.target_tilt_deg, dt
            )
            if pan_done and tilt_done:
                self.target_pan_deg = None
                self.target_tilt_deg = None
                self.state = proto.ControllerState.IDLE

        if self._valve_closes_at and now >= self._valve_closes_at:
            self.valve_open = False
            self._valve_closes_at = 0.0

    def _advance_axis(
        self, position: float, velocity: float, target: float, dt: float
    ) -> tuple[float, float, bool]:
        delta = target - position
        if abs(delta) < 0.001:
            return target, 0.0, True
        accel = self.manager.motion.accel_deg_s2
        desired = math.copysign(
            min(self._max_speed_deg_s, math.sqrt(max(0.0, 2.0 * accel * abs(delta)))), delta
        )
        change = max(-accel * dt, min(accel * dt, desired - velocity))
        velocity += change
        step = velocity * dt
        if step * delta > 0 and abs(step) >= abs(delta):
            return target, 0.0, True
        return position + step, velocity, False

    async def _emit_status(self) -> None:
        self._seq += 1
        motion = self.manager.motion
        await self.manager.handle_message(
            proto.Status(
                seq=self._seq,
                uptime_ms=int((time.monotonic() - self._started_at) * 1000),
                state=self.state,
                pan_deg=self.pan_deg,
                tilt_deg=self.tilt_deg,
                target_pan_deg=self.target_pan_deg,
                target_tilt_deg=self.target_tilt_deg,
                pan_rate_deg_s=self.pan_rate_deg_s,
                tilt_rate_deg_s=self.tilt_rate_deg_s,
                moving=self.state
                in {
                    proto.ControllerState.MOVING,
                    proto.ControllerState.JOGGING,
                    proto.ControllerState.HOMING,
                },
                homed=self.homed,
                armed=self.armed,
                valve_open=self.valve_open,
                estop=self.estop,
                limit_pan_min=self.pan_deg <= motion.pan_min_deg + 0.001,
                limit_pan_max=self.pan_deg >= motion.pan_max_deg - 0.001,
                limit_tilt_min=self.tilt_deg <= motion.tilt_min_deg + 0.001,
                limit_tilt_max=self.tilt_deg >= motion.tilt_max_deg - 0.001,
                error=self.error,
            )
        )

    def _reset_state(self) -> None:
        self.pan_rate_deg_s = 0.0
        self.tilt_rate_deg_s = 0.0
        self.target_pan_deg = None
        self.target_tilt_deg = None
        self.homed = False
        self.armed = False
        self.valve_open = False
        self.estop = False
        self.state = proto.ControllerState.IDLE
