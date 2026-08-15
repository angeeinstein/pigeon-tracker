"""RTSP camera source.

Two capture backends, picked automatically:

* **GStreamer** — preferred. ``appsink drop=true max-buffers=1`` guarantees the
  decoder never accumulates a backlog, and ``latency`` is directly tunable.
  Requires an OpenCV build with GStreamer support (Debian's ``python3-opencv``
  has it; the ``opencv-python`` PyPI wheels do not).
* **FFmpeg** — fallback, always present in the PyPI wheels. Latency is tamed
  with ``nobuffer``/``low_delay`` flags plus a one-frame capture buffer.

Either way the decode loop runs in its own thread, reconnects on failure, and
treats "no frame for N seconds" as a failure — RTSP sources routinely go quiet
without closing the socket.
"""

from __future__ import annotations

import contextlib
import os
import re
import string
import threading
import time
from functools import lru_cache
from urllib.parse import quote, urlsplit, urlunsplit

import cv2
import numpy as np

from app.camera.base import CameraSource, Frame
from app.camera.credentials import CameraCredentials
from app.logging_config import get_logger
from app.services.settings_schema import CameraConfig

log = get_logger(__name__)

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_credentials(url: str) -> str:
    """Expand ``${VAR}`` placeholders from the environment.

    Lets a camera URL live in the database while its password stays in the
    protected environment file. Unknown variables are left as-is so the
    resulting connection error names the missing variable.
    """

    def _replace(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return _ENV_PLACEHOLDER.sub(_replace, url)


def redact_url(url: str) -> str:
    """Strip credentials so a URL can safely be logged or shown in the UI."""
    return re.sub(r"//[^/@]*@", "//***:***@", url)


def inject_credentials(url: str, credentials: CameraCredentials | None) -> str:
    """Add protected credentials to a URL that does not already contain any."""
    if credentials is None:
        return url
    parsed = urlsplit(url)
    if parsed.username is not None or not parsed.hostname:
        return url
    username = quote(credentials.username, safe="")
    password = quote(credentials.password, safe="")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit(
        (
            parsed.scheme,
            f"{username}:{password}@{host}{port}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


@lru_cache(maxsize=1)
def gstreamer_available() -> bool:
    try:
        info = cv2.getBuildInformation()
    except Exception:  # pragma: no cover - defensive
        return False
    match = re.search(r"GStreamer:\s*(\S+)", info)
    return bool(match and match.group(1).upper() in {"YES", "ON"})


def build_gstreamer_pipeline(config: CameraConfig, url: str) -> str:
    """Low-latency RTSP pipeline.

    ``decodebin`` keeps this codec-agnostic (H.264 and H.265 cameras both
    work), and hardware decoders are picked up automatically when present.
    """
    protocols = "tcp" if config.transport == "tcp" else "udp"
    # Keep the decoded native image available to Python. CameraSource publishes
    # a downscaled copy for normal preview/inference while retaining one native
    # frame for motion-guided high-resolution crops.
    return (
        f'rtspsrc location="{url}" protocols={protocols} latency={config.latency_ms} '
        f"drop-on-latency=true do-retransmission=false ! "
        f"decodebin ! videoconvert ! "
        f"video/x-raw,format=BGR ! "
        f"appsink drop=true max-buffers=1 sync=false"
    )


def ffmpeg_capture_options(config: CameraConfig) -> str:
    """Value for ``OPENCV_FFMPEG_CAPTURE_OPTIONS`` (``key;value|key;value``)."""
    return "|".join(
        [
            f"rtsp_transport;{config.transport}",
            "fflags;nobuffer",
            "flags;low_delay",
            f"max_delay;{config.latency_ms * 1000}",
            "reorder_queue_size;0",
            "stimeout;5000000",
        ]
    )


class RtspCameraSource(CameraSource):
    """Decodes an RTSP stream in a background thread into the latest-frame buffer."""

    def __init__(self, config: CameraConfig, credentials: CameraCredentials | None = None) -> None:
        super().__init__(config)
        self._credentials = credentials
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seq = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"camera-{self.camera_id}", daemon=True
        )
        self._thread.start()
        log.info(
            "camera started",
            extra={"ctx": {"camera": self.camera_id, "url": redact_url(self.config.url)}},
        )

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():  # pragma: no cover - only on a wedged decoder
                log.warning("camera thread did not stop", extra={"ctx": {"camera": self.camera_id}})
        self._thread = None
        self.status.connected = False
        self.buffer.clear()
        log.info("camera stopped", extra={"ctx": {"camera": self.camera_id}})

    # -- worker ----------------------------------------------------------
    def _open(self) -> cv2.VideoCapture | None:
        url = inject_credentials(expand_credentials(self.config.url), self._credentials)
        if not url:
            self.status.error = "no URL configured"
            return None
        if _ENV_PLACEHOLDER.search(url):
            missing = ", ".join(sorted(set(_ENV_PLACEHOLDER.findall(url))))
            self.status.error = f"unresolved credential placeholder(s): {missing}"
            return None

        backend = self.config.backend
        if backend == "auto":
            backend = "gstreamer" if gstreamer_available() else "opencv"

        try:
            if backend == "gstreamer":
                pipeline = build_gstreamer_pipeline(self.config, url)
                capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
                self.status.backend = "gstreamer"
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = ffmpeg_capture_options(self.config)
                capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.status.backend = "ffmpeg"
        except Exception as exc:  # pragma: no cover - backend specific
            self.status.error = f"capture init failed: {exc}"
            return None

        if not capture.isOpened():
            capture.release()
            self.status.error = f"could not open stream ({self.status.backend})"
            return None
        return capture

    def _run(self) -> None:
        capture: cv2.VideoCapture | None = None
        last_frame_at = time.monotonic()

        while not self._stop.is_set():
            if capture is None:
                capture = self._open()
                if capture is None:
                    log.warning(
                        "camera connect failed",
                        extra={"ctx": {"camera": self.camera_id, "error": self.status.error}},
                    )
                    self.status.connected = False
                    self._stop.wait(self.config.reconnect_delay_s)
                    continue
                self.status.connected = True
                self.status.error = None
                last_frame_at = time.monotonic()
                log.info(
                    "camera connected",
                    extra={"ctx": {"camera": self.camera_id, "backend": self.status.backend}},
                )

            try:
                ok, image = capture.read()
            except Exception as exc:  # pragma: no cover - backend specific
                ok, image = False, None
                self.status.error = f"read failed: {exc}"

            now = time.monotonic()
            if not ok or image is None or image.size == 0:
                if now - last_frame_at > self.config.stall_timeout_s:
                    log.warning(
                        "camera stalled, reconnecting",
                        extra={
                            "ctx": {
                                "camera": self.camera_id,
                                "stall_s": round(now - last_frame_at, 1),
                            }
                        },
                    )
                    self._reconnect(capture)
                    capture = None
                    continue
                # Transient read failure: back off briefly, keep the stream.
                self._stop.wait(0.02)
                continue

            last_frame_at = now
            native_image = image
            image = self._maybe_downscale(native_image)
            self._seq += 1
            self.buffer.publish(
                Frame(
                    image=image,
                    seq=self._seq,
                    ts=now,
                    wall_ts=time.time(),
                    camera_id=self.camera_id,
                    native_image=native_image,
                )
            )
            self.status.frames = self._seq
            self.status.fps = self.buffer.fps()

        if capture is not None:
            capture.release()
        self.status.connected = False

    def _reconnect(self, capture: cv2.VideoCapture) -> None:
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            capture.release()
        self.status.connected = False
        self.status.reconnects += 1
        self.status.error = "stream stalled"
        self.buffer.clear()
        self._stop.wait(self.config.reconnect_delay_s)

    def _maybe_downscale(self, image: np.ndarray) -> np.ndarray:
        target = self.config.target_width
        if not target:
            return image
        height, width = image.shape[:2]
        if width <= target:
            return image
        scale = target / float(width)
        return cv2.resize(image, (target, round(height * scale)), interpolation=cv2.INTER_AREA)


def encode_jpeg(image: np.ndarray, quality: int = 70, max_width: int | None = None) -> bytes:
    """Encode a BGR image as JPEG, optionally downscaling first."""
    if max_width and image.shape[1] > max_width:
        scale = max_width / float(image.shape[1])
        image = cv2.resize(
            image,
            (max_width, round(image.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:  # pragma: no cover - only on a corrupt frame
        raise RuntimeError("JPEG encoding failed")
    return bytes(encoded)


def safe_filename(name: str) -> str:
    """Filesystem-safe name fragment (snapshots are named after events)."""
    allowed = set(string.ascii_letters + string.digits + "-_.")
    return "".join(ch if ch in allowed else "_" for ch in name)[:64]
