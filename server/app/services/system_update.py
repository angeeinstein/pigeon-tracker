"""Restart-safe application update status and version monitoring.

The web server never executes the installer as root itself. Production installs
provide a tightly scoped systemd path unit. The unprivileged server writes a
request marker, systemd launches the fixed root updater independently, and the
helper writes status into the data directory. A newly restarted server can
therefore continue reporting the same update run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.logging_config import get_logger

log = get_logger(__name__)

RUNNING_STATES = {"starting", "checking", "updating", "restarting", "verifying"}
TERMINAL_STATES = {"idle", "succeeded", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display_repository(repository: str) -> str:
    """Remove HTTP credentials before repository metadata reaches the API."""
    parsed = urlsplit(repository)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return repository
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


class SystemUpdateManager:
    """Check GitHub periodically and invoke the fixed-purpose updater unit."""

    def __init__(
        self,
        data_dir: Path,
        *,
        app_dir: Path = Path("/opt/turret-control"),
        unit_file: Path = Path("/etc/systemd/system/turret-control-updater.service"),
        path_unit_file: Path = Path("/etc/systemd/system/turret-control-updater.path"),
        helper_file: Path = Path("/usr/local/libexec/turret-control-web-update"),
        check_interval_s: float = 300.0,
    ) -> None:
        self.update_dir = data_dir / "update"
        self.status_file = self.update_dir / "status.json"
        self.log_file = self.update_dir / "update.log"
        self.request_file = self.update_dir / "request"
        self.app_dir = app_dir
        self.unit_file = unit_file
        self.path_unit_file = path_unit_file
        self.helper_file = helper_file
        self.check_interval_s = max(30.0, check_interval_s)
        self.git = shutil.which("git")
        self._version: dict[str, Any] = {
            "supported": bool(self.git and (self.app_dir / ".git").is_dir()),
            "checking": False,
            "update_available": None,
            "current_commit": None,
            "latest_commit": None,
            "branch": None,
            "repository": None,
            "checked_at": None,
            "check_error": None,
            "check_interval_s": self.check_interval_s,
        }

    @property
    def updater_available(self) -> bool:
        return bool(
            os.name == "posix"
            and self.unit_file.is_file()
            and self.path_unit_file.is_file()
            and self.helper_file.is_file()
        )

    async def monitor_versions(self) -> None:
        """Refresh remote version state immediately and every few minutes."""
        while True:
            try:
                if self.operation_status(include_log=False).get("state") not in RUNNING_STATES:
                    await self.refresh_version()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("update version check failed")
            await asyncio.sleep(self.check_interval_s)

    async def refresh_version(self) -> dict[str, Any]:
        self._version["checking"] = True
        self._version["check_error"] = None
        try:
            if not self.git or not (self.app_dir / ".git").is_dir():
                self._version.update(
                    supported=False,
                    update_available=None,
                    check_error="version checks are available only on an installed Git checkout",
                    checked_at=_utc_now(),
                )
                return dict(self._version)

            current = await self._git("rev-parse", "HEAD")
            branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
            repository = await self._git("remote", "get-url", "origin")
            if branch == "HEAD":
                branch = "main"
            code, output, error = await self._run_command(
                self.git,
                "ls-remote",
                repository,
                f"refs/heads/{branch}",
                timeout_s=15.0,
            )
            if code != 0:
                detail = error.strip().replace(repository, _display_repository(repository))
                raise RuntimeError(detail or "git ls-remote failed")
            latest = output.split()[0] if output.split() else ""
            if not latest:
                raise RuntimeError(f"remote branch {branch!r} was not found")
            self._version.update(
                supported=True,
                update_available=current != latest,
                current_commit=current,
                latest_commit=latest,
                branch=branch,
                repository=_display_repository(repository),
                checked_at=_utc_now(),
                check_error=None,
            )
        except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
            self._version.update(
                supported=True,
                update_available=None,
                checked_at=_utc_now(),
                check_error=str(exc),
            )
        finally:
            self._version["checking"] = False
        return dict(self._version)

    async def _git(self, *arguments: str) -> str:
        if not self.git:
            raise RuntimeError("git is not installed")
        code, output, error = await self._run_command(
            self.git,
            "-c",
            f"safe.directory={self.app_dir}",
            "-C",
            str(self.app_dir),
            *arguments,
            timeout_s=10.0,
        )
        if code != 0:
            raise RuntimeError(error.strip() or f"git {' '.join(arguments)} failed")
        return output.strip()

    @staticmethod
    async def _run_command(
        *command: str, timeout_s: float
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        return (
            int(process.returncode or 0),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def operation_status(self, *, include_log: bool = True) -> dict[str, Any]:
        default: dict[str, Any] = {
            "state": "idle",
            "phase": "idle",
            "progress": 0,
            "message": "No web update has been run yet.",
            "requested_at": None,
            "started_at": None,
            "finished_at": None,
            "current_commit": None,
            "target_commit": None,
            "installed_commit": None,
            "exit_code": None,
        }
        try:
            raw = json.loads(self.status_file.read_text(encoding="utf-8"))
            if (
                not isinstance(raw, dict)
                or raw.get("state") not in RUNNING_STATES | TERMINAL_STATES
            ):
                raise ValueError("invalid updater status")
            default.update(raw)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass
        if default["state"] == "starting" and default.get("requested_at"):
            try:
                requested = datetime.fromisoformat(str(default["requested_at"]))
                if requested.tzinfo is None:
                    requested = requested.replace(tzinfo=timezone.utc)
                age_s = (datetime.now(timezone.utc) - requested).total_seconds()
                if age_s > 20 and self.request_file.exists():
                    default.update(
                        state="failed",
                        phase="trigger",
                        message=(
                            "The systemd path service did not collect the update request within "
                            "20 seconds. Run the shell updater once to repair its permissions."
                        ),
                        finished_at=_utc_now(),
                        exit_code=1,
                    )
            except ValueError:
                pass
        if include_log:
            default["log_tail"] = self._log_tail()
        return default

    def status(self) -> dict[str, Any]:
        return {
            **self.operation_status(),
            "updater_available": self.updater_available,
            "permission_mode": (
                "systemd request path" if self.updater_available else "unavailable"
            ),
            "version_check": dict(self._version),
        }

    def overview(self) -> dict[str, Any]:
        """Small polling payload for the global navigation indicator."""
        operation = self.operation_status(include_log=False)
        return {
            "state": operation["state"],
            "updater_available": self.updater_available,
            "version_check": dict(self._version),
        }

    def _log_tail(self, max_lines: int = 160, max_bytes: int = 256 * 1024) -> list[str]:
        try:
            with self.log_file.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                data = handle.read().decode("utf-8", errors="replace")
            return data.splitlines()[-max_lines:]
        except OSError:
            return []

    def _write_operation_status(self, payload: dict[str, Any]) -> None:
        self.update_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.status_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            temporary.chmod(0o640)
        os.replace(temporary, self.status_file)

    async def start_update(self) -> dict[str, Any]:
        current = self.operation_status(include_log=False)
        if current["state"] in RUNNING_STATES:
            raise RuntimeError("an update is already running")
        if self._version.get("update_available") is False:
            raise RuntimeError("the installed server is already up to date")
        if not self.updater_available:
            raise RuntimeError(
                "the privileged web updater is not installed; run the shell updater once first"
            )

        starting = {
            "state": "starting",
            "phase": "request",
            "progress": 1,
            "message": "Handing the update request to the restricted systemd service.",
            "requested_at": _utc_now(),
            "started_at": None,
            "finished_at": None,
            "current_commit": self._version.get("current_commit"),
            "target_commit": self._version.get("latest_commit"),
            "installed_commit": None,
            "exit_code": None,
        }
        try:
            self._write_operation_status(starting)
        except OSError as exc:
            raise RuntimeError(f"cannot create updater status file: {exc}") from exc

        try:
            # Replace a stale marker from an interrupted run. The path unit
            # watches this exact file and invokes a command with no user input.
            self.request_file.unlink(missing_ok=True)
            temporary = self.request_file.with_suffix(".tmp")
            temporary.write_text(
                str(starting["requested_at"] or _utc_now()), encoding="utf-8"
            )
            os.replace(temporary, self.request_file)
        except OSError as exc:
            failed = {
                **starting,
                "state": "failed",
                "phase": "permission",
                "message": f"cannot create systemd update request: {exc}",
                "finished_at": _utc_now(),
                "exit_code": 1,
            }
            with contextlib.suppress(OSError):
                self._write_operation_status(failed)
            raise RuntimeError(str(failed["message"])) from exc
        return self.status()
