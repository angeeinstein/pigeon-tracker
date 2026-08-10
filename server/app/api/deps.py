"""FastAPI dependencies: runtime access and authentication guards."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket, status

from app.api.auth import SESSION_COOKIE, Session, verify_token
from app.config import DeploymentConfig, get_config
from app.services.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only during a failed startup
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="runtime not started"
        )
    return runtime


def get_runtime_ws(websocket: WebSocket) -> Runtime:
    runtime: Runtime | None = getattr(websocket.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover
        raise RuntimeError("runtime not started")
    return runtime


def _extract_token(
    cookies: dict[str, str], authorization: str | None, query_token: str | None
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    cookie = cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    # Query parameter is a concession to <img src> / EventSource style clients
    # that cannot set headers; it is only ever a session token, never a password.
    return query_token


def require_auth(request: Request) -> Session | None:
    """Authentication guard. A no-op when auth is disabled in the config."""
    config = get_config()
    if not config.auth_enabled:
        return None
    token = _extract_token(
        request.cookies,
        request.headers.get("authorization"),
        request.query_params.get("token"),
    )
    session = verify_token(config.resolve_secret_key(), token or "")
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return session


def websocket_authorised(websocket: WebSocket) -> bool:
    config = get_config()
    if not config.auth_enabled:
        return True
    token = _extract_token(
        dict(websocket.cookies),
        websocket.headers.get("authorization"),
        websocket.query_params.get("token"),
    )
    return verify_token(config.resolve_secret_key(), token or "") is not None


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
ConfigDep = Annotated[DeploymentConfig, Depends(get_config)]
AuthDep = Annotated[Session | None, Depends(require_auth)]
