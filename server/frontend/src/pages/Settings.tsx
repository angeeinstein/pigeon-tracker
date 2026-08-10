import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type {
  CameraConfig,
  OnvifDevice,
  OnvifProfile,
  OnvifProfileResult,
  Settings as SettingsType,
} from '../api/types';
import { Banner, Card, NumberField, SelectField, Spinner, TextField, Toggle } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { useToast } from '../state';

type SectionName = keyof SettingsType;

const TABS: { id: SectionName; label: string }[] = [
  { id: 'cameras', label: 'Camera' },
  { id: 'detector', label: 'AI' },
  { id: 'tracker', label: 'Tracking' },
  { id: 'motion', label: 'Motion' },
  { id: 'controller', label: 'Controller' },
  { id: 'spray', label: 'Spray' },
  { id: 'targeting', label: 'Targeting' },
  { id: 'ui', label: 'Interface' },
  { id: 'system', label: 'Retention' },
];

export default function Settings() {
  const settings = useAsync(() => api.settings(), []);
  const [tab, setTab] = useState<SectionName>('cameras');

  if (settings.loading) return <Spinner />;
  if (settings.error || !settings.data) return <Banner>{settings.error ?? 'no settings'}</Banner>;

  return (
    <div className="space-y-4">
      <nav className="grid grid-cols-3 gap-1 pb-1 sm:flex sm:overflow-x-auto">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            onClick={() => setTab(entry.id)}
            className={`whitespace-nowrap rounded-lg px-2 py-1.5 text-center text-sm transition sm:px-3 ${
              tab === entry.id ? 'bg-panelalt text-ink' : 'text-muted hover:text-ink'
            }`}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <SectionEditor key={tab} section={tab} settings={settings.data} onSaved={settings.reload} />
    </div>
  );
}

function SectionEditor({
  section,
  settings,
  onSaved,
}: {
  section: SectionName;
  settings: SettingsType;
  onSaved: () => void;
}) {
  const { attempt } = useToast();
  const [draft, setDraft] = useState<SettingsType[SectionName]>(settings[section]);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setDraft(settings[section]);
    setDirty(false);
  }, [section, settings]);

  const update = (patch: Record<string, unknown>) => {
    setDraft(
      (current) => ({ ...(current as object), ...patch }) as unknown as SettingsType[SectionName],
    );
    setDirty(true);
  };

  const save = async () => {
    const ok = await attempt(
      () => api.patchSettings(section, draft as never),
      'settings saved',
    );
    if (ok) {
      setDirty(false);
      onSaved();
    }
  };

  const reset = async () => {
    if (!window.confirm(`Reset "${section}" to defaults?`)) return;
    if (await attempt(() => api.resetSettings(section), 'section reset')) onSaved();
  };

  return (
    <Card
      title={section}
      actions={
        <div className="flex gap-2">
          <button className="btn px-2 py-1 text-xs" onClick={reset}>
            Reset to defaults
          </button>
          <button className="btn btn-primary px-3 py-1 text-xs" disabled={!dirty} onClick={save}>
            Save
          </button>
        </div>
      }
    >
      <div className="grid gap-x-6 md:grid-cols-2">
        <Fields section={section} draft={draft} update={update} onReload={onSaved} />
      </div>
    </Card>
  );
}

