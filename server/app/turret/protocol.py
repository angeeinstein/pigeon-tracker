"""Wire protocol between the server and the turret controller.

This module is the authoritative definition (see ``docs/PROTOCOL.md`` for the
prose version). ``server/tools/gen_protocol_header.py`` generates
``firmware/include/protocol_generated.h`` from the constants below, so the
firmware literally cannot use a message name or error code the server does not
know about.

Every inbound frame is validated by Pydantic before anything else touches it:
a controller — or something pretending to be one — cannot reach the rest of the
system with a malformed payload.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.version import PROTOCOL_VERSION

__all__ = [
    "PROTOCOL_VERSION",
    "ControllerEvent",
    "ControllerMessage",
    "ControllerState",
    "ErrorCode",
    "MessageType",
    "ProtocolError",
    "ServerMessage",
    "decode_controller_message",
    "encode",
]


class ProtocolError(ValueError):
    """Raised when a frame cannot be parsed or violates the protocol."""


# --------------------------------------------------------------------------
# Constants (mirrored into the firmware header)
# --------------------------------------------------------------------------


class MessageType:
    # server -> controller
    HELLO_ACK = "hello_ack"
    MOVE_ABSOLUTE = "move_absolute"
    MOVE_RELATIVE = "move_relative"
    JOG = "jog"
    HOME = "home"
    STOP = "stop"
    SPRAY = "spray"
    SPRAY_STOP = "spray_stop"
    ARM_OUTPUT = "arm_output"
    SET_CONFIG = "set_config"
    GET_CONFIG = "get_config"
    PING = "ping"
    REBOOT = "reboot"

    # controller -> server
    HELLO = "hello"
    STATUS = "status"
    ACK = "ack"
    EVENT = "event"
    PONG = "pong"
    CONFIG = "config"
    LOG = "log"


SERVER_MESSAGE_TYPES = (
    MessageType.HELLO_ACK,
    MessageType.MOVE_ABSOLUTE,
    MessageType.MOVE_RELATIVE,
    MessageType.JOG,
    MessageType.HOME,
    MessageType.STOP,
    MessageType.SPRAY,
    MessageType.SPRAY_STOP,
    MessageType.ARM_OUTPUT,
    MessageType.SET_CONFIG,
    MessageType.GET_CONFIG,
    MessageType.PING,
    MessageType.REBOOT,
)

CONTROLLER_MESSAGE_TYPES = (
    MessageType.HELLO,
    MessageType.STATUS,
    MessageType.ACK,
    MessageType.EVENT,
    MessageType.PONG,
    MessageType.CONFIG,
    MessageType.LOG,
)


class ErrorCode:
    NOT_HOMED = "NOT_HOMED"
    LIMIT = "LIMIT"
    DISARMED = "DISARMED"
    ESTOP = "ESTOP"
    INVALID_PARAM = "INVALID_PARAM"
    BUSY = "BUSY"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"
    FAULT = "FAULT"


ERROR_CODES = (
    ErrorCode.NOT_HOMED,
    ErrorCode.LIMIT,
    ErrorCode.DISARMED,
    ErrorCode.ESTOP,
    ErrorCode.INVALID_PARAM,
    ErrorCode.BUSY,
    ErrorCode.TIMEOUT,
    ErrorCode.UNSUPPORTED,
    ErrorCode.FAULT,
)


class ControllerState:
    BOOT = "BOOT"
    IDLE = "IDLE"
    MOVING = "MOVING"
    HOMING = "HOMING"
    JOGGING = "JOGGING"
    FAULT = "FAULT"
    ESTOP = "ESTOP"


CONTROLLER_STATES = (
    ControllerState.BOOT,
    ControllerState.IDLE,
    ControllerState.MOVING,
    ControllerState.HOMING,
    ControllerState.JOGGING,
    ControllerState.FAULT,
    ControllerState.ESTOP,
)


class ControllerEvent:
    BOOT = "boot"
    HOMING_STARTED = "homing_started"
    HOMING_COMPLETED = "homing_completed"
    HOMING_FAILED = "homing_failed"
    LIMIT_HIT = "limit_hit"
    ESTOP = "estop"
    ESTOP_CLEARED = "estop_cleared"
    VALVE_OPENED = "valve_opened"
    VALVE_CLOSED = "valve_closed"
    WATCHDOG_RESET = "watchdog_reset"
    CONFIG_SAVED = "config_saved"
    FAULT = "fault"


CONTROLLER_EVENTS = (
    ControllerEvent.BOOT,
    ControllerEvent.HOMING_STARTED,
    ControllerEvent.HOMING_COMPLETED,
    ControllerEvent.HOMING_FAILED,
    ControllerEvent.LIMIT_HIT,
    ControllerEvent.ESTOP,
    ControllerEvent.ESTOP_CLEARED,
    ControllerEvent.VALVE_OPENED,
    ControllerEvent.VALVE_CLOSED,
    ControllerEvent.WATCHDOG_RESET,
    ControllerEvent.CONFIG_SAVED,
    ControllerEvent.FAULT,
)

#: Reject anything larger before parsing (also enforced at the socket).
MAX_FRAME_BYTES = 16 * 1024

#: WebSocket close codes used by the hardware endpoint.
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_REPLACED = 4409
CLOSE_VERSION_MISMATCH = 4426


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------


class _Base(BaseModel):
    # Forward compatible: a newer firmware may send fields we do not know yet.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    v: int = PROTOCOL_VERSION


# --------------------------------------------------------------------------
# Server -> controller
# --------------------------------------------------------------------------


class HelloAck(_Base):
    type: Literal["hello_ack"] = "hello_ack"
    accepted: bool
    reason: str | None = None
    server_version: str = ""
    protocol_version: int = PROTOCOL_VERSION
    time_ms: int = 0


class MoveAbsolute(_Base):
    type: Literal["move_absolute"] = "move_absolute"
    id: int
    pan_deg: float = Field(ge=-360.0, le=360.0)
    tilt_deg: float = Field(ge=-360.0, le=360.0)
    max_speed_deg_s: float | None = Field(default=None, gt=0, le=1000.0)
    accel_deg_s2: float | None = Field(default=None, gt=0, le=10000.0)


class MoveRelative(_Base):
    type: Literal["move_relative"] = "move_relative"
    id: int
    pan_delta_deg: float = Field(ge=-720.0, le=720.0)
    tilt_delta_deg: float = Field(ge=-720.0, le=720.0)
    max_speed_deg_s: float | None = Field(default=None, gt=0, le=1000.0)


class Jog(_Base):
    type: Literal["jog"] = "jog"
    id: int
    pan_rate_deg_s: float = Field(ge=-1000.0, le=1000.0)
    tilt_rate_deg_s: float = Field(ge=-1000.0, le=1000.0)
    #: The controller decelerates to a stop if no refresh arrives in time.
    ttl_ms: int = Field(default=400, ge=50, le=5000)


class Home(_Base):
    type: Literal["home"] = "home"
    id: int
    axes: Literal["both", "pan", "tilt"] = "both"


class Stop(_Base):
    type: Literal["stop"] = "stop"
    id: int
    emergency: bool = False


class Spray(_Base):
    type: Literal["spray"] = "spray"
    id: int
    duration_ms: int = Field(ge=1, le=60_000)


class SprayStop(_Base):
    type: Literal["spray_stop"] = "spray_stop"
    id: int


class ArmOutput(_Base):
    type: Literal["arm_output"] = "arm_output"
    id: int
    armed: bool


class SetConfig(_Base):
    type: Literal["set_config"] = "set_config"
    id: int
    config: dict[str, Any]


class GetConfig(_Base):
    type: Literal["get_config"] = "get_config"
    id: int


class Ping(_Base):
    type: Literal["ping"] = "ping"
    id: int
    t_ms: int = 0


class Reboot(_Base):
    type: Literal["reboot"] = "reboot"
    id: int


ServerMessage = (
    HelloAck
    | MoveAbsolute
    | MoveRelative
    | Jog
    | Home
    | Stop
    | Spray
    | SprayStop
    | ArmOutput
    | SetConfig
    | GetConfig
    | Ping
    | Reboot
)


# --------------------------------------------------------------------------
# Controller -> server
# --------------------------------------------------------------------------


class Hello(_Base):
    type: Literal["hello"] = "hello"
    controller_id: str = Field(default="turret-1", max_length=64)
    firmware_version: str = Field(default="0.0.0", max_length=32)
    protocol_version: int = 0
    token: str | None = Field(default=None, max_length=256)
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    hardware: dict[str, Any] = Field(default_factory=dict)


class Status(_Base):
    type: Literal["status"] = "status"
    seq: int = 0
    uptime_ms: int = 0
    state: str = ControllerState.IDLE
    pan_deg: float = 0.0
    tilt_deg: float = 0.0
    target_pan_deg: float | None = None
    target_tilt_deg: float | None = None
    pan_rate_deg_s: float = 0.0
    tilt_rate_deg_s: float = 0.0
    moving: bool = False
    homed: bool = False
    armed: bool = False
    valve_open: bool = False
    limit_pan_min: bool = False
    limit_pan_max: bool = False
    limit_tilt_min: bool = False
    limit_tilt_max: bool = False
    estop: bool = False
    error: str | None = Field(default=None, max_length=256)


class Ack(_Base):
    type: Literal["ack"] = "ack"
    id: int
    ok: bool
    clamped: bool = False
    code: str | None = Field(default=None, max_length=32)
    error: str | None = Field(default=None, max_length=256)


class Event(_Base):
    type: Literal["event"] = "event"
    event: str = Field(max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)


class Pong(_Base):
    type: Literal["pong"] = "pong"
    id: int
    t_ms: int = 0


class ConfigReport(_Base):
    type: Literal["config"] = "config"
    id: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class LogMessage(_Base):
    type: Literal["log"] = "log"
    level: Literal["debug", "info", "warn", "error"] = "info"
    msg: str = Field(max_length=512)


ControllerMessage = Annotated[
    Hello | Status | Ack | Event | Pong | ConfigReport | LogMessage,
    Field(discriminator="type"),
]


class _ControllerEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: ControllerMessage


# --------------------------------------------------------------------------
# Codec
# --------------------------------------------------------------------------


def encode(message: BaseModel) -> str:
    """Serialise a server message to a WebSocket text frame."""
    return message.model_dump_json(exclude_none=True)


def decode_controller_message(raw: str | bytes) -> Any:
    """Parse and validate a controller frame.

    Raises :class:`ProtocolError` for anything that is not a well-formed,
    known, in-range message — the caller closes the connection rather than
    guessing at intent.
    """
    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME_BYTES:
            raise ProtocolError("frame too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("frame is not valid UTF-8") from exc
    elif len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise ProtocolError("frame must be a JSON object")
    message_type = payload.get("type")
    if not isinstance(message_type, str):
        raise ProtocolError("missing 'type'")
    if message_type not in CONTROLLER_MESSAGE_TYPES:
        raise ProtocolError(f"unknown message type: {message_type!r}")

    try:
        return _ControllerEnvelope.model_validate({"message": payload}).message
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"][1:])
        raise ProtocolError(f"{message_type}: {location}: {first['msg']}") from exc
