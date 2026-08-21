#!/usr/bin/env python3
"""Self-contained desktop trainer shipped with pigeon-tracker YOLO exports.

The outer process uses only the Python standard library. It creates a reusable
virtual environment, installs Ultralytics when necessary, and starts a worker
process for training. Keeping the ML imports in the worker lets the GUI show
dependency installation and training output without freezing.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

APP_NAME = "Pigeon Tracker Model Trainer"
DEPENDENCY = "ultralytics>=8.1,<9.0"
EVENT_PREFIX = "__PIGEON_TRAINER__"
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
    if cuda:
        for index in range(torch.cuda.device_count()):
            devices.append(torch.cuda.get_device_name(index))
    _emit_worker(
        "probe",
        python=sys.version.split()[0],
        ultralytics=ultralytics.__version__,
        torch=torch.__version__,
        cuda=cuda,
        devices=devices,
    )
    return 0


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _worker_train(config_path: Path) -> int:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    import torch
    from ultralytics import YOLO

    requested_device = str(config["device"])
    device: str | int = requested_device
    if requested_device == "auto":
        device = 0 if torch.cuda.is_available() else "cpu"
    _emit_worker(
        "training_start",
        device=str(device),
        gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    )

    model = YOLO(config["model"])
    last_batch_report: dict[int, int] = {}
    batch_counts: dict[int, int] = {}

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
    results = model.train(
        data=config["data"],
        imgsz=int(config["imgsz"]),
        epochs=int(config["epochs"]),
        patience=int(config["patience"]),
        batch=int(config["batch"]),
        device=device,
        workers=int(config["workers"]),
        seed=int(config["seed"]),
        deterministic=True,
        project=config["project"],
        name=config["name"],
        plots=True,
        verbose=True,
    )
    trainer = model.trainer
    save_dir = Path(str(getattr(trainer, "save_dir", config["project"]))).resolve()
    best = Path(str(getattr(trainer, "best", save_dir / "weights" / "best.pt"))).resolve()
    _emit_worker(
        "complete",
        save_dir=str(save_dir),
        best=str(best),
        result=str(results),
    )
    return 0


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
    )
    if register:
        register(process)
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\r\n")
        if line.startswith(EVENT_PREFIX):
            if event:
                try:
                    event(json.loads(line[len(EVENT_PREFIX) :]))
                except json.JSONDecodeError:
                    log(line)
        elif line:
            log(line)
    code = process.wait()
    if register:
        register(None)
    return code


def _ensure_environment(
    log: Callable[[str], None],
    register: Callable[[subprocess.Popen[str] | None], None],
    update: bool,
) -> Path:
    python = _venv_python()
    if not python.is_file():
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Creating reusable training environment in {VENV_DIR} ...")
        import venv

        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    probe = subprocess.run(
        [str(python), "-c", "import ultralytics, torch"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0 or update:
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
    return python


class TrainerGui:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("940x720")
        self.root.minsize(760, 580)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.cancel_requested = False
        self.last_output: Path | None = None

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
            "batch": tk.StringVar(value="8"),
            "workers": tk.StringVar(value="2"),
            "device": tk.StringVar(value="auto"),
            "name": tk.StringVar(value="pigeon-v1"),
            "update": tk.BooleanVar(value=False),
        }
        fields = (
            ("Starting model", "model"),
            ("Image size", "imgsz"),
            ("Epochs", "epochs"),
            ("Early-stop patience", "patience"),
            ("Batch size", "batch"),
            ("Data workers", "workers"),
            ("Device (auto, 0, cpu)", "device"),
            ("Run name", "name"),
        )
        for index, (label, key) in enumerate(fields):
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
        settings.columnconfigure(0, weight=1)
        settings.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            settings,
            text="Update Ultralytics and training dependencies before starting",
            variable=self.values["update"],
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.summary = ttk.Label(outer, text="Checking dataset...")
        self.summary.pack(anchor="w", pady=(10, 4))
        self.status = ttk.Label(outer, text="Ready")
        self.status.pack(anchor="w")
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

        self.log_widget = scrolledtext.ScrolledText(
            outer, height=20, wrap="word", font=("Consolas", 9)
        )
        self.log_widget.pack(fill="both", expand=True, pady=(10, 0))
        self.log_widget.configure(state="disabled")
        self._show_dataset_counts()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

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
        self.progress["value"] = 0
        self.status.configure(text="Preparing training environment...")
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
        )
        if not config["model"]:
            raise ValueError("Starting model cannot be empty")
        return config

    def _run(self, config: dict[str, Any]) -> None:
        try:
            python = _ensure_environment(
                self.log, self.register_process, bool(config.pop("update"))
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
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            self.events.put(("status", "Training..."))
            code = _stream_process(
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
            if self.cancel_requested:
                self.events.put(("cancelled", None))
            elif code != 0:
                raise RuntimeError(f"Training stopped with exit code {code}. See the log above.")
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _worker_event(self, payload: dict[str, Any]) -> None:
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
                self.log(f"GPU available: {names}")
            else:
                self.log(
                    "WARNING: CUDA is not available; training will use the CPU and be much slower."
                )
            self.log(
                f"Python {payload.get('python')}; PyTorch {payload.get('torch')}; "
                f"Ultralytics {payload.get('ultralytics')}"
            )
        elif event == "training_start":
            gpu = payload.get("gpu") or "none"
            self.status.configure(text=f"Training on device {payload.get('device')} ({gpu})")
        elif event == "batch":
            epoch = int(payload.get("epoch", 1))
            epochs = max(1, int(payload.get("epochs", 1)))
            batch = int(payload.get("batch", 0))
            batches = max(1, int(payload.get("batches", 1)))
            percent = ((epoch - 1) + min(1.0, batch / batches)) / epochs * 100
            self.progress["value"] = percent
            self.status.configure(text=f"Epoch {epoch}/{epochs}, batch {batch}/{batches}")
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
        elif event == "complete":
            self.last_output = Path(str(payload["save_dir"]))
            self.progress["value"] = 100
            self._finish_controls(f"Complete. Best model: {payload['best']}")
            self.open_button.configure(state="normal")

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