function Fields({
  section,
  draft,
  update,
  onReload,
}: {
  section: SectionName;
  draft: unknown;
  update: (patch: Record<string, unknown>) => void;
  onReload: () => void;
}) {
  switch (section) {
    case 'cameras':
      return (
        <CameraFields
          draft={draft as SettingsType['cameras']}
          update={update}
          onReload={onReload}
        />
      );
    case 'detector': {
      const value = draft as SettingsType['detector'];
      return (
        <>
          <Toggle
            label="Detection enabled"
            checked={value.enabled}
            onChange={(v) => update({ enabled: v })}
          />
          <SelectField
            label="Backend"
            value={value.backend}
            options={[
              { value: 'yolo', label: 'YOLO (ultralytics)' },
              { value: 'mock', label: 'Mock (development)' },
              { value: 'none', label: 'Disabled' },
            ]}
            onChange={(v) => update({ backend: v })}
          />
          <TextField
            label="Model"
            value={value.model_path}
            onChange={(v) => update({ model_path: v })}
            hint="File in the models directory, an absolute path, or a name ultralytics can fetch (yolov8n.pt)."
          />
          <SelectField
            label="Device"
            value={value.device}
            options={[
              { value: 'auto', label: 'auto (CUDA if available)' },
              { value: 'cpu', label: 'CPU' },
              { value: 'cuda', label: 'CUDA' },
            ]}
            onChange={(v) => update({ device: v })}
          />
          <NumberField
            label="Confidence threshold"
            value={value.confidence}
            step={0.05}
            min={0.01}
            max={0.99}
            onChange={(v) => update({ confidence: v })}
          />
          <NumberField
            label="NMS IoU"
            value={value.iou}
            step={0.05}
            min={0.05}
            max={0.95}
            onChange={(v) => update({ iou: v })}
          />
          <NumberField
            label="Inference rate"
            suffix="fps"
            value={value.fps}
            step={0.5}
            min={0.2}
            onChange={(v) => update({ fps: v })}
            hint="Real-time behaviour matters more than processing every camera frame."
          />
          <NumberField
            label="Input size"
            suffix="px"
            value={value.input_size}
            step={32}
            onChange={(v) => update({ input_size: v })}
          />
          <TextField
            label="Classes"
            value={value.classes.join(', ')}
            onChange={(v) =>
              update({ classes: v.split(',').map((s) => s.trim()).filter(Boolean) })
            }
            hint="Comma separated. Empty keeps every class the model produces."
          />
          <Toggle
            label="Half precision (CUDA only)"
            checked={value.half}
            onChange={(v) => update({ half: v })}
          />
        </>
      );
    }
    case 'tracker': {
      const value = draft as SettingsType['tracker'];
      return (
        <>
          <Toggle
            label="Tracking enabled"
            hint="Automatic targeting requires tracking: it needs stable ids."
            checked={value.enabled}
            onChange={(v) => update({ enabled: v })}
          />
          <NumberField
            label="Track threshold"
            value={value.track_thresh}
            step={0.05}
            onChange={(v) => update({ track_thresh: v })}
          />
          <NumberField
            label="Low threshold"
            value={value.low_thresh}
            step={0.05}
            onChange={(v) => update({ low_thresh: v })}
            hint="Low-confidence detections are used for association only (ByteTrack's second pass)."
          />
          <NumberField
            label="Match threshold (IoU distance)"
            value={value.match_thresh}
            step={0.05}
            onChange={(v) => update({ match_thresh: v })}
          />
          <NumberField
            label="Track buffer"
            suffix="frames"
            value={value.track_buffer}
            onChange={(v) => update({ track_buffer: v })}
          />
          <NumberField
            label="Minimum hits to confirm"
            value={value.min_hits}
            onChange={(v) => update({ min_hits: v })}
          />
        </>
      );
    }
    case 'motion': {
      const value = draft as SettingsType['motion'];
      return (
        <>
          <NumberField
            label="Pan minimum"
            suffix="°"
            value={value.pan_min_deg}
            onChange={(v) => update({ pan_min_deg: v })}
          />
          <NumberField
            label="Pan maximum"
            suffix="°"
            value={value.pan_max_deg}
            onChange={(v) => update({ pan_max_deg: v })}
          />
          <NumberField
            label="Tilt minimum"
            suffix="°"
            value={value.tilt_min_deg}
            onChange={(v) => update({ tilt_min_deg: v })}
          />
          <NumberField
            label="Tilt maximum"
            suffix="°"
            value={value.tilt_max_deg}
            onChange={(v) => update({ tilt_max_deg: v })}
          />
          <NumberField
            label="Maximum speed"
            suffix="°/s"
            value={value.max_speed_deg_s}
            onChange={(v) => update({ max_speed_deg_s: v })}
          />
          <NumberField
            label="Acceleration"
            suffix="°/s²"
            value={value.accel_deg_s2}
            onChange={(v) => update({ accel_deg_s2: v })}
          />
          <NumberField
            label="Manual/jog speed"
            suffix="°/s"
            value={value.manual_speed_deg_s}
            onChange={(v) => update({ manual_speed_deg_s: v })}
          />
          <NumberField
            label="Jog timeout"
            suffix="ms"
            value={value.jog_ttl_ms}
            step={50}
            onChange={(v) => update({ jog_ttl_ms: v })}
            hint="The controller stops if no new jog arrives within this window."
          />
          <NumberField
            label="Aim tolerance"
            suffix="°"
            value={value.aim_tolerance_deg}
            step={0.1}
            onChange={(v) => update({ aim_tolerance_deg: v })}
          />
          <NumberField
            label="Park pan"
            suffix="°"
            value={value.park_pan_deg}
            onChange={(v) => update({ park_pan_deg: v })}
          />
          <NumberField
            label="Park tilt"
            suffix="°"
            value={value.park_tilt_deg}
            onChange={(v) => update({ park_tilt_deg: v })}
          />
          <Toggle
            label="Home automatically when the controller connects"
            checked={value.auto_home_on_connect}
            onChange={(v) => update({ auto_home_on_connect: v })}
          />
        </>
      );
    }
    case 'controller': {
      const value = draft as SettingsType['controller'];
      const hardware = value.hardware as Record<string, number | boolean | string>;
      const setHardware = (patch: Record<string, unknown>) =>
        update({ hardware: { ...hardware, ...patch } });
      const num = (key: string) => Number(hardware[key] ?? 0);
      return (
        <>
          <TextField
            label="Controller id"
            value={value.controller_id}
            onChange={(v) => update({ controller_id: v })}
          />
          <NumberField
            label="Status timeout"
            suffix="s"
            value={value.status_timeout_s}
            step={0.5}
            onChange={(v) => update({ status_timeout_s: v })}
          />
          <NumberField
            label="Command timeout"
            suffix="s"
            value={value.command_timeout_s}
            step={0.5}
            onChange={(v) => update({ command_timeout_s: v })}
          />
          <NumberField
            label="Homing timeout"
            suffix="s"
            value={value.home_timeout_s}
            step={5}
            onChange={(v) => update({ home_timeout_s: v })}
          />
          <NumberField
            label="Ping interval"
            suffix="s"
            value={value.ping_interval_s}
            step={0.5}
            onChange={(v) => update({ ping_interval_s: v })}
          />
          <Toggle
            label="Push configuration on connect"
            checked={value.push_config_on_connect}
            onChange={(v) => update({ push_config_on_connect: v })}
          />
          <div className="md:col-span-2 mt-3 border-t border-edge pt-3">
            <p className="card-title">Mechanics (sent to the controller)</p>
          </div>
          <NumberField
            label="Motor steps per revolution"
            value={num('steps_per_rev')}
            onChange={(v) => setHardware({ steps_per_rev: v })}
          />
          <NumberField
            label="Pan microsteps"
            value={num('pan_microsteps')}
            onChange={(v) => setHardware({ pan_microsteps: v })}
          />
          <NumberField
            label="Tilt microsteps"
            value={num('tilt_microsteps')}
            onChange={(v) => setHardware({ tilt_microsteps: v })}
          />
          <NumberField
            label="Pan gear ratio"
            value={num('pan_gear_ratio')}
            step={0.1}
            onChange={(v) => setHardware({ pan_gear_ratio: v })}
            hint="Motor revolutions per output revolution."
          />
          <NumberField
            label="Tilt gear ratio"
            value={num('tilt_gear_ratio')}
            step={0.1}
            onChange={(v) => setHardware({ tilt_gear_ratio: v })}
          />
          <NumberField
            label="Homing speed"
            suffix="°/s"
            value={num('homing_speed_deg_s')}
            onChange={(v) => setHardware({ homing_speed_deg_s: v })}
          />
          <NumberField
            label="Homing back-off"
            suffix="°"
            value={num('homing_backoff_deg')}
            step={0.5}
            onChange={(v) => setHardware({ homing_backoff_deg: v })}
          />
          <NumberField
            label="Pan home offset"
            suffix="°"
            value={num('pan_home_offset_deg')}
            onChange={(v) => setHardware({ pan_home_offset_deg: v })}
          />
          <NumberField
            label="Tilt home offset"
            suffix="°"
            value={num('tilt_home_offset_deg')}
            onChange={(v) => setHardware({ tilt_home_offset_deg: v })}
          />
          <Toggle
            label="Pan homes toward maximum endstop"
            hint="Off means the minimum endstop. This must match the physical switch position."
            checked={num('pan_home_dir') === 1}
            onChange={(v) => setHardware({ pan_home_dir: v ? 1 : -1 })}
          />
          <Toggle
            label="Tilt homes toward maximum endstop"
            hint="Off means the minimum endstop. This must match the physical switch position."
            checked={num('tilt_home_dir') === 1}
            onChange={(v) => setHardware({ tilt_home_dir: v ? 1 : -1 })}
          />
          <Toggle
            label="Invert pan direction"
            checked={Boolean(hardware.pan_invert)}
            onChange={(v) => setHardware({ pan_invert: v })}
          />
          <Toggle
            label="Invert tilt direction"
            checked={Boolean(hardware.tilt_invert)}
            onChange={(v) => setHardware({ tilt_invert: v })}
          />
          <Toggle
            label="Endstops active low"
            checked={Boolean(hardware.endstop_active_low)}
            onChange={(v) => setHardware({ endstop_active_low: v })}
          />
          <Toggle
            label="Allow motion before homing"
            hint="Leave off. Absolute moves without a reference are how mechanics get bent."
            checked={Boolean(hardware.allow_unhomed_motion)}
            onChange={(v) => setHardware({ allow_unhomed_motion: v })}
          />
        </>
      );
    }
    case 'spray': {
      const value = draft as SettingsType['spray'];
      return (
        <>
          <Toggle
            label="Water output enabled"
            hint="Master switch. Off makes this a pure camera turret."
            checked={value.enabled}
            onChange={(v) => update({ enabled: v })}
          />
          <NumberField
            label="Default duration"
            suffix="ms"
            value={value.default_duration_ms}
            step={50}
            onChange={(v) => update({ default_duration_ms: v })}
          />
          <NumberField
            label="Maximum single burst"
            suffix="ms"
            value={value.max_duration_ms}
            step={50}
            onChange={(v) => update({ max_duration_ms: v })}
            hint="Hard clamp. The firmware enforces its own limit independently."
          />
          <NumberField
            label="Minimum interval"
            suffix="s"
            value={value.min_interval_s}
            step={0.5}
            onChange={(v) => update({ min_interval_s: v })}
          />
          <NumberField
            label="Duty budget"
            suffix="ms"
            value={value.duty_budget_ms}
            step={100}
            onChange={(v) => update({ duty_budget_ms: v })}
            hint="Total valve-open time allowed within the window below."
          />
          <NumberField
            label="Duty window"
            suffix="s"
            value={value.duty_window_s}
            step={10}
            onChange={(v) => update({ duty_window_s: v })}
          />
        </>
      );
    }
    case 'targeting': {
      const value = draft as SettingsType['targeting'];
      return (
        <>
          <Toggle
            label="Automatic targeting"
            checked={value.auto_enabled}
            onChange={(v) => update({ auto_enabled: v })}
          />
          <TextField
            label="Target classes"
            value={value.target_classes.join(', ')}
            onChange={(v) =>
              update({ target_classes: v.split(',').map((s) => s.trim()).filter(Boolean) })
            }
          />
          <NumberField
            label="Minimum confidence"
            value={value.min_confidence}
            step={0.05}
            onChange={(v) => update({ min_confidence: v })}
          />
          <NumberField
            label="Minimum track age"
            suffix="s"
            value={value.min_track_duration_s}
            step={0.1}
            onChange={(v) => update({ min_track_duration_s: v })}
          />
          <NumberField
            label="Selection stability"
            suffix="s"
            value={value.detect_stability_s}
            step={0.1}
            onChange={(v) => update({ detect_stability_s: v })}
          />
          <SelectField
            label="Selection policy"
            value={value.selection}
            options={[
              { value: 'highest_confidence', label: 'Highest confidence' },
              { value: 'largest', label: 'Largest' },
              { value: 'closest_to_center', label: 'Closest to centre' },
              { value: 'oldest', label: 'Longest tracked' },
            ]}
            onChange={(v) => update({ selection: v })}
          />
          <NumberField
            label="Aim point (horizontal)"
            value={value.aim_x_ratio}
            step={0.05}
            onChange={(v) => update({ aim_x_ratio: v })}
            hint="0 = left edge of the box, 1 = right edge."
          />
          <NumberField
            label="Aim point (vertical)"
            value={value.aim_y_ratio}
            step={0.05}
            onChange={(v) => update({ aim_y_ratio: v })}
            hint="0 = top of the box, 1 = bottom."
          />
          <NumberField
            label="Pan offset"
            suffix="°"
            value={value.aim_pan_offset_deg}
            step={0.1}
            onChange={(v) => update({ aim_pan_offset_deg: v })}
          />
          <NumberField
            label="Tilt offset"
            suffix="°"
            value={value.aim_tilt_offset_deg}
            step={0.1}
            onChange={(v) => update({ aim_tilt_offset_deg: v })}
            hint="Corrects for nozzle ballistics: water drops."
          />
          <NumberField
            label="Aim timeout"
            suffix="s"
            value={value.aim_timeout_s}
            step={0.5}
            onChange={(v) => update({ aim_timeout_s: v })}
          />
          <NumberField
            label="Verify duration"
            suffix="s"
            value={value.verify_duration_s}
            step={0.1}
            onChange={(v) => update({ verify_duration_s: v })}
          />
          <NumberField
            label="Lost-target grace"
            suffix="s"
            value={value.lost_grace_s}
            step={0.1}
            onChange={(v) => update({ lost_grace_s: v })}
          />
          <NumberField
            label="Result window"
            suffix="s"
            value={value.result_window_s}
            step={0.5}
            onChange={(v) => update({ result_window_s: v })}
          />
          <NumberField
            label="Maximum retries"
            value={value.max_retries}
            onChange={(v) => update({ max_retries: v })}
          />
          <NumberField
            label="Cooldown"
            suffix="s"
            value={value.cooldown_s}
            step={1}
            onChange={(v) => update({ cooldown_s: v })}
          />
          <NumberField
            label="Retarget deadband"
            suffix="°"
            value={value.retarget_deadband_deg}
            step={0.05}
            onChange={(v) => update({ retarget_deadband_deg: v })}
          />
          <Toggle
            label="Only engage inside active zones"
            checked={value.require_active_zone}
            onChange={(v) => update({ require_active_zone: v })}
          />
          <Toggle
            label="Follow the target continuously"
            checked={value.continuous_tracking}
            onChange={(v) => update({ continuous_tracking: v })}
          />
          <Toggle
            label="Save a snapshot on engagement"
            checked={value.snapshot_on_engage}
            onChange={(v) => update({ snapshot_on_engage: v })}
          />
        </>
      );
    }
    case 'ui': {
      const value = draft as SettingsType['ui'];
      return (
        <>
          <NumberField
            label="Preview rate"
            suffix="fps"
            value={value.preview_fps}
            onChange={(v) => update({ preview_fps: v })}
          />
          <NumberField
            label="Preview width"
            suffix="px"
            value={value.preview_width}
            step={80}
            onChange={(v) => update({ preview_width: v })}
          />
          <NumberField
            label="JPEG quality"
            value={value.preview_quality}
            onChange={(v) => update({ preview_quality: v })}
          />
          <NumberField
            label="Telemetry rate"
            suffix="Hz"
            value={value.telemetry_hz}
            onChange={(v) => update({ telemetry_hz: v })}
          />
          <Toggle
            label="Draw detection overlays into the video"
            checked={value.draw_overlays}
            onChange={(v) => update({ draw_overlays: v })}
          />
          <Toggle
            label="Draw zones into the video"
            checked={value.draw_zones}
            onChange={(v) => update({ draw_zones: v })}
          />
        </>
      );
    }
    case 'system': {
      const value = draft as SettingsType['system'];
      return (
        <>
          <NumberField
            label="Event retention"
            suffix="days"
            value={value.event_retention_days}
            onChange={(v) => update({ event_retention_days: v })}
          />
          <NumberField
            label="Maximum events"
            value={value.max_events}
            step={1000}
            onChange={(v) => update({ max_events: v })}
          />
          <NumberField
            label="Snapshot retention"
            suffix="days"
            value={value.snapshot_retention_days}
            onChange={(v) => update({ snapshot_retention_days: v })}
          />
          <NumberField
            label="Snapshot budget"
            suffix="MB"
            value={value.max_snapshot_mb}
            step={32}
            onChange={(v) => update({ max_snapshot_mb: v })}
          />
        </>
      );
    }
    default:
      return null;
  }
}

