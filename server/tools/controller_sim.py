#!/usr/bin/env python3
"""Turret controller simulator.

Speaks the real protocol over the real WebSocket endpoint, so the whole server
and UI — arming, homing, click-to-aim, the targeting state machine, spray
budgets — can be exercised before any hardware exists.

It models what actually matters for testing the server: acceleration-limited
motion (so "is the turret still moving?" is a real question), homing that takes
time and can fail, soft limits, an armed/disarmed output, and a valve with a
hard maximum burst length.

Usage::

    python server/tools/controller_sim.py --url ws://127.0.0.1:8080/ws/hardware
    python server/tools/controller_sim.py --token "$TURRET_CONTROLLER_TOKEN"
    python server/tools/controller_sim.py --fail-homing   # exercise the error path
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets

from app.version import PROTOCOL_VERSION

log = logging.getLogger("controller-sim")

FIRMWARE_VERSION = "0.1.0-sim"


@dataclass
class Axis:
    position: float = 0.0
    target: float = 0.0
    velocity: float = 0.0
    rate_command: float | None = None
    min_deg: float = -90.0
    max_deg: float = 90.0
    max_speed: float = 60.0
    accel: float = 180.0

    @property
    def moving(self) -> bool:
        return abs(self.velocity) > 0.01 or abs(self.target - self.position) > 0.05

    def step(self, dt: float) -> None:
        if self.rate_command is not None:
            desired = max(-self.max_speed, min(self.max_speed, self.rate_command))
        else:
            error = self.target - self.position
            # Trapezoidal profile: never enter a corner faster than the
            # remaining distance allows you to leave it.
            approach = math.copysign(math.sqrt(2 * self.accel * abs(error)), error)
            desired = max(-self.max_speed, min(self.max_speed, approach))
            if abs(error) < 0.02:
                desired = 0.0

        delta = desired - self.velocity
        max_delta = self.accel * dt
        self.velocity += max(-max_delta, min(max_delta, delta))
        self.position += self.velocity * dt

        if self.position <= self.min_deg:
            self.position, self.velocity = self.min_deg, 0.0
        elif self.position >= self.max_deg:
            self.position, self.velocity = self.max_deg, 0.0

    def at_min_endstop(self) -> bool:
        return self.position <= self.min_deg + 0.05

    def at_max_endstop(self) -> bool:
        return self.position >= self.max_deg - 0.05


@dataclass
class SimulatedController:
    controller_id: str = "turret-1"
    max_spray_ms: int = 2000
    link_timeout_s: float = 6.0
    fail_homing: bool = False

    pan: Axis = field(default_factory=lambda: Axis(min_deg=-90, max_deg=90))
    tilt: Axis = field(default_factory=lambda: Axis(min_deg=-45, max_deg=45))

    homed: bool = False
    armed: bool = False
    valve_open: bool = False
    estop: bool = False
    state: str = "BOOT"
    error: str | None = None
    seq: int = 0
    booted_at: float = field(default_factory=time.monotonic)

    _jog_expires: float = 0.0
    _valve_closes_at: float = 0.0
    _homing: bool = False

    # -- physics ---------------------------------------------------------
    def tick(self, dt: float, now: float) -> None:
        if self.estop:
            self.pan.velocity = self.tilt.velocity = 0.0
            self.pan.rate_command = self.tilt.rate_command = None
            self.close_valve("estop")
            self.state = "ESTOP"
            return

        # A jog that stops being refreshed decays to a stop. This is the
        # firmware behaviour the UI relies on when a phone drops off Wi-Fi.
        if self._jog_expires and now > self._jog_expires:
            self.pan.rate_command = None
            self.tilt.rate_command = None
            self.pan.target = self.pan.position
            self.tilt.target = self.tilt.position
            self._jog_expires = 0.0

        if self.valve_open and now >= self._valve_closes_at:
            self.close_valve("timer")

        self.pan.step(dt)
        self.tilt.step(dt)

        if self._homing:
            self.state = "HOMING"
        elif self.pan.rate_command is not None or self.tilt.rate_command is not None:
            self.state = "JOGGING"
        elif self.pan.moving or self.tilt.moving:
            self.state = "MOVING"
        elif self.error:
            self.state = "FAULT"
        else:
            self.state = "IDLE"

    # -- valve -----------------------------------------------------------
    def open_valve(self, duration_ms: int, now: float) -> int:
        duration = max(1, min(duration_ms, self.max_spray_ms))
        self.valve_open = True
        self._valve_closes_at = now + duration / 1000.0
        return duration

    def close_valve(self, reason: str) -> None:
        if self.valve_open:
            log.info("valve closed (%s)", reason)
        self.valve_open = False
        self._valve_closes_at = 0.0

    # -- status ----------------------------------------------------------
    def status(self) -> dict[str, object]:
        self.seq += 1
        return {
            "v": PROTOCOL_VERSION,
            "type": "status",
            "seq": self.seq,
            "uptime_ms": int((time.monotonic() - self.booted_at) * 1000),
            "state": self.state,
            "pan_deg": round(self.pan.position, 3),
            "tilt_deg": round(self.tilt.position, 3),
            "target_pan_deg": round(self.pan.target, 3),
            "target_tilt_deg": round(self.tilt.target, 3),
            "pan_rate_deg_s": round(self.pan.velocity, 3),
            "tilt_rate_deg_s": round(self.tilt.velocity, 3),
            "moving": self.pan.moving or self.tilt.moving,
            "homed": self.homed,
            "armed": self.armed,
            "valve_open": self.valve_open,
            "limit_pan_min": self.pan.at_min_endstop(),
            "limit_pan_max": self.pan.at_max_endstop(),
            "limit_tilt_min": self.tilt.at_min_endstop(),
            "limit_tilt_max": self.tilt.at_max_endstop(),
            "estop": self.estop,
            "error": self.error,
        }


def ack(
    command_id: int,
    ok: bool = True,
    code: str | None = None,
    error: str | None = None,
    clamped: bool = False,
) -> str:
    payload: dict[str, object] = {
        "v": PROTOCOL_VERSION,
        "type": "ack",
        "id": command_id,
        "ok": ok,
        "clamped": clamped,
    }
    if code:
        payload["code"] = code
    if error:
        payload["error"] = error
    return json.dumps(payload)


def event(name: str, detail: dict[str, object] | None = None) -> str:
    return json.dumps(
        {"v": PROTOCOL_VERSION, "type": "event", "event": name, "detail": detail or {}}
    )


class Simulator:
    def __init__(self, url: str, token: str | None, controller: SimulatedController) -> None:
        self.url = url
        self.token = token
        self.controller = controller
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._session()
                backoff = 1.0
            except (OSError, websockets.WebSocketException) as exc:
                log.warning("link lost (%s); reconnecting in %.0fs", exc, backoff)
                # Failsafe: the link is gone, so nothing may keep running.
                self.controller.close_valve("link lost")
                self.controller.armed = False
                self.controller.pan.rate_command = None
                self.controller.tilt.rate_command = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    async def _session(self) -> None:
        async with websockets.connect(self.url, max_size=2**16) as ws:
            self._ws = ws
            await ws.send(
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "hello",
                        "controller_id": self.controller.controller_id,
                        "firmware_version": FIRMWARE_VERSION,
                        "protocol_version": PROTOCOL_VERSION,
                        "token": self.token,
                        "capabilities": ["pan", "tilt", "valve", "endstops"],
                        "hardware": {"chip": "simulator", "mac": "00:00:00:00:00:00"},
                    }
                )
            )
            raw = await ws.recv()
            hello_ack = json.loads(raw)
            if not hello_ack.get("accepted"):
                log.error("server rejected the controller: %s", hello_ack.get("reason"))
                await asyncio.sleep(5)
                return
            log.info("connected to %s", self.url)
            await ws.send(event("boot", {"firmware": FIRMWARE_VERSION}))

            physics = asyncio.create_task(self._physics_loop(ws))
            try:
                async for message in ws:
                    await self._handle(ws, json.loads(message))
            finally:
                physics.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await physics
                self._ws = None

    async def _physics_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        dt = 0.02
        last_status = 0.0
        while True:
            now = time.monotonic()
            self.controller.tick(dt, now)
            if now - last_status >= 0.1:
                last_status = now
                await ws.send(json.dumps(self.controller.status()))
            await asyncio.sleep(dt)

    async def _handle(self, ws: websockets.WebSocketClientProtocol, message: dict) -> None:
        controller = self.controller
        kind = message.get("type")
        command_id = int(message.get("id", 0) or 0)
        now = time.monotonic()

        if kind == "ping":
            await ws.send(
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "pong",
                        "id": command_id,
                        "t_ms": message.get("t_ms", 0),
                    }
                )
            )
            return

        if kind == "stop":
            emergency = bool(message.get("emergency"))
            controller.pan.rate_command = controller.tilt.rate_command = None
            controller.pan.target = controller.pan.position
            controller.tilt.target = controller.tilt.position
            controller.close_valve("stop")
            controller.armed = False
            if emergency:
                controller.pan.velocity = controller.tilt.velocity = 0.0
                controller.estop = True
                controller.homed = False
                await ws.send(event("estop"))
            elif controller.estop:
                controller.estop = False
                controller.error = None
                await ws.send(event("estop_cleared"))
            await ws.send(ack(command_id))
            return

        if controller.estop:
            await ws.send(ack(command_id, False, "ESTOP", "emergency stop is latched"))
            return

        if kind == "move_absolute":
            if not controller.homed:
                await ws.send(ack(command_id, False, "NOT_HOMED", "homing required"))
                return
            pan = float(message["pan_deg"])
            tilt = float(message["tilt_deg"])
            clamped = not (
                controller.pan.min_deg <= pan <= controller.pan.max_deg
                and controller.tilt.min_deg <= tilt <= controller.tilt.max_deg
            )
            controller.pan.target = max(controller.pan.min_deg, min(controller.pan.max_deg, pan))
            controller.tilt.target = max(
                controller.tilt.min_deg, min(controller.tilt.max_deg, tilt)
            )
            controller.pan.rate_command = controller.tilt.rate_command = None
            if message.get("max_speed_deg_s"):
                controller.pan.max_speed = controller.tilt.max_speed = float(
                    message["max_speed_deg_s"]
                )
            await ws.send(ack(command_id, clamped=clamped))

        elif kind == "move_relative":
            controller.pan.target = controller.pan.position + float(message["pan_delta_deg"])
            controller.tilt.target = controller.tilt.position + float(message["tilt_delta_deg"])
            controller.pan.rate_command = controller.tilt.rate_command = None
            await ws.send(ack(command_id))

        elif kind == "jog":
            controller.pan.rate_command = float(message["pan_rate_deg_s"])
            controller.tilt.rate_command = float(message["tilt_rate_deg_s"])
            controller._jog_expires = now + float(message.get("ttl_ms", 400)) / 1000.0
            await ws.send(ack(command_id))

        elif kind == "home":
            await ws.send(event("homing_started"))
            controller._homing = True
            await asyncio.sleep(random.uniform(0.8, 1.6))
            controller._homing = False
            if controller.fail_homing:
                controller.error = "endstop not found"
                await ws.send(event("homing_failed", {"axis": "pan"}))
                await ws.send(ack(command_id, False, "TIMEOUT", "endstop not found"))
                return
            controller.pan.position = controller.pan.target = 0.0
            controller.tilt.position = controller.tilt.target = 0.0
            controller.pan.velocity = controller.tilt.velocity = 0.0
            controller.homed = True
            controller.error = None
            await ws.send(event("homing_completed", {"pan_deg": 0.0, "tilt_deg": 0.0}))
            await ws.send(ack(command_id))

        elif kind == "arm_output":
            controller.armed = bool(message.get("armed"))
            if not controller.armed:
                controller.close_valve("disarmed")
            await ws.send(ack(command_id))

        elif kind == "spray":
            if not controller.armed:
                await ws.send(ack(command_id, False, "DISARMED", "output is disarmed"))
                return
            duration = controller.open_valve(int(message["duration_ms"]), now)
            await ws.send(event("valve_opened", {"duration_ms": duration}))
            await ws.send(ack(command_id, clamped=duration != int(message["duration_ms"])))

        elif kind == "spray_stop":
            controller.close_valve("command")
            await ws.send(ack(command_id))

        elif kind == "set_config":
            config = message.get("config", {})
            controller.pan.min_deg = float(config.get("pan_min_deg", controller.pan.min_deg))
            controller.pan.max_deg = float(config.get("pan_max_deg", controller.pan.max_deg))
            controller.tilt.min_deg = float(config.get("tilt_min_deg", controller.tilt.min_deg))
            controller.tilt.max_deg = float(config.get("tilt_max_deg", controller.tilt.max_deg))
            speed = float(config.get("max_speed_deg_s", controller.pan.max_speed))
            accel = float(config.get("accel_deg_s2", controller.pan.accel))
            controller.pan.max_speed = controller.tilt.max_speed = speed
            controller.pan.accel = controller.tilt.accel = accel
            log.info("configuration applied: %s", json.dumps(config)[:160])
            await ws.send(event("config_saved"))
            await ws.send(ack(command_id))

        elif kind == "get_config":
            await ws.send(
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config",
                        "id": command_id,
                        "config": {
                            "pan_min_deg": controller.pan.min_deg,
                            "pan_max_deg": controller.pan.max_deg,
                            "tilt_min_deg": controller.tilt.min_deg,
                            "tilt_max_deg": controller.tilt.max_deg,
                            "max_speed_deg_s": controller.pan.max_speed,
                            "accel_deg_s2": controller.pan.accel,
                            "max_spray_ms": controller.max_spray_ms,
                        },
                    }
                )
            )
            await ws.send(ack(command_id))

        elif kind == "reboot":
            await ws.send(ack(command_id))
            controller.homed = False
            controller.armed = False
            controller.close_valve("reboot")
            await ws.close()

        else:
            await ws.send(ack(command_id, False, "UNSUPPORTED", f"unknown type: {kind}"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8080/ws/hardware")
    parser.add_argument("--token", default=None, help="pre-shared controller token")
    parser.add_argument("--controller-id", default="turret-1")
    parser.add_argument("--max-spray-ms", type=int, default=2000)
    parser.add_argument(
        "--fail-homing", action="store_true", help="always fail homing (error-path testing)"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    controller = SimulatedController(
        controller_id=args.controller_id,
        max_spray_ms=args.max_spray_ms,
        fail_homing=args.fail_homing,
    )
    simulator = Simulator(args.url, args.token, controller)
    try:
        asyncio.run(simulator.run())
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
