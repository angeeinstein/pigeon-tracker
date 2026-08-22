#!/usr/bin/env python3
"""Self-contained desktop trainer shipped with pigeon-tracker YOLO exports.

The outer process uses only the Python standard library. It creates a reusable
virtual environment, installs Ultralytics when necessary, and starts a worker
process for training. Keeping the ML imports in the worker lets the GUI show
dependency installation and training output without freezing.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

APP_NAME = "Pigeon Tracker Model Trainer"
DEPENDENCY = "ultralytics>=8.1,<9.0"
CUDA_INDEX_URL = "https://download.pytorch.org/whl/cu126"
EVENT_PREFIX = "__PIGEON_TRAINER__"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DATASET_DIR = Path(__file__).resolve().parent
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", DATASET_DIR)) / "PigeonTrackerTrainer"
VENV_DIR = APP_DATA_DIR / "venv"


def _runtime_is_complete() -> bool:
    try:
        import ensurepip  # noqa: F401
        import tkinter  # noqa: F401
        import venv  # noqa: F401
    except ImportError:
        return False
    return True


def _known_python_executables() -> list[Path]:
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        root = Path(local) / "Programs" / "Python"
        candidates.extend(sorted(root.glob("Python3*/python.exe"), reverse=True))
    for raw in (
        r"C:\Program Files\Python313\python.exe",
        r"C:\Program Files\Python312\python.exe",
        r"C:\Program Files\Python311\python.exe",
    ):
        candidates.append(Path(raw))
    return candidates


def _python_can_host_gui(command: list[str]) -> bool:
    probe = [*command, "-c", "import ensurepip, tkinter, venv"]
    try:
        return (
            subprocess.run(
                probe,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _find_standard_python() -> list[str] | None:
    launcher = shutil.which("py")
    if launcher:
        for version in ("-3.13", "-3.12", "-3.11", "-3"):
            command = [launcher, version]
            if _python_can_host_gui(command):
                return command
    for candidate in _known_python_executables():
        if candidate.is_file() and _python_can_host_gui([str(candidate)]):
            return [str(candidate)]
    return None


def _relaunch_with_standard_python() -> int:
    command = _find_standard_python()
    if command is None:
        winget = shutil.which("winget")
        if not winget:
            print(
                "A standard 64-bit Python installation is required. Install Python 3.11 "
                "or newer from python.org with 'Add python.exe to PATH' enabled, then run "
                "train_windows.bat again."
            )
            return 2
        print("The current Python is incomplete (this is common for ESP-IDF Python).")
        print("Installing the standard 64-bit Python 3.11 runtime with Windows Package Manager...")
        result = subprocess.run(
            [
                winget,
                "install",
                "--exact",
                "--id",
                "Python.Python.3.11",
                "--scope",
                "user",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            check=False,
        )
        if result.returncode != 0:
            print("Python installation failed. See the winget message above.")
            return result.returncode or 2
        command = _find_standard_python()
        if command is None:
            print(
                "Python was installed but could not yet be located. Close this window and run "
                "the launcher again."
            )
            return 2
    return subprocess.call([*command, str(Path(__file__).resolve()), *sys.argv[1:]])


def _emit_worker(event: str, **values: object) -> None:
    print(EVENT_PREFIX + json.dumps({"event": event, **values}), flush=True)


def _worker_probe() -> int:
    import torch
    import ultralytics

    cuda = bool(torch.cuda.is_available())
    devices = []
    device_memory_gb = []
    if cuda:
        for index in range(torch.cuda.device_count()):
            devices.append(torch.cuda.get_device_name(index))
            device_memory_gb.append(
                round(torch.cuda.get_device_properties(index).total_memory / (1024**3), 1)
            )
    _emit_worker(
        "probe",
        python=sys.version.split()[0],
        ultralytics=ultralytics.__version__,
        torch=torch.__version__,
        cuda_version=torch.version.cuda,
        cuda=cuda,
        devices=devices,
        device_memory_gb=device_memory_gb,
    )
    return 0


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


METRIC_KEYS = {
    "precision": "metrics/precision(B)",
    "recall": "metrics/recall(B)",
    "map50": "metrics/mAP50(B)",
    "map50_95": "metrics/mAP50-95(B)",
}

DEPLOYMENT_THRESHOLDS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.35, 0.40, 0.45, 0.50, 0.60)


def _numeric_metrics(raw: dict[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for short_name, source_name in METRIC_KEYS.items():
        value = _as_float(raw.get(source_name))
        if value is not None:
            metrics[short_name] = value
    return metrics


def _validation_fitness(metrics: dict[str, Any]) -> float | None:
    map50 = _as_float(metrics.get("map50"))
    map50_95 = _as_float(metrics.get("map50_95"))
    if map50 is None or map50_95 is None:
        return None
    return 0.1 * map50 + 0.9 * map50_95


def _model_recommendation(
    trained: dict[str, Any],
    baseline: dict[str, Any] | None,
    best_path: Path,
    starting_path: Path,
    trained_sweep: dict[str, Any] | None = None,
    baseline_sweep: dict[str, Any] | None = None,
) -> dict[str, str]:
    trained_operating = _operating_point(trained_sweep)
    baseline_operating = _operating_point(baseline_sweep)
    if trained_operating and baseline_operating:
        trained_is_safer = trained_operating["precision"] >= baseline_operating[
            "precision"
        ] - 0.02 and trained_operating["false_positive_image_rate"] <= max(
            0.05, baseline_operating["false_positive_image_rate"] + 0.02
        )
        trained_is_better = trained_operating["f1"] > baseline_operating["f1"]
        if not (trained_is_safer and trained_is_better):
            return {
                "label": f"Keep the starting model ({starting_path.name})",
                "path": str(starting_path),
                "reason": (
                    "The trained checkpoint did not improve the fixed-threshold bird result "
                    "without increasing false positives on held-out negative images."
                ),
            }
    trained_fitness = _validation_fitness(trained)
    baseline_fitness = _validation_fitness(baseline or {})
    if (
        trained_fitness is not None
        and baseline_fitness is not None
        and baseline_fitness > trained_fitness
    ):
        return {
            "label": f"Keep the starting model ({starting_path.name})",
            "path": str(starting_path),
            "reason": "It scored higher than the trained checkpoint on held-out validation.",
        }
    comparison = (
        "It outperformed the starting model on held-out validation."
        if baseline_fitness is not None
        else "It had the strongest held-out validation score during this run."
    )
    return {
        "label": "Use best.pt",
        "path": str(best_path),
        "reason": comparison,
    }


def _box_iou(left: list[float], right: list[float]) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    overlap = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - overlap
    return overlap / union if union > 0 else 0.0


def _threshold_statistics(
    samples: list[dict[str, Any]],
    thresholds: tuple[float, ...] = DEPLOYMENT_THRESHOLDS,
    *,
    match_iou: float = 0.5,
) -> dict[str, Any]:
    """Measure deployed confidence behavior, including negative-frame noise."""
    rows: list[dict[str, Any]] = []
    positive_images = sum(bool(sample["targets"]) for sample in samples)
    negative_images = len(samples) - positive_images
    for threshold in thresholds:
        true_positive = false_positive = false_negative = false_positive_images = 0
        for sample in samples:
            targets = [list(box) for box in sample["targets"]]
            predictions = sorted(
                (item for item in sample["predictions"] if float(item["confidence"]) >= threshold),
                key=lambda item: float(item["confidence"]),
                reverse=True,
            )
            unmatched = set(range(len(targets)))
            image_false_positives = 0
            for prediction in predictions:
                box = list(prediction["bbox"])
                matches = [(index, _box_iou(box, targets[index])) for index in unmatched]
                best = max(matches, key=lambda item: item[1], default=None)
                if best and best[1] >= match_iou:
                    true_positive += 1
                    unmatched.remove(best[0])
                else:
                    false_positive += 1
                    image_false_positives += 1
            false_negative += len(unmatched)
            if not targets and image_false_positives:
                false_positive_images += 1
        precision = true_positive / max(1, true_positive + false_positive)
        recall = true_positive / max(1, true_positive + false_negative)
        f1 = 2 * precision * recall / max(1e-12, precision + recall)
        rows.append(
            {
                "threshold": threshold,
                "tp": true_positive,
                "fp": false_positive,
                "fn": false_negative,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "false_positive_images": false_positive_images,
                "false_positive_image_rate": false_positive_images / max(1, negative_images),
                "boxes_per_negative_image": false_positive / max(1, negative_images),
            }
        )
    return {
        "images": len(samples),
        "positive_images": positive_images,
        "negative_images": negative_images,
        "thresholds": rows,
        "recommended": _recommend_thresholds(rows),
    }


def _threshold_breakdowns(
    samples: list[dict[str, Any]], sweep: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    recommended = dict(sweep.get("recommended") or {})
    threshold = _as_float(recommended.get("operational"))
    if threshold is None:
        return {}
    breakdowns: dict[str, list[dict[str, Any]]] = {}
    for dimension in ("camera", "day"):
        groups = sorted({str(sample.get(dimension) or "") for sample in samples} - {""})
        rows: list[dict[str, Any]] = []
        for group in groups:
            members = [sample for sample in samples if str(sample.get(dimension) or "") == group]
            result = _threshold_statistics(members, (threshold,))["thresholds"][0]
            rows.append({dimension: group, "images": len(members), **result})
        if rows:
            breakdowns[dimension] = rows
    return breakdowns


def _recommend_thresholds(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {"operational": 0.35, "capture": 0.25, "rescan": 0.40}
    acceptable = [
        row for row in rows if row["precision"] >= 0.90 and row["false_positive_image_rate"] <= 0.05
    ]
    operational = max(acceptable, key=lambda row: (row["recall"], row["f1"]), default=None)
    if operational is None:
        operational = max(rows, key=lambda row: (row["f1"], row["precision"]))
    capture_candidates = [
        row
        for row in rows
        if row["threshold"] <= operational["threshold"]
        and row["precision"] >= 0.80
        and row["false_positive_image_rate"] <= 0.01
        and row["boxes_per_negative_image"] <= 0.03
    ]
    capture = min(
        capture_candidates,
        key=lambda row: row["threshold"],
        default=operational,
    )
    return {
        "operational": float(operational["threshold"]),
        "capture": float(capture["threshold"]),
        "rescan": min(
            0.99,
            max(float(operational["threshold"]), float(capture["threshold"]) + 0.10),
        ),
    }


def _operating_point(sweep: dict[str, Any] | None) -> dict[str, Any] | None:
    if not sweep:
        return None
    recommended = dict(sweep.get("recommended") or {})
    threshold = _as_float(recommended.get("operational"))
    if threshold is None:
        return None
    return next(
        (
            row
            for row in sweep.get("thresholds") or []
            if abs(float(row["threshold"]) - threshold) < 1e-6
        ),
        None,
    )


def _training_history_summary(save_dir: Path) -> dict[str, Any]:
    csv_path = save_dir / "results.csv"
    rows: list[dict[str, str]] = []
    if csv_path.is_file():
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    best_epoch: int | None = None
    best_fitness: float | None = None
    for row in rows:
        metrics = _numeric_metrics(row)
        if "map50" not in metrics or "map50_95" not in metrics:
            continue
        # This is the fitness weighting used by Ultralytics detection models.
        fitness = _validation_fitness(metrics)
        if fitness is None:
            continue
        if best_fitness is None or fitness > best_fitness:
            best_fitness = fitness
            with suppress(TypeError, ValueError):
                best_epoch = int(float(row.get("epoch", "")))

    chart_names = (
        "results.png",
        "BoxPR_curve.png",
        "BoxF1_curve.png",
        "BoxP_curve.png",
        "BoxR_curve.png",
        "confusion_matrix_normalized.png",
        "confusion_matrix.png",
    )
    return {
        "epochs_completed": len(rows),
        "best_epoch": best_epoch,
        "charts": [str(save_dir / name) for name in chart_names if (save_dir / name).is_file()],
    }


def _exception_text(exc: BaseException) -> str:
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        messages.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " | ".join(messages)


def _is_pin_memory_failure(message: str) -> bool:
    normalized = message.casefold()
    return "pin memory" in normalized or "resource already mapped" in normalized


def _configure_ultralytics_pin_memory(enabled: bool) -> None:
    """Override Ultralytics' otherwise hard-coded DataLoader pinning choice."""
    import ultralytics.data.build as data_build

    original = data_build.build_dataloader

    def configured_build_dataloader(*args: Any, **kwargs: Any) -> Any:
        kwargs["pin_memory"] = enabled
        return original(*args, **kwargs)

    data_build.build_dataloader = configured_build_dataloader

    # Detection modules may bind the builder at import time, so update both.
    import ultralytics.models.yolo.detect.train as detect_train
    import ultralytics.models.yolo.detect.val as detect_val

    detect_train.build_dataloader = configured_build_dataloader
    detect_val.build_dataloader = configured_build_dataloader


