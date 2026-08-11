"""Freshness behaviour for vision results."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services.settings_schema import AppSettings
from app.vision.pipeline import VisionPipeline, VisionResult


def make_pipeline(*, connected: bool = True, frame_ts: float = 100.0) -> VisionPipeline:
    settings = AppSettings()
    cameras = Mock()
    cameras.get.return_value = SimpleNamespace(
        status=SimpleNamespace(connected=connected),
    )
    pipeline = VisionPipeline(cameras, settings, Path("."), force_mock=True)
    pipeline._latest = VisionResult(
        camera_id=settings.cameras.primary_id,
        frame_seq=1,
        frame_ts=frame_ts,
        wall_ts=1_700_000_000.0,
        frame_width=1280,
        frame_height=720,
    )
    return pipeline


def test_latest_result_is_available_while_camera_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = make_pipeline(frame_ts=100.0)
    monkeypatch.setattr(time, "monotonic", lambda: 101.0)

    assert pipeline.latest is not None


def test_latest_result_expires_after_camera_stall_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = make_pipeline(frame_ts=100.0)
    timeout = pipeline._settings.cameras.sources[0].stall_timeout_s
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: 100.0 + timeout + 0.01,
    )

    assert pipeline.latest is None
    assert pipeline.tracks == []
    assert pipeline.status()["result_stale"] is True


def test_latest_result_is_hidden_when_primary_camera_disconnects() -> None:
    pipeline = make_pipeline(connected=False, frame_ts=time.monotonic())

    assert pipeline.latest is None
    assert pipeline.status()["tracker"]["active_tracks"] == 0


def test_camera_change_clears_result_and_tracker_state() -> None:
    pipeline = make_pipeline(frame_ts=time.monotonic())
    pipeline._last_frame_seq = 42
    pipeline._settings.cameras.primary_id = "replacement"

    with patch.object(pipeline._tracker, "reset") as reset:
        asyncio.run(pipeline.apply_settings(pipeline._settings, {"cameras"}))

    assert pipeline.latest is None
    assert pipeline._last_frame_seq == -1
    reset.assert_called_once()
