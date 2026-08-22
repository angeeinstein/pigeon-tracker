"""Restart-safe web updater status, version checks and permission errors."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services.system_update import SystemUpdateManager


def manager(tmp_path: Path) -> SystemUpdateManager:
    app_dir = tmp_path / "app"
    (app_dir / ".git").mkdir(parents=True)
    unit = tmp_path / "turret-control-updater.service"
    path_unit = tmp_path / "turret-control-updater.path"
    helper = tmp_path / "turret-control-web-update"
    unit.write_text("unit", encoding="utf-8")
    path_unit.write_text("path", encoding="utf-8")
    helper.write_text("helper", encoding="utf-8")
    result = SystemUpdateManager(
        tmp_path / "data",
        app_dir=app_dir,
        unit_file=unit,
        path_unit_file=path_unit,
        helper_file=helper,
    )
    result.git = "/usr/bin/git"
    return result


@pytest.mark.asyncio
async def test_version_check_compares_installed_and_remote_commits(tmp_path: Path) -> None:
    updater = manager(tmp_path)
    updater._run_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            (0, "aaaa\n", ""),
            (0, "main\n", ""),
            (0, "https://secret-token@example.test/repo.git\n", ""),
            (0, "bbbb\trefs/heads/main\n", ""),
        ]
    )

    result = await updater.refresh_version()

    assert result["current_commit"] == "aaaa"
    assert result["latest_commit"] == "bbbb"
    assert result["update_available"] is True
    assert result["check_error"] is None
    assert result["repository"] == "https://example.test/repo.git"


@pytest.mark.asyncio
async def test_start_writes_only_systemd_request_marker_and_persists_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = manager(tmp_path)
    monkeypatch.setattr(SystemUpdateManager, "updater_available", property(lambda _self: True))
    updater._version.update(update_available=True, current_commit="aaaa", latest_commit="bbbb")

    result = await updater.start_update()

    assert result["state"] == "starting"
    assert updater.request_file.is_file()
    saved = json.loads(updater.status_file.read_text(encoding="utf-8"))
    assert saved["target_commit"] == "bbbb"


@pytest.mark.asyncio
async def test_permission_failure_is_visible_in_persisted_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = manager(tmp_path)
    monkeypatch.setattr(SystemUpdateManager, "updater_available", property(lambda _self: True))
    updater._version["update_available"] = True
    original_replace = __import__("os").replace

    def refuse_request(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == updater.request_file:
            raise PermissionError("request directory is not writable")
        original_replace(source, destination)

    monkeypatch.setattr("app.services.system_update.os.replace", refuse_request)

    with pytest.raises(RuntimeError, match="not writable"):
        await updater.start_update()

    assert updater.operation_status()["state"] == "failed"
    assert "not writable" in updater.operation_status()["message"]


def test_status_includes_bounded_external_log_tail(tmp_path: Path) -> None:
    updater = manager(tmp_path)
    updater.update_dir.mkdir(parents=True)
    updater.status_file.write_text(
        json.dumps({"state": "updating", "phase": "python", "progress": 52}),
        encoding="utf-8",
    )
    updater.log_file.write_text(
        "\n".join(f"line {index}" for index in range(200)), encoding="utf-8"
    )

    result = updater.operation_status()

    assert result["state"] == "updating"
    assert len(result["log_tail"]) == 160
    assert result["log_tail"][-1] == "line 199"
