"""Settings store: validation, persistence, partial updates, notifications."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.settings import SettingsError, SettingsStore, deep_merge
from app.services.settings_schema import AppSettings, MotionSettings, SceneMotionSettings


class TestDeepMerge:
    def test_merges_nested_dicts(self) -> None:
        result = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}})
        assert result == {"a": {"b": 9, "c": 2}}

    def test_replaces_lists_wholesale(self) -> None:
        # Merging lists element-wise would make removing a camera impossible.
        result = deep_merge({"items": [1, 2, 3]}, {"items": [4]})
        assert result == {"items": [4]}


class TestDefaults:
    def test_defaults_are_safe(self) -> None:
        settings = AppSettings()
        assert settings.spray.enabled is False, "water must be off out of the box"
        assert settings.targeting.auto_enabled is False
        assert settings.controller.mode == "physical"
        assert settings.controller.hardware.allow_unhomed_motion is False
        assert settings.detector.device == "cpu"
        assert settings.detector.input_size == 960
        assert settings.detector.capture_confidence < settings.detector.confidence
        assert settings.scene_motion.enabled is True
        assert settings.scene_motion.rescan_confidence < settings.detector.capture_confidence

    def test_motion_limits_are_validated(self) -> None:
        with pytest.raises(ValueError, match="pan_min_deg"):
            MotionSettings(pan_min_deg=50, pan_max_deg=10)

    def test_scene_motion_area_limits_are_validated(self) -> None:
        with pytest.raises(ValueError, match="min_area_ratio"):
            SceneMotionSettings(min_area_ratio=0.2, max_area_ratio=0.1)

    def test_clamp(self) -> None:
        motion = MotionSettings(pan_min_deg=-30, pan_max_deg=30, tilt_min_deg=-10, tilt_max_deg=10)
        assert motion.clamp(100, -100) == (30, -10)
        assert motion.within_limits(0, 0) is True
        assert motion.within_limits(100, 0) is False

    def test_default_spray_duration_cannot_exceed_the_maximum(self) -> None:
        from app.services.settings_schema import SpraySettings

        settings = SpraySettings(default_duration_ms=5000, max_duration_ms=800)
        assert settings.default_duration_ms == 800


class TestStore:
    async def test_load_returns_defaults_on_an_empty_database(self, temp_database: Path) -> None:
        store = SettingsStore()
        settings = await store.load()
        assert settings.detector.enabled is True
        assert settings.spray.enabled is False

    async def test_update_persists_and_survives_a_reload(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        await store.update_section("motion", {"max_speed_deg_s": 42.0})

        reloaded = SettingsStore()
        settings = await reloaded.load()
        assert settings.motion.max_speed_deg_s == 42.0

    async def test_scene_motion_settings_persist(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        await store.update_section(
            "scene_motion", {"processing_width": 640, "max_rescans_per_event": 5}
        )

        reloaded = SettingsStore()
        settings = await reloaded.load()
        assert settings.scene_motion.processing_width == 640
        assert settings.scene_motion.max_rescans_per_event == 5

    async def test_partial_update_keeps_other_fields(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        before = store.current.motion.accel_deg_s2
        await store.update_section("motion", {"max_speed_deg_s": 33.0})
        assert store.current.motion.accel_deg_s2 == before

    async def test_invalid_update_is_rejected_and_changes_nothing(
        self, temp_database: Path
    ) -> None:
        store = SettingsStore()
        await store.load()
        with pytest.raises(SettingsError):
            await store.update_section("motion", {"max_speed_deg_s": -5})
        assert store.current.motion.max_speed_deg_s > 0

    async def test_unknown_section_is_rejected(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        with pytest.raises(SettingsError, match="unknown settings section"):
            await store.update_section("nonsense", {})

    async def test_listeners_are_told_what_changed(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        seen: list[set[str]] = []

        async def listener(_settings: AppSettings, changed: set[str]) -> None:
            seen.append(changed)

        store.subscribe(listener)
        await store.update_section("ui", {"preview_fps": 5})
        assert seen == [{"ui"}]

    async def test_no_notification_when_nothing_actually_changed(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        seen: list[set[str]] = []

        async def listener(_settings: AppSettings, changed: set[str]) -> None:
            seen.append(changed)

        store.subscribe(listener)
        await store.update_section("ui", {"preview_fps": store.current.ui.preview_fps})
        assert seen == []

    async def test_reset_restores_defaults(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        await store.update_section("ui", {"preview_fps": 3})
        await store.reset_section("ui")
        assert store.current.ui.preview_fps == 12.0

    async def test_camera_ids_must_be_unique(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        with pytest.raises(SettingsError, match="unique"):
            await store.update_section(
                "cameras",
                {
                    "sources": [
                        {"id": "a", "name": "A"},
                        {"id": "a", "name": "B"},
                    ]
                },
            )

    async def test_primary_id_follows_a_removed_camera(self, temp_database: Path) -> None:
        store = SettingsStore()
        await store.load()
        await store.update_section(
            "cameras",
            {"sources": [{"id": "turret-cam", "name": "Turret"}], "primary_id": "overview"},
        )
        assert store.current.cameras.primary_id == "turret-cam"
