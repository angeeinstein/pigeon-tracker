"""Per-box evidence review and capture-level completion rules."""

from pathlib import Path

import numpy as np

from app.services.detection_capture import DetectionCaptureStore, _episode_splits
from app.vision.detector import Detection


def proposal(class_name: str, confidence: float, offset: float) -> Detection:
    return Detection(
        x1=10 + offset,
        y1=20 + offset,
        x2=110 + offset,
        y2=220 + offset,
        confidence=confidence,
        class_id=14 if class_name == "bird" else 0,
        class_name=class_name,
    )


async def test_capture_completes_only_after_every_box_is_reviewed(
    temp_database: Path, tmp_path: Path
) -> None:
    store = DetectionCaptureStore(tmp_path / "detections")
    capture = await store.create(
        image=np.zeros((360, 640, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=1,
        detections=[proposal("bird", 0.8, 0), proposal("person", 0.2, 150)],
        class_name="bird",
        confidence=0.8,
        model_name="test.pt",
        detector_settings={},
        jpeg_quality=80,
    )

    assert [item["review_status"] for item in capture["detections"]] == [
        "unreviewed",
        "unreviewed",
    ]
    capture_id = int(capture["id"])

    partly_reviewed = await store.review_annotation(
        capture_id, 0, review_status="accepted", review_label="bird"
    )
    assert partly_reviewed is not None
    assert partly_reviewed["review_status"] == "unreviewed"

    completed = await store.review_annotation(
        capture_id, 1, review_status="rejected", review_label=""
    )
    assert completed is not None
    assert completed["review_status"] == "training"
    assert completed["review_label"] == "bird"


async def test_all_rejected_boxes_make_a_negative_capture(
    temp_database: Path, tmp_path: Path
) -> None:
    store = DetectionCaptureStore(tmp_path / "detections")
    capture = await store.create(
        image=np.zeros((360, 640, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=2,
        detections=[proposal("airplane", 0.15, 0)],
        class_name="airplane",
        confidence=0.15,
        model_name="test.pt",
        detector_settings={},
        jpeg_quality=80,
    )

    completed = await store.reject_unreviewed_annotations(int(capture["id"]))
    assert completed is not None
    assert completed["review_status"] == "rejected"
    assert completed["review_label"] == "not-bird"


async def test_rejecting_last_box_completes_negative_capture(
    temp_database: Path, tmp_path: Path
) -> None:
    store = DetectionCaptureStore(tmp_path / "detections")
    capture = await store.create(
        image=np.zeros((360, 640, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=3,
        detections=[proposal("bird", 0.06, 0)],
        class_name="bird",
        confidence=0.06,
        model_name="test.pt",
        detector_settings={},
        jpeg_quality=80,
    )

    completed = await store.review_annotation(int(capture["id"]), 0, review_status="rejected")

    assert completed is not None
    assert completed["review_status"] == "rejected"
    assert completed["review_label"] == "not-bird"
    assert completed["detections"][0]["review_status"] == "rejected"  # type: ignore[index]


async def test_capture_filters_and_bulk_review(temp_database: Path, tmp_path: Path) -> None:
    store = DetectionCaptureStore(tmp_path / "detections")
    first = await store.create(
        image=np.zeros((120, 160, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=1,
        detections=[proposal("bird", 0.12, 0)],
        class_name="bird",
        confidence=0.12,
        model_name="noisy.pt",
        detector_settings={},
        jpeg_quality=80,
    )
    second = await store.create(
        image=np.zeros((120, 160, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=2,
        detections=[proposal("bird", 0.8, 0)],
        class_name="bird",
        confidence=0.8,
        model_name="strong.pt",
        detector_settings={},
        jpeg_quality=80,
        trigger="motion-rescan",
    )

    page = await store.page(model_name="noisy.pt", max_confidence=0.2)
    assert [item["id"] for item in page["items"]] == [first["id"]]  # type: ignore[index]
    motion = await store.page(trigger="motion-rescan", min_confidence=0.5)
    assert [item["id"] for item in motion["items"]] == [second["id"]]  # type: ignore[index]

    result = await store.bulk_review([int(first["id"]), int(second["id"])], "reject")
    assert result == {"affected": 2}
    rejected = await store.page(review_status="rejected")
    assert rejected["total"] == 2


async def test_motion_rescan_source_is_preserved_for_review(
    temp_database: Path, tmp_path: Path
) -> None:
    store = DetectionCaptureStore(tmp_path / "detections")
    motion = proposal("bird", 0.2, 0)
    motion = Detection(
        x1=motion.x1,
        y1=motion.y1,
        x2=motion.x2,
        y2=motion.y2,
        confidence=motion.confidence,
        class_id=motion.class_id,
        class_name=motion.class_name,
        source="motion_rescan",
    )
    capture = await store.create(
        image=np.zeros((360, 640, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=3,
        detections=[motion],
        class_name="bird",
        confidence=0.2,
        model_name="test.pt",
        detector_settings={},
        jpeg_quality=80,
        trigger="motion-rescan",
    )

    assert capture["trigger"] == "motion-rescan"
    assert capture["detections"][0]["source"] == "motion_rescan"  # type: ignore[index]


async def test_legacy_motion_only_capture_is_hidden_from_review_queue(
    temp_database: Path, tmp_path: Path
) -> None:
    store = DetectionCaptureStore(tmp_path / "detections")
    raw_motion = proposal("motion", 0.2, 0)
    raw_motion = Detection(
        x1=raw_motion.x1,
        y1=raw_motion.y1,
        x2=raw_motion.x2,
        y2=raw_motion.y2,
        confidence=raw_motion.confidence,
        class_id=-1,
        class_name="motion",
        source="motion",
    )
    await store.create(
        image=np.zeros((360, 640, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=4,
        detections=[raw_motion],
        class_name="motion",
        confidence=None,
        model_name="test.pt",
        detector_settings={},
        jpeg_quality=80,
        trigger="motion-rescan",
    )
    visible = await store.create(
        image=np.zeros((360, 640, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=5,
        detections=[proposal("bird", 0.2, 0)],
        class_name="bird",
        confidence=0.2,
        model_name="test.pt",
        detector_settings={},
        jpeg_quality=80,
        trigger="motion-rescan",
    )

    page = await store.page(review_status="unreviewed")
    listed = await store.list(review_status="unreviewed")
    current = await store.navigate(
        int(visible["id"]), direction="current", review_status="unreviewed"
    )

    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == [visible["id"]]  # type: ignore[index]
    assert [item["id"] for item in listed] == [visible["id"]]
    assert current is not None and current["total"] == 1


async def test_current_navigation_survives_capture_leaving_review_filter(
    temp_database: Path, tmp_path: Path
) -> None:
    store = DetectionCaptureStore(tmp_path / "detections")
    older = await store.create(
        image=np.zeros((360, 640, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=6,
        detections=[proposal("bird", 0.2, 0)],
        class_name="bird",
        confidence=0.2,
        model_name="test.pt",
        detector_settings={},
        jpeg_quality=80,
    )
    current = await store.create(
        image=np.zeros((360, 640, 3), dtype=np.uint8),
        camera_id="overview",
        frame_seq=7,
        detections=[proposal("bird", 0.2, 0)],
        class_name="bird",
        confidence=0.2,
        model_name="test.pt",
        detector_settings={},
        jpeg_quality=80,
    )
    await store.update_review(int(current["id"]), review_status="rejected", review_label="not-bird")

    context = await store.navigate(
        int(current["id"]), direction="current", review_status="unreviewed"
    )
    next_capture = await store.navigate(
        int(current["id"]), direction="next", review_status="unreviewed"
    )

    assert context is not None
    assert context["capture"]["review_status"] == "rejected"  # type: ignore[index]
    assert context["total"] == 1
    assert context["has_next"] is True
    assert next_capture is not None
    assert next_capture["capture"]["id"] == older["id"]  # type: ignore[index]


def test_dataset_split_keeps_adjacent_camera_frames_together() -> None:
    rows = [
        {"id": 1, "camera_id": "overview", "ts": "2026-08-14T10:00:00+00:00"},
        {"id": 2, "camera_id": "overview", "ts": "2026-08-14T10:00:10+00:00"},
        {"id": 3, "camera_id": "overview", "ts": "2026-08-14T12:00:00+00:00"},
    ]

    splits = _episode_splits(rows)

    assert splits[1] == splits[2]
    assert set(splits.values()) == {"train", "val"}
    assert splits[3] == "val"
