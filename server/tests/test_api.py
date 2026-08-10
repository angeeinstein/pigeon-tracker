"""End-to-end API smoke tests.

The whole application is started — settings, database, simulated camera, mock
detector, targeting loop — with no hardware and no model. If this passes, a
fresh install will at least come up and be controllable.
"""

from __future__ import annotations

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


class TestSettings:
    def test_read_all(self, client: TestClient) -> None:
        payload = client.get("/api/settings").json()
        assert "targeting" in payload and "motion" in payload

    def test_patch_and_read_back(self, client: TestClient) -> None:
        response = client.patch("/api/settings/ui", json={"preview_fps": 4})
        assert response.status_code == 200
        assert client.get("/api/settings/ui").json()["preview_fps"] == 4

    def test_invalid_patch_is_rejected(self, client: TestClient) -> None:
        response = client.patch("/api/settings/motion", json={"max_speed_deg_s": -1})
        assert response.status_code == 422

    def test_unknown_section(self, client: TestClient) -> None:
        assert client.get("/api/settings/nope").status_code == 404

    def test_schema_endpoint(self, client: TestClient) -> None:
        payload = client.get("/api/settings-schema").json()
        assert "properties" in payload["spray"]


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


class TestFrontendFallback:
    def test_unknown_api_route_is_404(self, client: TestClient) -> None:
        assert client.get("/api/does-not-exist").status_code == 404

    def test_spa_fallback_serves_html(self, client: TestClient) -> None:
        response = client.get("/some/deep/route")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
