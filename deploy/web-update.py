#!/usr/bin/env python3
"""Run the turret-control installer outside the web service's systemd unit.

Installed as /usr/local/libexec/turret-control-web-update and launched only by
turret-control-updater.service. Keep this dependency-free: it must still work
while the application's virtual environment and source tree are being updated.
"""

from __future__ import annotations

import fcntl
import grp
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

APP_DIR = Path("/opt/turret-control")
DATA_DIR = Path("/var/lib/turret-control")
UPDATE_DIR = DATA_DIR / "update"
STATUS_FILE = UPDATE_DIR / "status.json"
LOG_FILE = UPDATE_DIR / "update.log"
LOCK_FILE = Path("/run/lock/turret-control-update.lock")
REQUEST_FILE = UPDATE_DIR / "request"
INSTALLER = APP_DIR / "install.sh"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

PHASES = (
    ("Updating turret-control", "updating", "preparing", 5),
    ("Backing up configuration and data", "updating", "backup", 10),
    ("Installing system packages", "updating", "packages", 20),
    ("Creating directories", "updating", "directories", 25),
    ("Fetching the application", "updating", "source", 35),
    ("Installing management command", "updating", "management", 42),
    ("Setting up the Python environment", "updating", "python", 52),
    ("Building the web interface", "updating", "frontend", 68),
    ("Configuring", "updating", "configuration", 76),
    ("Installing the systemd service", "updating", "services", 82),
    ("Installing web update service", "updating", "permissions", 85),
    ("Restarting the service", "restarting", "restart", 88),
    ("Verifying", "verifying", "health", 92),
    ("Checking application endpoints", "verifying", "endpoints", 96),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(APP_DIR), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def prepare_files() -> int:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    group_id = grp.getgrnam("turret").gr_gid
    os.chown(UPDATE_DIR, 0, group_id)
    # The unprivileged web process writes the initial "starting" record; the
    # root helper replaces it with authoritative progress immediately after.
    os.chmod(UPDATE_DIR, 0o770)
    return group_id


def write_status(payload: dict[str, object], group_id: int) -> None:
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chown(temporary, 0, group_id)
    os.chmod(temporary, 0o640)
    os.replace(temporary, STATUS_FILE)


def log_line(handle: TextIO, line: str) -> None:
    handle.write(f"[{now()}] {line.rstrip()}\n")
    handle.flush()


def main() -> int:
    group_id = prepare_files()
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        REQUEST_FILE.unlink(missing_ok=True)

        current = commit()
        requested_at: object = None
        target_commit: object = None
        try:
            previous = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            requested_at = previous.get("requested_at")
            target_commit = previous.get("target_commit")
        except (OSError, ValueError, AttributeError):
            pass
        status: dict[str, object] = {
            "state": "checking",
            "phase": "version",
            "progress": 3,
            "message": "Checking the installed and remote versions.",
            "requested_at": requested_at,
            "started_at": now(),
            "finished_at": None,
            "current_commit": current,
            "target_commit": target_commit,
            "installed_commit": None,
            "exit_code": None,
        }
        write_status(status, group_id)

        if not INSTALLER.is_file():
            status.update(
                state="failed",
                phase="preflight",
                message=f"Installer not found at {INSTALLER}.",
                finished_at=now(),
                exit_code=2,
            )
            write_status(status, group_id)
            return 2

        with LOG_FILE.open("w", encoding="utf-8", buffering=1) as log:
            os.chown(LOG_FILE, 0, group_id)
            os.chmod(LOG_FILE, 0o640)
            log_line(log, "Web update started.")
            log_line(log, f"Installed commit: {current or 'unknown'}")
            process = subprocess.Popen(
                ["bash", str(INSTALLER), "--update", "--yes"],
                cwd="/",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = ANSI_RE.sub("", raw_line).rstrip()
                log_line(log, line)
                for marker, state, phase, progress in PHASES:
                    if marker in line:
                        status.update(
                            state=state,
                            phase=phase,
                            progress=progress,
                            message=line.lstrip("=> "),
                        )
                        write_status(status, group_id)
                        break
            exit_code = process.wait()
            installed = commit()
            if exit_code == 0:
                log_line(log, f"Update completed at commit {installed or 'unknown'}.")
                status.update(
                    state="succeeded",
                    phase="complete",
                    progress=100,
                    message="Update completed and all server endpoints passed verification.",
                    installed_commit=installed,
                    finished_at=now(),
                    exit_code=0,
                )
            else:
                log_line(log, f"Update failed with exit code {exit_code}.")
                status.update(
                    state="failed",
                    phase="failed",
                    message=(
                        "Update failed. The installer attempted rollback; see the log below "
                        "for the failing step."
                    ),
                    installed_commit=installed,
                    finished_at=now(),
                    exit_code=exit_code,
                )
            write_status(status, group_id)
            REQUEST_FILE.unlink(missing_ok=True)
            return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"web updater failed: {exc}", file=sys.stderr)
        # Preserve a useful failure for the web UI even when setup or process
        # creation fails before the normal completion path.
        try:
            group_id = prepare_files()
            try:
                failed = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
                if not isinstance(failed, dict):
                    failed = {}
            except (OSError, ValueError):
                failed = {}
            failed.update(
                state="failed",
                phase="updater",
                message=f"The external updater failed: {exc}",
                finished_at=now(),
                exit_code=1,
            )
            write_status(failed, group_id)
            REQUEST_FILE.unlink(missing_ok=True)
        except Exception as status_exc:
            print(f"could not persist updater failure: {status_exc}", file=sys.stderr)
        raise
