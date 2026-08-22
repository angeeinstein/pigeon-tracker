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


def detection(class_name: str, confidence: float, *, x1: float = 100) -> Detection:
    return Detection(
        x1=x1,
        y1=100,
        x2=x1 + 100,
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
    runtime._evidence_history = {}
    runtime._evidence_suppressed = {}
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


async def test_repetitive_weak_evidence_is_suppressed_without_removing_detection() -> None:
    runtime = runtime_for_test()
    runtime.detection_captures.create.return_value = {"id": 42}
    first = result_at(10.0, detection("bird", 0.2))
    first.image = np.zeros((300, 320, 3), dtype=np.uint8)
    repeated = result_at(20.0, detection("bird", 0.2))
    repeated.image = first.image

    await runtime._on_vision_result(first)
    await runtime._on_vision_result(repeated)

    runtime.detection_captures.create.assert_awaited_once()
    assert repeated.detections, "operational pipeline data must remain untouched"


async def test_operational_evidence_repeat_is_suppressed_without_removing_detection() -> None:
    runtime = runtime_for_test()
    runtime.detection_captures.create.return_value = {"id": 42}
    first = result_at(10.0, detection("bird", 0.8))
    first.image = np.zeros((300, 320, 3), dtype=np.uint8)
    repeated = result_at(20.0, detection("bird", 0.8))
    repeated.image = first.image

    await runtime._on_vision_result(first)
    await runtime._on_vision_result(repeated)

    runtime.detection_captures.create.assert_awaited_once()
    assert repeated.detections, "operational pipeline data must remain untouched"


async def test_alternating_static_hotspots_are_remembered_together() -> None:
    runtime = runtime_for_test()
    runtime.detection_captures.create.return_value = {"id": 42}
    results = [
        result_at(10.0, detection("bird", 0.8, x1=100)),
        result_at(20.0, detection("bird", 0.8, x1=500)),
        result_at(30.0, detection("bird", 0.8, x1=100)),
    ]
    for result in results:
        result.image = np.zeros((300, 800, 3), dtype=np.uint8)
        await runtime._on_vision_result(result)

    assert runtime.detection_captures.create.await_count == 2


async def test_jittering_box_still_counts_as_same_static_hotspot() -> None:
    runtime = runtime_for_test()
    runtime.detection_captures.create.return_value = {"id": 42}
    first = result_at(10.0, detection("bird", 0.8, x1=100))
    jittered = result_at(20.0, detection("bird", 0.8, x1=120))
    first.image = np.zeros((300, 300, 3), dtype=np.uint8)
    jittered.image = first.image

    await runtime._on_vision_result(first)
    await runtime._on_vision_result(jittered)

    runtime.detection_captures.create.assert_awaited_once()


async def test_changed_appearance_at_same_location_is_captured() -> None:
    runtime = runtime_for_test()
    runtime.detection_captures.create.return_value = {"id": 42}
    first = result_at(10.0, detection("bird", 0.8))
    changed = result_at(20.0, detection("bird", 0.8))
    first.image = np.zeros((300, 320, 3), dtype=np.uint8)
    changed.image = first.image.copy()
    changed.image[100:250, 100:200] = 255

    await runtime._on_vision_result(first)
    await runtime._on_vision_result(changed)

    assert runtime.detection_captures.create.await_count == 2


async def test_static_location_becomes_capturable_after_repeat_interval() -> None:
    runtime = runtime_for_test()
    runtime.detection_captures.create.return_value = {"id": 42}
    first = result_at(10.0, detection("bird", 0.8))
    repeated_later = result_at(3_611.0, detection("bird", 0.8))
    first.image = np.zeros((300, 320, 3), dtype=np.uint8)
    repeated_later.image = first.image

    await runtime._on_vision_result(first)
    await runtime._on_vision_result(repeated_later)

    assert runtime.detection_captures.create.await_count == 2
