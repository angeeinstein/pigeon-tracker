"""Server-side water budget.

This is the *policy* half of the spray safety story. The controller enforces a
hard per-burst timer in firmware; this class enforces the things only the
server can know — how much water has already gone out recently, and how long
ago the last burst was.

Both layers are independent on purpose. Neither trusts the other.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.services.settings_schema import SpraySettings


@dataclass(frozen=True)
class SprayDecision:
    allowed: bool
    duration_ms: int
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "duration_ms": self.duration_ms, "reason": self.reason}


class SprayGuard:
    def __init__(self, settings: SpraySettings) -> None:
        self.settings = settings
        #: (timestamp, duration_ms) of recent bursts, newest last.
        self._history: deque[tuple[float, int]] = deque(maxlen=512)

    def update_settings(self, settings: SpraySettings) -> None:
        self.settings = settings

    # -- accounting ------------------------------------------------------
    def _prune(self, now: float) -> None:
        window = self.settings.duty_window_s
        while self._history and now - self._history[0][0] > window:
            self._history.popleft()

    def used_ms(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        self._prune(now)
        return sum(duration for _, duration in self._history)

    def last_spray_at(self) -> float | None:
        return self._history[-1][0] if self._history else None

    # -- decisions -------------------------------------------------------
    def check(self, duration_ms: int | None = None, now: float | None = None) -> SprayDecision:
        """Decide whether a burst may happen, and for how long.

        Returns the *clamped* duration, so callers never have to remember to
        clamp; passing an absurd value simply yields the maximum.
        """
        cfg = self.settings
        now = time.monotonic() if now is None else now
        self._prune(now)

        requested = cfg.default_duration_ms if duration_ms is None else int(duration_ms)
        duration = max(20, min(requested, cfg.max_duration_ms))

        if not cfg.enabled:
            return SprayDecision(False, duration, "water output is disabled in settings")

        last = self.last_spray_at()
        if last is not None and (now - last) < cfg.min_interval_s:
            wait = cfg.min_interval_s - (now - last)
            return SprayDecision(
                False, duration, f"minimum interval not elapsed ({wait:.1f}s left)"
            )

        if cfg.duty_budget_ms > 0:
            used = sum(d for _, d in self._history)
            remaining = cfg.duty_budget_ms - used
            if remaining <= 0:
                return SprayDecision(
                    False,
                    duration,
                    f"duty budget exhausted ({used} ms in the last {cfg.duty_window_s:.0f}s)",
                )
            if duration > remaining:
                duration = int(remaining)

        return SprayDecision(True, duration, None)

    def record(self, duration_ms: int, now: float | None = None) -> None:
        """Register a burst that actually happened."""
        now = time.monotonic() if now is None else now
        self._history.append((now, int(duration_ms)))
        self._prune(now)

    def status(self, now: float | None = None) -> dict[str, Any]:
        cfg = self.settings
        now = time.monotonic() if now is None else now
        self._prune(now)
        used = sum(d for _, d in self._history)
        last = self.last_spray_at()
        return {
            "enabled": cfg.enabled,
            "bursts_in_window": len(self._history),
            "used_ms": used,
            "budget_ms": cfg.duty_budget_ms,
            "window_s": cfg.duty_window_s,
            "seconds_since_last": round(now - last, 1) if last is not None else None,
            "ready": self.check(now=now).allowed,
        }
