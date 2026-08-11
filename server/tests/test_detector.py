"""Detector proposal and operational confidence tiers."""

from app.services.settings_schema import DetectorSettings
from app.vision.detector import Detection, MockDetector


def detection(confidence: float) -> Detection:
    return Detection(
        x1=10,
        y1=10,
        x2=20,
        y2=20,
        confidence=confidence,
        class_id=14,
        class_name="bird",
    )


def test_low_confidence_proposal_is_captured_but_not_tracked() -> None:
    detector = MockDetector(
        DetectorSettings(
            classes=["bird"],
            capture_enabled=True,
            capture_confidence=0.1,
            confidence=0.35,
        )
    )
    raw = [detection(0.2), detection(0.7)]

    assert [item.confidence for item in detector.capturable(raw)] == [0.7, 0.2]
    assert [item.confidence for item in detector.operational(raw)] == [0.7]


def test_capture_threshold_does_not_remove_operational_detections() -> None:
    detector = MockDetector(
        DetectorSettings(
            classes=["bird"],
            capture_enabled=True,
            capture_confidence=0.7,
            confidence=0.35,
        )
    )
    raw = [detection(0.5)]

    assert detector.capturable(raw) == []
    assert detector.operational(raw) == raw
