"""Adaptive scene-motion masks and native crop geometry."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from app.camera.base import Frame
from app.services.settings_schema import AppSettings, SceneMotionSettings
from app.vision.detector import Detection
from app.vision.pipeline import VisionPipeline, _native_crop_bounds
from app.vision.scene_motion import MotionRegion, SceneMotionDetector, _Event


def _region(event_id: int = 1) -> MotionRegion:
    return MotionRegion(
        x1=400,
        y1=200,
        x2=500,
        y2=300,
        area_ratio=0.01,
        fill_ratio=0.8,
        speed_ratio_s=0.1,
        score=1.0,
        event_id=event_id,
    )


def test_motion_mask_finds_a_sudden_connected_region() -> None:
    settings = SceneMotionSettings(
        processing_width=320,
        warmup_s=0,
        min_area_ratio=0.0001,
        max_area_ratio=0.2,
        max_frame_change_ratio=0.5,
        min_fill_ratio=0.01,
    )
    detector = SceneMotionDetector(settings)
    background = np.zeros((360, 640, 3), dtype=np.uint8)

    # Let MOG2 learn a stable empty scene before introducing the object.
    for index in range(20):
        detector.update(background, now=index / 6)

    changed = background.copy()
    changed[120:220, 240:360] = 255
    analysis = detector.update(changed, now=4.0)

    assert analysis.global_change is False
    assert analysis.changed_ratio > 0
    assert analysis.regions
    region = analysis.regions[0]
    assert region.x1 < 260 < region.x2
    assert region.y1 < 150 < region.y2
    mask = detector.mask_image()
    assert mask is not None
    assert mask.ndim == 2
    assert np.count_nonzero(mask) > 0


def test_global_change_is_not_returned_as_motion_region() -> None:
    settings = SceneMotionSettings(
        processing_width=320,
        warmup_s=0,
        max_frame_change_ratio=0.2,
    )
    detector = SceneMotionDetector(settings)
    dark = np.zeros((240, 320, 3), dtype=np.uint8)
    for index in range(20):
        detector.update(dark, now=index / 6)

    analysis = detector.update(np.full_like(dark, 255), now=4.0)

    assert analysis.global_change is True
    assert analysis.regions == ()


def test_event_rescans_are_rate_limited_and_bounded() -> None:
    settings = SceneMotionSettings(
        warmup_s=0,
        min_persistence_frames=1,
        max_rescans_per_event=2,
        rescan_interval_s=0.5,
        event_rearm_s=1.0,
    )
    detector = SceneMotionDetector(settings)
    event = detector._events[1] = _Event(1, (0, 0, 10, 10), (5, 5), 0.0, 0.0)

    assert detector.claim_rescans([_region()], 0.0)
    assert detector.claim_rescans([_region()], 0.2) == []
    assert detector.claim_rescans([_region()], 0.5)
    assert detector.claim_rescans([_region()], 2.0) == []
    assert event.rescans == 2


def test_native_crop_maps_display_region_and_adds_context() -> None:
    bounds = _native_crop_bounds(
        _region(),
        display_width=1280,
        display_height=720,
        native_width=3840,
        native_height=2160,
        padding_ratio=1.0,
        min_width_ratio=0.18,
    )

    x1, y1, x2, y2 = bounds
    # The unpadded native box is x=1200..1500, y=600..900.
    assert x1 < 1200 < 1500 < x2
    assert y1 < 600 < 900 < y2
    assert x2 - x1 >= round(3840 * 0.18)


def test_frame_exposes_native_image_without_changing_normal_image() -> None:
    display = np.zeros((360, 640, 3), dtype=np.uint8)
    native = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame = Frame(
        image=display,
        native_image=native,
        seq=1,
        ts=1.0,
        wall_ts=2.0,
        camera_id="overview",
    )

    assert frame.width == 640
    assert frame.native.shape[:2] == (1080, 1920)


@pytest.mark.asyncio
async def test_motion_crop_rescan_uses_native_pixels_and_stays_evidence_only() -> None:
    settings = AppSettings()
    settings.scene_motion.crop_padding_ratio = 0.0
    settings.scene_motion.min_crop_width_ratio = 0.1
    cameras = Mock()
    pipeline = VisionPipeline(cameras, settings, Path("."), force_mock=True)
    display = np.zeros((360, 640, 3), dtype=np.uint8)
    native = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame = Frame(
        image=display,
        native_image=native,
        seq=7,
        ts=10.0,
        wall_ts=20.0,
        camera_id="overview",
    )
    detector = Mock()
    detector.infer.return_value = [
        Detection(
            x1=10,
            y1=20,
            x2=50,
            y2=80,
            confidence=0.2,
            class_id=14,
            class_name="bird",
        )
    ]

    evidence = await pipeline._rescan_motion(detector, frame, [_region()])

    assert evidence is not None
    assert evidence.image is native
    assert evidence.class_name == "bird"
    assert evidence.detections[0].source == "motion_rescan"
    assert pipeline.latest is None, "motion rescan evidence must not become an operational result"
    crop = detector.infer.call_args.args[0]
    assert crop.shape[1] >= round(native.shape[1] * 0.1)
    assert (
        detector.infer.call_args.kwargs["min_confidence"] == settings.scene_motion.rescan_confidence
    )