function suggestedCameraId(result: OnvifProfileResult, profile: OnvifProfile): string {
  const base = `${result.device.manufacturer}-${result.device.model}-${profile.name}`
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 55);
  const hostSuffix = result.device.host.split('.').at(-1)?.replace(/[^a-z0-9]/gi, '') ?? '';
  return `${base || 'camera'}${hostSuffix ? `-${hostSuffix}` : ''}`.slice(0, 64);
}

function CameraDiscoveryPanel({ onAdded }: { onAdded: () => void }) {
  const { notify } = useToast();
  const [devices, setDevices] = useState<OnvifDevice[]>([]);
  const [xaddr, setXaddr] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [result, setResult] = useState<OnvifProfileResult | null>(null);
  const [role, setRole] = useState<'overview' | 'turret' | 'aux'>('overview');
  const [busy, setBusy] = useState<'discover' | 'connect' | string | null>(null);

  const discover = async () => {
    setBusy('discover');
    setResult(null);
    try {
      const response = await api.discoverCameras();
      setDevices(response.devices);
      if (response.devices.length === 1) setXaddr(response.devices[0].xaddr);
      notify(
        response.devices.length
          ? `found ${response.devices.length} ONVIF camera${response.devices.length === 1 ? '' : 's'}`
          : 'no cameras found by multicast; you can enter the ONVIF address below',
        response.devices.length ? 'good' : 'info',
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), 'bad');
    } finally {
      setBusy(null);
    }
  };

  const connect = async () => {
    setBusy('connect');
    setResult(null);
    try {
      const response = await api.onvifProfiles({ xaddr, username, password });
      setResult(response);
      notify(`connected to ${response.device.manufacturer} ${response.device.model}`, 'good');
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), 'bad');
    } finally {
      setBusy(null);
    }
  };

  const add = async (profile: OnvifProfile) => {
    const cameraId = suggestedCameraId(result!, profile);
    setBusy(profile.token);
    try {
      await api.onboardCamera({
        id: cameraId,
        name: `${result!.device.manufacturer} ${result!.device.model} — ${profile.name}`.trim(),
        role,
        uri: profile.uri,
        username,
        password,
        make_primary: role === 'overview',
      });
      notify(`camera added as ${cameraId}`, 'good');
      onAdded();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), 'bad');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="md:col-span-2 mb-4 rounded-lg border border-accent/30 bg-accent/5 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Add a network camera</h3>
          <p className="mt-0.5 text-xs text-muted">
            ONVIF finds the manufacturer-specific RTSP stream. Video still streams directly over
            RTSP, so discovery adds no ongoing processing cost.
          </p>
        </div>
        <button className="btn px-3 py-1 text-xs" disabled={busy !== null} onClick={discover}>
          {busy === 'discover' ? 'Searching…' : 'Discover cameras'}
        </button>
      </div>

      {devices.length > 0 && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {devices.map((device) => (
            <button
              key={device.xaddr}
              type="button"
              className={`rounded-lg border p-2 text-left text-sm transition ${
                xaddr === device.xaddr
                  ? 'border-accent bg-accent/10'
                  : 'border-edge bg-panelalt hover:border-muted'
              }`}
              onClick={() => {
                setXaddr(device.xaddr);
                setResult(null);
              }}
            >
              <span className="block font-medium">{device.name || device.host}</span>
              <span className="block text-xs text-muted">
                {[device.hardware, device.host].filter(Boolean).join(' · ')}
              </span>
            </button>
          ))}
        </div>
      )}

      <div className="mt-3 grid gap-x-4 md:grid-cols-2">
        <div className="md:col-span-2">
          <TextField
            label="ONVIF device service URL"
            value={xaddr}
            onChange={(value) => {
              setXaddr(value);
              setResult(null);
            }}
            placeholder="http://192.168.1.217:2020/onvif/device_service"
            hint="Manual entry works when multicast cannot cross from the server to the camera subnet."
          />
        </div>
        <TextField
          label="Camera account username"
          value={username}
          onChange={setUsername}
          autoComplete="username"
        />
        <TextField
          label="Camera account password"
          type="password"
          value={password}
          onChange={setPassword}
          hint="Stored separately from settings and never returned by the API."
          autoComplete="current-password"
        />
        <SelectField
          label="Camera role"
          value={role}
          options={[
            { value: 'overview', label: 'Overview (make primary)' },
            { value: 'turret', label: 'Turret-mounted' },
            { value: 'aux', label: 'Auxiliary' },
          ]}
          onChange={setRole}
        />
        <div className="flex items-end py-1.5">
          <button
            className="btn btn-primary w-full"
            disabled={!xaddr.trim() || busy !== null}
            onClick={connect}
          >
            {busy === 'connect' ? 'Connecting…' : 'Connect and list streams'}
          </button>
        </div>
      </div>

      {result && (
        <div className="mt-3 space-y-2 border-t border-edge pt-3">
          <p className="text-xs text-muted">
            {result.device.manufacturer} {result.device.model} at {result.device.host}. Choose a
            stream; a lower-resolution substream uses less CPU and network bandwidth.
          </p>
          {result.profiles.map((profile) => (
            <div
              key={profile.token}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-edge bg-panelalt p-2"
            >
              <div>
                <p className="text-sm font-medium">{profile.name}</p>
                <p className="text-xs text-muted">
                  {[profile.encoding, profile.width && profile.height ? `${profile.width}×${profile.height}` : '', profile.fps ? `${profile.fps} fps` : '']
                    .filter(Boolean)
                    .join(' · ')}
                </p>
              </div>
              <button
                className="btn btn-primary px-3 py-1 text-xs"
                disabled={busy !== null}
                onClick={() => add(profile)}
              >
                {busy === profile.token ? 'Adding…' : 'Add this stream'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CameraFields({
  draft,
  update,
  onReload,
}: {
  draft: SettingsType['cameras'];
  update: (patch: Record<string, unknown>) => void;
  onReload: () => void;
}) {
  const sources = draft.sources ?? [];

  const patchSource = (index: number, patch: Partial<CameraConfig>) => {
    const next = sources.map((source, i) => (i === index ? { ...source, ...patch } : source));
    update({ sources: next });
  };

  const addSource = () => {
    const id = `camera${sources.length + 1}`;
    update({
      sources: [
        ...sources,
        {
          id,
          name: 'New camera',
          url: '',
          enabled: false,
          role: 'aux',
          backend: 'auto',
          transport: 'tcp',
          latency_ms: 100,
          target_width: 1280,
          reconnect_delay_s: 3,
          stall_timeout_s: 8,
        } satisfies CameraConfig,
      ],
    });
  };

  return (
    <>
      <CameraDiscoveryPanel onAdded={onReload} />

      <div className="md:col-span-2">
        <SelectField
          label="Primary camera (detection & calibration)"
          value={draft.primary_id}
          options={sources.map((source) => ({ value: source.id, label: `${source.name} (${source.id})` }))}
          onChange={(v) => update({ primary_id: v })}
        />
      </div>

      {sources.map((source, index) => (
        <div key={source.id} className="md:col-span-2 mt-3 rounded-lg border border-edge p-3">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-medium">{source.name || source.id}</h3>
            {sources.length > 1 && (
              <button
                className="btn px-2 py-0.5 text-xs"
                onClick={() => update({ sources: sources.filter((_, i) => i !== index) })}
              >
                Remove
              </button>
            )}
          </div>
          <div className="grid gap-x-6 md:grid-cols-2">
            <TextField label="Id" value={source.id} onChange={(v) => patchSource(index, { id: v })} />
            <TextField
              label="Name"
              value={source.name}
              onChange={(v) => patchSource(index, { name: v })}
            />
            <div className="md:col-span-2">
              <TextField
                label="RTSP URL"
                value={source.url}
                onChange={(v) => patchSource(index, { url: v })}
                hint="Credentials may be written as ${VARIABLE} and resolved from the environment file. Leave empty to use the built-in simulated source."
              />
            </div>
            <SelectField
              label="Role"
              value={source.role}
              options={[
                { value: 'overview', label: 'Overview (fixed)' },
                { value: 'turret', label: 'Turret-mounted' },
                { value: 'aux', label: 'Auxiliary' },
              ]}
              onChange={(v) => patchSource(index, { role: v })}
            />
            <SelectField
              label="Backend"
              value={source.backend}
              options={[
                { value: 'auto', label: 'auto' },
                { value: 'gstreamer', label: 'GStreamer' },
                { value: 'opencv', label: 'FFmpeg (OpenCV)' },
                { value: 'simulated', label: 'Simulated' },
              ]}
              onChange={(v) => patchSource(index, { backend: v })}
            />
            <SelectField
              label="Transport"
              value={source.transport}
              options={[
                { value: 'tcp', label: 'TCP (reliable)' },
                { value: 'udp', label: 'UDP (lower latency)' },
              ]}
              onChange={(v) => patchSource(index, { transport: v })}
            />
            <NumberField
              label="Latency buffer"
              suffix="ms"
              value={source.latency_ms}
              step={10}
              onChange={(v) => patchSource(index, { latency_ms: v })}
            />
            <NumberField
              label="Downscale width"
              suffix="px"
              value={source.target_width}
              step={80}
              onChange={(v) => patchSource(index, { target_width: v })}
              hint="0 keeps the native resolution."
            />
            <NumberField
              label="Stall timeout"
              suffix="s"
              value={source.stall_timeout_s}
              step={1}
              onChange={(v) => patchSource(index, { stall_timeout_s: v })}
            />
            <Toggle
              label="Enabled"
              checked={source.enabled}
              onChange={(v) => patchSource(index, { enabled: v })}
            />
          </div>
        </div>
      ))}

      <div className="md:col-span-2 mt-3">
        <button className="btn" onClick={addSource}>
          Add camera
        </button>
      </div>
    </>
  );
}
