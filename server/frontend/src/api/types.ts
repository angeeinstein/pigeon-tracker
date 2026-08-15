/** Types mirroring the server's JSON payloads. */

export interface VersionInfo {
  server_version: string;
  protocol_version: number;
  git_commit: string;
}

export interface TrackView {
  track_id: number;
  bbox: [number, number, number, number];
  confidence: number;
  class_name: string;
  hits: number;
  lost: boolean;
  confirmed: boolean;
  age_s: number;
}

export interface AimSolutionView {
  pan_deg: number;
  tilt_deg: number;
  extrapolated: boolean;
  surface: string;
  nearest_distance: number;
  strategy: string;
}

export interface TargetView {
  track_id: number;
  class: string;
  confidence: number;
  aim_norm: [number, number];
  solution: AimSolutionView | null;
}

export interface Telemetry {
  ts: number;
  system_state: string;
  state_reason: string | null;
  armed: boolean;
  auto_enabled: boolean;
  detection_enabled: boolean;
  spray_enabled: boolean;
  camera_connected: boolean;
  controller_connected: boolean;
  controller_mode: 'physical' | 'simulated';
  controller_simulated: boolean;
  controller_fault: string | null;
  pan_deg: number;
  tilt_deg: number;
  moving: boolean;
  homed: boolean;
  valve_open: boolean;
  estop: boolean;
  limits: { pan_min: boolean; pan_max: boolean; tilt_min: boolean; tilt_max: boolean };
  turret_point: [number, number] | null;
  target: TargetView | null;
  tracks: TrackView[];
  frame: { width: number; height: number; seq: number; inference_ms: number } | null;
  spray: {
    enabled: boolean;
    bursts_in_window: number;
    used_ms: number;
    budget_ms: number;
    window_s: number;
    seconds_since_last: number | null;
    ready: boolean;
  };
  targeting: {
    state: string;
    reason: string | null;
    target_track_id: number | null;
    aim_pan_deg: number | null;
    aim_tilt_deg: number | null;
    retries: number;
    state_age_s: number;
    engagements: number;
    sprays: number;
  };
}

export interface EventRecord {
  id?: number;
  ts: string;
  level: string;
  category: string;
  message: string;
  data: Record<string, unknown> | null;
  snapshot: string | null;
}

export type DetectionReviewStatus = 'unreviewed' | 'training' | 'rejected';
export type DetectionAnnotationStatus = 'unreviewed' | 'accepted' | 'rejected';

export interface DetectionAnnotation {
  bbox: [number, number, number, number];
  confidence: number | null;
  class_id: number | null;
  class_name: string;
  source: 'proposal' | 'manual' | 'motion' | 'motion_rescan';
  review_status: DetectionAnnotationStatus;
  review_label: string;
}

export interface DetectionCapture {
  id: number;
  ts: string;
  camera_id: string;
  trigger: 'detection' | 'manual' | 'motion-rescan';
  class_name: string;
  confidence: number | null;
  frame_seq: number;
  frame_width: number;
  frame_height: number;
  model_name: string;
  image_name: string;
  detections: DetectionAnnotation[];
  settings: Record<string, unknown>;
  review_status: DetectionReviewStatus;
  review_label: string;
  updated_at: string;
}

export interface DetectionCapturePage {
  items: DetectionCapture[];
  total: number;
  offset: number;
  limit: number;
}

export interface DetectionCaptureNavigation {
  capture: DetectionCapture;
  position: number;
  total: number;
  has_previous: boolean;
  has_next: boolean;
}

export interface CalibrationPoint {
  id: number;
  camera_id: string;
  surface: string;
  label: string;
  cam_x: number;
  cam_y: number;
  pan_deg: number;
  tilt_deg: number;
  enabled: boolean;
  created_at: string | null;
}

export type ZoneTypeName =
  | 'active'
  | 'no_target'
  | 'no_spray'
  | 'railing'
  | 'planter'
  | 'floor';

export interface ZoneRecord {
  id: number;
  camera_id: string;
  name: string;
  zone_type: ZoneTypeName;
  points: [number, number][];
  enabled: boolean;
  priority: number;
}

export interface Preset {
  id: number;
  name: string;
  pan_deg: number;
  tilt_deg: number;
}

export interface CameraStatus {
  camera_id: string;
  name: string;
  enabled: boolean;
  connected: boolean;
  backend: string;
  width: number;
  height: number;
  fps: number;
  frames: number;
  reconnects: number;
  last_frame_age_s: number | null;
  error: string | null;
}

export interface Health {
  status: string;
  checks: Record<string, boolean>;
  uptime_s: number;
  version: VersionInfo;
  armed: boolean;
  system_state: string;
  camera: { primary_id: string; connected: boolean; cameras: CameraStatus[] };
  controller: {
    mode: 'physical' | 'simulated';
    link: string;
    connected: boolean;
    ready: boolean;
    fault: string | null;
    rtt_ms: number | null;
    commands_sent: number;
    commands_failed: number;
    controller: {
      controller_id: string;
      firmware_version: string;
      protocol_version: number;
      capabilities: string[];
      hardware: Record<string, unknown>;
      connected_since_s: number | null;
    };
    state: Record<string, unknown>;
  };
  vision: Record<string, unknown>;
  database: Record<string, unknown>;
  calibration: { camera_id: string; calibrated: boolean; strategy: string; surfaces: unknown[] };
  telemetry_clients: number;
}

