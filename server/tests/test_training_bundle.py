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