def _benchmark_starting_model(
    model_path: str,
    data_path: str,
    image_size: int,
    device: str | int,
    save_dir: Path,
) -> dict[str, Any]:
    """Evaluate only the starting model's bird class against our class 0 labels."""
    import torch
    from ultralytics import YOLO
    from ultralytics.models.yolo.detect import DetectionValidator

    starting_model = YOLO(model_path)
    names = dict(starting_model.names)
    bird_class = next(
        (int(index) for index, name in names.items() if str(name).strip().casefold() == "bird"),
        None,
    )
    if bird_class is None:
        raise ValueError("The starting model has no class named 'bird', so it cannot be compared.")

    class BirdOnlyValidator(DetectionValidator):
        def postprocess(self, predictions: Any) -> Any:
            processed = super().postprocess(predictions)
            for result in processed:
                if not len(result["cls"]):
                    continue
                keep = result["cls"] == bird_class
                for key in ("bboxes", "conf", "cls", "extra"):
                    if key in result:
                        result[key] = result[key][keep]
                if len(result["cls"]):
                    result["cls"] = torch.zeros_like(result["cls"])
            return processed

    results = starting_model.val(
        validator=BirdOnlyValidator,
        data=data_path,
        imgsz=image_size,
        device=device,
        workers=0,
        plots=False,
        project=str(save_dir),
        name="starting-model-benchmark",
        exist_ok=True,
        verbose=False,
    )
    return {
        "model": Path(model_path).name,
        "bird_class": bird_class,
        "precision": float(results.box.mp),
        "recall": float(results.box.mr),
        "map50": float(results.box.map50),
        "map50_95": float(results.box.map),
    }


def _validation_images(data_path: str) -> list[Path]:
    from ultralytics.utils import YAML

    yaml_path = Path(data_path).resolve()
    config = YAML.load(yaml_path)
    root = Path(str(config.get("path") or yaml_path.parent))
    if not root.is_absolute():
        root = (yaml_path.parent / root).resolve()
    raw_val = config.get("val")
    entries = raw_val if isinstance(raw_val, list) else [raw_val]
    images: list[Path] = []
    suffixes = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    for raw in entries:
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_absolute():
            path = root / path
        if path.is_dir():
            images.extend(
                item for item in sorted(path.rglob("*")) if item.suffix.casefold() in suffixes
            )
        elif path.suffix.casefold() == ".txt" and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                candidate = Path(line.strip())
                if not candidate.is_absolute():
                    candidate = root / candidate
                if candidate.is_file():
                    images.append(candidate)
        elif path.is_file():
            images.append(path)
    return images


def _label_path_for_image(image: Path) -> Path:
    parts = list(image.parts)
    lowered = [part.casefold() for part in parts]
    if "images" in lowered:
        parts[lowered.index("images")] = "labels"
        return Path(*parts).with_suffix(".txt")
    return image.with_suffix(".txt")


