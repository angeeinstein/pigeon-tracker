"""YOLO-family detector backed by Ultralytics.

Kept in its own module so the heavy imports (torch, ultralytics) only happen
when a YOLO model is actually configured. The rest of the server imports
nothing from here at start-up.
"""

from __future__ import annotations

import importlib.util
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from app.logging_config import get_logger
from app.services.settings_schema import DetectorSettings
from app.vision.detector import Detection, Detector

log = get_logger(__name__)


@lru_cache(maxsize=1)
def ultralytics_available() -> bool:
    return importlib.util.find_spec("ultralytics") is not None


@lru_cache(maxsize=1)
def cuda_available() -> bool:
    """True if torch reports a usable CUDA device. Never raises."""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def gpu_info() -> dict[str, Any]:
    """Diagnostics for the system page. Safe to call without torch."""
    info: dict[str, Any] = {"torch": False, "cuda": False, "devices": []}
    try:
        import torch
    except Exception:
        return info
    info["torch"] = True
    info["torch_version"] = torch.__version__
    try:
        info["cuda"] = bool(torch.cuda.is_available())
        if info["cuda"]:
            info["devices"] = [
                torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())
            ]
    except Exception as exc:  # pragma: no cover - driver dependent
        info["error"] = str(exc)
    return info


class YoloDetector(Detector):
    def __init__(self, settings: DetectorSettings, models_dir: Path) -> None:
        super().__init__(settings)
        self.models_dir = Path(models_dir)
        self._model: Any | None = None
        self._names: dict[int, str] = {}

    def _resolve_model_path(self) -> str:
        """Local file if we have one, otherwise the bare name.

        Ultralytics resolves well-known names (``yolov8n.pt``) by downloading
        them on first use; that download is what makes a fresh install work
        without shipping weights.
        """
        candidate = Path(self.settings.model_path)
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)
        local = self.models_dir / self.settings.model_path
        if local.exists():
            return str(local)
        return self.settings.model_path

    def _resolve_device(self) -> str:
        if self.settings.device == "cuda":
            if not cuda_available():
                log.warning("CUDA requested but unavailable, using CPU")
                return "cpu"
            return "cuda"
        if self.settings.device == "cpu":
            return "cpu"
        return "cuda" if cuda_available() else "cpu"

    def _prepare_writable_dirs(self) -> None:
        """Point every cache Ultralytics/matplotlib might write at the models dir.

        Under systemd the code directory is read-only and ``HOME`` may not be
        somewhere useful. Without this, model loading dies with a confusing
        ``Read-only file system`` error on a perfectly correct installation.
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            "YOLO_CONFIG_DIR": self.models_dir / ".ultralytics",
            "MPLCONFIGDIR": self.models_dir / ".matplotlib",
        }
        for name, fallback in defaults.items():
            # The directory must exist whether the value came from the
            # environment (systemd sets both) or from the fallback: ultralytics
            # tests writability at import time and silently drops to /tmp if
            # the directory is missing, losing its settings on every restart.
            path = Path(os.environ.get(name) or fallback)
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - depends on deployment
                log.warning(
                    "cache directory is not creatable",
                    extra={"ctx": {"var": name, "path": str(path), "error": str(exc)}},
                )
                continue
            os.environ[name] = str(path)

    def load(self) -> None:
        self._prepare_writable_dirs()

        from ultralytics import YOLO

        model_path = self._resolve_model_path()
        device = self._resolve_device()
        log.info(
            "loading detector model",
            extra={"ctx": {"model": model_path, "device": device}},
        )
        try:
            model = YOLO(model_path)
            model.to(device)
            self._model = model
            self._names = dict(getattr(model, "names", {}) or {})
            self.status.loaded = True
            self.status.backend = "yolo"
            self.status.device = device
            self.status.model = model_path
            self.status.classes = list(self._names.values())
            self.status.error = None
        except Exception as exc:
            self.status.loaded = False
            self.status.error = f"model load failed: {exc}"
            log.error("detector load failed", extra={"ctx": {"error": str(exc)}})
            raise

    def infer(self, image: np.ndarray) -> list[Detection]:
        if self._model is None:
            return []
        started = time.perf_counter()
        results = self._model.predict(
            source=image,
            imgsz=self.settings.input_size,
            conf=self.settings.confidence,
            iou=self.settings.iou,
            device=self.status.device,
            half=self.settings.half and self.status.device == "cuda",
            max_det=self.settings.max_detections,
            verbose=False,
        )
        detections: list[Detection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            xyxy = boxes.xyxy.cpu().numpy()
            confidences = boxes.conf.cpu().numpy()
            class_ids = boxes.cls.cpu().numpy().astype(int)
            for (x1, y1, x2, y2), conf, class_id in zip(xyxy, confidences, class_ids, strict=False):
                detections.append(
                    Detection(
                        x1=float(x1),
                        y1=float(y1),
                        x2=float(x2),
                        y2=float(y2),
                        confidence=float(conf),
                        class_id=int(class_id),
                        class_name=self._names.get(int(class_id), str(class_id)),
                    )
                )

        self.status.last_inference_ms = (time.perf_counter() - started) * 1000.0
        self.status.inferences += 1
        return self._filter(detections)

    def close(self) -> None:
        self._model = None
        self.status.loaded = False
