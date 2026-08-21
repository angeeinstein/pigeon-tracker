"""End-to-end API smoke tests.

The whole application is started — settings, database, simulated camera, mock
detector, targeting loop — with no hardware and no model. If this passes, a
fresh install will at least come up and be controllable.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import DeploymentConfig, get_config
from app.main import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    data_dir: Path = tmp_path_factory.mktemp("turret-data")
    config = DeploymentConfig(
        data_dir=data_dir,
        force_simulated_camera=True,
        force_mock_detector=True,
        auth_enabled=False,
        log_level="WARNING",
        _env_file=None,  # type: ignore[call-arg]
    )
    # deps.require_auth reads the cached global config; point it at ours.
    get_config.cache_clear()
    get_config.__wrapped__ = lambda: config  # type: ignore[attr-defined]
    app = create_app(config)
    with TestClient(app) as test_client:
        yield test_client
    get_config.cache_clear()


class TestHealth:
    def test_health_reports_subsystems(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] in {"ok", "degraded"}
        assert set(payload["checks"]) == {"database", "camera", "controller", "ai"}
        assert payload["checks"]["database"] is True
        # No controller is connected in this test, so the system must say so
        # rather than pretending to be healthy.
        assert payload["checks"]["controller"] is False

    def test_version(self, client: TestClient) -> None:
        payload = client.get("/api/version").json()
        assert payload["protocol_version"] >= 1
        assert payload["server_version"]

    def test_system_info(self, client: TestClient) -> None:
        payload = client.get("/api/system").json()
        assert "paths" in payload and "gpu" in payload

    def test_scene_motion_mask_is_available_without_replacing_preview(
        self, client: TestClient
    ) -> None:
        deadline = time.monotonic() + 2.0
        response = client.get("/api/scene-motion/mask")
        while response.status_code == 404 and time.monotonic() < deadline:
            time.sleep(0.02)
            response = client.get("/api/scene-motion/mask")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content.startswith(b"\xff\xd8")


class TestSettings:
    def test_read_all(self, client: TestClient) -> None:
        payload = client.get("/api/settings").json()
        assert "targeting" in payload and "motion" in payload and "scene_motion" in payload

    def test_patch_and_read_back(self, client: TestClient) -> None:
        response = client.patch("/api/settings/ui", json={"preview_fps": 4})
        assert response.status_code == 200
        assert client.get("/api/settings/ui").json()["preview_fps"] == 4

    def test_defaults_are_separate_from_saved_values(self, client: TestClient) -> None:
        defaults = client.get("/api/settings-defaults")
        assert defaults.status_code == 200
        assert defaults.json()["ui"]["preview_fps"] == 12
        assert client.get("/api/settings/ui").json()["preview_fps"] == 4

    def test_patch_multiple_sections_atomically(self, client: TestClient) -> None:
        response = client.patch(
            "/api/settings",
            json={"ui": {"preview_fps": 5}, "motion": {"max_speed_deg_s": 55}},
        )
        assert response.status_code == 200
        assert response.json()["ui"]["preview_fps"] == 5
        assert response.json()["motion"]["max_speed_deg_s"] == 55

        rejected = client.patch(
            "/api/settings",
            json={"ui": {"preview_fps": 6}, "motion": {"max_speed_deg_s": -1}},
        )
        assert rejected.status_code == 422
        settings = client.get("/api/settings").json()
        assert settings["ui"]["preview_fps"] == 5
        assert settings["motion"]["max_speed_deg_s"] == 55

    def test_invalid_patch_is_rejected(self, client: TestClient) -> None:
        response = client.patch("/api/settings/motion", json={"max_speed_deg_s": -1})
        assert response.status_code == 422

    def test_unknown_section(self, client: TestClient) -> None:
        assert client.get("/api/settings/nope").status_code == 404

    def test_schema_endpoint(self, client: TestClient) -> None:
        payload = client.get("/api/settings-schema").json()
        assert "properties" in payload["spray"]

    def test_detector_catalog_reports_model_classes_and_filter_conflicts(
        self, client: TestClient
    ) -> None:
        original = client.get("/api/settings").json()
        try:
            response = client.patch(
                "/api/settings",
                json={
                    "detector": {**original["detector"], "classes": ["bird", "dragon"]},
                    "targeting": {**original["targeting"], "target_classes": ["person"]},
                },
            )
            assert response.status_code == 200

            catalog = client.get("/api/detector/catalog")
            assert catalog.status_code == 200
            payload = catalog.json()
            assert payload["available_classes"] == ["bird"]
            assert payload["invalid_detector_classes"] == ["dragon"]
            assert payload["invalid_target_classes"] == ["person"]
            assert payload["target_classes_excluded_by_detector"] == ["person"]
        finally:
            client.patch(
                "/api/settings",
                json={"detector": original["detector"], "targeting": original["targeting"]},
            )

    def test_detector_model_upload_is_validated_and_listed(self, client: TestClient) -> None:
        # Deliberately cross the normal 16 KiB JSON request limit. Model
        # uploads are streamed and have their own 512 MiB limit.
        checkpoint = b"PK\x03\x04" + b"x" * (20 * 1024)
        uploaded = client.post(
            "/api/detector/models?filename=pigeon-v1.pt",
            content=checkpoint,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["filename"] == "pigeon-v1.pt"
        assert uploaded.json()["size_bytes"] == len(checkpoint)
        assert "pigeon-v1.pt" in uploaded.json()["installed_models"]
        assert "pigeon-v1.pt" in client.get("/api/detector/catalog").json()["installed_models"]

        duplicate = client.post(
            "/api/detector/models?filename=pigeon-v1.pt",
            content=checkpoint,
        )
        assert duplicate.status_code == 409

        invalid = client.post(
            "/api/detector/models?filename=not-a-model.pt",
            content=b"not a PyTorch checkpoint",
        )
        assert invalid.status_code == 422
        traversal = client.post(
            "/api/detector/models?filename=../outside.pt",
            content=checkpoint,
        )
        assert traversal.status_code == 422


class TestCameraOnboarding:
    def test_discovery_and_profiles_do_not_echo_password(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.api.routes.discover_onvif",
            lambda timeout: [
                {
                    "host": "192.168.1.20",
                    "port": 2020,
                    "xaddr": "http://192.168.1.20:2020/onvif/device_service",
                    "xaddrs": [],
                    "name": "Balcony",
                    "hardware": "C100",
                    "location": "",
                    "types": [],
                }
            ],
        )
        discovery = client.post("/api/cameras/discover").json()
        assert discovery["devices"][0]["name"] == "Balcony"

        monkeypatch.setattr(
            "app.api.routes.fetch_onvif_profiles",
            lambda xaddr, username, password: {
                "device": {"host": "192.168.1.20", "xaddr": xaddr},
                "profiles": [{"token": "main", "name": "Main", "uri": "rtsp://192.168.1.20/live"}],
            },
        )
        profiles = client.post(
            "/api/cameras/onvif/profiles",
            json={
                "xaddr": "http://192.168.1.20:2020/onvif/device_service",
                "username": "viewer",
                "password": "very-secret",
            },
        ).json()
        assert "very-secret" not in str(profiles)

    def test_onboard_stores_redacted_credentials(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.api.routes.validate_private_device_url",
            lambda url, schemes: ("192.168.1.20", 554, False),
        )
        response = client.post(
            "/api/cameras/onboard",
            json={
                "id": "test-camera",
                "name": "Test camera",
                "role": "aux",
                "uri": "rtsp://192.168.1.20/live",
                "username": "viewer",
                "password": "very-secret",
                "make_primary": False,
            },
        )
        assert response.status_code == 201
        assert "very-secret" not in response.text
        assert [source["id"] for source in response.json()["sources"]] == ["test-camera"]
        status = client.get("/api/cameras/credentials").json()["test-camera"]
        assert status == {"camera_id": "test-camera", "configured": True, "username": "viewer"}

    def test_profile_query_has_a_hard_timeout(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.api.routes.ONVIF_PROFILE_TIMEOUT_S", 0.001)
        monkeypatch.setattr(
            "app.api.routes.fetch_onvif_profiles",
            lambda *args: time.sleep(0.05),
        )
        response = client.post(
            "/api/cameras/onvif/profiles",
            json={
                "xaddr": "http://192.168.1.20:2020/onvif/device_service",
                "username": "viewer",
                "password": "secret",
            },
        )
        assert response.status_code == 504
        assert "secret" not in response.text
        assert "did not answer" in response.json()["detail"]


class TestControlWithoutHardware:
    def test_move_without_a_controller_is_a_conflict(self, client: TestClient) -> None:
        response = client.post("/api/control/move", json={"pan_deg": 10, "tilt_deg": 0})
        assert response.status_code == 409
        assert "not connected" in response.json()["detail"]

    def test_arming_without_a_controller_is_refused(self, client: TestClient) -> None:
        response = client.post("/api/control/arm", json={"armed": True})
        assert response.status_code == 409

    def test_emergency_stop_always_answers(self, client: TestClient) -> None:
        # Even with no hardware attached, the operator must get a definite
        # answer rather than an error page.
        response = client.post("/api/control/estop")
        assert response.status_code == 200
        assert response.json()["armed"] is False

    def test_spray_without_arming_is_refused(self, client: TestClient) -> None:
        response = client.post("/api/control/spray", json={"duration_ms": 200})
        assert response.status_code == 409

    def test_move_validation(self, client: TestClient) -> None:
        assert (
            client.post("/api/control/move", json={"pan_deg": 9999, "tilt_deg": 0}).status_code
            == 422
        )


class TestZonesAndCalibration:
    def test_zone_crud(self, client: TestClient) -> None:
        created = client.post(
            "/api/zones",
            json={
                "name": "balcony",
                "zone_type": "active",
                "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
            },
        )
        assert created.status_code == 201
        zone_id = created.json()["id"]

        assert any(z["id"] == zone_id for z in client.get("/api/zones").json())

        patched = client.patch(f"/api/zones/{zone_id}", json={"enabled": False})
        assert patched.status_code == 200 and patched.json()["enabled"] is False

        assert client.delete(f"/api/zones/{zone_id}").status_code == 204
        assert not any(z["id"] == zone_id for z in client.get("/api/zones").json())

    def test_zone_validation(self, client: TestClient) -> None:
        response = client.post(
            "/api/zones",
            json={"name": "bad", "zone_type": "active", "points": [[0, 0], [1, 0], [2, 0]]},
        )
        assert response.status_code == 422

    def test_calibration_round_trip(self, client: TestClient) -> None:
        points = [
            {"x": 0.2, "y": 0.2, "pan_deg": -20, "tilt_deg": 10},
            {"x": 0.8, "y": 0.2, "pan_deg": 20, "tilt_deg": 10},
            {"x": 0.8, "y": 0.8, "pan_deg": 20, "tilt_deg": -10},
            {"x": 0.2, "y": 0.8, "pan_deg": -20, "tilt_deg": -10},
        ]
        for point in points:
            assert client.post("/api/calibration/points", json=point).status_code == 201

        model = client.get("/api/calibration/model").json()
        assert model["calibrated"] is True

        solved = client.get("/api/calibration/solve", params={"x": 0.5, "y": 0.5}).json()
        assert solved["pan_deg"] == pytest.approx(0.0, abs=1.0)
        assert solved["tilt_deg"] == pytest.approx(0.0, abs=1.0)

        assert client.delete("/api/calibration/points").json()["removed"] == 4

    def test_solve_without_calibration_is_a_conflict(self, client: TestClient) -> None:
        assert client.get("/api/calibration/solve", params={"x": 0.5, "y": 0.5}).status_code == 409


class TestEventsAndPreview:
    def test_events_are_recorded(self, client: TestClient) -> None:
        events = client.get("/api/events", params={"limit": 50}).json()
        assert any(event["message"] == "server started" for event in events)

    def test_event_categories(self, client: TestClient) -> None:
        assert "targeting" in client.get("/api/events/categories").json()

    def test_snapshot_path_traversal_is_blocked(self, client: TestClient) -> None:
        assert client.get("/api/snapshots/..%2F..%2Fetc%2Fpasswd").status_code == 404

    def test_camera_status(self, client: TestClient) -> None:
        payload = client.get("/api/cameras").json()
        assert payload["cameras"][0]["backend"] in {"simulated", "none", "ffmpeg", "gstreamer"}

    def test_snapshot_returns_an_image(self, client: TestClient) -> None:
        response = client.get("/api/camera/snapshot.jpg")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content[:2] == b"\xff\xd8"  # JPEG SOI


class TestDetectionCaptures:
    def test_manual_capture_review_and_delete(self, client: TestClient) -> None:
        deadline = time.monotonic() + 2.0
        created = client.post("/api/detection-captures/manual")
        while created.status_code == 503 and time.monotonic() < deadline:
            time.sleep(0.03)
            created = client.post("/api/detection-captures/manual")
        assert created.status_code == 201
        capture = created.json()
        assert capture["trigger"] == "manual"
        assert capture["review_status"] == "unreviewed"

        image = client.get(f"/api/detection-captures/{capture['id']}/image")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/jpeg"
        assert image.content[:2] == b"\xff\xd8"

        reviewed = client.patch(
            f"/api/detection-captures/{capture['id']}",
            json={"review_status": "training", "review_label": "bird"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["review_label"] == "bird"
        assert any(
            item["id"] == capture["id"]
            for item in client.get(
                "/api/detection-captures", params={"review_status": "training"}
            ).json()
        )

        assert client.delete(f"/api/detection-captures/{capture['id']}").status_code == 204
        assert client.get(f"/api/detection-captures/{capture['id']}/image").status_code == 404

    def test_box_review_manual_annotation_and_yolo_export(self, client: TestClient) -> None:
        deadline = time.monotonic() + 2.0
        created = client.post("/api/detection-captures/manual")
        while created.status_code == 503 and time.monotonic() < deadline:
            time.sleep(0.03)
            created = client.post("/api/detection-captures/manual")
        assert created.status_code == 201
        capture = created.json()

        added = client.post(
            f"/api/detection-captures/{capture['id']}/annotations",
            json={"bbox": [10, 20, 110, 220], "class_name": "bird"},
        )
        assert added.status_code == 201
        annotation = added.json()["detections"][0]
        assert annotation["source"] == "manual"
        assert annotation["review_status"] == "accepted"
        assert added.json()["review_status"] == "training"

        reset = client.patch(
            f"/api/detection-captures/{capture['id']}/annotations/0",
            json={"review_status": "unreviewed", "review_label": ""},
        )
        assert reset.status_code == 200
        assert reset.json()["review_status"] == "unreviewed"

        accepted = client.patch(
            f"/api/detection-captures/{capture['id']}/annotations/0",
            json={"review_status": "accepted", "review_label": "bird"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["review_status"] == "training"

        exported = client.get("/api/detection-captures/export/yolo.zip")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            names = archive.namelist()
            label_name = next(
                name for name in names if name.endswith(".txt") and "/labels/" in name
            )
            label = archive.read(label_name).decode().strip().split()
            assert label[0] == "0"
            assert len(label) == 5
            manifest = json.loads(archive.read("pigeon-dataset/manifest.json"))
            assert manifest["captures"][0]["accepted_boxes"][0]["review_label"] == "bird"
            assert "pigeon-dataset/dataset.yaml" in names
            assert "pigeon-dataset/train_model.py" in names
            assert "pigeon-dataset/train_windows.bat" in names
            trainer_source = archive.read("pigeon-dataset/train_model.py").decode()
            compile(trainer_source, "train_model.py", "exec")
            assert (
                "double-click train_windows.bat"
                in archive.read("pigeon-dataset/README.txt").decode()
            )

        rejected = client.patch(
            f"/api/detection-captures/{capture['id']}",
            json={"review_status": "rejected", "review_label": "not-bird"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["detections"][0]["review_status"] == "rejected"

        assert client.delete(f"/api/detection-captures/{capture['id']}").status_code == 204

    def test_capture_paging_and_navigation_are_not_limited_to_one_page(
        self, client: TestClient
    ) -> None:
        captures = []
        for _ in range(3):
            deadline = time.monotonic() + 2.0
            created = client.post("/api/detection-captures/manual")
            while created.status_code == 503 and time.monotonic() < deadline:
                time.sleep(0.03)
                created = client.post("/api/detection-captures/manual")
            assert created.status_code == 201
            captures.append(created.json())

        page = client.get(
            "/api/detection-captures/page",
            params={
                "limit": 2,
                "offset": 0,
                "review_status": "unreviewed",
                "class_name": "manual",
            },
        )
        assert page.status_code == 200
        assert page.json()["total"] == 3
        assert len(page.json()["items"]) == 2

        newest, middle, oldest = reversed(captures)
        adjacent = client.get(
            f"/api/detection-captures/{newest['id']}/navigate",
            params={
                "direction": "next",
                "review_status": "unreviewed",
                "class_name": "manual",
            },
        )
        assert adjacent.status_code == 200
        assert adjacent.json()["capture"]["id"] == middle["id"]
        assert adjacent.json()["position"] == 2
        assert adjacent.json()["total"] == 3

        assert (
            client.patch(
                f"/api/detection-captures/{newest['id']}",
                json={"review_status": "rejected", "review_label": "not-bird"},
            ).status_code
            == 200
        )
        compacted = client.get(
            f"/api/detection-captures/{newest['id']}/navigate",
            params={
                "direction": "next",
                "review_status": "unreviewed",
                "class_name": "manual",
            },
        )
        assert compacted.json()["capture"]["id"] == middle["id"]
        assert compacted.json()["position"] == 1
        assert compacted.json()["total"] == 2

        for capture in (newest, middle, oldest):
            assert client.delete(f"/api/detection-captures/{capture['id']}").status_code == 204


class TestControllerHandshake:
    """A controller connecting must be able to complete a full exchange.

    This is a regression test for a deadlock: the route used to *await* the
    post-connect work (config push, disarm) before starting its read loop, so
    the acknowledgements it was waiting for could never be read. Every
    connection burned two command timeouts and the config push always failed —
    while everything still *looked* connected.
    """

    def test_config_push_is_acknowledged_and_no_commands_fail(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/hardware") as ws:
            ws.send_json(
                {
                    "v": 1,
                    "type": "hello",
                    "controller_id": "turret-1",
                    "firmware_version": "0.0.0-test",
                    "protocol_version": 1,
                    "capabilities": ["pan", "tilt", "valve"],
                }
            )
            assert ws.receive_json()["accepted"] is True

            # Answer whatever the server asks for, and record what that was.
            seen: list[str] = []
            for _ in range(6):
                message = ws.receive_json()
                seen.append(message["type"])
                if message.get("id") is not None and message["type"] != "ping":
                    ws.send_json({"v": 1, "type": "ack", "id": message["id"], "ok": True})
                if "set_config" in seen and "arm_output" in seen:
                    break

            assert "set_config" in seen, f"no configuration push; saw {seen}"
            assert "arm_output" in seen, f"output was never disarmed; saw {seen}"

            health = client.get("/api/health").json()
            assert health["checks"]["controller"] is True
            assert health["controller"]["commands_failed"] == 0, (
                "commands timed out even though the controller answered them"
            )

    def test_wrong_protocol_version_is_refused(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/hardware") as ws:
            ws.send_json({"v": 1, "type": "hello", "protocol_version": 99})
            reply = ws.receive_json()
            assert reply["accepted"] is False
            assert reply["reason"] == "protocol_version_mismatch"

    def test_first_frame_must_be_hello(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect as WSDisconnect

        with pytest.raises(WSDisconnect), client.websocket_connect("/ws/hardware") as ws:
            ws.send_json({"v": 1, "type": "status", "pan_deg": 0.0})
            ws.receive_json()


class TestSimulatedController:
    def test_full_control_path_and_calibration_are_isolated(self, client: TestClient) -> None:
        enabled = client.patch("/api/settings/controller", json={"mode": "simulated"})
        assert enabled.status_code == 200
        try:
            status = client.get("/api/control/status").json()
            assert status["controller_connected"] is True
            assert status["controller_simulated"] is True
            assert status["turret_point"] == pytest.approx([0.5, 0.5], abs=0.01)

            homed = client.post("/api/control/home", json={"axes": "both"})
            assert homed.status_code == 200
            assert homed.json()["homed"] is True

            moved = client.post(
                "/api/control/move_relative",
                json={"pan_delta_deg": 10, "tilt_delta_deg": 5},
            )
            assert moved.status_code == 200
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status = client.get("/api/control/status").json()
                if not status["moving"]:
                    break
                time.sleep(0.03)
            assert status["pan_deg"] == pytest.approx(10, abs=0.1)
            assert status["tilt_deg"] == pytest.approx(5, abs=0.1)
            assert status["turret_point"][0] > 0.5
            assert status["turret_point"][1] < 0.5

            jogged = client.post("/api/control/jog", json={"pan": 0.4, "tilt": -0.2})
            assert jogged.status_code == 200
            assert client.get("/api/control/status").json()["moving"] is True
            time.sleep(0.55)
            assert client.get("/api/control/status").json()["moving"] is False

            point = client.post(
                "/api/calibration/points",
                json={"x": 0.6, "y": 0.4, "surface": "default"},
            )
            assert point.status_code == 201
            assert point.json()["camera_id"].startswith("sim-")
            assert len(client.get("/api/calibration/points").json()) == 1
            assert client.delete("/api/calibration/points").json()["removed"] == 1
        finally:
            disabled = client.patch("/api/settings/controller", json={"mode": "physical"})
            assert disabled.status_code == 200

        status = client.get("/api/control/status").json()
        assert status["controller_connected"] is False
        assert status["controller_mode"] == "physical"
        assert client.get("/api/calibration/points").json() == []


class TestFrontendFallback:
    def test_unknown_api_route_is_404(self, client: TestClient) -> None:
        assert client.get("/api/does-not-exist").status_code == 404

    def test_spa_fallback_serves_html(self, client: TestClient) -> None:
        response = client.get("/some/deep/route")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
