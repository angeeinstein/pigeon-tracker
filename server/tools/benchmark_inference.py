#!/usr/bin/env python3
"""Compare detector CPU and CUDA operation through the turret-control API.

Run this inside the server/LXC for container CPU and memory measurements. GPU
power comes from ``nvidia-smi``. CPU package power is reported only when real
Intel RAPL energy counters are readable; it is never estimated from CPU load.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import math
import os
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class BenchmarkError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str, timeout_s: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with self.opener.open(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.reason
            try:
                parsed = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = parsed.get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise BenchmarkError(f"API {method} {path} failed ({exc.code}): {detail}") from exc
        except (OSError, URLError) as exc:
            raise BenchmarkError(f"cannot reach {self.base_url}: {exc}") from exc
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise BenchmarkError(f"API {path} returned an unexpected response")
        return parsed

    def authenticate(self, username: str | None, password: str | None) -> None:
        state = self.request("GET", "/api/auth/me")
        if not state.get("auth_enabled") or state.get("authenticated"):
            return
        username = username or input("Web UI username [admin]: ").strip() or "admin"
        password = password or getpass.getpass("Web UI password: ")
        self.request("POST", "/api/auth/login", {"username": username, "password": password})


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _allocated_cores(cgroup: Path) -> float:
    try:
        quota, period = (cgroup / "cpu.max").read_text(encoding="utf-8").split()[:2]
        if quota != "max":
            return max(0.01, int(quota) / int(period))
    except (OSError, ValueError, IndexError):
        pass
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        return float(len(affinity(0)))
    return float(os.cpu_count() or 1)


class ContainerResources:
    def __init__(self, cgroup: Path = Path("/sys/fs/cgroup")) -> None:
        self.cgroup = cgroup
        self.cores = _allocated_cores(cgroup)
        self.previous_usage: int | None = None
        self.previous_time: float | None = None

    def _cpu_usage_usec(self) -> int | None:
        try:
            values = dict(
                line.split()[:2]
                for line in (self.cgroup / "cpu.stat").read_text(encoding="utf-8").splitlines()
                if len(line.split()) >= 2
            )
            return int(values["usage_usec"])
        except (OSError, KeyError, ValueError):
            return None

    def sample(self) -> dict[str, float | None]:
        now = time.monotonic()
        usage = self._cpu_usage_usec()
        cpu_cores: float | None = None
        cpu_percent: float | None = None
        if usage is not None and self.previous_usage is not None and self.previous_time is not None:
            elapsed = max(0.001, now - self.previous_time)
            cpu_cores = max(0.0, (usage - self.previous_usage) / 1_000_000 / elapsed)
            cpu_percent = cpu_cores / self.cores * 100.0
        self.previous_usage = usage
        self.previous_time = now

        memory_bytes = _read_int(self.cgroup / "memory.current")
        memory_limit: int | None = None
        try:
            raw_limit = (self.cgroup / "memory.max").read_text(encoding="utf-8").strip()
            if raw_limit != "max":
                memory_limit = int(raw_limit)
        except (OSError, ValueError):
            pass
        memory_percent = (
            memory_bytes / memory_limit * 100.0
            if memory_bytes is not None and memory_limit and memory_limit > 0
            else None
        )
        return {
            "cpu_cores": cpu_cores,
            "cpu_percent": cpu_percent,
            "memory_mb": memory_bytes / 1024**2 if memory_bytes is not None else None,
            "memory_percent": memory_percent,
        }


@dataclass
class RaplDomain:
    energy_file: Path
    max_energy_uj: int | None
    previous_energy_uj: int | None = None


class CpuPower:
    def __init__(self, root: Path = Path("/sys/class/powercap")) -> None:
        self.domains: list[RaplDomain] = []
        self.previous_time: float | None = None
        for directory in sorted(root.glob("intel-rapl:[0-9]*")):
            try:
                name = (directory / "name").read_text(encoding="utf-8").strip().lower()
            except OSError:
                continue
            if not name.startswith("package"):
                continue
            energy_file = directory / "energy_uj"
            if _read_int(energy_file) is None:
                continue
            self.domains.append(
                RaplDomain(energy_file, _read_int(directory / "max_energy_range_uj"))
            )

    @property
    def available(self) -> bool:
        return bool(self.domains)

    def sample(self) -> float | None:
        now = time.monotonic()
        total_delta = 0
        complete = True
        for domain in self.domains:
            current = _read_int(domain.energy_file)
            if current is None:
                complete = False
                continue
            if domain.previous_energy_uj is not None:
                delta = current - domain.previous_energy_uj
                if delta < 0 and domain.max_energy_uj:
                    delta += domain.max_energy_uj
                total_delta += max(0, delta)
            else:
                complete = False
            domain.previous_energy_uj = current
        power = None
        if complete and self.previous_time is not None and self.domains:
            power = total_delta / 1_000_000 / max(0.001, now - self.previous_time)
        self.previous_time = now
        return power


def gpu_sample() -> dict[str, float | str | None]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,power.draw,utilization.gpu,memory.used,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
        values = [value.strip() for value in result.stdout.splitlines()[0].split(",")]
        return {
            "gpu_name": values[0],
            "gpu_power_w": float(values[1]),
            "gpu_util_percent": float(values[2]),
            "gpu_memory_mb": float(values[3]),
            "gpu_temperature_c": float(values[4]),
        }
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return {
            "gpu_name": None,
            "gpu_power_w": None,
            "gpu_util_percent": None,
            "gpu_memory_mb": None,
            "gpu_temperature_c": None,
        }


def _vision(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    vision = payload.get("vision")
    if not isinstance(vision, dict):
        raise BenchmarkError("health response has no vision status")
    detector = vision.get("detector")
    if not isinstance(detector, dict):
        raise BenchmarkError("health response has no detector status")
    return vision, detector


def wait_for_detector(client: ApiClient, requested: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    expected = "cpu" if requested == "cpu" else "cuda"
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        health = client.request("GET", "/api/health")
        _, detector = _vision(health)
        last = detector
        if detector.get("error") or detector.get("reload_error"):
            raise BenchmarkError(
                f"detector reload failed: {detector.get('error') or detector.get('reload_error')}"
            )
        if (
            detector.get("loaded")
            and not detector.get("reload_pending")
            and detector.get("device") == expected
        ):
            return health
        time.sleep(1)
    raise BenchmarkError(
        f"detector did not become ready on {expected} within {timeout_s:.0f}s; last={last}"
    )


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def collect_sample(
    client: ApiClient,
    resources: ContainerResources,
    cpu_power: CpuPower,
    mode: str,
    elapsed_s: float,
) -> dict[str, Any]:
    health = client.request("GET", "/api/health")
    vision, detector = _vision(health)
    sample: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "elapsed_s": round(elapsed_s, 2),
        "health": health.get("status"),
        "camera_ok": health.get("checks", {}).get("camera"),
        "ai_ok": health.get("checks", {}).get("ai"),
        "actual_device": detector.get("device"),
        "detector_loaded": detector.get("loaded"),
        "inference_ms": _number(detector.get("last_inference_ms")),
        "inferences": detector.get("inferences"),
        "effective_fps": _number(vision.get("effective_fps")),
        "target_fps": _number(vision.get("target_fps")),
        "error": detector.get("error") or vision.get("error"),
        **resources.sample(),
        "cpu_power_w": cpu_power.sample(),
        **gpu_sample(),
    }
    return sample


def _values(samples: list[dict[str, Any]], key: str) -> list[float]:
    return [float(sample[key]) for sample in samples if isinstance(sample.get(key), int | float)]


def _mean(samples: list[dict[str, Any]], key: str) -> float | None:
    values = _values(samples, key)
    return statistics.fmean(values) if values else None


def _p95(samples: list[dict[str, Any]], key: str) -> float | None:
    values = sorted(_values(samples, key))
    if not values:
        return None
    return values[min(len(values) - 1, math.ceil(len(values) * 0.95) - 1)]


def summarize(mode: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
    inferences = [int(value) for value in _values(samples, "inferences")]
    return {
        "mode": mode,
        "actual_device": samples[-1].get("actual_device") if samples else None,
        "samples": len(samples),
        "inferences_observed": max(0, inferences[-1] - inferences[0]) if inferences else 0,
        "inference_ms_mean": _mean(samples, "inference_ms"),
        "inference_ms_p95": _p95(samples, "inference_ms"),
        "effective_fps_mean": _mean(samples, "effective_fps"),
        "cpu_percent_mean": _mean(samples, "cpu_percent"),
        "cpu_cores_mean": _mean(samples, "cpu_cores"),
        "cpu_power_w_mean": _mean(samples, "cpu_power_w"),
        "memory_mb_mean": _mean(samples, "memory_mb"),
        "memory_mb_max": max(_values(samples, "memory_mb"), default=None),
        "gpu_power_w_mean": _mean(samples, "gpu_power_w"),
        "gpu_util_percent_mean": _mean(samples, "gpu_util_percent"),
        "gpu_memory_mb_mean": _mean(samples, "gpu_memory_mb"),
        "gpu_temperature_c_max": max(_values(samples, "gpu_temperature_c"), default=None),
        "errors": sorted({str(sample["error"]) for sample in samples if sample.get("error")}),
    }


def compare(summaries: list[dict[str, Any]]) -> dict[str, float | None]:
    gpu = next((item for item in summaries if item.get("actual_device") == "cuda"), None)
    cpu = next((item for item in summaries if item.get("actual_device") == "cpu"), None)
    if gpu is None or cpu is None:
        return {}

    def ratio(numerator: object, denominator: object) -> float | None:
        if not isinstance(numerator, int | float) or not isinstance(denominator, int | float):
            return None
        return float(numerator) / float(denominator) if denominator else None

    def difference(left: object, right: object) -> float | None:
        if not isinstance(left, int | float) or not isinstance(right, int | float):
            return None
        return float(left) - float(right)

    gpu_total = None
    cpu_total = None
    if isinstance(gpu.get("cpu_power_w_mean"), int | float) and isinstance(
        gpu.get("gpu_power_w_mean"), int | float
    ):
        gpu_total = float(gpu["cpu_power_w_mean"]) + float(gpu["gpu_power_w_mean"])
    if isinstance(cpu.get("cpu_power_w_mean"), int | float) and isinstance(
        cpu.get("gpu_power_w_mean"), int | float
    ):
        cpu_total = float(cpu["cpu_power_w_mean"]) + float(cpu["gpu_power_w_mean"])
    return {
        "gpu_inference_speedup_x": ratio(
            cpu.get("inference_ms_mean"), gpu.get("inference_ms_mean")
        ),
        "gpu_cpu_percent_reduction_points": difference(
            cpu.get("cpu_percent_mean"), gpu.get("cpu_percent_mean")
        ),
        "gpu_incremental_board_power_w": difference(
            gpu.get("gpu_power_w_mean"), cpu.get("gpu_power_w_mean")
        ),
        "measured_cpu_plus_gpu_power_change_w": difference(gpu_total, cpu_total),
        "gpu_memory_change_mb": difference(gpu.get("memory_mb_mean"), cpu.get("memory_mb_mean")),
    }


def _display(value: object, suffix: str = "", digits: int = 1) -> str:
    if not isinstance(value, int | float):
        return "n/a"
    return f"{float(value):.{digits}f}{suffix}"


def print_sample(sample: dict[str, Any]) -> None:
    print(
        f"{sample['mode']:>4} {sample['elapsed_s']:>6.1f}s  "
        f"device={sample.get('actual_device') or '?':<4}  "
        f"infer={_display(sample.get('inference_ms'), 'ms'):>8}  "
        f"fps={_display(sample.get('effective_fps')):>5}  "
        f"CPU={_display(sample.get('cpu_percent'), '%'):>7}  "
        f"CPU power={_display(sample.get('cpu_power_w'), 'W'):>7}  "
        f"RAM={_display(sample.get('memory_mb'), 'MiB', 0):>8}  "
        f"GPU={_display(sample.get('gpu_power_w'), 'W'):>7}/"
        f"{_display(sample.get('gpu_util_percent'), '%', 0):>4}"
    )


def run_mode(
    client: ApiClient,
    resources: ContainerResources,
    cpu_power: CpuPower,
    mode: str,
    duration_s: float,
    warmup_s: float,
    interval_s: float,
    reload_timeout_s: float,
) -> list[dict[str, Any]]:
    print(f"\nSwitching detector to {mode}...")
    client.request(
        "PATCH", "/api/settings/detector", {"enabled": True, "backend": "yolo", "device": mode}
    )
    ready = wait_for_detector(client, mode, reload_timeout_s)
    _, detector = _vision(ready)
    print(f"Detector ready on {detector.get('device')} using {detector.get('model')}.")
    print(f"Warming up for {warmup_s:.0f}s...")
    warmup_end = time.monotonic() + warmup_s
    while time.monotonic() < warmup_end:
        resources.sample()
        cpu_power.sample()
        time.sleep(min(interval_s, max(0.05, warmup_end - time.monotonic())))

    samples: list[dict[str, Any]] = []
    started = time.monotonic()
    deadline = started + duration_s
    while time.monotonic() < deadline:
        sample = collect_sample(client, resources, cpu_power, mode, time.monotonic() - started)
        samples.append(sample)
        print_sample(sample)
        time.sleep(min(interval_s, max(0.05, deadline - time.monotonic())))
    return samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="turret-control URL")
    parser.add_argument("--duration", type=float, default=120, help="measured seconds per mode")
    parser.add_argument("--warmup", type=float, default=20, help="warm-up seconds per mode")
    parser.add_argument("--interval", type=float, default=2, help="sample interval in seconds")
    parser.add_argument(
        "--modes", default="auto,cpu", help="comma-separated detector modes (auto,cpu)"
    )
    parser.add_argument("--reload-timeout", type=float, default=90)
    parser.add_argument("--username")
    parser.add_argument("--password", default=os.environ.get("TURRET_BENCHMARK_PASSWORD"))
    parser.add_argument("--output", help="output path prefix without .csv/.json")
    parser.add_argument(
        "--no-restore", action="store_true", help="leave the final benchmark settings active"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration <= 0 or args.warmup < 0 or args.interval <= 0:
        raise BenchmarkError("duration/interval must be positive and warmup cannot be negative")
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    if not modes or any(mode not in {"auto", "cpu"} for mode in modes):
        raise BenchmarkError("modes must contain only auto and cpu")

    client = ApiClient(args.url)
    client.authenticate(args.username, args.password)
    original = client.request("GET", "/api/settings/detector")
    health = client.request("GET", "/api/health")
    if not health.get("checks", {}).get("camera"):
        raise BenchmarkError("no camera is connected; inference cannot be benchmarked")
    if health.get("armed"):
        print("System was armed; disarming it for the benchmark (it will remain disarmed).")
        client.request("POST", "/api/control/arm", {"armed": False})

    resources = ContainerResources()
    cpu_power = CpuPower()
    print(f"API: {args.url}")
    print(f"Allocated CPU capacity: {resources.cores:.2f} cores")
    print(
        "CPU package power: Intel RAPL available"
        if cpu_power.available
        else "CPU package power: unavailable (RAPL is not exposed/readable here)"
    )
    all_samples: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    changed = {
        "enabled": original.get("enabled", True),
        "backend": original.get("backend", "yolo"),
        "device": original.get("device", "cpu"),
    }
    try:
        for mode in modes:
            samples = run_mode(
                client,
                resources,
                cpu_power,
                mode,
                args.duration,
                args.warmup,
                args.interval,
                args.reload_timeout,
            )
            all_samples.extend(samples)
            summary = summarize(mode, samples)
            summaries.append(summary)
            print("Summary:", json.dumps(summary, indent=2))
    finally:
        if not args.no_restore:
            print(f"\nRestoring detector settings: {changed}")
            try:
                client.request("PATCH", "/api/settings/detector", changed)
            except BenchmarkError as exc:
                print(f"WARNING: settings restoration failed: {exc}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = Path(args.output or f"detector-benchmark-{stamp}").expanduser()
    csv_path = prefix.with_suffix(".csv")
    json_path = prefix.with_suffix(".json")
    if all_samples:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(all_samples[0]))
            writer.writeheader()
            writer.writerows(all_samples)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "duration_s_per_mode": args.duration,
        "warmup_s_per_mode": args.warmup,
        "interval_s": args.interval,
        "allocated_cpu_cores": resources.cores,
        "cpu_power_available": cpu_power.available,
        "original_detector": changed,
        "summaries": summaries,
        "comparison": compare(summaries),
    }
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if report["comparison"]:
        print("\nCPU/GPU comparison:")
        print(json.dumps(report["comparison"], indent=2))
    print(f"\nCSV samples: {csv_path.resolve()}")
    print(f"JSON summary: {json_path.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BenchmarkError, KeyboardInterrupt) as exc:
        print(f"\nBenchmark stopped: {exc}")
        raise SystemExit(1) from exc
