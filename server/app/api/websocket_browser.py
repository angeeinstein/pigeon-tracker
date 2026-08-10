"""Browser WebSockets: live telemetry and the low-latency JPEG preview.

Telemetry is push-only from the server plus a small inbound command set — jog
in particular, because routing a joystick through REST adds a request per
sample and feels awful on a phone.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.api.deps import get_runtime_ws, websocket_authorised
from app.logging_config import get_logger
from app.services.runtime import Runtime
from app.turret.models import TurretError
from app.version import version_info

router = APIRouter()
log = get_logger(__name__)

#: Largest inbound browser frame we will even look at.
MAX_CLIENT_FRAME = 4096

CLOSE_UNAUTHORIZED = 4401
CLOSE_TOO_LARGE = 4413


class _WebSocketAdapter:
    """Adapts FastAPI's WebSocket to the hub's minimal interface."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def send_text(self, data: str) -> None:
        await self._ws.send_text(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._ws.client_state is WebSocketState.CONNECTED:
            await self._ws.close(code=code, reason=reason)


@router.websocket("/ws/telemetry")
async def telemetry_socket(websocket: WebSocket) -> None:
    if not websocket_authorised(websocket):
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return

    runtime = get_runtime_ws(websocket)
    await websocket.accept()
    adapter = _WebSocketAdapter(websocket)
    await runtime.telemetry.register(adapter)

    async def push_event(record: dict[str, Any]) -> None:
        await runtime.telemetry.send_to(adapter, {"type": "event", "data": record})

    runtime.events.subscribe(push_event)

    try:
        await runtime.telemetry.send_to(
            adapter,
            {
                "type": "hello",
                "data": {
                    "version": version_info(),
                    "settings": runtime.settings_store.as_dict(),
                    "recent_events": runtime.events.recent[-30:],
                },
            },
        )
        await runtime.telemetry.send_to(
            adapter, {"type": "telemetry", "data": runtime.telemetry_snapshot()}
        )

        while True:
            raw = await websocket.receive_text()
            if len(raw) > MAX_CLIENT_FRAME:
                await websocket.close(code=CLOSE_TOO_LARGE)
                return
            await _handle_client_message(runtime, adapter, raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("telemetry socket failed")
    finally:
        runtime.events.unsubscribe(push_event)
        await runtime.telemetry.unregister(adapter)


async def _handle_client_message(runtime: Runtime, adapter: _WebSocketAdapter, raw: str) -> None:
    """Handle the small inbound command set. Unknown types are ignored."""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return

    kind = message.get("type")
    try:
        if kind == "ping":
            await runtime.telemetry.send_to(adapter, {"type": "pong", "data": {"t": time.time()}})
        elif kind == "jog":
            pan = float(message.get("pan", 0.0))
            tilt = float(message.get("tilt", 0.0))
            await runtime.jog(pan, tilt)
        elif kind == "stop":
            await runtime.stop_motion()
        elif kind == "telemetry":
            await runtime.telemetry.send_to(
                adapter, {"type": "telemetry", "data": runtime.telemetry_snapshot()}
            )
    except (TurretError, ValueError, TypeError) as exc:
        await runtime.telemetry.send_to(
            adapter, {"type": "error", "data": {"command": kind, "message": str(exc)}}
        )


@router.websocket("/ws/preview")
async def preview_socket(websocket: WebSocket) -> None:
    """Binary JPEG preview.

    Lower latency and less overhead than MJPEG over HTTP, and the client can
    stop requesting frames when the tab is hidden. The MJPEG endpoint remains
    available as the no-JavaScript fallback.
    """
    if not websocket_authorised(websocket):
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return

    runtime = get_runtime_ws(websocket)
    camera_id = websocket.query_params.get("camera_id") or None
    overlays_param = websocket.query_params.get("overlays")
    overlays = None if overlays_param is None else overlays_param.lower() in {"1", "true", "yes"}

    await websocket.accept()
    last_seq = -1
    try:
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
                    await websocket.send_bytes(payload)
                except LookupError:
                    pass
            elif frame is None:
                await websocket.send_text(json.dumps({"type": "status", "camera": "unavailable"}))
                await asyncio.sleep(1.0)
                continue

            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("preview socket closed", exc_info=True)
