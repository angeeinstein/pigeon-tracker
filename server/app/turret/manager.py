"""Turret manager: the server's single point of contact with the controller.

Responsibilities:

* own the (at most one) controller connection and its handshake;
* serialise commands, match acknowledgements to command ids, and time out
  rather than hang;
* keep the last known hardware state and decide when the link is stale;
* refuse to command hardware that speaks an incompatible protocol version.

Everything that wants to move the turret goes through here — the REST API, the
state machine and the simulator all use the same methods, so there is exactly
one place where a command can be validated or blocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel

from app.logging_config import get_logger
from app.services.settings_schema import ControllerSettings, MotionSettings
from app.turret import protocol as proto
from app.turret.models import ControllerInfo, LinkState, TurretError, TurretState
from app.version import PROTOCOL_VERSION, SERVER_VERSION

log = get_logger(__name__)

StatusListener = Callable[[TurretState], Awaitable[None]]
EventListener = Callable[[str, dict[str, Any]], Awaitable[None]]


class ControllerConnection(Protocol):
    """Minimal transport interface, so the manager does not depend on FastAPI."""

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class TurretManager:
    def __init__(self, settings: ControllerSettings, motion: MotionSettings) -> None:
        self.settings = settings
        self.motion = motion

        self.state = TurretState()
        self.info = ControllerInfo()
        self.link = LinkState.DISCONNECTED

        self._connection: ControllerConnection | None = None
        self._pending: dict[int, asyncio.Future[proto.Ack]] = {}
        self._next_id = 1
        self._send_lock = asyncio.Lock()
        self._ping_task: asyncio.Task[None] | None = None
        self._status_listeners: list[StatusListener] = []
        self._event_listeners: list[EventListener] = []
        self._last_rx = 0.0
        self._rtt_ms: float | None = None
        self._commands_sent = 0
        self._commands_failed = 0
        #: Set when the server refuses to command (version mismatch, e-stop).
        self.block_reason: str | None = None

    # -- configuration ---------------------------------------------------
    def update_settings(self, controller: ControllerSettings, motion: MotionSettings) -> None:
        self.settings = controller
        self.motion = motion

    def subscribe_status(self, listener: StatusListener) -> None:
        self._status_listeners.append(listener)

    def subscribe_events(self, listener: EventListener) -> None:
        self._event_listeners.append(listener)

    # -- link state ------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.link is LinkState.CONNECTED and self._connection is not None

    @property
    def ready(self) -> bool:
        """Connected, compatible, fresh status, and not faulted."""
        return (
            self.connected
            and not self.state.is_stale(self.settings.status_timeout_s)
            and not self.state.estop
            and self.block_reason is None
        )

    def fault_reason(self) -> str | None:
        if not self.connected:
            return "controller disconnected"
        if self.block_reason:
            return self.block_reason
        if self.state.estop:
            return "emergency stop active"
        if self.state.error:
            return f"controller error: {self.state.error}"
        if self.state.is_stale(self.settings.status_timeout_s):
            return "no status from controller"
        return None

    def status_dict(self) -> dict[str, Any]:
        return {
            "link": self.link.value,
            "connected": self.connected,
            "ready": self.ready,
            "fault": self.fault_reason(),
            "rtt_ms": round(self._rtt_ms, 1) if self._rtt_ms is not None else None,
            "commands_sent": self._commands_sent,
            "commands_failed": self._commands_failed,
            "controller": self.info.as_dict(),
            "state": self.state.as_dict(),
        }

    # -- connection lifecycle --------------------------------------------
    async def attach(self, connection: ControllerConnection, hello: proto.Hello) -> bool:
        """Complete the handshake. Returns False if the controller was rejected."""
        if self._connection is not None:
            log.warning(
                "replacing existing controller connection",
                extra={"ctx": {"controller_id": hello.controller_id}},
            )
            with contextlib.suppress(Exception):
                await self._connection.close(proto.CLOSE_REPLACED, "replaced by new connection")
            await self._teardown()

        self._connection = connection
        self.link = LinkState.HANDSHAKE
        self.info = ControllerInfo(
            controller_id=hello.controller_id,
            firmware_version=hello.firmware_version,
            protocol_version=hello.protocol_version,
            capabilities=list(hello.capabilities),
            hardware=dict(hello.hardware),
            connected_at=time.monotonic(),
        )

        if hello.protocol_version != PROTOCOL_VERSION:
            self.link = LinkState.INCOMPATIBLE
            self.block_reason = (
                f"firmware speaks protocol v{hello.protocol_version}, "
                f"server speaks v{PROTOCOL_VERSION}"
            )
            log.error("controller protocol mismatch", extra={"ctx": {"reason": self.block_reason}})
            await self._send_raw(
                proto.HelloAck(
                    accepted=False,
                    reason="protocol_version_mismatch",
                    server_version=SERVER_VERSION,
                    time_ms=int(time.time() * 1000),
                )
            )
            return False

        self.block_reason = None
        self.link = LinkState.CONNECTED
        self._last_rx = time.monotonic()
        await self._send_raw(
            proto.HelloAck(
                accepted=True,
                server_version=SERVER_VERSION,
                time_ms=int(time.time() * 1000),
            )
        )
        self._ping_task = asyncio.create_task(self._ping_loop(), name="turret-ping")
        log.info(
            "controller connected",
            extra={
                "ctx": {
                    "controller_id": hello.controller_id,
                    "firmware": hello.firmware_version,
                }
            },
        )
        return True

    async def detach(self) -> None:
        """Called when the controller socket closes for any reason."""
        await self._teardown()
        log.info("controller disconnected")

    async def _teardown(self) -> None:
        self._connection = None
        self.link = LinkState.DISCONNECTED
        self.state.reset()
        self.block_reason = None
        if self._ping_task is not None:
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ping_task
            self._ping_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TurretError("controller disconnected"))
        self._pending.clear()

    # -- inbound ---------------------------------------------------------
    async def handle_message(self, message: BaseModel) -> None:
        """Dispatch a validated controller message."""
        self._last_rx = time.monotonic()

        if isinstance(message, proto.Status):
            self.state.apply(message)
            for listener in list(self._status_listeners):
                try:
                    await listener(self.state)
                except Exception:
                    log.exception("status listener failed")

        elif isinstance(message, proto.Ack):
            future = self._pending.pop(message.id, None)
            if future is not None and not future.done():
                future.set_result(message)
            elif future is None:
                log.debug("ack for unknown command", extra={"ctx": {"id": message.id}})

        elif isinstance(message, proto.Pong):
            if message.t_ms:
                self._rtt_ms = max(0.0, time.time() * 1000 - message.t_ms)

        elif isinstance(message, proto.Event):
            log.info(
                "controller event",
                extra={"ctx": {"event": message.event, **message.detail}},
            )
            for listener in list(self._event_listeners):
                try:
                    await listener(message.event, message.detail)
                except Exception:
                    log.exception("controller event listener failed")

        elif isinstance(message, proto.LogMessage):
            level = {"warn": "warning"}.get(message.level, message.level)
            getattr(log, level, log.info)(message.msg, extra={"ctx": {"source": "controller"}})

        elif isinstance(message, proto.ConfigReport):
            self.info.hardware["config"] = message.config

    # -- outbound --------------------------------------------------------
    def _allocate_id(self) -> int:
        command_id = self._next_id
        # 31-bit wrap keeps ids friendly for the firmware's int32.
        self._next_id = 1 if self._next_id >= 2**31 - 1 else self._next_id + 1
        return command_id

    async def _send_raw(self, message: BaseModel) -> None:
        connection = self._connection
        if connection is None:
            raise TurretError("controller not connected")
        async with self._send_lock:
            await connection.send_text(proto.encode(message))

    async def send_command(
        self,
        build: Callable[[int], BaseModel],
        *,
        timeout: float | None = None,
        require_ready: bool = True,
    ) -> proto.Ack:
        """Send a command and wait for its acknowledgement.

        Raises :class:`TurretError` on rejection, timeout or a missing link —
        callers never have to guess whether the hardware acted.
        """
        if self._connection is None:
            raise TurretError("controller not connected")
        if self.block_reason:
            raise TurretError(self.block_reason, code=proto.ErrorCode.FAULT)
        if require_ready and self.link is not LinkState.CONNECTED:
            raise TurretError(f"controller link is {self.link.value}")

        command_id = self._allocate_id()
        message = build(command_id)
        future: asyncio.Future[proto.Ack] = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future

        try:
            await self._send_raw(message)
            self._commands_sent += 1
            ack = await asyncio.wait_for(future, timeout=timeout or self.settings.command_timeout_s)
        except TimeoutError as exc:
            self._pending.pop(command_id, None)
            self._commands_failed += 1
            raise TurretError(
                f"no acknowledgement for {getattr(message, 'type', 'command')}",
                code=proto.ErrorCode.TIMEOUT,
            ) from exc
        except TurretError:
            self._pending.pop(command_id, None)
            self._commands_failed += 1
            raise
        finally:
            self._pending.pop(command_id, None)

        if not ack.ok:
            self._commands_failed += 1
            raise TurretError(ack.error or "command rejected", code=ack.code)
        return ack

    # -- high level operations -------------------------------------------
    async def move_absolute(
        self, pan_deg: float, tilt_deg: float, max_speed_deg_s: float | None = None
    ) -> proto.Ack:
        pan, tilt = self.motion.clamp(pan_deg, tilt_deg)
        return await self.send_command(
            lambda cid: proto.MoveAbsolute(
                id=cid,
                pan_deg=pan,
                tilt_deg=tilt,
                max_speed_deg_s=max_speed_deg_s or self.motion.max_speed_deg_s,
                accel_deg_s2=self.motion.accel_deg_s2,
            )
        )

    async def move_relative(
        self, pan_delta_deg: float, tilt_delta_deg: float, max_speed_deg_s: float | None = None
    ) -> proto.Ack:
        return await self.send_command(
            lambda cid: proto.MoveRelative(
                id=cid,
                pan_delta_deg=pan_delta_deg,
                tilt_delta_deg=tilt_delta_deg,
                max_speed_deg_s=max_speed_deg_s or self.motion.manual_speed_deg_s,
            )
        )

    async def jog(self, pan_rate_deg_s: float, tilt_rate_deg_s: float) -> proto.Ack:
        return await self.send_command(
            lambda cid: proto.Jog(
                id=cid,
                pan_rate_deg_s=pan_rate_deg_s,
                tilt_rate_deg_s=tilt_rate_deg_s,
                ttl_ms=self.motion.jog_ttl_ms,
            ),
            timeout=1.5,
        )

    async def home(self, axes: str = "both") -> proto.Ack:
        return await self.send_command(
            lambda cid: proto.Home(id=cid, axes=axes),  # type: ignore[arg-type]
            timeout=self.settings.home_timeout_s,
        )

    async def stop(self, emergency: bool = False) -> proto.Ack:
        # An emergency stop must go out even when the link is "not ready".
        return await self.send_command(
            lambda cid: proto.Stop(id=cid, emergency=emergency),
            timeout=2.0,
            require_ready=not emergency,
        )

    async def spray(self, duration_ms: int) -> proto.Ack:
        return await self.send_command(lambda cid: proto.Spray(id=cid, duration_ms=duration_ms))

    async def spray_stop(self) -> proto.Ack:
        return await self.send_command(lambda cid: proto.SprayStop(id=cid), timeout=2.0)

    async def arm_output(self, armed: bool) -> proto.Ack:
        return await self.send_command(
            lambda cid: proto.ArmOutput(id=cid, armed=armed),
            timeout=2.0,
            require_ready=armed,  # disarming must always be possible
        )

    async def wait_for_state(
        self, predicate: Callable[[TurretState], bool], timeout: float = 2.0
    ) -> bool:
        """Wait until a *reported* hardware state satisfies ``predicate``.

        Command acknowledgements arrive before the next status frame, so a
        caller that needs the resulting state (did homing take? is it still
        moving?) must wait for the hardware to say so rather than trust the
        cached snapshot.
        """
        deadline = time.monotonic() + timeout
        while True:
            if predicate(self.state):
                return True
            if time.monotonic() >= deadline or not self.connected:
                return predicate(self.state)
            await asyncio.sleep(0.02)

    async def push_config(self) -> proto.Ack:
        """Send the hardware configuration derived from server settings."""
        hardware = self.settings.hardware
        config: dict[str, Any] = hardware.model_dump()
        config.update(
            {
                "pan_min_deg": self.motion.pan_min_deg,
                "pan_max_deg": self.motion.pan_max_deg,
                "tilt_min_deg": self.motion.tilt_min_deg,
                "tilt_max_deg": self.motion.tilt_max_deg,
                "max_speed_deg_s": self.motion.max_speed_deg_s,
                "accel_deg_s2": self.motion.accel_deg_s2,
            }
        )
        return await self.send_command(lambda cid: proto.SetConfig(id=cid, config=config))

    async def get_config(self) -> proto.Ack:
        return await self.send_command(lambda cid: proto.GetConfig(id=cid))

    async def reboot(self) -> proto.Ack:
        return await self.send_command(lambda cid: proto.Reboot(id=cid), timeout=2.0)

    # -- background ------------------------------------------------------
    async def _ping_loop(self) -> None:
        try:
            while self._connection is not None:
                await asyncio.sleep(self.settings.ping_interval_s)
                if self._connection is None:
                    return
                try:
                    await self._send_raw(
                        proto.Ping(id=self._allocate_id(), t_ms=int(time.time() * 1000))
                    )
                except Exception:
                    log.debug("ping failed; link is going away")
                    return
        except asyncio.CancelledError:
            raise
