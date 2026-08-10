"""Water budget enforcement."""

from __future__ import annotations

from app.services.settings_schema import SpraySettings
from app.targeting.spray_guard import SprayGuard


def guard(**kwargs) -> SprayGuard:
    defaults = {
        "enabled": True,
        "default_duration_ms": 400,
        "max_duration_ms": 1000,
        "min_interval_s": 3.0,
        "duty_budget_ms": 2000,
        "duty_window_s": 60.0,
    }
    return SprayGuard(SpraySettings(**{**defaults, **kwargs}))


class TestBasics:
    def test_allows_a_first_burst(self) -> None:
        decision = guard().check(now=0.0)
        assert decision.allowed is True
        assert decision.duration_ms == 400

    def test_disabled_output_refuses_everything(self) -> None:
        decision = guard(enabled=False).check(now=0.0)
        assert decision.allowed is False
        assert "disabled" in (decision.reason or "")

    def test_clamps_to_the_maximum(self) -> None:
        assert guard().check(9999, now=0.0).duration_ms == 1000

    def test_enforces_a_floor(self) -> None:
        assert guard().check(1, now=0.0).duration_ms == 20


class TestIntervals:
    def test_minimum_interval_is_enforced(self) -> None:
        g = guard()
        g.record(400, now=0.0)
        assert g.check(now=1.0).allowed is False
        assert g.check(now=3.1).allowed is True

    def test_zero_interval_allows_back_to_back(self) -> None:
        g = guard(min_interval_s=0.0)
        g.record(400, now=0.0)
        assert g.check(now=0.0).allowed is True


class TestDutyBudget:
    def test_budget_is_consumed(self) -> None:
        g = guard(min_interval_s=0.0, duty_budget_ms=1000)
        g.record(800, now=0.0)
        decision = g.check(400, now=1.0)
        # Only 200 ms of budget left: the burst is shortened, not refused.
        assert decision.allowed is True
        assert decision.duration_ms == 200

    def test_exhausted_budget_refuses(self) -> None:
        g = guard(min_interval_s=0.0, duty_budget_ms=1000)
        g.record(1000, now=0.0)
        decision = g.check(now=1.0)
        assert decision.allowed is False
        assert "budget" in (decision.reason or "")

    def test_budget_recovers_after_the_window(self) -> None:
        g = guard(min_interval_s=0.0, duty_budget_ms=1000, duty_window_s=60.0)
        g.record(1000, now=0.0)
        assert g.check(now=30.0).allowed is False
        assert g.check(now=61.0).allowed is True

    def test_disabled_budget_is_unlimited(self) -> None:
        g = guard(min_interval_s=0.0, duty_budget_ms=0)
        for index in range(50):
            g.record(1000, now=float(index))
        assert g.check(now=50.0).allowed is True


class TestStatus:
    def test_status_reports_usage(self) -> None:
        g = guard(min_interval_s=0.0)
        g.record(400, now=0.0)
        g.record(400, now=1.0)
        status = g.status(now=2.0)
        assert status["bursts_in_window"] == 2
        assert status["used_ms"] == 800
        assert status["seconds_since_last"] == 1.0
