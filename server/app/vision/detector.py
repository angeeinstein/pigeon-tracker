"""Detector abstraction.

The rest of the application only ever sees :class:`Detection` objects and the
:class:`Detector` interface, so swapping a generic COCO model for a
pigeon-specific one — or for something that is not YOLO at all — is a matter of
adding one class and one registry entry.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from app.logging_config import get_logger
from app.services.settings_schema import DetectorSettings

log = get_logger(__name__)


@dataclass(frozen=True)
class Detection:
    """One detected object in image pixel coordinates."""

    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    class_id: int
    class_name: str

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    def as_tlbr(self) -> np.ndarray:
        return np.array([self.x1, self.y1, self.x2, self.y2], dtype=np.float32)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bbox": [round(self.x1, 1), round(self.y1, 1), round(self.x2, 1), round(self.y2, 1)],
            "confidence": round(self.confidence, 3),
            "class_id": self.class_id,
            "class_name": self.class_name,
        }


@dataclass
class DetectorStatus:
    backend: str = "none"
    loaded: bool = False
    device: str = "cpu"
    model: str = ""
    classes: list[str] = field(default_factory=list)
    last_inference_ms: float = 0.0
    inferences: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "loaded": self.loaded,
            "device": self.device,
            "model": self.model,
            "classes": list(self.classes),
            "last_inference_ms": round(self.last_inference_ms, 1),
            "inferences": self.inferences,
            "error": self.error,
        }


class Detector(abc.ABC):
    """Synchronous detector. Called from a worker thread, never from the loop."""

    def __init__(self, settings: DetectorSettings) -> None:
        self.settings = settings
        self.status = DetectorStatus(backend=settings.backend)

    @abc.abstractmethod
    def load(self) -> None:
        """Load weights/resources. May block. Must be safe to call twice."""

    @abc.abstractmethod
    def infer(self, image: np.ndarray) -> list[Detection]:
        """Run detection on a BGR image."""

    def close(self) -> None:  # noqa: B027 - optional hook; not every detector holds resources
        """Release resources. Default implementation does nothing."""

    def _filter(
        self,
        detections: list[Detection],
        *,
        min_confidence: float | None = None,
    ) -> list[Detection]:
        wanted = {c.lower() for c in self.settings.classes}
        threshold = self.settings.confidence if min_confidence is None else min_confidence
        result = [d for d in detections if d.confidence >= threshold]
        if wanted:
            result = [d for d in result if d.class_name.lower() in wanted]
        result.sort(key=lambda d: d.confidence, reverse=True)
        return result[: self.settings.max_detections]

    def operational(self, proposals: list[Detection]) -> list[Detection]:
        """Apply the normal tracking threshold to capture-level proposals."""
        return self._filter(proposals, min_confidence=self.settings.confidence)

    def capturable(self, proposals: list[Detection]) -> list[Detection]:
        """Return proposals that qualify for evidence collection."""
        threshold = (
            self.settings.capture_confidence
            if self.settings.capture_enabled
            else self.settings.confidence
        )
        return self._filter(proposals, min_confidence=threshold)


class NullDetector(Detector):
    """Detection disabled. Keeps the pipeline shape identical."""

    def load(self) -> None:
        self.status.loaded = True
        self.status.backend = "none"
        self.status.model = "disabled"

    def infer(self, image: np.ndarray) -> list[Detection]:
        return []


class MockDetector(Detector):
    """Blob detector used for development and tests.

    Finds dark compact regions, which is exactly what the simulated camera
    draws. It needs no model file, no network and no GPU, so the whole
    targeting chain can be exercised on a laptop.
    """

    def __init__(self, settings: DetectorSettings, dark_threshold: int = 90) -> None:
        super().__init__(settings)
        self.dark_threshold = dark_threshold

    def load(self) -> None:
        self.status.loaded = True
        self.status.backend = "mock"
        self.status.model = "blob"
        self.status.device = "cpu"
        self.status.classes = ["bird"]

    def infer(self, image: np.ndarray) -> list[Detection]:
        started = time.perf_counter()
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, self.dark_threshold, 255, cv2.THRESH_BINARY_INV)
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        frame_area = float(image.shape[0] * image.shape[1])
        detections: list[Detection] = []
        for contour in contours:
            area = cv2.contourArea(contour)
            # Reject specks and anything the size of a wall.
            if area < frame_area * 2e-4 or area > frame_area * 0.05:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            fill = area / float(max(1, w * h))
            if fill < 0.4:
                continue
            confidence = float(min(0.99, 0.55 + fill * 0.4))
            detections.append(
                Detection(
                    x1=float(x),
                    y1=float(y),
                    x2=float(x + w),
                    y2=float(y + h),
                    confidence=confidence,
                    class_id=14,
                    class_name="bird",
                )
            )

        self.status.last_inference_ms = (time.perf_counter() - started) * 1000.0
        self.status.inferences += 1
        threshold = min(self.settings.capture_confidence, self.settings.confidence)
        if not self.settings.capture_enabled:
            threshold = self.settings.confidence
        return self._filter(detections, min_confidence=threshold)


def create_detector(
    settings: DetectorSettings, models_dir: Any, *, force_mock: bool = False
) -> Detector:
    """Build the detector for the current settings.

    Falls back to the mock detector — loudly — when the AI stack is missing, so
    a machine without torch installed still runs the whole application.
    """
    if not settings.enabled or settings.backend == "none":
        return NullDetector(settings)
    if force_mock or settings.backend == "mock":
        return MockDetector(settings)

    from app.vision.yolo_detector import YoloDetector, ultralytics_available

    if not ultralytics_available():
        log.warning(
            "ultralytics not installed, falling back to the mock detector "
            "(install requirements/ai.txt for real detection)"
        )
        detector = MockDetector(settings)
        detector.status.error = "ultralytics not installed - using mock detector"
        return detector
    return YoloDetector(settings, models_dir)
