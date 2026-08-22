"""Detector proposal and operational confidence tiers."""

from pathlib import Path

import numpy as np

from app.services.settings_schema import DetectorSettings
from app.vision.detector import Detection, MockDetector
from app.vision.yolo_detector import YoloDetector, nvidia_device_exposed


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


def test_nvidia_device_requires_control_and_numbered_gpu_nodes(tmp_path: Path) -> None:
    (tmp_path / "nvidia1").touch()
    assert not nvidia_device_exposed(tmp_path)

    (tmp_path / "nvidiactl").touch()
    assert nvidia_device_exposed(tmp_path)


def test_yolo_omits_deprecated_half_option_when_disabled(tmp_path: Path) -> None:
    class Model:
        options: dict[str, object] = {}

        def predict(self, **options: object) -> list[object]:
            self.options = options
            return []

    detector = YoloDetector(DetectorSettings(half=False), tmp_path)
    model = Model()
    detector._model = model
    detector.status.device = "cuda"

    detector.infer(np.zeros((32, 32, 3), dtype=np.uint8))

    assert "half" not in model.options
