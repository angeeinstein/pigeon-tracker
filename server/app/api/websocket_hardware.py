"""Controller (ESP32) WebSocket endpoint.

The controller connects *to* the server, so it owns reconnection and the server
owns nothing but a socket it can drop at any time. The handshake is strict:
first frame must be a valid ``hello`` with a matching protocol version and, if
configured, the correct pre-shared token. Anything else is closed immediately
with a specific close code so the firmware can log a useful reason.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.api.auth import check_controller_token
from app.api.deps import get_runtime_ws
from app.config import get_config
from app.logging_config import get_logger
from app.services import event_log as ev
from app.turret import protocol as proto

router = APIRouter()
log = get_logger(__name__)

#: How long the controller has to send its `hello` after connecting.
HANDSHAKE_TIMEOUT_S = 10.0


class _ControllerAdapter:
    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def send_text(self, data: str) -> None:
        await self._ws.send_text(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._ws.client_state is WebSocketState.CONNECTED:
            await self._ws.close(code=code, reason=reason[:120])


@router.websocket("/ws/hardware")
async def hardware_socket(websocket: WebSocket) -> None:
    config = get_config()
    runtime = get_runtime_ws(websocket)
    peer = websocket.client.host if websocket.client else "?"

    await websocket.accept()
    adapter = _ControllerAdapter(websocket)
    attached = False
    post_connect: asyncio.Task[None] | None = None

    if runtime.settings.controller.mode == "simulated":
        await adapter.close(proto.CLOSE_REPLACED, "simulated controller mode is enabled")
        return

    try:
        # --- handshake ---------------------------------------------------
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            await adapter.close(proto.CLOSE_BAD_REQUEST, "handshake timeout")
            return

        try:
            message = proto.decode_controller_message(raw)
        except proto.ProtocolError as exc:
            log.warning(
                "rejected controller frame", extra={"ctx": {"peer": peer, "error": str(exc)}}
            )
            await adapter.close(proto.CLOSE_BAD_REQUEST, str(exc))
            return

        if not isinstance(message, proto.Hello):
            await adapter.close(proto.CLOSE_BAD_REQUEST, "first frame must be 'hello'")
            return

        header_token = websocket.headers.get("authorization", "")
        bearer = header_token[7:].strip() if header_token.lower().startswith("bearer ") else None
        if not check_controller_token(config.controller_token, message.token or bearer):
            log.warning("controller authentication failed", extra={"ctx": {"peer": peer}})
            await runtime.events.emit(
                ev.CAT_SECURITY,
                "controller rejected: bad token",
                level="warning",
                data={"peer": peer, "controller_id": message.controller_id},
            )
            await adapter.send_text(
                proto.encode(proto.HelloAck(accepted=False, reason="unauthorized"))
            )
            await adapter.close(proto.CLOSE_UNAUTHORIZED, "unauthorized")
            return

        if not config.controller_token:
            log.warning(
                "controller connected without a token (TURRET_CONTROLLER_TOKEN is unset)",
                extra={"ctx": {"peer": peer}},
            )

        expected_id = runtime.settings.controller.controller_id
        if expected_id and message.controller_id != expected_id:
            log.warning(
                "unexpected controller id",
                extra={"ctx": {"expected": expected_id, "got": message.controller_id}},
            )

        accepted = await runtime.turret.attach(adapter, message)
        attached = True
        if not accepted:
            await runtime.events.emit(
                ev.CAT_CONTROLLER,
                "controller rejected: protocol version mismatch",
                level="error",
                data={
                    "firmware_protocol": message.protocol_version,
                    "server_protocol": proto.PROTOCOL_VERSION,
                    "firmware_version": message.firmware_version,
                },
            )
            await adapter.close(proto.CLOSE_VERSION_MISMATCH, "protocol version mismatch")
            return

        # Post-connect work (pushing configuration, disarming, optional auto
        # homing) sends commands and waits for their acknowledgements. Those
        # acknowledgements can only arrive through the read loop below, so this
        # must NOT be awaited here - doing so deadlocks every command until it
        # times out, and makes the link useless for the first few seconds of
        # every connection.
        post_connect = asyncio.create_task(
            runtime.on_controller_connected(), name="controller-post-connect"
        )

        # --- message loop -------------------------------------------------
        while True:
            raw = await websocket.receive_text()
            try:
                message = proto.decode_controller_message(raw)
            except proto.ProtocolError as exc:
                # A bad frame from an authenticated controller is a firmware
                # bug, not an attack: log it, keep the link, keep moving.
                log.warning(
                    "invalid controller frame",
                    extra={"ctx": {"peer": peer, "error": str(exc)}},
                )
                continue
            await runtime.turret.handle_message(message)

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("controller socket failed")
    finally:
        if post_connect is not None and not post_connect.done():
            post_connect.cancel()
        if attached:
            detached = await runtime.turret.detach(adapter)
            if detached:
                await runtime.on_controller_disconnected()
