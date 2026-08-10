"""Automatic targeting state machine.

Written as sequences of ticks with a controlled clock, so a full engagement —
detect, aim, verify, spray, verify result, cooldown — runs in microseconds and
every branch (lost target, no-spray zone, budget exhausted, retry limit, aim
timeout) is exercised deterministically.
"""

from __future__ import annotations

import pytest

from app.services.settings_schema import SpraySettings, TargetingSettings
from app.targeting.spray_guard import SprayGuard
from app.targeting.state_machine import (
    ActionKind,
    AutoState,
    TargetingStateMachine,
    TickContext,
)
from tests.conftest import make_candidate, make_selection, make_track


class Harness:
    """Drives the machine with a fake clock and a fixed candidate."""

    def __init__(
        self,
        settings: TargetingSettings,
        spray: SpraySettings | None = None,
    ) -> None:
        self.guard = SprayGuard(spray or SpraySettings(enabled=True, min_interval_s=0.0))
        self.machine = TargetingStateMachine(settings, self.guard)
        self.now = 100.0
        self.pan = 0.0
        self.tilt = 0.0
        self.armed = True
        self.connected = True
        self.homed = True
        self.moving = False
        self.selection = make_selection(make_candidate())
        self.actions: list = []
        self.events: list[str] = []

    def tick(self, advance: float = 0.1, *, snap_to_target: bool = True):
        self.now += advance
        ctx = TickContext(
            now=self.now,
            armed=self.armed,
            auto_enabled=True,
            controller_connected=self.connected,
            homed=self.homed,
            moving=self.moving,
            pan_deg=self.pan,
            tilt_deg=self.tilt,
            selection=self.selection,
            aim_tolerance_deg=1.5,
        )
        outcome = self.machine.step(ctx)
        self.actions.extend(outcome.actions)
        self.events.extend(message for message, _ in outcome.events)
        # Pretend the turret reaches whatever it was told to reach.
        if snap_to_target:
            for action in outcome.actions:
                if action.kind is ActionKind.MOVE:
                    self.pan = action.pan_deg or 0.0
                    self.tilt = action.tilt_deg or 0.0
        return outcome

    def run_to(self, state: AutoState, limit: int = 40, advance: float = 0.1) -> None:
        for _ in range(limit):
            if self.machine.state is state:
                return
            self.tick(advance)
        raise AssertionError(f"never reached {state} (stuck in {self.machine.state})")

    @property
    def sprays(self) -> list:
        return [a for a in self.actions if a.kind is ActionKind.SPRAY]


@pytest.fixture()
def harness(targeting_settings: TargetingSettings) -> Harness:
    return Harness(targeting_settings)


class TestGuards:
    def test_starts_disarmed(self, harness: Harness) -> None:
        assert harness.machine.state is AutoState.DISARMED

    def test_disarmed_system_never_leaves_disarmed(self, harness: Harness) -> None:
        harness.armed = False
        for _ in range(10):
            harness.tick()
        assert harness.machine.state is AutoState.DISARMED
        assert harness.sprays == []

    def test_disarming_mid_engagement_stops_everything(self, harness: Harness) -> None:
        harness.run_to(AutoState.AIMING)
        harness.moving = True
        harness.armed = False
        outcome = harness.tick()
        assert outcome.state is AutoState.DISARMED
        assert any(a.kind is ActionKind.STOP for a in outcome.actions)
        assert harness.machine.target_track_id is None

    def test_disconnected_controller_is_an_error_state(self, harness: Harness) -> None:
        harness.tick()  # leave DISARMED
        harness.connected = False
        outcome = harness.tick()
        assert outcome.state is AutoState.ERROR
        assert "disconnected" in (outcome.reason or "")

    def test_unhomed_turret_is_an_error_state(self, harness: Harness) -> None:
        harness.tick()
        harness.homed = False
        assert harness.tick().state is AutoState.ERROR

    def test_recovers_to_idle_when_the_fault_clears(self, harness: Harness) -> None:
        harness.tick()
        harness.connected = False
        harness.tick()
        harness.connected = True
        assert harness.tick().state is AutoState.IDLE


class TestHappyPath:
    def test_full_engagement(self, harness: Harness) -> None:
        states = []
        for _ in range(30):
            states.append(harness.tick().state)
            if harness.machine.state is AutoState.COOLDOWN:
                break

        assert AutoState.DETECTED in states
        assert AutoState.TRACKING in states
        assert AutoState.AIMING in states
        assert AutoState.VERIFY_TARGET in states
        assert AutoState.SPRAY in states
        assert AutoState.VERIFY_RESULT in states
        assert len(harness.sprays) >= 1

    def test_a_move_is_commanded_before_spraying(self, harness: Harness) -> None:
        harness.run_to(AutoState.VERIFY_TARGET)
        moves = [a for a in harness.actions if a.kind is ActionKind.MOVE]
        assert moves, "no move command was issued"
        assert moves[0].pan_deg == pytest.approx(10.0)
        assert moves[0].tilt_deg == pytest.approx(-5.0)

    def test_target_leaving_after_the_spray_returns_to_idle(self, harness: Harness) -> None:
        harness.run_to(AutoState.VERIFY_RESULT)
        harness.selection = make_selection()  # the bird left
        for _ in range(20):
            outcome = harness.tick(0.2)
            if outcome.state is AutoState.IDLE:
                break
        assert harness.machine.state is AutoState.IDLE
        assert "target left after spray" in harness.events

    def test_cooldown_expires_back_to_idle(self, targeting_settings: TargetingSettings) -> None:
        targeting_settings.max_retries = 0
        harness = Harness(targeting_settings)
        harness.run_to(AutoState.COOLDOWN, limit=60)
        for _ in range(40):
            if harness.tick(0.2).state is AutoState.IDLE:
                break
        assert harness.machine.state is AutoState.IDLE


