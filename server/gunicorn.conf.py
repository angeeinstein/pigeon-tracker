"""Gunicorn configuration for the production service.

Why exactly one worker: this process owns live hardware state — the controller
WebSocket, the camera decoders, the loaded model and the targeting state
machine. A second worker would open a second controller link, load a second
copy of the model, and run a second state machine competing for the same
turret. Gunicorn is here for supervision, graceful reloads and clean signal
handling, not for horizontal scaling.

Concurrency comes from asyncio inside that worker plus a thread pool for
inference and JPEG encoding.
"""

from __future__ import annotations

import os

_config_port = os.environ.get("TURRET_PORT", "8080")
_config_host = os.environ.get("TURRET_HOST", "0.0.0.0")

bind = f"{_config_host}:{_config_port}"
workers = 1
worker_class = "uvicorn.workers.UvicornWorker"

# Vision work can block a request briefly; the default 30 s is plenty, but a
# model load on first start can exceed it on a slow CPU.
timeout = 180
graceful_timeout = 30
keepalive = 5

# Logging goes to stdout/stderr and from there into the journal. The
# application installs its own structured formatter, so gunicorn's access log
# stays off to avoid duplicate, differently-formatted lines.
accesslog = None
errorlog = "-"
loglevel = os.environ.get("TURRET_LOG_LEVEL", "info").lower()

# Never silently restart the worker mid-engagement.
max_requests = 0
preload_app = False
