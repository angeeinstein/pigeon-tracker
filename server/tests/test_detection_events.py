"""Detector output is represented in the persistent event history."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np

from app.services import event_log as ev
from app.services.runtime import Runtime
from app.services.settings_schema import AppSettings
from app.vision.detector import Detection
from app.vision.pipeline import VisionResult


def result_at(
    frame_ts: float,
    *detections: Detection,
    camera_id: str = "overview",
) -> VisionResult:
    return VisionResult(
        camera_id=camera_id,
        frame_seq=int(frame_ts * 10),
        frame_ts=frame_ts,
        wall_ts=1_700_000_000.0 + frame_ts,
        frame_width=1280,
        frame_height=720,
        proposals=list(detections),
        detections=list(detections),
    )


def detection(class_name: str, confidence: float) -> Detection:
    return Detection(
        x1=100,
        y1=100,
        x2=200,
        y2=250,
        confidence=confidence,
        class_id=0,
        class_name=class_name,
    )


def runtime_for_test() -> Runtime:
    runtime = Runtime.__new__(Runtime)
    runtime.events = AsyncMock()
    runtime.detection_captures = AsyncMock()
    runtime.settings_store = SimpleNamespace(current=AppSettings())
    runtime._detection_last_seen = {}
    return runtime


async def test_detection_event_contains_class_count_and_confidence() -> None:
    runtime = runtime_for_test()

    await runtime._on_vision_result(
        result_at(10.0, detection("person", 0.72), detection("person", 0.91))
    )

    runtime.events.emit.assert_awaited_once_with(
        ev.CAT_DETECTION,
        "person detected",
        data={
            "camera_id": "overview",
            "class": "person",
            "count": 2,
            "max_confidence": 0.91,
            "frame_seq": 100,
        },
    )


async def test_repeated_detection_is_logged_once_then_rearms_after_absence() -> None:
    runtime = runtime_for_test()
    person = detection("person", 0.8)

    await runtime._on_vision_result(result_at(10.0, person))
    await runtime._on_vision_result(result_at(11.0, person))
    await runtime._on_vision_result(result_at(17.0, person))

    assert runtime.events.emit.await_count == 2


async def test_each_detected_class_gets_its_own_event() -> None:
    runtime = runtime_for_test()

    await runtime._on_vision_result(
        result_at(10.0, detection("person", 0.8), detection("bird", 0.7))
    )

    assert [call.args[1] for call in runtime.events.emit.await_args_list] == [
        "person detected",
        "bird detected",
    ]


async def test_detection_image_is_saved_and_linked_from_event() -> None:
    runtime = runtime_for_test()
    runtime.detection_captures.create.return_value = {"id": 42}
    result = result_at(10.0, detection("bird", 0.4))
    result.image = np.zeros((100, 160, 3), dtype=np.uint8)

    await runtime._on_vision_result(result)

    runtime.detection_captures.create.assert_awaited_once()
    assert runtime.events.emit.await_args.kwargs["data"]["capture_id"] == 42
