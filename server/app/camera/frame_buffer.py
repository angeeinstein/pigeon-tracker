"""Latest-frame buffer.

The core rule of this pipeline: **the newest frame wins**. There is no queue.
A producer thread overwrites a single slot; consumers read whatever is there
when they get around to it. If inference takes 300 ms on a 25 fps stream, the
intervening frames are simply never seen — which is exactly what you want for
a real-time turret, and the opposite of what a queue would do.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from app.camera.base import Frame


class LatestFrameBuffer:
    """Single-slot, thread-safe frame holder with sequence numbers."""

    #: Granularity of :meth:`wait_new`. Small enough to be invisible next to
    #: any realistic frame interval, large enough to cost nothing while idle.
    POLL_INTERVAL_S = 0.004

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._seq = 0
        self._dropped = 0
        #: Timestamps of recent publishes, for the fps estimate.
        self._recent: list[float] = []

    # -- producer side ---------------------------------------------------
    def publish(self, frame: Frame) -> None:
        with self._lock:
            if self._frame is not None:
                # The previous frame was replaced before anyone consumed it.
                self._dropped += 1
            self._frame = frame
            self._seq = frame.seq
            now = time.monotonic()
            self._recent.append(now)
            if len(self._recent) > 30:
                del self._recent[: len(self._recent) - 30]

    def clear(self) -> None:
        with self._lock:
            self._frame = None
            self._recent.clear()

    # -- consumer side ---------------------------------------------------
    def latest(self) -> Frame | None:
        with self._lock:
            return self._frame

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def fps(self) -> float:
        with self._lock:
            if len(self._recent) < 2:
                return 0.0
            span = self._recent[-1] - self._recent[0]
            if span <= 0:
                return 0.0
            return (len(self._recent) - 1) / span

    async def wait_new(self, after_seq: int, timeout: float = 2.0) -> Frame | None:
        """Wait for a frame newer than ``after_seq``.

        Returns ``None`` on timeout. Implemented by polling rather than
        cross-thread event signalling: the producer is a plain thread, and a
        4 ms poll is both simpler and cheaper than marshalling wakeups into
        every consumer's event loop.
        """
        deadline = time.monotonic() + timeout
        while True:
            frame = self.latest()
            if frame is not None and frame.seq > after_seq:
                return frame
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self.POLL_INTERVAL_S)
