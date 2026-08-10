"""Application entry point.

Run in production through gunicorn with a **single** uvicorn worker (see
``gunicorn.conf.py``); in development with ``uvicorn app.main:app --reload``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.api import routes, websocket_browser, websocket_hardware
from app.config import DeploymentConfig, get_config
from app.database.db import dispose_engine, init_engine
from app.logging_config import configure_logging, get_logger
from app.services.runtime import Runtime
from app.version import SERVER_VERSION, version_info

log = get_logger(__name__)

PLACEHOLDER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>turret-control</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e8eb;margin:0;
      display:grid;place-items:center;min-height:100vh;padding:2rem}
 main{max-width:40rem} code{background:#1b1f27;padding:.15rem .4rem;border-radius:.25rem}
 a{color:#6ea8fe}
</style></head><body><main>
<h1>turret-control is running</h1>
<p>The web interface has not been built yet, so only the API is available.</p>
<p>Build it with:</p>
<pre><code>cd server/frontend
npm install
npm run build</code></pre>
<p>Or run the dev server with <code>npm run dev</code> (proxies to this API), and check
<a href="/api/health">/api/health</a> meanwhile.</p>
</main></body></html>
"""


class PayloadLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies before they are buffered."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        length = request.headers.get("content-length")
        if length is not None:
            with contextlib.suppress(ValueError):
                if int(length) > self.max_bytes:
                    return JSONResponse({"detail": "payload too large"}, status_code=413)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config: DeploymentConfig = app.state.config
    config.ensure_directories()
    init_engine(config.database_path)

    runtime = Runtime(config)
    app.state.runtime = runtime
    try:
        await runtime.start()
    except Exception:
        # A failure here must not leave a half-started runtime behind.
        log.exception("runtime failed to start")
        await runtime.stop()
        dispose_engine()
        raise

    log.info(
        "server ready",
        extra={"ctx": {"version": SERVER_VERSION, "port": config.port}},
    )
    try:
        yield
    finally:
        await runtime.stop()
        dispose_engine()


def create_app(config: DeploymentConfig | None = None) -> FastAPI:
    config = config or get_config()
    configure_logging(config.log_level, config.log_format)

    app = FastAPI(
        title="turret-control",
        version=SERVER_VERSION,
        description="Networked 2-axis ball turret: control, vision and targeting.",
        lifespan=lifespan,
        root_path=config.root_path,
    )
    app.state.config = config

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(PayloadLimitMiddleware, max_bytes=config.max_payload_bytes)
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(routes.router)
    app.include_router(websocket_browser.router)
    app.include_router(websocket_hardware.router)

    _mount_frontend(app, config.resolved_static_dir)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the built SPA, falling back to a helpful placeholder page."""
    index = static_dir / "index.html"
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Any:
        if full_path.startswith("api/") or full_path.startswith("ws/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (static_dir / full_path).resolve()
        if (
            full_path
            and str(candidate).startswith(str(static_dir.resolve()))
            and candidate.is_file()
        ):
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(PLACEHOLDER_PAGE)


app = create_app()


def main() -> None:  # pragma: no cover - convenience for `python -m app.main`
    import uvicorn

    config = get_config()
    print(f"turret-control {version_info()} on http://{config.host}:{config.port}")
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        log_config=None,
        ws_max_size=config.max_payload_bytes,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