export interface DetectorCatalog {
  backend: string;
  loaded: boolean;
  device: string;
  model: string;
  classes: string[];
  available_classes: string[];
  configured_backend: string;
  configured_model: string;
  active_backend: string | null;
  active_model: string | null;
  catalog_current: boolean;
  reload_pending: boolean;
  reload_error: string | null;
  error: string | null;
  validation_available: boolean;
  configured_classes: string[];
  configured_target_classes: string[];
  invalid_detector_classes: string[];
  invalid_target_classes: string[];
  target_classes_excluded_by_detector: string[];
}

/** Settings sections. Kept loose where the UI only round-trips values. */
export interface CameraConfig {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  role: 'overview' | 'turret' | 'aux';
  backend: 'auto' | 'gstreamer' | 'opencv' | 'simulated';
  transport: 'tcp' | 'udp';
  latency_ms: number;
  target_width: number;
  reconnect_delay_s: number;
  stall_timeout_s: number;
}

export interface OnvifDevice {
  host: string;
  port: number;
  xaddr: string;
  xaddrs: string[];
  name: string;
  hardware: string;
  location: string;
  types: string[];
}

export interface OnvifProfile {
  token: string;
  name: string;
  uri: string;
  encoding: string;
  width: number;
  height: number;
  fps: number;
}

export interface OnvifProfileResult {
  device: {
    manufacturer: string;
    model: string;
    firmware: string;
    serial_number: string;
    host: string;
    xaddr: string;
  };
  profiles: OnvifProfile[];
}

export interface Settings {
  cameras: { sources: CameraConfig[]; primary_id: string };
  detector: {
    enabled: boolean;
    backend: 'yolo' | 'mock' | 'none';
    model_path: string;
    device: 'auto' | 'cpu' | 'cuda';
    capture_confidence: number;
    capture_enabled: boolean;
    capture_rearm_s: number;
    capture_jpeg_quality: number;
    confidence: number;
    iou: number;
    input_size: number;
    classes: string[];
    max_detections: number;
    fps: number;
    half: boolean;
  };
  tracker: {
    enabled: boolean;
    algorithm: string;
    track_thresh: number;
    low_thresh: number;
    match_thresh: number;
    track_buffer: number;
    min_hits: number;
  };
  scene_motion: {
    enabled: boolean;
    processing_width: number;
    history_frames: number;
    variance_threshold: number;
    detect_shadows: boolean;
    warmup_s: number;
    min_area_ratio: number;
    max_area_ratio: number;
    max_frame_change_ratio: number;
    min_fill_ratio: number;
    min_speed_ratio_s: number;
    max_speed_ratio_s: number;
    min_persistence_frames: number;
    crop_padding_ratio: number;
    min_crop_width_ratio: number;
    rescan_confidence: number;
    rescan_classes: string[];
    rescan_interval_s: number;
    max_rescans_per_event: number;
    event_rearm_s: number;
    max_regions: number;
    save_motion_evidence: boolean;
  };
  motion: {
    pan_min_deg: number;
    pan_max_deg: number;
    tilt_min_deg: number;
    tilt_max_deg: number;
    max_speed_deg_s: number;
    accel_deg_s2: number;
    manual_speed_deg_s: number;
    jog_ttl_ms: number;
    aim_tolerance_deg: number;
    park_pan_deg: number;
    park_tilt_deg: number;
    auto_home_on_connect: boolean;
  };
  controller: {
    mode: 'physical' | 'simulated';
    controller_id: string;
    status_timeout_s: number;
    command_timeout_s: number;
    home_timeout_s: number;
    ping_interval_s: number;
    push_config_on_connect: boolean;
    hardware: Record<string, number | boolean | string>;
  };
  spray: {
    enabled: boolean;
    default_duration_ms: number;
    max_duration_ms: number;
    min_interval_s: number;
    duty_budget_ms: number;
    duty_window_s: number;
    require_armed: boolean;
  };
  targeting: {
    auto_enabled: boolean;
    target_classes: string[];
    min_confidence: number;
    min_track_duration_s: number;
    detect_stability_s: number;
    selection: string;
    aim_y_ratio: number;
    aim_x_ratio: number;
    aim_pan_offset_deg: number;
    aim_tilt_offset_deg: number;
    aim_timeout_s: number;
    verify_duration_s: number;
    lost_grace_s: number;
    result_window_s: number;
    max_retries: number;
    cooldown_s: number;
    require_active_zone: boolean;
    continuous_tracking: boolean;
    retarget_deadband_deg: number;
    snapshot_on_engage: boolean;
  };
  ui: {
    preview_fps: number;
    preview_quality: number;
    preview_width: number;
    draw_overlays: boolean;
    draw_zones: boolean;
    telemetry_hz: number;
  };
  system: {
    event_retention_days: number;
    max_events: number;
    snapshot_retention_days: number;
    max_snapshot_mb: number;
    detection_retention_days: number;
    max_detection_mb: number;
  };
}

export type SettingsSection = keyof Settings;
