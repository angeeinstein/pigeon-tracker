from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from app.camera.credentials import CameraCredentialStore
from app.camera.onvif import OnvifError, _clean_stream_uri, validate_private_device_url
from app.camera.rtsp import inject_credentials, redact_url


def test_credentials_are_persisted_but_status_is_redacted(tmp_path: Path) -> None:
    path = tmp_path / "camera_credentials.json"
    store = CameraCredentialStore(path)
    store.set("balcony", "viewer", "very-secret")

    assert store.status("balcony") == {
        "camera_id": "balcony",
        "configured": True,
        "username": "viewer",
    }
    assert "very-secret" not in json.dumps(store.status())
    assert CameraCredentialStore(path).get("balcony").password == "very-secret"  # type: ignore[union-attr]


def test_updating_username_can_preserve_password(tmp_path: Path) -> None:
    store = CameraCredentialStore(tmp_path / "credentials.json")
    store.set("camera-1", "old", "secret")
    store.set("camera-1", "new", None)
    assert store.get("camera-1") is not None
    assert store.get("camera-1").password == "secret"  # type: ignore[union-attr]


def test_rtsp_credentials_are_encoded_and_logs_are_redacted(tmp_path: Path) -> None:
    store = CameraCredentialStore(tmp_path / "credentials.json")
    store.set("camera", "user@example", "p@ss/word")
    url = inject_credentials("rtsp://192.168.1.20:554/live", store.get("camera"))
    assert url == "rtsp://user%40example:p%40ss%2Fword@192.168.1.20:554/live"
    assert redact_url(url) == "rtsp://***:***@192.168.1.20:554/live"


def test_stream_uri_is_sanitized_and_bad_camera_host_is_replaced() -> None:
    cleaned = _clean_stream_uri("rtsp://admin:secret@0.0.0.0:554/stream1", "192.168.1.20")
    assert cleaned == "rtsp://192.168.1.20:554/stream1"
    assert "secret" not in cleaned


def test_device_url_is_confined_to_private_network(monkeypatch: pytest.MonkeyPatch) -> None:
    private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 2020))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: private)
    assert validate_private_device_url(
        "http://camera.local:2020/onvif/device_service", {"http", "https"}
    ) == ("camera.local", 2020, False)

    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: public)
    with pytest.raises(OnvifError, match="private LAN"):
        validate_private_device_url("http://example.test/onvif/device_service", {"http", "https"})


def test_device_url_rejects_embedded_credentials() -> None:
    with pytest.raises(OnvifError, match="separate"):
        validate_private_device_url(
            "http://admin:secret@192.168.1.20/onvif/device_service", {"http", "https"}
        )
