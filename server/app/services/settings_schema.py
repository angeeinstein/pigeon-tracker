"""Runtime settings schema.

Everything in here is user-editable from the web UI and stored in SQLite, one
JSON document per section. Defaults are deliberately *safe*: automatic mode
off, spray disabled, conservative speeds, tight spray limits. A fresh install
never sprays until someone deliberately arms it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# Cameras
# --------------------------------------------------------------------------

CameraBackend = Literal["auto", "gstreamer", "opencv", "simulated"]
CameraRole = Literal["overview", "turret", "aux"]


class CameraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="overview", min_length=1, max_length=64)
    name: str = Field(default="Overview camera", max_length=128)
    #: RTSP URL. May contain ${ENV_VAR} placeholders so credentials can live in
    #: the protected environment file instead of the database.
    url: str = Field(default="", max_length=1024)
    enabled: bool = True
    role: CameraRole = "overview"
    backend: CameraBackend = "auto"
    #: RTSP transport. TCP is more reliable over Wi-Fi; UDP has lower latency.
    transport: Literal["tcp", "udp"] = "tcp"
    #: Jitter buffer for the GStreamer pipeline, milliseconds. Lower = fresher.
    latency_ms: int = Field(default=100, ge=0, le=2000)
    #: Optional downscale applied right after decode (0 = keep native size).
    target_width: int = Field(default=1280, ge=0, le=7680)
    reconnect_delay_s: float = Field(default=3.0, ge=0.5, le=60.0)
    #: Give up on a stalled stream after this long without a frame.
    stall_timeout_s: float = Field(default=8.0, ge=1.0, le=120.0)

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not all(ch.isalnum() or ch in "-_" for ch in value):
            raise ValueError("camera id may only contain letters, digits, '-' and '_'")
        return value


class CamerasSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[CameraConfig] = Field(
        default_factory=lambda: [CameraConfig()],
        max_length=8,
    )
    #: The camera detection and calibration operate on.
    primary_id: str = "overview"

    @model_validator(mode="after")
    def _check_ids(self) -> CamerasSettings:
        ids = [c.id for c in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("camera ids must be unique")
        if self.sources and self.primary_id not in ids:
            self.primary_id = ids[0]
        return self

    def get(self, camera_id: str) -> CameraConfig | None:
        return next((c for c in self.sources if c.id == camera_id), None)


# --------------------------------------------------------------------------
# Detection & tracking
# --------------------------------------------------------------------------


class DetectorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    backend: Literal["yolo", "mock", "none"] = "yolo"
    #: Filename inside the models directory, or an absolute path. Ultralytics
    #: downloads well-known names (e.g. "yolov8n.pt") on first use.
    model_path: str = "yolov8n.pt"
    device: Literal["auto", "cpu", "cuda"] = "cpu"
    #: Proposals at or above this threshold are available for evidence capture.
    #: Tracking still uses ``confidence`` below.
    capture_confidence: float = Field(default=0.10, ge=0.01, le=0.99)
    capture_enabled: bool = True
    #: The same class must disappear for this long before a new capture is made.
    capture_rearm_s: float = Field(default=5.0, ge=1.0, le=3600.0)
    capture_jpeg_quality: int = Field(default=90, ge=40, le=100)
    confidence: float = Field(default=0.35, ge=0.01, le=0.99)
    iou: float = Field(default=0.45, ge=0.05, le=0.95)
    input_size: int = Field(default=960, ge=160, le=1920)
    #: Class names kept after inference. Empty list = keep everything.
    classes: list[str] = Field(default_factory=lambda: ["bird"])
    max_detections: int = Field(default=32, ge=1, le=300)
    #: Inference rate. Real-time behaviour matters more than every frame.
    fps: float = Field(default=6.0, ge=0.2, le=60.0)
    #: Half precision on CUDA. Ignored on CPU.
    half: bool = False


class TrackerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: Only ByteTrack is implemented (see app/vision/tracker.py). The field
    #: exists so a second algorithm can be added without a schema migration.
    algorithm: Literal["bytetrack"] = "bytetrack"
    #: Detections above this confidence start new tracks.
    track_thresh: float = Field(default=0.5, ge=0.05, le=0.95)
    #: Low-confidence detections above this are used for association only.
    low_thresh: float = Field(default=0.1, ge=0.01, le=0.9)
    #: IoU distance threshold for matching.
    match_thresh: float = Field(default=0.8, ge=0.1, le=0.99)
    #: Frames a lost track survives before it is removed.
    track_buffer: int = Field(default=30, ge=1, le=300)
    #: Frames a new track must be seen before it is reported as confirmed.
    min_hits: int = Field(default=3, ge=1, le=30)


# --------------------------------------------------------------------------
# Motion
# --------------------------------------------------------------------------


class MotionSettings(BaseModel):
    """Server-side motion policy.

    The controller enforces its own soft limits; these are the limits the UI
    and the targeting logic obey, and they are pushed to the controller when
    ``controller.push_config_on_connect`` is enabled.
    """

    model_config = ConfigDict(extra="forbid")

    pan_min_deg: float = Field(default=-90.0, ge=-360.0, le=360.0)
    pan_max_deg: float = Field(default=90.0, ge=-360.0, le=360.0)
    tilt_min_deg: float = Field(default=-45.0, ge=-180.0, le=180.0)
    tilt_max_deg: float = Field(default=45.0, ge=-180.0, le=180.0)
    max_speed_deg_s: float = Field(default=60.0, gt=0, le=720.0)
    accel_deg_s2: float = Field(default=180.0, gt=0, le=5000.0)
    #: Speed used for jog/joystick input at 100 % deflection.
    manual_speed_deg_s: float = Field(default=25.0, gt=0, le=720.0)
    #: A jog command expires after this long without a refresh (failsafe).
    jog_ttl_ms: int = Field(default=400, ge=100, le=2000)
    #: Automatic tracking is considered "on target" within this error.
    aim_tolerance_deg: float = Field(default=1.5, gt=0, le=30.0)
    #: Where the turret goes on "Center"/park.
    park_pan_deg: float = 0.0
    park_tilt_deg: float = 0.0
    #: Home automatically as soon as the controller connects.
    auto_home_on_connect: bool = False

    @model_validator(mode="after")
    def _check_ranges(self) -> MotionSettings:
        if self.pan_min_deg >= self.pan_max_deg:
            raise ValueError("pan_min_deg must be less than pan_max_deg")
        if self.tilt_min_deg >= self.tilt_max_deg:
            raise ValueError("tilt_min_deg must be less than tilt_max_deg")
        return self

    def clamp(self, pan_deg: float, tilt_deg: float) -> tuple[float, float]:
        return (
            min(max(pan_deg, self.pan_min_deg), self.pan_max_deg),
            min(max(tilt_deg, self.tilt_min_deg), self.tilt_max_deg),
        )

    def within_limits(self, pan_deg: float, tilt_deg: float) -> bool:
        return (
            self.pan_min_deg <= pan_deg <= self.pan_max_deg
            and self.tilt_min_deg <= tilt_deg <= self.tilt_max_deg
        )


# --------------------------------------------------------------------------
# Controller (ESP32)
# --------------------------------------------------------------------------


class ControllerHardwareConfig(BaseModel):
    """Mirror of the controller's persisted configuration (see docs/PROTOCOL.md §6)."""

    model_config = ConfigDict(extra="forbid")

    steps_per_rev: int = Field(default=200, ge=1, le=10000)
    pan_microsteps: int = Field(default=16, ge=1, le=256)
    tilt_microsteps: int = Field(default=16, ge=1, le=256)
    #: Motor revolutions per output revolution.
    pan_gear_ratio: float = Field(default=1.0, gt=0, le=1000.0)
    tilt_gear_ratio: float = Field(default=1.0, gt=0, le=1000.0)
    pan_invert: bool = False
    tilt_invert: bool = False
    homing_speed_deg_s: float = Field(default=15.0, gt=0, le=200.0)
    homing_backoff_deg: float = Field(default=3.0, gt=0, le=45.0)
    pan_home_dir: Literal[-1, 1] = -1
    tilt_home_dir: Literal[-1, 1] = -1
    pan_home_offset_deg: float = 0.0
    tilt_home_offset_deg: float = 0.0
    endstop_active_low: bool = True
    allow_unhomed_motion: bool = False


class ControllerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Use the in-process ESP32/turret emulator while the mechanics are absent.
    #: Physical remains the safe default so an upgrade never silently swaps a
    #: real controller for a simulated one.
    mode: Literal["physical", "simulated"] = "physical"
    #: Expected controller id; a mismatch is logged and surfaced in the UI.
    controller_id: str = Field(default="turret-1", max_length=64)
    #: Link is considered dead without a status frame for this long.
    status_timeout_s: float = Field(default=3.0, ge=0.5, le=60.0)
    #: How long to wait for a command acknowledgement.
    command_timeout_s: float = Field(default=5.0, ge=0.5, le=120.0)
    #: Homing may legitimately take a while.
    home_timeout_s: float = Field(default=90.0, ge=5.0, le=600.0)
    ping_interval_s: float = Field(default=2.0, ge=0.5, le=30.0)
    #: Push motion/hardware config to the controller when it connects.
    push_config_on_connect: bool = True
    hardware: ControllerHardwareConfig = Field(default_factory=ControllerHardwareConfig)


# --------------------------------------------------------------------------
# Spray / water output
# --------------------------------------------------------------------------


class SpraySettings(BaseModel):
    """Water output limits. The controller enforces its own hard clamp too."""

    model_config = ConfigDict(extra="forbid")

    #: Master switch for the water function. Off = pure camera turret.
    enabled: bool = False
    default_duration_ms: int = Field(default=400, ge=20, le=10_000)
    #: Hard clamp on any single spray, manual or automatic.
    max_duration_ms: int = Field(default=1500, ge=20, le=10_000)
    #: Minimum time between two sprays.
    min_interval_s: float = Field(default=3.0, ge=0.0, le=3600.0)
    #: Cumulative open time budget within ``duty_window_s``.
    duty_budget_ms: int = Field(default=6000, ge=0, le=600_000)
    duty_window_s: float = Field(default=300.0, ge=1.0, le=86_400.0)
    #: Refuse to spray if a manual/automatic burst is already in flight.
    require_armed: bool = True

    @model_validator(mode="after")
    def _check(self) -> SpraySettings:
        if self.default_duration_ms > self.max_duration_ms:
            self.default_duration_ms = self.max_duration_ms
        return self


