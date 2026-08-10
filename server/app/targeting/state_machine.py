"""Automatic targeting state machine.

Explicitly *not* "if bird then spray". Every engagement walks through states
with their own timeouts, verification steps and exits, so the failure modes are
enumerable and testable:

    DISARMED → IDLE → DETECTED → TRACKING → AIMING → VERIFY_TARGET
             → SPRAY → VERIFY_RESULT → (IDLE | AIMING | COOLDOWN) → IDLE

The machine is a pure function of its context: :meth:`TargetingStateMachine.step`
takes a snapshot of the world and returns the actions to perform. It never
talks to hardware, the database or the clock directly — which is what makes the
whole engagement sequence unit-testable in milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.services.settings_schema import TargetingSettings
from app.targeting.spray_guard import SprayGuard
from app.targeting.target_selector import Candidate, SelectionResult


class AutoState(str, Enum):
    DISARMED = "DISARMED"
    IDLE = "IDLE"
    DETECTED = "DETECTED"
    TRACKING = "TRACKING"
    AIMING = "AIMING"
    VERIFY_TARGET = "VERIFY_TARGET"
    SPRAY = "SPRAY"
    VERIFY_RESULT = "VERIFY_RESULT"
    COOLDOWN = "COOLDOWN"
    ERROR = "ERROR"


class ActionKind(str, Enum):
    MOVE = "move"
    STOP = "stop"
    SPRAY = "spray"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    pan_deg: float | None = None
    tilt_deg: float | None = None
    max_speed_deg_s: float | None = None
    duration_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None} | {"kind": self.kind.value}


@dataclass
class TickContext:
    """Everything the machine is allowed to know about the world."""

    now: float
    armed: bool
    auto_enabled: bool
    controller_connected: bool
    homed: bool
    moving: bool
    pan_deg: float
    tilt_deg: float
    selection: SelectionResult | None
    #: Pointing tolerance, from motion settings (the machine does not read
    #: settings it does not own).
    aim_tolerance_deg: float = 1.5
    #: Non-empty when the system cannot act (controller fault, e-stop, ...).
    fault: str | None = None


@dataclass
class StepOutcome:
    state: AutoState
    previous_state: AutoState
    actions: list[Action] = field(default_factory=list)
    #: ``(message, data)`` pairs the caller should write to the event log.
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    target_track_id: int | None = None
    aim_pan_deg: float | None = None
    aim_tilt_deg: float | None = None
    reason: str | None = None

    @property
    def changed(self) -> bool:
        return self.state is not self.previous_state


class TargetingStateMachine:
    def __init__(self, settings: TargetingSettings, spray_guard: SprayGuard) -> None:
        self.settings = settings
        self.spray_guard = spray_guard

        self.state = AutoState.DISARMED
        self.target_track_id: int | None = None
        self.aim_pan_deg: float | None = None
        self.aim_tilt_deg: float | None = None
        self.retries = 0
        self.reason: str | None = None

        self._state_since = 0.0
        self._target_since = 0.0
        self._target_lost_since: float | None = None
        self._verify_since: float | None = None
        self._last_commanded: tuple[float, float] | None = None
        self._engagements = 0
        self._sprays = 0

    # -- helpers ---------------------------------------------------------
    def update_settings(self, settings: TargetingSettings) -> None:
        self.settings = settings

    def _elapsed(self, now: float) -> float:
        return now - self._state_since

    def _enter(
        self,
        state: AutoState,
        now: float,
        outcome: StepOutcome,
        reason: str | None = None,
        event: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if state is not self.state:
            self._state_since = now
        self.state = state
        outcome.state = state
        self.reason = reason
        outcome.reason = reason
        if event:
            outcome.events.append((event, data or {}))

    def _clear_target(self) -> None:
        self.target_track_id = None
        self.aim_pan_deg = None
        self.aim_tilt_deg = None
        self._target_lost_since = None
        self._verify_since = None
        self._last_commanded = None
        self.retries = 0

    def _current_candidate(self, selection: SelectionResult | None) -> Candidate | None:
        if selection is None or self.target_track_id is None:
            return None
        candidate = selection.find(self.target_track_id)
        if candidate is None or not candidate.eligible or candidate.solution is None:
            return None
        return candidate

    def _aim_error(self, ctx: TickContext) -> float | None:
        if self.aim_pan_deg is None or self.aim_tilt_deg is None:
            return None
        return max(abs(ctx.pan_deg - self.aim_pan_deg), abs(ctx.tilt_deg - self.aim_tilt_deg))

    def _move_action(self, candidate: Candidate) -> Action | None:
        """Command a move if the aim point has drifted past the deadband."""
        cfg = self.settings
        assert candidate.solution is not None
        pan = candidate.solution.pan_deg + cfg.aim_pan_offset_deg
        tilt = candidate.solution.tilt_deg + cfg.aim_tilt_offset_deg
        self.aim_pan_deg, self.aim_tilt_deg = pan, tilt

        if self._last_commanded is not None:
            delta = max(
                abs(pan - self._last_commanded[0]),
                abs(tilt - self._last_commanded[1]),
            )
            if delta < cfg.retarget_deadband_deg:
                return None
        self._last_commanded = (pan, tilt)
        return Action(kind=ActionKind.MOVE, pan_deg=pan, tilt_deg=tilt)

    # -- main entry point ------------------------------------------------
    def step(self, ctx: TickContext) -> StepOutcome:
        outcome = StepOutcome(state=self.state, previous_state=self.state)

        # --- global guards, checked before anything else -----------------
        if not ctx.armed or not ctx.auto_enabled:
            if self.state is not AutoState.DISARMED:
                self._clear_target()
                if ctx.moving:
                    outcome.actions.append(Action(kind=ActionKind.STOP))
                self._enter(
                    AutoState.DISARMED,
                    ctx.now,
                    outcome,
                    "disarmed" if not ctx.armed else "automatic mode off",
                    event="automatic targeting stopped",
                )
            outcome.state = AutoState.DISARMED
            return self._finish(outcome)

        if ctx.fault or not ctx.controller_connected:
            reason = ctx.fault or "controller disconnected"
            if self.state is not AutoState.ERROR:
                self._clear_target()
                self._enter(
                    AutoState.ERROR,
                    ctx.now,
                    outcome,
                    reason,
                    event="targeting error",
                    data={"reason": reason},
                )
            else:
                self.reason = reason
                outcome.reason = reason
            return self._finish(outcome)

        if not ctx.homed:
            if self.state is not AutoState.ERROR:
                self._clear_target()
                self._enter(
                    AutoState.ERROR,
                    ctx.now,
                    outcome,
                    "turret not homed",
                    event="targeting error",
                    data={"reason": "not homed"},
                )
            return self._finish(outcome)

        if self.state in {AutoState.DISARMED, AutoState.ERROR}:
            self._clear_target()
            self._enter(AutoState.IDLE, ctx.now, outcome, None, event="automatic targeting ready")
            return self._finish(outcome)

        handler = getattr(self, f"_on_{self.state.value.lower()}")
        handler(ctx, outcome)
        return self._finish(outcome)

    def _finish(self, outcome: StepOutcome) -> StepOutcome:
        outcome.state = self.state
        outcome.target_track_id = self.target_track_id
        outcome.aim_pan_deg = self.aim_pan_deg
        outcome.aim_tilt_deg = self.aim_tilt_deg
        if outcome.reason is None:
            outcome.reason = self.reason
        return outcome

    # -- states ----------------------------------------------------------
    def _on_idle(self, ctx: TickContext, outcome: StepOutcome) -> None:
        self._clear_target()
        best = ctx.selection.best if ctx.selection else None
        if best is None:
            return
        self.target_track_id = best.track.track_id
        self._target_since = ctx.now
        self._enter(
            AutoState.DETECTED,
            ctx.now,
            outcome,
            None,
            event="target detected",
            data={
                "track_id": best.track.track_id,
                "class": best.track.class_name,
                "confidence": round(best.track.confidence, 3),
            },
        )

    def _on_detected(self, ctx: TickContext, outcome: StepOutcome) -> None:
        """Dwell state: the same candidate must stay selected for a moment."""
        best = ctx.selection.best if ctx.selection else None
        if best is None or best.track.track_id != self.target_track_id:
            self._clear_target()
            self._enter(AutoState.IDLE, ctx.now, outcome, "target lost before confirmation")
            return
        if ctx.now - self._target_since >= self.settings.detect_stability_s:
            self._engagements += 1
            self._enter(
                AutoState.TRACKING,
                ctx.now,
                outcome,
                None,
                event="target selected",
                data={"track_id": self.target_track_id},
            )

    def _on_tracking(self, ctx: TickContext, outcome: StepOutcome) -> None:
        candidate = self._current_candidate(ctx.selection)
        if candidate is None:
            self._handle_missing(ctx, outcome)
            return
        self._target_lost_since = None
        action = self._move_action(candidate)
        if action is not None:
            outcome.actions.append(action)
        self._enter(AutoState.AIMING, ctx.now, outcome)

    def _on_aiming(self, ctx: TickContext, outcome: StepOutcome) -> None:
        cfg = self.settings
        candidate = self._current_candidate(ctx.selection)
        if candidate is None:
            self._handle_missing(ctx, outcome)
            return
        self._target_lost_since = None

        if cfg.continuous_tracking:
            action = self._move_action(candidate)
            if action is not None:
                outcome.actions.append(action)

        error = self._aim_error(ctx)
        if error is not None and error <= ctx.aim_tolerance_deg and not ctx.moving:
            self._verify_since = ctx.now
            self._enter(AutoState.VERIFY_TARGET, ctx.now, outcome)
            return

        if self._elapsed(ctx.now) > cfg.aim_timeout_s:
            self._enter(
                AutoState.COOLDOWN,
                ctx.now,
                outcome,
                "aim timeout",
                event="aiming timed out",
                data={"track_id": self.target_track_id, "error_deg": round(error or -1.0, 2)},
            )

    def _on_verify_target(self, ctx: TickContext, outcome: StepOutcome) -> None:
        cfg = self.settings
        candidate = self._current_candidate(ctx.selection)
        if candidate is None:
            self._handle_missing(ctx, outcome)
            return
        self._target_lost_since = None

        # Target drifted while we were verifying: aim again.
        if (
            self.aim_pan_deg is not None
            and self.aim_tilt_deg is not None
            and candidate.solution is not None
        ):
            drift = max(
                abs(candidate.solution.pan_deg + cfg.aim_pan_offset_deg - self.aim_pan_deg),
                abs(candidate.solution.tilt_deg + cfg.aim_tilt_offset_deg - self.aim_tilt_deg),
            )
            if drift > cfg.retarget_deadband_deg:
                self._enter(AutoState.TRACKING, ctx.now, outcome, "target moved")
                return

        if not candidate.verdict.spray_allowed:
            self._enter(
                AutoState.COOLDOWN,
                ctx.now,
                outcome,
                "aim point is inside a no-spray zone",
                event="engagement blocked by no-spray zone",
                data={"track_id": self.target_track_id},
            )
            return

        if self._elapsed(ctx.now) >= cfg.verify_duration_s:
            self._enter(AutoState.SPRAY, ctx.now, outcome)

    def _on_spray(self, ctx: TickContext, outcome: StepOutcome) -> None:
        candidate = self._current_candidate(ctx.selection)
        if candidate is None:
            self._handle_missing(ctx, outcome)
            return

        # Last line of defence before water: re-check the zone verdict, then
        # the budget. Both can have changed since VERIFY_TARGET.
        if not candidate.verdict.spray_allowed:
            self._enter(
                AutoState.COOLDOWN,
                ctx.now,
                outcome,
                "no-spray zone",
                event="engagement blocked by no-spray zone",
            )
            return

        decision = self.spray_guard.check(now=ctx.now)
        if not decision.allowed:
            self._enter(
                AutoState.COOLDOWN,
                ctx.now,
                outcome,
                decision.reason,
                event="spray refused",
                data={"reason": decision.reason or "", "track_id": self.target_track_id},
            )
            return

        outcome.actions.append(Action(kind=ActionKind.SPRAY, duration_ms=decision.duration_ms))
        self.spray_guard.record(decision.duration_ms, now=ctx.now)
        self._sprays += 1
        self._enter(
            AutoState.VERIFY_RESULT,
            ctx.now,
            outcome,
            None,
            event="automatic spray activated",
            data={
                "track_id": self.target_track_id,
                "duration_ms": decision.duration_ms,
                "pan_deg": round(ctx.pan_deg, 2),
                "tilt_deg": round(ctx.tilt_deg, 2),
                "attempt": self.retries + 1,
            },
        )

    def _on_verify_result(self, ctx: TickContext, outcome: StepOutcome) -> None:
        cfg = self.settings
        candidate = self._current_candidate(ctx.selection)

        if candidate is None:
            # Give the bird a moment to actually leave before declaring success.
            if self._elapsed(ctx.now) < min(cfg.result_window_s, cfg.lost_grace_s):
                return
            self._enter(
                AutoState.IDLE,
                ctx.now,
                outcome,
                "target left",
                event="target left after spray",
                data={"track_id": self.target_track_id},
            )
            self._clear_target()
            return

        if self._elapsed(ctx.now) < cfg.result_window_s:
            return

        if self.retries < cfg.max_retries:
            self.retries += 1
            self._last_commanded = None
            self._enter(
                AutoState.TRACKING,
                ctx.now,
                outcome,
                "target still present",
                event="retrying engagement",
                data={"track_id": self.target_track_id, "attempt": self.retries + 1},
            )
            return

        self._enter(
            AutoState.COOLDOWN,
            ctx.now,
            outcome,
            "retry limit reached",
            event="engagement gave up",
            data={"track_id": self.target_track_id, "attempts": self.retries + 1},
        )

    def _on_cooldown(self, ctx: TickContext, outcome: StepOutcome) -> None:
        if self._elapsed(ctx.now) >= self.settings.cooldown_s:
            self._clear_target()
            self._enter(AutoState.IDLE, ctx.now, outcome, None)

    def _on_error(self, ctx: TickContext, outcome: StepOutcome) -> None:  # pragma: no cover
        # Reached only if the global guards above cleared; go back to idle.
        self._clear_target()
        self._enter(AutoState.IDLE, ctx.now, outcome, None)

    def _on_disarmed(self, ctx: TickContext, outcome: StepOutcome) -> None:  # pragma: no cover
        self._enter(AutoState.IDLE, ctx.now, outcome, None)

    # -- shared transitions ----------------------------------------------
    def _handle_missing(self, ctx: TickContext, outcome: StepOutcome) -> None:
        """The current target is gone or no longer eligible."""
        if self._target_lost_since is None:
            self._target_lost_since = ctx.now
        if ctx.now - self._target_lost_since < self.settings.lost_grace_s:
            return
        track_id = self.target_track_id
        self._clear_target()
        self._enter(
            AutoState.IDLE,
            ctx.now,
            outcome,
            "target lost",
            event="target lost",
            data={"track_id": track_id},
        )

    # -- introspection ---------------------------------------------------
    def status(self, now: float) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "target_track_id": self.target_track_id,
            "aim_pan_deg": round(self.aim_pan_deg, 3) if self.aim_pan_deg is not None else None,
            "aim_tilt_deg": round(self.aim_tilt_deg, 3) if self.aim_tilt_deg is not None else None,
            "retries": self.retries,
            "state_age_s": round(now - self._state_since, 2),
            "engagements": self._engagements,
            "sprays": self._sprays,
        }
