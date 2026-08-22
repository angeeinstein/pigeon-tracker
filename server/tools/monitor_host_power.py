#!/usr/bin/env python3
"""Record host CPU-package and NVIDIA board power during an LXC benchmark.

Run this as root on the Proxmox host. Intel RAPL energy counters are converted
to watts from the measured energy delta; NVIDIA values come from nvidia-smi.
The script never estimates power from CPU utilisation.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return path.name


@dataclass
class RaplDomain:
    name: str
    energy_file: Path
    max_energy_uj: int | None
    previous_energy_uj: int | None = None


class RaplPower:
    """Read top-level RAPL packages without double-counting child domains."""

    def __init__(
        self,
        root: Path = Path("/sys/class/powercap"),
        directories: list[Path] | None = None,
    ) -> None:
        self.domains: list[RaplDomain] = []
        self.previous_time: float | None = None
        candidates = (
            directories if directories is not None else sorted(root.glob("intel-rapl:[0-9]*"))
        )
        for directory in candidates:
            name = _read_text(directory / "name")
            energy_file = directory / "energy_uj"
            if not name.lower().startswith("package") or _read_int(energy_file) is None:
                continue
            self.domains.append(
                RaplDomain(
                    name=name,
                    energy_file=energy_file,
                    max_energy_uj=_read_int(directory / "max_energy_range_uj"),
                )
            )

    def sample(self) -> dict[str, float | None]:
        now = time.monotonic()
        elapsed = None if self.previous_time is None else now - self.previous_time
        values: dict[str, float | None] = {}
        for domain in self.domains:
            current = _read_int(domain.energy_file)
            watts = None
            if current is not None and domain.previous_energy_uj is not None and elapsed:
                delta = current - domain.previous_energy_uj
                if delta < 0 and domain.max_energy_uj:
                    delta += domain.max_energy_uj
                watts = max(0, delta) / 1_000_000 / max(0.001, elapsed)
            if current is not None:
                domain.previous_energy_uj = current
            values[domain.name] = watts
        self.previous_time = now
        return values


def nvidia_gpus() -> list[dict[str, Any]]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,pci.bus_id,power.draw,utilization.gpu,memory.used,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    gpus: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 7:
            continue
        try:
            gpus.append(
                {
                    "index": int(fields[0]),
                    "name": fields[1],
                    "bus_id": fields[2],
                    "power_w": float(fields[3]),
                    "util_percent": float(fields[4]),
                    "memory_mb": float(fields[5]),
                    "temperature_c": float(fields[6]),
                }
            )
        except ValueError:
            continue
    return gpus


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), int | float)]
    return statistics.fmean(values) if values else None


def _selected_gpus(gpus: list[dict[str, Any]], selected: set[int] | None) -> list[dict[str, Any]]:
    return gpus if selected is None else [gpu for gpu in gpus if gpu["index"] in selected]


def probe(rapl: RaplPower) -> int:
    print("CPU package power sources:")
    if rapl.domains:
        for domain in rapl.domains:
            print(f"  {domain.name}: {domain.energy_file}")
    else:
        print("  none")
        print("  Try as root: modprobe intel_rapl_common; modprobe intel_rapl_msr")
        print("  Then run this probe again. AMD hosts may expose different hwmon sensors.")

    print("NVIDIA GPU power sources:")
    gpus = nvidia_gpus()
    if gpus:
        for gpu in gpus:
            print(
                f"  GPU {gpu['index']}: {gpu['name']} ({gpu['bus_id']}), "
                f"currently {gpu['power_w']:.2f} W"
            )
    else:
        print("  none (nvidia-smi unavailable or no supported GPU)")
    return 0 if rapl.domains or gpus else 1


def monitor(args: argparse.Namespace, rapl: RaplPower) -> int:
    selected = set(args.gpu) if args.gpu else None
    initial_gpus = _selected_gpus(nvidia_gpus(), selected)
    if not rapl.domains:
        print("WARNING: CPU package power unavailable; CSV CPU watts will be empty.")
    if not initial_gpus:
        print("WARNING: no selected NVIDIA GPU power source is available.")
    elif selected is None:
        print("Summing every NVIDIA GPU. Use --gpu INDEX to restrict the total.")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    deadline = started + args.duration if args.duration > 0 else None
    rapl.sample()  # Establish the energy/time baseline.
    print(f"Writing {output}. Press Ctrl+C to stop.")
    try:
        time.sleep(args.interval)
        while deadline is None or time.monotonic() < deadline:
            sample_started = time.monotonic()
            packages = rapl.sample()
            gpus = _selected_gpus(nvidia_gpus(), selected)
            cpu_values = [value for value in packages.values() if value is not None]
            gpu_values = [float(gpu["power_w"]) for gpu in gpus]
            cpu_power = sum(cpu_values) if cpu_values else None
            gpu_power = sum(gpu_values) if gpu_values else None
            combined = (
                cpu_power + gpu_power if cpu_power is not None and gpu_power is not None else None
            )
            row: dict[str, Any] = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": round(sample_started - started, 3),
                "cpu_package_power_w": cpu_power,
                "gpu_board_power_w": gpu_power,
                "cpu_plus_gpu_power_w": combined,
                "gpus": gpus,
                "rapl_packages_w": packages,
            }
            rows.append(row)
            cpu_text = "n/a" if cpu_power is None else f"{cpu_power:.1f} W"
            gpu_text = "n/a" if gpu_power is None else f"{gpu_power:.1f} W"
            total_text = "n/a" if combined is None else f"{combined:.1f} W"
            print(
                f"{row['elapsed_s']:7.1f}s  CPU package {cpu_text:>8}  "
                f"GPU {gpu_text:>8}  CPU+GPU {total_text:>8}",
                flush=True,
            )
            remaining = args.interval - (time.monotonic() - sample_started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        print("Stopping...")

    fields = [
        "timestamp_utc",
        "elapsed_s",
        "cpu_package_power_w",
        "gpu_board_power_w",
        "cpu_plus_gpu_power_w",
        "gpu_details_json",
        "rapl_packages_json",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fields[:5]},
                    "gpu_details_json": json.dumps(row["gpus"], separators=(",", ":")),
                    "rapl_packages_json": json.dumps(row["rapl_packages_w"], separators=(",", ":")),
                }
            )

    summary = {
        "samples": len(rows),
        "elapsed_s": round(time.monotonic() - started, 3),
        "cpu_package_power_w_mean": _mean(rows, "cpu_package_power_w"),
        "gpu_board_power_w_mean": _mean(rows, "gpu_board_power_w"),
        "cpu_plus_gpu_power_w_mean": _mean(rows, "cpu_plus_gpu_power_w"),
        "output": str(output),
    }
    print(json.dumps(summary, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="list usable power sources and exit")
    parser.add_argument(
        "--gpu",
        type=int,
        action="append",
        help="NVIDIA host GPU index to include (repeatable; default: all)",
    )
    parser.add_argument("--interval", type=float, default=2.0, help="sample interval in seconds")
    parser.add_argument(
        "--duration", type=float, default=0, help="stop after this many seconds (0: Ctrl+C)"
    )
    parser.add_argument("--output", default="pigeon-host-power.csv", help="output CSV path")
    args = parser.parse_args()
    if args.interval < 0.25:
        parser.error("--interval must be at least 0.25 seconds")
    if args.duration < 0:
        parser.error("--duration cannot be negative")
    return args


def main() -> int:
    args = parse_args()
    rapl = RaplPower()
    if args.probe:
        return probe(rapl)
    return monitor(args, rapl)


if __name__ == "__main__":
    raise SystemExit(main())