def _read_target_boxes(image: Path, width: int, height: int) -> list[list[float]]:
    label = _label_path_for_image(image)
    if not label.is_file():
        return []
    boxes: list[list[float]] = []
    for line in label.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        center_x, center_y, box_width, box_height = map(float, fields[1:5])
        boxes.append(
            [
                (center_x - box_width / 2) * width,
                (center_y - box_height / 2) * height,
                (center_x + box_width / 2) * width,
                (center_y + box_height / 2) * height,
            ]
        )
    return boxes


def _threshold_sweep_model(
    model_path: str,
    data_path: str,
    image_size: int,
    device: str | int,
    *,
    progress_name: str,
) -> dict[str, Any]:
    """Predict validation images once, then score every deployment threshold."""
    from ultralytics import YOLO

    images = _validation_images(data_path)
    if not images:
        raise ValueError("No validation images were found for threshold evaluation.")
    model = YOLO(model_path)
    context: dict[str, dict[str, str]] = {}
    manifest_path = Path(data_path).resolve().parent / "manifest.json"
    with suppress(OSError, json.JSONDecodeError, TypeError):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for capture in manifest.get("captures") or []:
            stem = f"capture-{int(capture['capture_id']):08d}"
            context[stem] = {
                "camera": str(capture.get("camera_id") or ""),
                "day": str(capture.get("timestamp") or "")[:10],
            }
    names = dict(model.names)
    bird_class = next(
        (int(index) for index, name in names.items() if str(name).strip().casefold() == "bird"),
        None,
    )
    if bird_class is None:
        raise ValueError(f"{Path(model_path).name} has no class named bird.")
    samples: list[dict[str, Any]] = []
    results = model.predict(
        source=[str(path) for path in images],
        imgsz=image_size,
        conf=min(DEPLOYMENT_THRESHOLDS),
        iou=0.45,
        device=device,
        classes=[bird_class],
        stream=True,
        verbose=False,
    )
    for index, (image, result) in enumerate(zip(images, results, strict=False), start=1):
        height, width = map(int, result.orig_shape)
        predictions: list[dict[str, Any]] = []
        boxes = getattr(result, "boxes", None)
        if boxes is not None:
            for xyxy, confidence in zip(
                boxes.xyxy.cpu().tolist(), boxes.conf.cpu().tolist(), strict=False
            ):
                predictions.append({"bbox": list(map(float, xyxy)), "confidence": confidence})
        samples.append(
            {
                "image": str(image),
                "targets": _read_target_boxes(image, width, height),
                "predictions": predictions,
                **context.get(image.stem, {}),
            }
        )
        if index == len(images) or index % max(1, len(images) // 20) == 0:
            _emit_worker(
                "threshold_progress",
                model=progress_name,
                completed=index,
                total=len(images),
            )
    sweep = _threshold_statistics(samples)
    sweep["breakdowns"] = _threshold_breakdowns(samples, sweep)
    sweep["model"] = Path(model_path).name
    return sweep


def _write_threshold_chart(save_dir: Path, sweep: dict[str, Any]) -> Path | None:
    rows = list(sweep.get("thresholds") or [])
    if not rows:
        return None
    import matplotlib.pyplot as plt

    thresholds = [float(row["threshold"]) for row in rows]
    figure, axis = plt.subplots(figsize=(10, 5.6))
    axis.plot(
        thresholds,
        [row["precision"] for row in rows],
        "o-",
        color="#2563eb",
        label="Precision",
    )
    axis.plot(
        thresholds,
        [row["recall"] for row in rows],
        "s--",
        color="#60a5fa",
        label="Recall",
    )
    axis.plot(
        thresholds,
        [row["f1"] for row in rows],
        "^-",
        color="#1e3a8a",
        linewidth=2.2,
        label="F1",
    )
    axis.plot(
        thresholds,
        [row["false_positive_image_rate"] for row in rows],
        "--",
        color="#d97706",
        label="Negative images with a false box",
    )
    recommended = dict(sweep.get("recommended") or {})
    operational = _as_float(recommended.get("operational"))
    if operational is not None:
        axis.axvline(operational, color="#374151", linestyle=":", label="Recommended operational")
    axis.set_title(
        "Validation behavior by confidence threshold\n"
        f"{int(sweep.get('positive_images', 0))} positive and "
        f"{int(sweep.get('negative_images', 0))} negative held-out images"
    )
    axis.set_xlabel("Confidence threshold")
    axis.set_ylabel("Rate")
    axis.set_ylim(0, 1.02)
    axis.grid(True, color="#d1d5db", alpha=0.55)
    axis.legend(loc="best")
    figure.tight_layout()
    path = save_dir / "threshold_tradeoff.png"
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return path


def _write_deployment_package(
    save_dir: Path,
    best: Path,
    manifest: dict[str, Any],
) -> tuple[Path, Path]:
    manifest_path = save_dir / "deployment.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    package = save_dir / "pigeon-model.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(best, "model.pt")
        archive.write(manifest_path, "deployment.json")
        archive.writestr(
            "README.txt",
            "Upload this complete ZIP in Settings > AI > Install a trained model. "
            "It includes best.pt plus validated threshold recommendations.\n",
        )
    return manifest_path, package


def _worker_train(config_path: Path) -> int:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    import torch
    from ultralytics import YOLO

    pin_memory = bool(config.get("pin_memory", False))
    _configure_ultralytics_pin_memory(pin_memory)

    nvml: Any | None = None
    nvml_handle: Any | None = None
    if torch.cuda.is_available():
        try:
            import pynvml

            pynvml.nvmlInit()
            nvml = pynvml
            nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(torch.cuda.current_device())
        except Exception:
            nvml = None
            nvml_handle = None

    requested_device = str(config["device"])
    device: str | int = requested_device
    if requested_device == "auto":
        device = 0 if torch.cuda.is_available() else "cpu"
    _emit_worker(
        "training_start",
        device=str(device),
        gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        workers=int(config["workers"]),
        pin_memory=pin_memory,
        resumed=bool(config.get("resume")),
    )

    model = YOLO(config.get("resume") or config["model"])
    last_batch_report: dict[int, int] = {}
    batch_counts: dict[int, int] = {}

    def gpu_telemetry() -> dict[str, float | None]:
        telemetry: dict[str, float | None] = {
            "gpu_utilization_pct": None,
            "gpu_temperature_c": None,
        }
        if nvml is None or nvml_handle is None:
            return telemetry
        with suppress(Exception):
            telemetry["gpu_utilization_pct"] = float(
                nvml.nvmlDeviceGetUtilizationRates(nvml_handle).gpu
            )
            telemetry["gpu_temperature_c"] = float(
                nvml.nvmlDeviceGetTemperature(nvml_handle, nvml.NVML_TEMPERATURE_GPU)
            )
        return telemetry

    def on_train_batch_end(trainer: Any) -> None:
        epoch = int(getattr(trainer, "epoch", 0)) + 1
        batch = batch_counts.get(epoch, 0) + 1
        batch_counts[epoch] = batch
        try:
            batches = len(trainer.train_loader)
        except (AttributeError, TypeError):
            return
        interval = max(1, batches // 20)
        if batch == batches or batch - last_batch_report.get(epoch, 0) >= interval:
            last_batch_report[epoch] = batch
            _emit_worker(
                "batch",
                epoch=epoch,
                epochs=int(config["epochs"]),
                batch=batch,
                batches=batches,
                batch_size=int(getattr(trainer, "batch_size", 0) or 0),
                gpu_allocated_gb=(
                    round(torch.cuda.memory_allocated() / (1024**3), 2)
                    if torch.cuda.is_available()
                    else None
                ),
                gpu_reserved_gb=(
                    round(torch.cuda.memory_reserved() / (1024**3), 2)
                    if torch.cuda.is_available()
                    else None
                ),
                **gpu_telemetry(),
            )

    def on_fit_epoch_end(trainer: Any) -> None:
        metrics: dict[str, float] = {}
        for key, value in dict(getattr(trainer, "metrics", {}) or {}).items():
            number = _as_float(value)
            if number is not None:
                metrics[str(key)] = number
        _emit_worker(
            "epoch",
            epoch=int(getattr(trainer, "epoch", 0)) + 1,
            epochs=int(config["epochs"]),
            metrics=metrics,
        )

    model.add_callback("on_train_batch_end", on_train_batch_end)
    model.add_callback("on_fit_epoch_end", on_fit_epoch_end)
    train_arguments = {
        "data": config["data"],
        "imgsz": int(config["imgsz"]),
        "epochs": int(config["epochs"]),
        "patience": int(config["patience"]),
        "batch": int(config["batch"]),
        "device": device,
        "workers": int(config["workers"]),
        "seed": int(config["seed"]),
        "deterministic": True,
        "project": config["project"],
        "name": config["name"],
        "plots": True,
        "verbose": True,
    }
    if config.get("resume"):
        train_arguments["resume"] = str(config["resume"])
    try:
        results = model.train(**train_arguments)
        trainer = model.trainer
        save_dir = Path(str(getattr(trainer, "save_dir", config["project"]))).resolve()
        best = Path(str(getattr(trainer, "best", save_dir / "weights" / "best.pt"))).resolve()
        last = Path(str(getattr(trainer, "last", save_dir / "weights" / "last.pt"))).resolve()
        history = _training_history_summary(save_dir)
        metrics = _numeric_metrics(dict(getattr(trainer, "metrics", {}) or {}))
        trained_sweep: dict[str, Any] | None = None
        threshold_error: str | None = None
        try:
            _emit_worker("threshold_start", model="best.pt")
            trained_sweep = _threshold_sweep_model(
                str(best),
                str(config["data"]),
                int(config["imgsz"]),
                device,
                progress_name="best.pt",
            )
            chart = _write_threshold_chart(save_dir, trained_sweep)
            if chart is not None:
                history["charts"].append(str(chart))
        except Exception as exc:
            threshold_error = _exception_text(exc)
            _emit_worker("threshold_unavailable", model="best.pt", message=threshold_error)
        baseline: dict[str, Any] | None = None
        baseline_sweep: dict[str, Any] | None = None
        baseline_error: str | None = None
        if config.get("benchmark", True):
            _emit_worker("benchmark_start", model=Path(str(config["model"])).name)
            try:
                baseline = _benchmark_starting_model(
                    str(config["model"]),
                    str(config["data"]),
                    int(config["imgsz"]),
                    device,
                    save_dir,
                )
                baseline_sweep = _threshold_sweep_model(
                    str(config["model"]),
                    str(config["data"]),
                    int(config["imgsz"]),
                    device,
                    progress_name=Path(str(config["model"])).name,
                )
                _emit_worker("benchmark_complete", baseline=baseline)
            except Exception as exc:
                baseline_error = _exception_text(exc)
                _emit_worker("benchmark_unavailable", message=baseline_error)
        starting_path = Path(str(config["model"]))
        if not starting_path.is_absolute():
            starting_path = DATASET_DIR / starting_path
        recommendation = _model_recommendation(
            metrics,
            baseline,
            best,
            starting_path.resolve(),
            trained_sweep,
            baseline_sweep,
        )
        deployment_manifest = {
            "format_version": 1,
            "model": best.name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "metrics": metrics,
            "baseline": baseline,
            "threshold_evaluation": trained_sweep,
            "baseline_threshold_evaluation": baseline_sweep,
            "recommended_thresholds": (
                dict(trained_sweep.get("recommended") or {}) if trained_sweep else {}
            ),
            "recommendation": recommendation,
        }
        deployment_manifest_path, deployment_package = _write_deployment_package(
            save_dir, best, deployment_manifest
        )
        _emit_worker(
            "complete",
            save_dir=str(save_dir),
            best=str(best),
            last=str(last),
            result=str(results),
            metrics=metrics,
            baseline=baseline,
            baseline_error=baseline_error,
            trained_sweep=trained_sweep,
            baseline_sweep=baseline_sweep,
            threshold_error=threshold_error,
            recommendation=recommendation,
            deployment_manifest=str(deployment_manifest_path),
            deployment_package=str(deployment_package),
            requested_epochs=int(config["epochs"]),
            **history,
        )
        return 0
    except Exception as exc:
        trainer = getattr(model, "trainer", None)
        save_dir = Path(str(getattr(trainer, "save_dir", config["project"]))).resolve()
        last = Path(str(getattr(trainer, "last", save_dir / "weights" / "last.pt"))).resolve()
        message = _exception_text(exc)
        retryable = _is_pin_memory_failure(message)
        _emit_worker(
            "failure",
            message=message,
            retryable_safe_loader=retryable,
            save_dir=str(save_dir),
            last=str(last) if last.is_file() else None,
        )
        traceback.print_exc()
        return 75 if retryable else 1
    finally:
        if nvml is not None:
            with suppress(Exception):
                nvml.nvmlShutdown()


def _worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.probe:
        return _worker_probe()
    if args.config:
        return _worker_train(args.config)
    parser.error("worker requires --probe or --config")
    return 2


def _dataset_counts(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for split in ("train", "val"):
        images = root / "images" / split
        labels = root / "labels" / split
        if not images.is_dir() or not labels.is_dir():
            raise ValueError(f"Missing images/{split} or labels/{split} in {root}")
        image_files = [
            path for path in images.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        ]
        positive = 0
        boxes = 0
        for image in image_files:
            label = labels / f"{image.stem}.txt"
            if not label.is_file():
                raise ValueError(f"Missing label file for {image.name}")
            lines = [
                line for line in label.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            positive += bool(lines)
            boxes += len(lines)
        counts[f"{split}_images"] = len(image_files)
        counts[f"{split}_positive"] = positive
        counts[f"{split}_negative"] = len(image_files) - positive
        counts[f"{split}_boxes"] = boxes
    if counts["train_images"] == 0:
        raise ValueError("The training split contains no images")
    return counts


def _write_local_dataset_yaml(root: Path) -> Path:
    target = root / "dataset.local.yaml"
    absolute = root.resolve().as_posix().replace('"', '\\"')
    target.write_text(
        f'path: "{absolute}"\ntrain: images/train\nval: images/val\nnames:\n  0: bird\n',
        encoding="utf-8",
    )
    return target


def _venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _nvidia_smi() -> str | None:
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    candidates = [
        shutil.which("nvidia-smi"),
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "nvidia-smi.exe"),
        str(program_files / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"),
    ]
    return next(
        (candidate for candidate in candidates if candidate and Path(candidate).is_file()), None
    )


def _nvidia_devices() -> list[dict[str, str]]:
    executable = _nvidia_smi()
    if not executable:
        return []
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    devices = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) == 3:
            devices.append({"name": parts[0], "driver": parts[1], "memory_mb": parts[2]})
    return devices


def _torch_environment(python: Path) -> dict[str, Any]:
    script = (
        "import json, torch; "
        "print(json.dumps({'version': torch.__version__, "
        "'cuda': bool(torch.cuda.is_available()), 'cuda_version': torch.version.cuda, "
        "'devices': [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]}))"
    )
    result = subprocess.run(
        [str(python), "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return {"available": False, "error": result.stderr.strip()}
    try:
        return {"available": True, **json.loads(result.stdout.strip().splitlines()[-1])}
    except (IndexError, json.JSONDecodeError):
        return {"available": False, "error": result.stdout.strip() or result.stderr.strip()}


def _stream_process(
    command: list[str],
    log: Callable[[str], None],
    event: Callable[[dict[str, Any]], None] | None = None,
    register: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> int:
    log("\n> " + subprocess.list2cmdline(command))
    process = subprocess.Popen(
        command,
        cwd=str(DATASET_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    if register:
        register(process)
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = ANSI_ESCAPE_RE.sub("", raw_line.rstrip("\r\n"))
        event_index = line.find(EVENT_PREFIX)
        if event_index >= 0:
            if event:
                try:
                    event(json.loads(line[event_index + len(EVENT_PREFIX) :]))
                except json.JSONDecodeError:
                    log(line)
        elif line:
            log(line)
    code = process.wait()
    if register:
        register(None)
    return code


def _install_cuda_pytorch(
    python: Path,
    log: Callable[[str], None],
    register: Callable[[subprocess.Popen[str] | None], None],
) -> None:
    code = _stream_process(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--force-reinstall",
            "torch",
            "torchvision",
            "--index-url",
            CUDA_INDEX_URL,
        ],
        log,
        register=register,
    )
    if code != 0:
        raise RuntimeError(
            "CUDA-enabled PyTorch installation failed. The CPU build was not used silently; "
            "see the package log above."
        )


def _ensure_environment(
    log: Callable[[str], None],
    register: Callable[[subprocess.Popen[str] | None], None],
    update: bool,
    requested_device: str,
) -> Path:
    python = _venv_python()
    if not python.is_file():
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Creating reusable training environment in {VENV_DIR} ...")
        import venv

        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    import_probe = subprocess.run(
        [str(python), "-c", "import ultralytics, torch"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    nvidia_devices = _nvidia_devices()
    wants_cuda = requested_device.casefold() != "cpu" and bool(nvidia_devices)
    if nvidia_devices:
        descriptions = ", ".join(
            f"{item['name']} ({item['memory_mb']} MiB, driver {item['driver']})"
            for item in nvidia_devices
        )
        log(f"NVIDIA hardware detected: {descriptions}")

    if import_probe.returncode != 0 and wants_cuda:
        log(
            "Installing the official CUDA 12.6 PyTorch build before the remaining training "
            "dependencies. This is a large one-time download..."
        )
        _install_cuda_pytorch(python, log, register)

    if import_probe.returncode != 0 or update:
        log(
            "Installing training dependencies. The first installation is large and can take "
            "several minutes..."
        )
        command = [str(python), "-m", "pip", "install"]
        if update:
            command.append("--upgrade")
        command.append(DEPENDENCY)
        code = _stream_process(command, log, register=register)
        if code != 0:
            raise RuntimeError(f"Dependency installation failed with exit code {code}")
    else:
        log("Training dependencies are already installed.")

    torch_info = _torch_environment(python)
    if wants_cuda and not torch_info.get("cuda"):
        log(
            f"CPU-only PyTorch {torch_info.get('version', '')} is installed. Replacing it with "
            "the official CUDA 12.6 build for the detected NVIDIA GPU..."
        )
        _install_cuda_pytorch(python, log, register)
        torch_info = _torch_environment(python)

    if wants_cuda and not torch_info.get("cuda"):
        raise RuntimeError(
            "An NVIDIA GPU was detected, but PyTorch still cannot use CUDA after repair. "
            f"PyTorch={torch_info.get('version')}, CUDA build={torch_info.get('cuda_version')}. "
            "Update the NVIDIA driver and retry, or explicitly choose device 'cpu'."
        )
    if requested_device.casefold() not in {"auto", "cpu"} and not torch_info.get("cuda"):
        raise RuntimeError(
            f"Device '{requested_device}' requires CUDA, but no usable NVIDIA CUDA device "
            "was found."
        )
    return python


class TrainerGui:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1000x860")
        self.root.minsize(820, 680)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.cancel_requested = False
        self.last_output: Path | None = None
        self.last_report: dict[str, Any] | None = None
        self.report_image_references: list[list[Any]] = []
        self.training_started_at: float | None = None
        self.gpu_total_gb: float | None = None

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=f"Dataset: {DATASET_DIR}\nThe GPU is used only while training is running.",
        ).pack(anchor="w", pady=(2, 12))

        settings = ttk.LabelFrame(outer, text="Training settings", padding=10)
        settings.pack(fill="x")
        self.values: dict[str, Any] = {
            "model": tk.StringVar(value="yolov8n.pt"),
            "imgsz": tk.StringVar(value="960"),
            "epochs": tk.StringVar(value="100"),
            "patience": tk.StringVar(value="20"),
            "batch": tk.StringVar(value="-1"),
            "workers": tk.StringVar(value="4"),
            "device": tk.StringVar(value="auto"),
            "name": tk.StringVar(value="pigeon-v1"),
            "update": tk.BooleanVar(value=False),
            "pin_memory": tk.BooleanVar(value=False),
            "benchmark": tk.BooleanVar(value=True),
        }
        fields = (
            (
                "Starting model",
                "model",
                "yolov8n.pt matches the server. yolov8s.pt may be more accurate but is slower "
                "there.",
            ),
            (
                "Image size",
                "imgsz",
                "960 is recommended for these small birds; 1280 costs more time and VRAM.",
            ),
            (
                "Epochs",
                "epochs",
                "Maximum passes through the data. Early stopping will usually finish sooner.",
            ),
            (
                "Early-stop patience",
                "patience",
                "Stop after this many epochs without improvement. 20 is a good first run.",
            ),
            (
                "Batch size",
                "batch",
                "Use -1 to choose a batch using about 60% of GPU memory; use 8 for CPU.",
            ),
            (
                "Data workers",
                "workers",
                "4 keeps image preparation parallel. The trainer retries with 0 if Windows "
                "loading fails.",
            ),
            (
                "Device (auto, 0, cpu)",
                "device",
                "auto uses the NVIDIA GPU when present; 0 forces GPU 0; cpu is an explicit "
                "fallback.",
            ),
            (
                "Run name",
                "name",
                "Use versioned names such as pigeon-v1, pigeon-v2, so results remain comparable.",
            ),
        )
        for index, (label, key, help_text) in enumerate(fields):
            row, column = divmod(index, 2)
            group = ttk.Frame(settings)
            group.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 8, 8 if column == 0 else 0),
                pady=4,
            )
            ttk.Label(group, text=label).pack(anchor="w")
            ttk.Entry(group, textvariable=self.values[key]).pack(fill="x")
            ttk.Label(group, text=help_text, foreground="#666666", wraplength=440).pack(
                anchor="w", pady=(1, 0)
            )
        settings.columnconfigure(0, weight=1)
        settings.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            settings,
            text="Update Ultralytics and training dependencies before starting",
            variable=self.values["update"],
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            settings,
            text="Use pinned data-loader memory (advanced; leave off on Windows)",
            variable=self.values["pin_memory"],
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(
            settings,
            text="Compare the trained model with the starting model after training",
            variable=self.values["benchmark"],
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(
            settings,
            text="Restore recommended settings",
            command=self.restore_recommended,
        ).grid(row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Button(
            settings,
            text="What do these settings mean?",
            command=self.show_settings_help,
        ).grid(row=7, column=1, sticky="e", pady=(8, 0))

        self.summary = ttk.Label(outer, text="Checking dataset...")
        self.summary.pack(anchor="w", pady=(10, 4))
        self.status = ttk.Label(outer, text="Ready")
        self.status.pack(anchor="w")
        self.details = ttk.Label(outer, text="", foreground="#555555")
        self.details.pack(anchor="w")
        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 8))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="Start training", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(
            buttons, text="Open results", command=self.open_results, state="disabled"
        )
        self.open_button.pack(side="left")
        self.report_button = ttk.Button(
            buttons,
            text="View training report",
            command=self.show_training_report,
            state="disabled",
        )
        self.report_button.pack(side="left", padx=8)

        self.log_widget = scrolledtext.ScrolledText(
            outer, height=20, wrap="word", font=("Consolas", 9)
        )
        self.log_widget.pack(fill="both", expand=True, pady=(10, 0))
        self.log_widget.configure(state="disabled")
        self._show_dataset_counts()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def restore_recommended(self) -> None:
        recommended = {
            "model": "yolov8n.pt",
            "imgsz": "960",
            "epochs": "100",
            "patience": "20",
            "batch": "-1",
            "workers": "4",
            "device": "auto",
            "name": "pigeon-v1",
        }
        for key, value in recommended.items():
            self.values[key].set(value)
        self.values["pin_memory"].set(False)
        self.values["benchmark"].set(True)

    def show_settings_help(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo(
            "Training settings guide",
            "Recommended first run for this dataset and RTX 4070 SUPER:\n\n"
            "Model: yolov8n.pt keeps server inference fast. Compare yolov8s.pt later only if "
            "the small model still misses too many birds.\n\n"
            "Image size: 960 preserves more detail for distant birds than 640. Try 1280 only "
            "as a later accuracy experiment.\n\n"
            "Epochs/patience: 100/20 is a ceiling plus automatic early stopping, not a promise "
            "to run all 100 epochs.\n\n"
            "Batch: -1 lets Ultralytics target about 60% of VRAM. A fixed smaller number is "
            "useful only when diagnosing memory problems.\n\n"
            "Workers: 4 prepares images in parallel. If Windows data loading fails, the trainer "
            "automatically restarts with the safest 0-worker mode.\n\n"
            "Pinned memory: leave this off on Windows. It can slightly accelerate transfers, "
            "but the current PyTorch/Windows combination can fail in its pin-memory thread.\n\n"
            "Starting-model comparison: leave this enabled to measure the untouched starting "
            "model on the same validation images after training. It usually adds only a few "
            "seconds.\n\n"
            "Device: auto now verifies CUDA and repairs CPU-only PyTorch automatically. It will "
            "not silently use CPU when an NVIDIA GPU is detected.",
            parent=self.root,
        )

    def _show_dataset_counts(self) -> None:
        try:
            counts = _dataset_counts(DATASET_DIR)
            self.summary.configure(
                text=(
                    f"Train: {counts['train_images']} images ({counts['train_positive']} positive, "
                    f"{counts['train_negative']} negative, {counts['train_boxes']} boxes)   |   "
                    f"Validation: {counts['val_images']} images "
                    f"({counts['val_positive']} positive, "
                    f"{counts['val_negative']} negative, {counts['val_boxes']} boxes)"
                )
            )
        except Exception as exc:  # GUI must explain malformed exports instead of crashing.
            self.summary.configure(text=f"Dataset problem: {exc}")
            self.start_button.configure(state="disabled")

    def log(self, message: str) -> None:
        self.events.put(("log", message))

    def register_process(self, process: subprocess.Popen[str] | None) -> None:
        self.process = process

    def start(self) -> None:
        from tkinter import messagebox

        try:
            config = self._read_config()
            _dataset_counts(DATASET_DIR)
        except (TypeError, ValueError) as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        self.cancel_requested = False
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.report_button.configure(state="disabled")
        self.last_report = None
        self.progress["value"] = 0
        self.status.configure(text="Preparing training environment...")
        self.details.configure(text="")
        self.training_started_at = None
        self.gpu_total_gb = None
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")
        threading.Thread(target=self._run, args=(config,), daemon=True).start()

    def _read_config(self) -> dict[str, Any]:
        integer_fields = ("imgsz", "epochs", "patience", "batch", "workers")
        config: dict[str, Any] = {}
        for key in integer_fields:
            try:
                config[key] = int(self.values[key].get())
            except ValueError as exc:
                raise ValueError(f"{key} must be a whole number") from exc
        if config["imgsz"] < 320 or config["epochs"] < 1 or config["batch"] == 0:
            raise ValueError(
                "Image size must be at least 320; epochs and batch size must be non-zero"
            )
        config.update(
            model=self.values["model"].get().strip(),
            device=self.values["device"].get().strip() or "auto",
            name=self.values["name"].get().strip() or "pigeon-v1",
            seed=42,
            update=bool(self.values["update"].get()),
            pin_memory=bool(self.values["pin_memory"].get()),
            benchmark=bool(self.values["benchmark"].get()),
        )
        if not config["model"]:
            raise ValueError("Starting model cannot be empty")
        return config

    def _run(self, config: dict[str, Any]) -> None:
        try:
            python = _ensure_environment(
                self.log,
                self.register_process,
                bool(config.pop("update")),
                str(config["device"]),
            )
            if self.cancel_requested:
                self.events.put(("cancelled", None))
                return
            self.events.put(("status", "Checking GPU and installed packages..."))
            code = _stream_process(
                [str(python), str(Path(__file__).resolve()), "--worker", "--probe"],
                self.log,
                self._worker_event,
                self.register_process,
            )
            if code != 0:
                raise RuntimeError(f"Hardware check failed with exit code {code}")
            local_yaml = _write_local_dataset_yaml(DATASET_DIR)
            output = DATASET_DIR / "training-runs"
            output.mkdir(exist_ok=True)
            config.update(data=str(local_yaml), project=str(output))
            config_path = DATASET_DIR / ".training-config.json"
            code = self._run_training_worker(python, config_path, config)
            failure = getattr(self, "worker_failure", None)
            if (
                code != 0
                and not self.cancel_requested
                and failure
                and failure.get("retryable_safe_loader")
            ):
                safe_config = dict(config)
                safe_config.update(workers=0, pin_memory=False)
                checkpoint = failure.get("last")
                if checkpoint and Path(str(checkpoint)).is_file():
                    safe_config["resume"] = str(checkpoint)
                    recovery = f"resuming from {checkpoint}"
                else:
                    safe_config.pop("resume", None)
                    recovery = "restarting the run (no completed epoch checkpoint existed yet)"
                self.log(
                    "\nWindows pinned-memory loading failed. Starting a new clean worker with "
                    f"pinned memory off and workers=0, {recovery}."
                )
                self.events.put(("status", "Recovering with safe Windows data loading..."))
                code = self._run_training_worker(python, config_path, safe_config)
            if self.cancel_requested:
                self.events.put(("cancelled", None))
            elif code != 0:
                raise RuntimeError(f"Training stopped with exit code {code}. See the log above.")
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _run_training_worker(self, python: Path, config_path: Path, config: dict[str, Any]) -> int:
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self.worker_failure: dict[str, Any] | None = None
        self.events.put(("status", "Training..."))
        return _stream_process(
            [
                str(python),
                str(Path(__file__).resolve()),
                "--worker",
                "--config",
                str(config_path),
            ],
            self.log,
            self._worker_event,
            self.register_process,
        )

    def _worker_event(self, payload: dict[str, Any]) -> None:
        if payload.get("event") == "failure":
            self.worker_failure = payload
        self.events.put(("worker", payload))

    def _poll(self) -> None:
        from tkinter import messagebox

        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log_widget.configure(state="normal")
                    self.log_widget.insert("end", str(value) + "\n")
                    self.log_widget.see("end")
                    self.log_widget.configure(state="disabled")
                elif kind == "status":
                    self.status.configure(text=str(value))
                elif kind == "worker":
                    self._handle_worker_event(value)
                elif kind == "cancelled":
                    self._finish_controls("Training cancelled")
                elif kind == "error":
                    self._finish_controls("Training failed")
                    messagebox.showerror(APP_NAME, str(value), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle_worker_event(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        if event == "probe":
            if payload.get("cuda"):
                names = ", ".join(payload.get("devices") or [])
                memories = payload.get("device_memory_gb") or []
                if memories:
                    self.gpu_total_gb = float(memories[0])
                memory_text = (
                    f" ({', '.join(f'{float(value):g} GiB' for value in memories)})"
                    if memories
                    else ""
                )
                self.log(f"GPU available: {names}{memory_text}")
            else:
                self.log(
                    "WARNING: CUDA is not available; training will use the CPU and be much slower."
                )
            self.log(
                f"Python {payload.get('python')}; PyTorch {payload.get('torch')}; "
                f"CUDA runtime {payload.get('cuda_version') or 'none'}; "
                f"Ultralytics {payload.get('ultralytics')}"
            )
        elif event == "training_start":
            gpu = payload.get("gpu") or "none"
            self.training_started_at = time.monotonic()
            self.status.configure(text=f"Training on device {payload.get('device')} ({gpu})")
            self.log(
                f"Data loader: {payload.get('workers')} workers; pinned memory "
                f"{'on' if payload.get('pin_memory') else 'off'}"
                + ("; resumed checkpoint" if payload.get("resumed") else "")
            )
        elif event == "batch":
            epoch = int(payload.get("epoch", 1))
            epochs = max(1, int(payload.get("epochs", 1)))
            batch = int(payload.get("batch", 0))
            batches = max(1, int(payload.get("batches", 1)))
            percent = ((epoch - 1) + min(1.0, batch / batches)) / epochs * 100
            self.progress["value"] = percent
            self.status.configure(text=f"Epoch {epoch}/{epochs}, batch {batch}/{batches}")
            self._update_timing(percent / 100, payload)
        elif event == "epoch":
            epoch = int(payload.get("epoch", 1))
            epochs = max(1, int(payload.get("epochs", 1)))
            self.progress["value"] = epoch / epochs * 100
            metrics = payload.get("metrics") or {}
            compact = []
            for key, label in (
                ("metrics/precision(B)", "precision"),
                ("metrics/recall(B)", "recall"),
                ("metrics/mAP50(B)", "mAP50"),
                ("metrics/mAP50-95(B)", "mAP50-95"),
            ):
                if key in metrics:
                    compact.append(f"{label}={float(metrics[key]):.3f}")
            suffix = " | " + ", ".join(compact) if compact else ""
            self.status.configure(text=f"Completed epoch {epoch}/{epochs}{suffix}")
            self._update_timing(epoch / epochs, payload)
        elif event == "benchmark_start":
            self.status.configure(
                text=f"Comparing with starting model {payload.get('model', '')}..."
            )
        elif event == "threshold_start":
            self.status.configure(
                text=f"Testing deployment thresholds for {payload.get('model', '')}..."
            )
        elif event == "threshold_progress":
            completed = int(payload.get("completed", 0) or 0)
            total = max(1, int(payload.get("total", 1) or 1))
            self.status.configure(
                text=(
                    f"Threshold test {payload.get('model', '')}: "
                    f"{completed}/{total} validation images"
                )
            )
        elif event == "threshold_unavailable":
            self.log(
                f"Threshold evaluation unavailable for {payload.get('model', '')}: "
                f"{payload.get('message')}"
            )
        elif event == "benchmark_complete":
            baseline = payload.get("baseline") or {}
            self.log(
                "Starting-model benchmark: "
                f"precision={float(baseline.get('precision', 0)):.3f}, "
                f"recall={float(baseline.get('recall', 0)):.3f}, "
                f"mAP50={float(baseline.get('map50', 0)):.3f}, "
                f"mAP50-95={float(baseline.get('map50_95', 0)):.3f}"
            )
        elif event == "benchmark_unavailable":
            self.log(f"Starting-model comparison unavailable: {payload.get('message')}")
        elif event == "complete":
            self.last_output = Path(str(payload["save_dir"]))
            self.last_report = payload
            self.progress["value"] = 100
            self._finish_controls(f"Complete. Best model: {payload['best']}")
            if self.training_started_at is not None:
                elapsed = time.monotonic() - self.training_started_at
                self.details.configure(text=f"Total training time: {_format_duration(elapsed)}")
            self.open_button.configure(state="normal")
            self.report_button.configure(state="normal")
            self.root.after(100, self.show_training_report)
        elif event == "failure":
            if payload.get("retryable_safe_loader"):
                self.status.configure(text="Data loader failed; preparing automatic safe retry...")
            else:
                self.status.configure(text="Training worker failed")

    def _update_timing(self, fraction: float, payload: dict[str, Any]) -> None:
        if self.training_started_at is None:
            return
        elapsed = time.monotonic() - self.training_started_at
        details = [f"Elapsed {_format_duration(elapsed)}"]
        if fraction > 0.002:
            remaining = elapsed * (1 - fraction) / fraction
            details.append(f"estimated remaining {_format_duration(remaining)}")
        allocated = payload.get("gpu_allocated_gb")
        reserved = payload.get("gpu_reserved_gb")
        if allocated is not None:
            memory = f"GPU memory {float(allocated):g} GiB allocated"
            if reserved is not None:
                memory += f", {float(reserved):g} GiB reserved"
            if self.gpu_total_gb:
                memory += f" / {self.gpu_total_gb:g} GiB"
            details.append(memory)
        utilization = payload.get("gpu_utilization_pct")
        temperature = payload.get("gpu_temperature_c")
        if utilization is not None:
            gpu_activity = f"GPU {float(utilization):g}%"
            if temperature is not None:
                gpu_activity += f" at {float(temperature):g}°C"
            details.append(gpu_activity)
        batch_size = int(payload.get("batch_size", 0) or 0)
        if batch_size:
            details.append(f"effective batch {batch_size}")
        self.details.configure(text="  |  ".join(details))

    def _finish_controls(self, status: str) -> None:
        self.process = None
        self.status.configure(text=status)
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")

    def cancel(self) -> None:
        self.cancel_requested = True
        self.status.configure(text="Stopping...")
        process = self.process
        if process is not None and process.poll() is None:
            with suppress(OSError):
                process.terminate()

    def show_training_report(self) -> None:
        report = self.last_report
        if not report:
            return

        window = self.tk.Toplevel(self.root)
        window.title("Training report")
        window.geometry("1120x840")
        window.minsize(820, 640)
        outer = self.ttk.Frame(window, padding=14)
        outer.pack(fill="both", expand=True)

        self.ttk.Label(outer, text="Training complete", font=("Segoe UI", 17, "bold")).pack(
            anchor="w"
        )
        epochs_completed = int(report.get("epochs_completed", 0) or 0)
        requested_epochs = int(report.get("requested_epochs", 0) or 0)
        best_epoch = report.get("best_epoch")
        run_text = f"Completed {epochs_completed} of {requested_epochs} requested epochs"
        if best_epoch:
            run_text += f"; the strongest validation result was epoch {best_epoch}"
        self.ttk.Label(outer, text=run_text).pack(anchor="w", pady=(2, 10))

        recommendation = self.ttk.LabelFrame(outer, text="Recommendation", padding=10)
        recommendation.pack(fill="x")
        best_path = str(report.get("best", ""))
        last_path = str(report.get("last", ""))
        model_recommendation = dict(report.get("recommendation") or {})
        recommended_path = str(model_recommendation.get("path") or best_path)
        self.ttk.Label(
            recommendation,
            text=str(model_recommendation.get("label") or "Use best.pt"),
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w")
        self.ttk.Label(
            recommendation,
            text=(
                str(
                    model_recommendation.get("reason")
                    or "This checkpoint had the best held-out validation score."
                )
                + " last.pt is useful for resuming an interrupted run, but it is not the "
                "deployment recommendation."
            ),
            wraplength=1030,
        ).pack(anchor="w", pady=(2, 6))
        path_row = self.ttk.Frame(recommendation)
        path_row.pack(fill="x")
        path_value = self.tk.StringVar(value=recommended_path)
        self.ttk.Entry(path_row, textvariable=path_value, state="readonly").pack(
            side="left", fill="x", expand=True
        )

        def copy_best_path() -> None:
            window.clipboard_clear()
            window.clipboard_append(recommended_path)

        self.ttk.Button(path_row, text="Copy path", command=copy_best_path).pack(
            side="left", padx=(8, 0)
        )

        metrics = dict(report.get("metrics") or {})
        baseline = dict(report.get("baseline") or {})
        metric_frame = self.ttk.Frame(outer)
        metric_frame.pack(fill="x", pady=(10, 6))
        metric_specs = (
            ("Precision", "precision", "How often a predicted bird was correct"),
            ("Recall", "recall", "How many labeled birds the model found"),
            ("mAP50", "map50", "Detection quality at a forgiving box overlap"),
            ("mAP50-95", "map50_95", "Detection and box quality across strict overlaps"),
        )
        for column, (label, key, explanation) in enumerate(metric_specs):
            card = self.ttk.LabelFrame(metric_frame, text=label, padding=9)
            card.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 4, 0 if column == 3 else 4),
            )
            value = _as_float(metrics.get(key))
            self.ttk.Label(
                card,
                text=f"{value * 100:.1f}%" if value is not None else "—",
                font=("Segoe UI", 15, "bold"),
            ).pack(anchor="w")
            baseline_value = _as_float(baseline.get(key))
            if value is not None and baseline_value is not None:
                change = (value - baseline_value) * 100
                comparison = f"Starting model {baseline_value * 100:.1f}%  |  {change:+.1f} points"
            else:
                comparison = explanation
            self.ttk.Label(card, text=comparison, wraplength=235).pack(anchor="w", pady=(3, 0))
            metric_frame.columnconfigure(column, weight=1)

        if baseline:
            self.ttk.Label(
                outer,
                text=(
                    f"Comparison uses {baseline.get('model', 'the starting model')} on the same "
                    "held-out validation images, restricted to its bird class."
                ),
                foreground="#555555",
            ).pack(anchor="w", pady=(0, 6))
        elif report.get("baseline_error"):
            self.ttk.Label(
                outer,
                text=f"Starting-model comparison was unavailable: {report['baseline_error']}",
                foreground="#8a5a00",
                wraplength=1030,
            ).pack(anchor="w", pady=(0, 6))

        trained_sweep = dict(report.get("trained_sweep") or {})
        recommended_thresholds = dict(trained_sweep.get("recommended") or {})
        if recommended_thresholds:
            threshold_frame = self.ttk.LabelFrame(
                outer, text="Recommended deployment thresholds", padding=9
            )
            threshold_frame.pack(fill="x", pady=(4, 8))
            threshold_text = "   |   ".join(
                (
                    f"Operational {float(recommended_thresholds.get('operational', 0)):.2f}",
                    f"Evidence capture {float(recommended_thresholds.get('capture', 0)):.2f}",
                    f"Motion rescan {float(recommended_thresholds.get('rescan', 0)):.2f}",
                )
            )
            self.ttk.Label(
                threshold_frame, text=threshold_text, font=("Segoe UI", 11, "bold")
            ).pack(anchor="w")
            operating = _operating_point(trained_sweep)
            if operating:
                self.ttk.Label(
                    threshold_frame,
                    text=(
                        f"At the recommended operational threshold: precision "
                        f"{float(operating['precision']) * 100:.1f}%, recall "
                        f"{float(operating['recall']) * 100:.1f}%, and "
                        f"{float(operating['false_positive_image_rate']) * 100:.1f}% of held-out "
                        "negative images produced a false box."
                    ),
                    wraplength=1030,
                ).pack(anchor="w", pady=(3, 0))
            package = str(report.get("deployment_package") or "")
            if package:
                self.ttk.Label(
                    threshold_frame,
                    text=f"Upload package: {package}",
                    wraplength=1030,
                ).pack(anchor="w", pady=(3, 0))

        notebook = self.ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(4, 0))
        threshold_rows = list(trained_sweep.get("thresholds") or [])
        if threshold_rows:
            table_tab = self.ttk.Frame(notebook, padding=8)
            notebook.add(table_tab, text="Threshold table")
            columns = ("threshold", "precision", "recall", "f1", "fp_images", "fp_boxes")
            table = self.ttk.Treeview(table_tab, columns=columns, show="headings", height=10)
            headings = {
                "threshold": "Confidence",
                "precision": "Precision",
                "recall": "Recall",
                "f1": "F1",
                "fp_images": "Negative images with FP",
                "fp_boxes": "FP boxes / negative image",
            }
            for name in columns:
                table.heading(name, text=headings[name])
                table.column(name, anchor="center", width=155)
            for row in threshold_rows:
                table.insert(
                    "",
                    "end",
                    values=(
                        f"{float(row['threshold']):.2f}",
                        f"{float(row['precision']) * 100:.1f}%",
                        f"{float(row['recall']) * 100:.1f}%",
                        f"{float(row['f1']) * 100:.1f}%",
                        (
                            f"{int(row['false_positive_images'])} "
                            f"({float(row['false_positive_image_rate']) * 100:.1f}%)"
                        ),
                        f"{float(row['boxes_per_negative_image']):.3f}",
                    ),
                )
            table.pack(fill="both", expand=True)
            self.ttk.Label(
                table_tab,
                text=(
                    f"Validation: {int(trained_sweep.get('positive_images', 0))} positive and "
                    f"{int(trained_sweep.get('negative_images', 0))} negative images. "
                    "False-positive image rate is a deployment guardrail, not part of mAP."
                ),
                wraplength=1030,
            ).pack(anchor="w", pady=(6, 0))

        day_rows = list(dict(trained_sweep.get("breakdowns") or {}).get("day") or [])
        if day_rows:
            day_tab = self.ttk.Frame(notebook, padding=8)
            notebook.add(day_tab, text="Results by day")
            day_columns = ("day", "images", "precision", "recall", "fp_images")
            day_table = self.ttk.Treeview(day_tab, columns=day_columns, show="headings", height=10)
            for name, label in (
                ("day", "Day"),
                ("images", "Images"),
                ("precision", "Precision"),
                ("recall", "Recall"),
                ("fp_images", "Negative images with FP"),
            ):
                day_table.heading(name, text=label)
                day_table.column(name, anchor="center", width=180)
            for row in day_rows:
                day_table.insert(
                    "",
                    "end",
                    values=(
                        row["day"],
                        int(row["images"]),
                        f"{float(row['precision']) * 100:.1f}%",
                        f"{float(row['recall']) * 100:.1f}%",
                        int(row["false_positive_images"]),
                    ),
                )
            day_table.pack(fill="both", expand=True)
        chart_titles = {
            "results.png": "Training curves",
            "BoxPR_curve.png": "Precision-recall",
            "BoxF1_curve.png": "F1 vs confidence",
            "BoxP_curve.png": "Precision vs confidence",
            "BoxR_curve.png": "Recall vs confidence",
            "confusion_matrix_normalized.png": "Normalized confusion matrix",
            "confusion_matrix.png": "Confusion matrix",
            "threshold_tradeoff.png": "Deployment thresholds",
        }
        image_references: list[Any] = []
        for raw_path in report.get("charts") or []:
            chart_path = Path(str(raw_path))
            if not chart_path.is_file():
                continue
            tab = self.ttk.Frame(notebook, padding=8)
            notebook.add(tab, text=chart_titles.get(chart_path.name, chart_path.stem))
            try:
                image = self.tk.PhotoImage(file=str(chart_path))
                divisor = max(
                    1,
                    (image.width() + 1039) // 1040,
                    (image.height() + 519) // 520,
                )
                if divisor > 1:
                    image = image.subsample(divisor, divisor)
                image_references.append(image)
                self.ttk.Label(tab, image=image).pack(fill="both", expand=True)

                def open_chart(path: Path = chart_path) -> None:
                    os.startfile(path)  # type: ignore[attr-defined]

                self.ttk.Button(
                    tab,
                    text="Open full-size graph",
                    command=open_chart,
                ).pack(pady=(6, 0))
            except self.tk.TclError as exc:
                self.ttk.Label(tab, text=f"Could not display {chart_path.name}: {exc}").pack()
        if not image_references:
            tab = self.ttk.Frame(notebook, padding=12)
            notebook.add(tab, text="Graphs")
            self.ttk.Label(tab, text="No generated graph files were found for this run.").pack()
        self.report_image_references.append(image_references)

        footer = self.ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        self.ttk.Button(footer, text="Open result folder", command=self.open_results).pack(
            side="left"
        )
        if last_path:
            self.ttk.Label(
                footer,
                text="Keep last.pt only if you may resume training later.",
                foreground="#555555",
            ).pack(side="left", padx=10)
        self.ttk.Button(footer, text="Close", command=window.destroy).pack(side="right")

    def open_results(self) -> None:
        if self.last_output and self.last_output.exists():
            os.startfile(self.last_output)  # type: ignore[attr-defined]

    def _close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            from tkinter import messagebox

            if not messagebox.askyesno(
                APP_NAME, "Training is still running. Stop it and close?", parent=self.root
            ):
                return
            self.cancel()
            time.sleep(0.2)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if "--worker" in sys.argv:
        index = sys.argv.index("--worker")
        return _worker_main(sys.argv[index + 1 :])
    if not _runtime_is_complete():
        return _relaunch_with_standard_python()
    TrainerGui().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