# --------------------------------------------------------------------------
# Automatic targeting
# --------------------------------------------------------------------------


class TargetingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Master switch for the automatic state machine. Arming is separate.
    auto_enabled: bool = False
    #: Detector classes considered valid targets.
    target_classes: list[str] = Field(default_factory=lambda: ["bird"])
    min_confidence: float = Field(default=0.5, ge=0.01, le=0.99)
    #: A track must exist continuously for this long before it is a candidate.
    min_track_duration_s: float = Field(default=1.0, ge=0.0, le=60.0)
    #: The *same* candidate must stay selected this long before engaging.
    #: Guards against flapping between two birds (min_track_duration_s is
    #: about the track's own age, this is about selection stability).
    detect_stability_s: float = Field(default=0.5, ge=0.0, le=30.0)
    #: Selection policy when several candidates qualify.
    selection: Literal["highest_confidence", "largest", "closest_to_center", "oldest"] = (
        "highest_confidence"
    )
    #: Vertical aim point inside the bounding box (0 = top, 1 = bottom).
    aim_y_ratio: float = Field(default=0.65, ge=0.0, le=1.0)
    aim_x_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Fixed correction applied after mapping, e.g. for nozzle ballistics.
    aim_pan_offset_deg: float = Field(default=0.0, ge=-45.0, le=45.0)
    aim_tilt_offset_deg: float = Field(default=0.0, ge=-45.0, le=45.0)
    #: Give up on aiming if the turret cannot settle in time.
    aim_timeout_s: float = Field(default=6.0, ge=0.5, le=120.0)
    #: Target must still be present and valid for this long before spraying.
    verify_duration_s: float = Field(default=0.4, ge=0.0, le=30.0)
    #: How long a target may be missing before it counts as lost.
    lost_grace_s: float = Field(default=1.5, ge=0.0, le=60.0)
    #: After a spray, watch this long to see whether the bird left.
    result_window_s: float = Field(default=2.5, ge=0.1, le=60.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    cooldown_s: float = Field(default=20.0, ge=0.0, le=3600.0)
    #: Only engage targets inside an ACTIVE zone (if any active zone exists).
    require_active_zone: bool = True
    #: Follow the target continuously while tracking, not just once.
    continuous_tracking: bool = True
    #: Re-issue a move only if the aim point moved at least this much.
    retarget_deadband_deg: float = Field(default=0.75, ge=0.0, le=20.0)
    #: Save a camera snapshot on engagement events.
    snapshot_on_engage: bool = True


# --------------------------------------------------------------------------
# UI / preview
# --------------------------------------------------------------------------


class UiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_fps: float = Field(default=12.0, ge=1.0, le=30.0)
    preview_quality: int = Field(default=70, ge=10, le=95)
    preview_width: int = Field(default=960, ge=160, le=3840)
    #: Draw boxes/aim point server-side into the preview stream.
    draw_overlays: bool = True
    draw_zones: bool = True
    telemetry_hz: float = Field(default=8.0, ge=1.0, le=30.0)


class SystemSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_retention_days: int = Field(default=30, ge=1, le=3650)
    max_events: int = Field(default=20_000, ge=100, le=1_000_000)
    snapshot_retention_days: int = Field(default=7, ge=1, le=365)
    max_snapshot_mb: int = Field(default=512, ge=16, le=100_000)
    detection_retention_days: int = Field(default=90, ge=1, le=3650)
    max_detection_mb: int = Field(default=2048, ge=64, le=1_000_000)


# --------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------


class AppSettings(BaseModel):
    """The complete runtime configuration."""

    model_config = ConfigDict(extra="forbid")

    cameras: CamerasSettings = Field(default_factory=CamerasSettings)
    detector: DetectorSettings = Field(default_factory=DetectorSettings)
    tracker: TrackerSettings = Field(default_factory=TrackerSettings)
    motion: MotionSettings = Field(default_factory=MotionSettings)
    controller: ControllerSettings = Field(default_factory=ControllerSettings)
    spray: SpraySettings = Field(default_factory=SpraySettings)
    targeting: TargetingSettings = Field(default_factory=TargetingSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    system: SystemSettings = Field(default_factory=SystemSettings)


#: Section name -> model, used by the settings store and the REST API.
SECTION_MODELS: dict[str, type[BaseModel]] = {
    name: field.annotation  # type: ignore[misc]
    for name, field in AppSettings.model_fields.items()
}