class TestSafety:
    def test_never_sprays_into_a_no_spray_zone(self, harness: Harness) -> None:
        harness.selection = make_selection(make_candidate(spray_allowed=False))
        for _ in range(40):
            harness.tick()
            if harness.machine.state is AutoState.COOLDOWN:
                break
        assert harness.sprays == []
        assert "engagement blocked by no-spray zone" in harness.events

    def test_disabled_water_output_blocks_the_spray(
        self, targeting_settings: TargetingSettings
    ) -> None:
        harness = Harness(targeting_settings, SpraySettings(enabled=False))
        for _ in range(40):
            harness.tick()
            if harness.machine.state is AutoState.COOLDOWN:
                break
        assert harness.sprays == []
        assert "spray refused" in harness.events

    def test_exhausted_duty_budget_blocks_the_spray(
        self, targeting_settings: TargetingSettings
    ) -> None:
        spray = SpraySettings(
            enabled=True, min_interval_s=0.0, duty_budget_ms=100, default_duration_ms=400
        )
        harness = Harness(targeting_settings, spray)
        harness.guard.record(100, now=harness.now)  # budget already spent
        for _ in range(40):
            harness.tick()
            if harness.machine.state is AutoState.COOLDOWN:
                break
        assert harness.sprays == []

    def test_spray_duration_is_clamped_to_the_maximum(
        self, targeting_settings: TargetingSettings
    ) -> None:
        spray = SpraySettings(
            enabled=True, min_interval_s=0.0, default_duration_ms=5000, max_duration_ms=800
        )
        harness = Harness(targeting_settings, spray)
        harness.run_to(AutoState.VERIFY_RESULT, limit=60)
        assert harness.sprays[0].duration_ms == 800

    def test_ineligible_candidate_is_never_engaged(self, harness: Harness) -> None:
        harness.selection = make_selection(make_candidate(rejected="confidence too low"))
        for _ in range(20):
            harness.tick()
        assert harness.machine.state is AutoState.IDLE
        assert harness.sprays == []


class TestLostTarget:
    def test_target_lost_during_aiming_returns_to_idle(self, harness: Harness) -> None:
        harness.run_to(AutoState.AIMING)
        harness.selection = make_selection()
        for _ in range(20):
            if harness.tick(0.2).state is AutoState.IDLE:
                break
        assert harness.machine.state is AutoState.IDLE
        assert "target lost" in harness.events

    def test_brief_disappearance_within_the_grace_window_is_tolerated(
        self, harness: Harness
    ) -> None:
        harness.run_to(AutoState.AIMING)
        harness.selection = make_selection()
        harness.tick(0.1)
        assert harness.machine.state is AutoState.AIMING  # still within grace
        harness.selection = make_selection(make_candidate())
        harness.tick(0.1)
        assert harness.machine.state in {AutoState.AIMING, AutoState.VERIFY_TARGET}


class TestAiming:
    def test_aim_timeout_goes_to_cooldown(self, targeting_settings: TargetingSettings) -> None:
        targeting_settings.aim_timeout_s = 0.5
        harness = Harness(targeting_settings)
        harness.run_to(AutoState.AIMING)
        harness.moving = True  # never settles
        for _ in range(20):
            if harness.tick(0.2, snap_to_target=False).state is AutoState.COOLDOWN:
                break
        assert harness.machine.state is AutoState.COOLDOWN
        assert "aiming timed out" in harness.events

    def test_small_target_drift_does_not_re_command_a_move(self, harness: Harness) -> None:
        harness.run_to(AutoState.AIMING)
        before = len([a for a in harness.actions if a.kind is ActionKind.MOVE])
        # Drift below the deadband (0.75 deg by default).
        harness.selection = make_selection(make_candidate(pan=10.2, tilt=-5.1))
        harness.tick()
        after = len([a for a in harness.actions if a.kind is ActionKind.MOVE])
        assert after == before

    def test_large_target_drift_re_commands_a_move(self, harness: Harness) -> None:
        harness.run_to(AutoState.AIMING)
        before = len([a for a in harness.actions if a.kind is ActionKind.MOVE])
        harness.selection = make_selection(make_candidate(pan=25.0, tilt=-5.0))
        harness.tick()
        after = len([a for a in harness.actions if a.kind is ActionKind.MOVE])
        assert after > before


class TestRetries:
    def test_retries_while_the_target_stays(self, targeting_settings: TargetingSettings) -> None:
        targeting_settings.max_retries = 2
        harness = Harness(targeting_settings)
        for _ in range(120):
            harness.tick(0.1)
            if harness.machine.state is AutoState.COOLDOWN:
                break
        # First attempt plus two retries.
        assert len(harness.sprays) == 3
        assert "engagement gave up" in harness.events

    def test_zero_retries_gives_up_after_one_spray(
        self, targeting_settings: TargetingSettings
    ) -> None:
        targeting_settings.max_retries = 0
        harness = Harness(targeting_settings)
        for _ in range(80):
            harness.tick(0.1)
            if harness.machine.state is AutoState.COOLDOWN:
                break
        assert len(harness.sprays) == 1


class TestSelectionStability:
    def test_flapping_between_targets_never_engages(
        self, targeting_settings: TargetingSettings
    ) -> None:
        targeting_settings.detect_stability_s = 1.0
        harness = Harness(targeting_settings)
        for index in range(20):
            # A different track id every tick: nothing is ever stable.
            harness.selection = make_selection(make_candidate(make_track(track_id=index + 1)))
            harness.tick(0.1)
        assert harness.sprays == []
        assert harness.machine.state in {AutoState.IDLE, AutoState.DETECTED}
