from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def trainer_module() -> ModuleType:
    source = Path(__file__).parents[1] / "app" / "services" / "training_assets" / "train_model.py"
    spec = importlib.util.spec_from_file_location("pigeon_training_bundle", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_duration_formatting(trainer_module: ModuleType) -> None:
    assert trainer_module._format_duration(8) == "8s"
    assert trainer_module._format_duration(125) == "2m 05s"
    assert trainer_module._format_duration(3725) == "1h 02m"


def test_windows_pin_memory_failures_are_retryable(trainer_module: ModuleType) -> None:
    assert trainer_module._is_pin_memory_failure(
        "Caught AcceleratorError in pin memory thread for device 0"
    )
    assert trainer_module._is_pin_memory_failure("CUDA error: resource already mapped")
    assert not trainer_module._is_pin_memory_failure("CUDA out of memory")


def test_training_history_selects_best_validation_epoch(
    trainer_module: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "results.csv").write_text(
        "epoch,metrics/precision(B),metrics/recall(B),metrics/mAP50(B),"
        "metrics/mAP50-95(B)\n"
        "1,0.7,0.4,0.5,0.3\n"
        "2,0.8,0.6,0.7,0.5\n"
        "3,0.9,0.5,0.75,0.45\n",
        encoding="utf-8",
    )
    (tmp_path / "results.png").touch()

    summary = trainer_module._training_history_summary(tmp_path)

    assert summary["epochs_completed"] == 3
    assert summary["best_epoch"] == 2
    assert summary["charts"] == [str(tmp_path / "results.png")]


def test_metric_names_are_normalized(trainer_module: ModuleType) -> None:
    assert trainer_module._numeric_metrics(
        {
            "metrics/precision(B)": "0.91",
            "metrics/recall(B)": 0.8,
            "metrics/mAP50(B)": 0.84,
            "metrics/mAP50-95(B)": 0.57,
        }
    ) == {
        "precision": 0.91,
        "recall": 0.8,
        "map50": 0.84,
        "map50_95": 0.57,
    }


def test_recommendation_uses_model_with_stronger_validation(
    trainer_module: ModuleType, tmp_path: Path
) -> None:
    best = tmp_path / "best.pt"
    starting = tmp_path / "yolov8n.pt"
    trained = {"map50": 0.84, "map50_95": 0.57}
    baseline = {"map50": 0.48, "map50_95": 0.37}

    recommendation = trainer_module._model_recommendation(trained, baseline, best, starting)
    assert recommendation["label"] == "Use best.pt"
    assert recommendation["path"] == str(best)

    recommendation = trainer_module._model_recommendation(baseline, trained, best, starting)
    assert recommendation["label"] == "Keep the starting model (yolov8n.pt)"
    assert recommendation["path"] == str(starting)


def test_threshold_sweep_exposes_negative_image_false_positives(
    trainer_module: ModuleType,
) -> None:
    samples = [
        {
            "targets": [[10, 10, 30, 30]],
            "predictions": [
                {"bbox": [10, 10, 30, 30], "confidence": 0.8},
                {"bbox": [50, 50, 60, 60], "confidence": 0.2},
            ],
        },
        {
            "targets": [],
            "predictions": [{"bbox": [5, 5, 15, 15], "confidence": 0.1}],
        },
    ]

    sweep = trainer_module._threshold_statistics(samples)
    low = sweep["thresholds"][0]
    operational = next(row for row in sweep["thresholds"] if row["threshold"] == 0.35)

    assert low["false_positive_images"] == 1
    assert low["fp"] == 2
    assert operational["precision"] == 1.0
    assert operational["recall"] == 1.0
    assert sweep["recommended"] == {
        "operational": 0.25,
        "capture": 0.25,
        "rescan": 0.35,
    }


def test_recommendation_rejects_noisy_trained_operating_point(
    trainer_module: ModuleType, tmp_path: Path
) -> None:
    trained_sweep = {
        "recommended": {"operational": 0.35},
        "thresholds": [
            {
                "threshold": 0.35,
                "precision": 0.7,
                "recall": 0.9,
                "f1": 0.79,
                "false_positive_image_rate": 0.3,
            }
        ],
    }
    baseline_sweep = {
        "recommended": {"operational": 0.35},
        "thresholds": [
            {
                "threshold": 0.35,
                "precision": 0.9,
                "recall": 0.6,
                "f1": 0.72,
                "false_positive_image_rate": 0.03,
            }
        ],
    }

    recommendation = trainer_module._model_recommendation(
        {"map50": 0.9, "map50_95": 0.7},
        {"map50": 0.5, "map50_95": 0.4},
        tmp_path / "best.pt",
        tmp_path / "yolov8n.pt",
        trained_sweep,
        baseline_sweep,
    )

    assert recommendation["label"] == "Keep the starting model (yolov8n.pt)"


def test_nvidia_smi_output_is_parsed(
    trainer_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trainer_module, "_nvidia_smi", lambda: "nvidia-smi.exe")
    monkeypatch.setattr(
        trainer_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="NVIDIA GeForce RTX 4070 SUPER, 591.86, 12282\n",
        ),
    )
    assert trainer_module._nvidia_devices() == [
        {
            "name": "NVIDIA GeForce RTX 4070 SUPER",
            "driver": "591.86",
            "memory_mb": "12282",
        }
    ]


def test_cpu_torch_is_repaired_when_nvidia_is_present(
    trainer_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "python.exe"
    python.touch()
    monkeypatch.setattr(trainer_module, "_venv_python", lambda: python)
    monkeypatch.setattr(
        trainer_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        trainer_module,
        "_nvidia_devices",
        lambda: [{"name": "RTX", "driver": "591.86", "memory_mb": "12282"}],
    )
    probes = iter(
        [
            {"available": True, "version": "2.13.0+cpu", "cuda": False},
            {
                "available": True,
                "version": "2.13.0+cu126",
                "cuda": True,
                "cuda_version": "12.6",
            },
        ]
    )
    monkeypatch.setattr(trainer_module, "_torch_environment", lambda unused: next(probes))
    repaired: list[Path] = []
    monkeypatch.setattr(
        trainer_module,
        "_install_cuda_pytorch",
        lambda selected, log, register: repaired.append(selected),
    )

    selected = trainer_module._ensure_environment(
        lambda message: None, lambda process: None, False, "auto"
    )

    assert selected == python
    assert repaired == [python]


def test_auto_device_refuses_silent_cpu_fallback(
    trainer_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "python.exe"
    python.touch()
    monkeypatch.setattr(trainer_module, "_venv_python", lambda: python)
    monkeypatch.setattr(
        trainer_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        trainer_module,
        "_nvidia_devices",
        lambda: [{"name": "RTX", "driver": "591.86", "memory_mb": "12282"}],
    )
    monkeypatch.setattr(
        trainer_module,
        "_torch_environment",
        lambda unused: {"available": True, "version": "2.13.0+cpu", "cuda": False},
    )
    monkeypatch.setattr(trainer_module, "_install_cuda_pytorch", lambda *args: None)

    with pytest.raises(RuntimeError, match="still cannot use CUDA"):
        trainer_module._ensure_environment(
            lambda message: None,
            lambda process: None,
            False,
            "auto",
        )
