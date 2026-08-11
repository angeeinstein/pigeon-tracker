import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ComponentProps,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from 'react';
import { api } from '../api/client';
import type {
  CameraConfig,
  OnvifDevice,
  OnvifProfile,
  OnvifProfileResult,
  Settings as SettingsType,
} from '../api/types';
import {
  Banner,
  Card,
  NumberField as BaseNumberField,
  Pill,
  SelectField as BaseSelectField,
  Spinner,
  TextField as BaseTextField,
  Toggle as BaseToggle,
  type SettingStatus,
} from '../components/ui';
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

const SECTION_DESCRIPTIONS: Record<SectionName, string> = {
  cameras: 'Choose video sources and decide which camera drives detection and calibration.',
  detector: 'Configure the model, input size, classes and confidence filters.',
  tracker: 'Control how detections become stable identities across frames.',
  motion: 'Set server-side movement limits, speed, acceleration and parking behaviour.',
  controller: 'Choose simulated or physical hardware and configure its mechanics and link.',
  spray: 'Set water-output safety limits and timing budgets.',
  targeting: 'Control target selection, aiming, verification, retries and engagement rules.',
  ui: 'Tune preview quality, telemetry rate and visual overlays.',
  system: 'Set database event and snapshot retention limits.',
};

const FIELD_PATHS: Record<SectionName, Record<string, string>> = {
  cameras: {
    'Primary camera (detection & calibration)': 'primary_id',
    Id: 'id', Name: 'name', 'RTSP URL': 'url', Role: 'role', Backend: 'backend',
    Transport: 'transport', 'Latency buffer': 'latency_ms', 'Downscale width': 'target_width',
    'Stall timeout': 'stall_timeout_s', Enabled: 'enabled',
  },
  detector: {
    'Detection enabled': 'enabled', Backend: 'backend', Model: 'model_path', Device: 'device',
    'Confidence threshold': 'confidence', 'NMS IoU': 'iou', 'Inference rate': 'fps',
    'Input size': 'input_size', Classes: 'classes', 'Half precision (CUDA only)': 'half',
  },
  tracker: {
    'Tracking enabled': 'enabled', 'Track threshold': 'track_thresh', 'Low threshold': 'low_thresh',
    'Match threshold (IoU distance)': 'match_thresh', 'Track buffer': 'track_buffer',
    'Minimum hits to confirm': 'min_hits',
  },
  motion: {
    'Pan minimum': 'pan_min_deg', 'Pan maximum': 'pan_max_deg', 'Tilt minimum': 'tilt_min_deg',
    'Tilt maximum': 'tilt_max_deg', 'Maximum speed': 'max_speed_deg_s', Acceleration: 'accel_deg_s2',
    'Manual/jog speed': 'manual_speed_deg_s', 'Jog timeout': 'jog_ttl_ms',
    'Aim tolerance': 'aim_tolerance_deg', 'Park pan': 'park_pan_deg', 'Park tilt': 'park_tilt_deg',
    'Home automatically when the controller connects': 'auto_home_on_connect',
  },
  controller: {
    'Controller mode': 'mode', 'Controller id': 'controller_id', 'Status timeout': 'status_timeout_s',
    'Command timeout': 'command_timeout_s', 'Homing timeout': 'home_timeout_s',
    'Ping interval': 'ping_interval_s', 'Push configuration on connect': 'push_config_on_connect',
    'Motor steps per revolution': 'hardware.steps_per_rev', 'Pan microsteps': 'hardware.pan_microsteps',
    'Tilt microsteps': 'hardware.tilt_microsteps', 'Pan gear ratio': 'hardware.pan_gear_ratio',
    'Tilt gear ratio': 'hardware.tilt_gear_ratio', 'Homing speed': 'hardware.homing_speed_deg_s',
    'Homing back-off': 'hardware.homing_backoff_deg', 'Pan home offset': 'hardware.pan_home_offset_deg',
    'Tilt home offset': 'hardware.tilt_home_offset_deg',
    'Pan homes toward maximum endstop': 'hardware.pan_home_dir',
    'Tilt homes toward maximum endstop': 'hardware.tilt_home_dir',
    'Invert pan direction': 'hardware.pan_invert', 'Invert tilt direction': 'hardware.tilt_invert',
    'Endstops active low': 'hardware.endstop_active_low',
    'Allow motion before homing': 'hardware.allow_unhomed_motion',
  },
  spray: {
    'Water output enabled': 'enabled', 'Default duration': 'default_duration_ms',
    'Maximum single burst': 'max_duration_ms', 'Minimum interval': 'min_interval_s',
    'Duty budget': 'duty_budget_ms', 'Duty window': 'duty_window_s',
  },
  targeting: {
    'Automatic targeting': 'auto_enabled', 'Target classes': 'target_classes',
    'Minimum confidence': 'min_confidence', 'Minimum track age': 'min_track_duration_s',
    'Selection stability': 'detect_stability_s', 'Selection policy': 'selection',
    'Aim point (horizontal)': 'aim_x_ratio', 'Aim point (vertical)': 'aim_y_ratio',
    'Pan offset': 'aim_pan_offset_deg', 'Tilt offset': 'aim_tilt_offset_deg',
    'Aim timeout': 'aim_timeout_s', 'Verify duration': 'verify_duration_s',
    'Lost-target grace': 'lost_grace_s', 'Result window': 'result_window_s',
    'Maximum retries': 'max_retries', Cooldown: 'cooldown_s',
    'Retarget deadband': 'retarget_deadband_deg', 'Only engage inside active zones': 'require_active_zone',
    'Follow the target continuously': 'continuous_tracking',
    'Save a snapshot on engagement': 'snapshot_on_engage',
  },
  ui: {
    'Preview rate': 'preview_fps', 'Preview width': 'preview_width', 'JPEG quality': 'preview_quality',
    'Telemetry rate': 'telemetry_hz', 'Draw detection overlays into the video': 'draw_overlays',
    'Draw zones into the video': 'draw_zones',
  },
  system: {
    'Event retention': 'event_retention_days', 'Maximum events': 'max_events',
    'Snapshot retention': 'snapshot_retention_days', 'Snapshot budget': 'max_snapshot_mb',
  },
};

function same(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function slugCameraId(name: string): string {
  return name
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'camera';
}

function uniqueCameraId(name: string, sources: CameraConfig[], currentIndex = -1): string {
  const used = new Set(
    sources.filter((_, index) => index !== currentIndex).map((source) => source.id),
  );
  const base = slugCameraId(name);
  if (!used.has(base)) return base;
  for (let suffix = 2; suffix < 1000; suffix += 1) {
    const ending = `-${suffix}`;
    const candidate = `${base.slice(0, 64 - ending.length)}${ending}`;
    if (!used.has(candidate)) return candidate;
  }
  return `${base.slice(0, 52)}-${Date.now().toString(36)}`.slice(0, 64);
}

function cameraIdError(id: string, sources: CameraConfig[], currentIndex: number): string | undefined {
  if (!id) return 'An id is required.';
  if (!/^[A-Za-z0-9_-]+$/.test(id)) return "Use only letters, numbers, '-' and '_'.";
  if (id.length > 64) return 'Use at most 64 characters.';
  if (sources.some((source, index) => index !== currentIndex && source.id === id)) {
    return 'This id is already used by another camera.';
  }
  return undefined;
}

function firstCameraIdError(cameras: SettingsType['cameras']): string | undefined {
  for (let index = 0; index < cameras.sources.length; index += 1) {
    const error = cameraIdError(cameras.sources[index].id, cameras.sources, index);
    if (error) return `Camera ${index + 1}: ${error}`;
  }
  return undefined;
}

function getPath(value: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((current, part) => {
    if (current === null || typeof current !== 'object') return undefined;
    return (current as Record<string, unknown>)[part];
  }, value);
}

function setPath(value: unknown, path: string, next: unknown): void {
  const parts = path.split('.');
  let current = value as Record<string, unknown>;
  parts.slice(0, -1).forEach((part) => {
    current = current[part] as Record<string, unknown>;
  });
  current[parts.at(-1)!] = next;
}

type FieldContextValue = {
  section: SectionName;
  saved: unknown;
  draft: unknown;
  defaults: unknown;
  prefix: string;
  cameraSource?: boolean;
  resetValue: (path: string, value: unknown) => void;
};

const FieldContext = createContext<FieldContextValue | null>(null);

function useSettingAdornment(label: string) {
  const context = useContext(FieldContext);
  const relativePath = context ? FIELD_PATHS[context.section][label] : undefined;
  if (!context || !relativePath) return {};
  const path = `${context.prefix}${relativePath}`;
  const draftValue = getPath(context.draft, path);
  const savedValue = getPath(context.saved, path);
  let defaultValue = getPath(context.defaults, path);
  let canFactoryReset = true;
  if (context.cameraSource && /^(id|name|url)$/.test(path)) {
    defaultValue = undefined;
    canFactoryReset = false;
  }
  if (context.section === 'cameras' && path === 'primary_id') {
    const sourceIds = ((context.draft as SettingsType['cameras']).sources ?? []).map(
      (source) => source.id,
    );
    canFactoryReset = typeof defaultValue === 'string' && sourceIds.includes(defaultValue);
  }
  const unsaved = !same(draftValue, savedValue);
  const savedOverride = defaultValue !== undefined && !same(savedValue, defaultValue);
  const settingStatus: SettingStatus | undefined = unsaved
    ? 'unsaved'
    : savedOverride
      ? 'saved'
      : undefined;
  if (!settingStatus) return {};
  if (unsaved && savedValue === undefined) return { settingStatus };
  if (!unsaved && !canFactoryReset) return { settingStatus };
  const resetTarget = unsaved ? savedValue : defaultValue;
  return {
    settingStatus,
    onSettingReset: () => context.resetValue(path, clone(resetTarget)),
    settingResetLabel: unsaved ? `Undo unsaved change to ${label}` : `Reset ${label} to factory default`,
  };
}

function TextField(props: ComponentProps<typeof BaseTextField>) {
  return <BaseTextField {...props} {...useSettingAdornment(props.label)} />;
}

function NumberField(props: ComponentProps<typeof BaseNumberField>) {
  return <BaseNumberField {...props} {...useSettingAdornment(props.label)} />;
}

function Toggle(props: ComponentProps<typeof BaseToggle>) {
  return <BaseToggle {...props} {...useSettingAdornment(props.label)} />;
}

function SelectField<T extends string>(props: {
  label: string;
  value: T;
  options: readonly { value: T; label: string }[];
  onChange: (value: T) => void;
  hint?: string;
}) {
  return <BaseSelectField {...props} {...useSettingAdornment(props.label)} />;
}

export default function Settings() {
  const loaded = useAsync(async () => {
    const [values, defaults] = await Promise.all([api.settings(), api.settingsDefaults()]);
    return { values, defaults };
  }, []);
  const [tab, setTab] = useState<SectionName>('cameras');
  const [saved, setSaved] = useState<SettingsType | null>(null);
  const [draft, setDraft] = useState<SettingsType | null>(null);
  const [saving, setSaving] = useState(false);
  const { notify } = useToast();

  useEffect(() => {
    if (loaded.data && saved === null) {
      setSaved(clone(loaded.data.values));
      setDraft(clone(loaded.data.values));
    }
  }, [loaded.data, saved]);

  if (loaded.loading) return <Spinner />;
  if (loaded.error || !loaded.data) return <Banner>{loaded.error ?? 'no settings'}</Banner>;
  if (!saved || !draft) return <Spinner />;
  const defaults = loaded.data.defaults;

  const dirtySections = TABS.filter((entry) => !same(draft[entry.id], saved[entry.id])).map(
    (entry) => entry.id,
  );
  const cameraProblem = firstCameraIdError(draft.cameras);
  const save = async () => {
    if (dirtySections.length === 0) return;
    if (cameraProblem) {
      notify(cameraProblem, 'bad');
      return;
    }
    setSaving(true);
    try {
      const patch = Object.fromEntries(
        dirtySections.map((section) => [section, draft[section]]),
      ) as Partial<SettingsType>;
      const updated = await api.patchAllSettings(patch);
      setSaved(clone(updated));
      setDraft(clone(updated));
      notify('All settings saved', 'good');
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), 'bad');
    } finally {
      setSaving(false);
    }
  };

  const updateSection = (section: SectionName, patch: Record<string, unknown>) => {
    setDraft((current) => ({
      ...current!,
      [section]: { ...(current![section] as object), ...patch },
    }));
  };

  const cameraAdded = (cameras: SettingsType['cameras']) => {
    setSaved((current) => ({ ...current!, cameras: clone(cameras) }));
    setDraft((current) => {
      const local = current!.cameras;
      const localIds = new Set(local.sources.map((source) => source.id));
      const added = cameras.sources.filter((source) => !localIds.has(source.id));
      return {
        ...current!,
        cameras: {
          ...local,
          sources: [...local.sources, ...added],
          primary_id: same(local.primary_id, saved.cameras.primary_id)
            ? cameras.primary_id
            : local.primary_id,
        },
      };
    });
  };

  return (
    <div className="space-y-4">
      <nav className="grid grid-cols-3 gap-1 rounded-xl border border-edge bg-panel p-1.5 shadow-lg shadow-black/20 sm:flex sm:overflow-x-auto">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            onClick={() => setTab(entry.id)}
            className={`whitespace-nowrap rounded-lg border px-2 py-2 text-center text-sm transition sm:px-3 ${
              tab === entry.id
                ? 'border-accent/50 bg-accent/15 text-ink shadow-sm'
                : 'border-transparent text-muted hover:border-edge hover:bg-panelalt hover:text-ink'
            }`}
          >
            <span
              className={`mr-1.5 inline-block h-2 w-2 rounded-full ${
                !same(draft[entry.id], saved[entry.id])
                  ? 'bg-warn'
                  : !same(saved[entry.id], defaults[entry.id])
                    ? 'bg-accent'
                    : 'invisible'
              }`}
            />
            {entry.label}
          </button>
        ))}
      </nav>

      <div className="sticky top-2 z-20 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-accent/25 bg-panel/95 px-3 py-2 shadow-lg backdrop-blur">
        <p className={`text-xs ${cameraProblem ? 'text-bad' : 'text-muted'}`}>
          {cameraProblem ? (
            <><span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-bad" />{cameraProblem}</>
          ) : dirtySections.length > 0 ? (
            <><span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-warn" />{dirtySections.length} tab{dirtySections.length === 1 ? '' : 's'} with unsaved changes</>
          ) : (
            <><span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-accent" />All changes saved</>
          )}
        </p>
        <button className="btn btn-primary px-3 py-1 text-xs" disabled={dirtySections.length === 0 || saving || Boolean(cameraProblem)} onClick={save}>
          {saving ? 'Saving…' : 'Save all changes'}
        </button>
      </div>

      <SectionEditor
        section={tab}
        saved={saved}
        draft={draft}
        defaults={defaults}
        update={(patch) => updateSection(tab, patch)}
        setDraft={setDraft}
        onCameraAdded={cameraAdded}
      />
    </div>
  );
}

function SectionEditor({
  section,
  saved,
  draft,
  defaults,
  update,
  setDraft,
  onCameraAdded,
}: {
  section: SectionName;
  saved: SettingsType;
  draft: SettingsType;
  defaults: SettingsType;
  update: (patch: Record<string, unknown>) => void;
  setDraft: Dispatch<SetStateAction<SettingsType | null>>;
  onCameraAdded: (cameras: SettingsType['cameras']) => void;
}) {
  const resetValue = (path: string, value: unknown) => {
    setDraft((current) => {
      const next = clone(current!);
      setPath(next[section], path, value);
      return next;
    });
  };

  const resetSection = () => {
    setDraft((current) => ({ ...current!, [section]: clone(defaults[section]) }));
  };

  return (
    <Card
      title={`${TABS.find((entry) => entry.id === section)?.label ?? section} settings`}
      titleClassName="text-base font-semibold text-ink"
      className="border-2 border-edge/90"
      actions={
        <button className="btn px-2 py-1 text-xs" onClick={resetSection} disabled={same(draft[section], defaults[section])}>
          Reset section
        </button>
      }
    >
      <FieldContext.Provider value={{ section, saved: saved[section], draft: draft[section], defaults: defaults[section], prefix: '', resetValue }}>
        <p className="mb-4 border-b border-edge pb-3 text-sm text-muted">
          {SECTION_DESCRIPTIONS[section]}
        </p>
        <div className="grid gap-x-6 md:grid-cols-2">
          <Fields section={section} draft={draft[section]} update={update} onCameraAdded={onCameraAdded} />
        </div>
      </FieldContext.Provider>
    </Card>
  );
}

function Fields({
  section,
  draft,
  update,
  onCameraAdded,
}: {
  section: SectionName;
  draft: unknown;
  update: (patch: Record<string, unknown>) => void;
  onCameraAdded: (cameras: SettingsType['cameras']) => void;
}) {
  switch (section) {
    case 'cameras':
      return (
        <CameraFields
          draft={draft as SettingsType['cameras']}
          update={update}
          onCameraAdded={onCameraAdded}
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
          <div className="md:col-span-2">
            <SelectField
              label="Controller mode"
              value={value.mode}
              options={[
                { value: 'physical', label: 'Physical ESP32' },
                { value: 'simulated', label: 'Simulated turret' },
              ]}
              onChange={(v) => update({ mode: v })}
              hint="Simulated mode exercises the normal controller protocol, joystick, calibration, targeting and spray state without physical hardware."
            />
          </div>
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

function CameraDiscoveryPanel({
  onAdded,
}: {
  onAdded: (cameras: SettingsType['cameras']) => void;
}) {
  const { notify } = useToast();
  const [devices, setDevices] = useState<OnvifDevice[]>([]);
  const [xaddr, setXaddr] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [result, setResult] = useState<OnvifProfileResult | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [role, setRole] = useState<'overview' | 'turret' | 'aux'>('overview');
  const [busy, setBusy] = useState<'discover' | 'connect' | string | null>(null);

  const discover = async () => {
    setBusy('discover');
    setResult(null);
    setConnectionError(null);
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
      const message = error instanceof Error ? error.message : String(error);
      setConnectionError(message);
      notify(message, 'bad');
    } finally {
      setBusy(null);
    }
  };

  const connect = async () => {
    setBusy('connect');
    setResult(null);
    setConnectionError(null);
    try {
      const response = await api.onvifProfiles({ xaddr, username, password });
      setResult(response);
      notify(`connected to ${response.device.manufacturer} ${response.device.model}`, 'good');
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setConnectionError(message);
      notify(message, 'bad');
    } finally {
      setBusy(null);
    }
  };

  const add = async (profile: OnvifProfile) => {
    const cameraId = suggestedCameraId(result!, profile);
    setBusy(profile.token);
    try {
      const cameras = await api.onboardCamera({
        id: cameraId,
        name: `${result!.device.manufacturer} ${result!.device.model} — ${profile.name}`.trim(),
        role,
        uri: profile.uri,
        username,
        password,
        make_primary: role === 'overview',
      });
      notify(`camera added as ${cameraId}`, 'good');
      onAdded(cameras);
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), 'bad');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="md:col-span-2 mb-5 rounded-xl border-2 border-accent/35 bg-accent/10 p-4 shadow-md shadow-black/20">
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
              setConnectionError(null);
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
          <BaseTextField
            label="ONVIF device service URL"
            value={xaddr}
            onChange={(value) => {
              setXaddr(value);
              setResult(null);
              setConnectionError(null);
            }}
            placeholder="http://192.168.1.217:2020/onvif/device_service"
            hint="Manual entry works across subnets. Include the camera's ONVIF port; it is often not port 80 (for example :2020 or :8000)."
          />
        </div>
        <BaseTextField
          label="Camera account username"
          value={username}
          onChange={setUsername}
          autoComplete="username"
        />
        <BaseTextField
          label="Camera account password"
          type="password"
          value={password}
          onChange={setPassword}
          hint="Stored separately from settings and never returned by the API."
          autoComplete="current-password"
        />
        <BaseSelectField
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

      {connectionError && (
        <div className="mt-3">
          <Banner>{connectionError}</Banner>
        </div>
      )}

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
  onCameraAdded,
}: {
  draft: SettingsType['cameras'];
  update: (patch: Record<string, unknown>) => void;
  onCameraAdded: (cameras: SettingsType['cameras']) => void;
}) {
  const sources = draft.sources ?? [];

  const patchSource = (index: number, patch: Partial<CameraConfig>) => {
    const next = sources.map((source, i) => (i === index ? { ...source, ...patch } : source));
    const previous = sources[index];
    update({
      sources: next,
      ...(patch.id !== undefined && draft.primary_id === previous.id
        ? { primary_id: patch.id }
        : {}),
    });
  };

  const removeSource = (index: number) => {
    const removed = sources[index];
    const remaining = sources.filter((_, candidate) => candidate !== index);
    update({
      sources: remaining,
      ...(draft.primary_id === removed.id ? { primary_id: remaining[0]?.id ?? '' } : {}),
    });
  };

  const addSource = () => {
    const name = `New camera ${sources.length + 1}`;
    const id = uniqueCameraId(name, sources);
    update({
      sources: [
        ...sources,
        {
          id,
          name,
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
      <CameraDiscoveryPanel onAdded={onCameraAdded} />

      <div className="md:col-span-2 mb-5 rounded-xl border-2 border-accent/50 bg-accent/10 p-4 shadow-md shadow-black/20">
        <div className="mb-2 flex items-center gap-2">
          <span className="grid h-7 w-7 place-items-center rounded-full bg-accent/20 text-accent">◎</span>
          <div>
            <h3 className="text-sm font-semibold text-ink">Primary camera</h3>
            <p className="text-xs text-muted">Used for detection, tracking, zones and calibration.</p>
          </div>
        </div>
        <SelectField
          label="Primary camera (detection & calibration)"
          value={draft.primary_id}
          options={sources.map((source) => ({
            value: source.id,
            label: `${source.name} (${source.id})`,
          }))}
          onChange={(v) => update({ primary_id: v })}
        />
      </div>

      {sources.map((source, index) => {
        const isPrimary = source.id === draft.primary_id;
        const generatedId = uniqueCameraId(source.name, sources, index);
        const idFollowsName = source.id === generatedId;
        const idProblem = cameraIdError(source.id, sources, index);
        const changeName = (name: string) => {
          patchSource(index, {
            name,
            ...(idFollowsName ? { id: uniqueCameraId(name, sources, index) } : {}),
          });
        };
        return (
          <CameraFieldScope key={index} source={source} index={index}>
            <article
              className={`md:col-span-2 mb-4 overflow-hidden rounded-xl border-2 shadow-lg shadow-black/20 ${
                isPrimary
                  ? 'border-accent/60 bg-accent/[0.04]'
                  : 'border-edge bg-panelalt/35'
              }`}
            >
              <header className="flex flex-wrap items-center justify-between gap-3 border-b border-edge bg-panelalt/70 px-4 py-3">
                <div className="min-w-0">
                  <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">
                    Camera {index + 1}
                  </p>
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="truncate text-base font-semibold text-ink">
                      {source.name || source.id || 'Unnamed camera'}
                    </h3>
                    {isPrimary && <Pill tone="info">primary</Pill>}
                    <Pill tone={source.enabled ? 'good' : 'idle'}>
                      {source.enabled ? 'enabled' : 'disabled'}
                    </Pill>
                    <Pill tone="idle">{source.role}</Pill>
                  </div>
                </div>
                <div className="flex gap-2">
                  {!isPrimary && (
                    <button
                      className="btn px-2 py-1 text-xs"
                      onClick={() => update({ primary_id: source.id })}
                      disabled={Boolean(idProblem)}
                    >
                      Make primary
                    </button>
                  )}
                  {sources.length > 1 && (
                    <button
                      className="btn px-2 py-1 text-xs text-bad"
                      onClick={() => removeSource(index)}
                    >
                      Remove
                    </button>
                  )}
                </div>
              </header>

              <div className="space-y-4 p-4">
                <section className="rounded-lg border border-edge bg-[#11151d]/70 p-3">
                  <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-accent">
                    Identity
                  </h4>
                  <div className="grid gap-x-6 md:grid-cols-2">
                    <TextField label="Name" value={source.name} onChange={changeName} />
                    <TextField
                      label="Id"
                      value={source.id}
                      onChange={(id) => patchSource(index, { id })}
                      error={idProblem}
                      hint={
                        idFollowsName
                          ? 'Generated from the name. Edit the id to manage it manually.'
                          : 'Manually managed stable identifier.'
                      }
                    />
                  </div>
                </section>

                <section className="rounded-lg border border-edge bg-[#11151d]/70 p-3">
                  <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-accent">
                    Stream and purpose
                  </h4>
                  <div className="grid gap-x-6 md:grid-cols-2">
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
                  </div>
                </section>

                <section className="rounded-lg border border-edge bg-[#11151d]/70 p-3">
                  <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-accent">
                    Connection and performance
                  </h4>
                  <div className="grid gap-x-6 md:grid-cols-2">
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
                </section>
              </div>
            </article>
          </CameraFieldScope>
        );
      })}

      <div className="md:col-span-2 mt-3">
        <button className="btn" onClick={addSource}>
          Add camera
        </button>
      </div>
    </>
  );
}

function CameraFieldScope({
  source,
  index,
  children,
}: {
  source: CameraConfig;
  index: number;
  children: ReactNode;
}) {
  const context = useContext(FieldContext);
  if (!context) return children;
  const savedCameras = context.saved as SettingsType['cameras'];
  const defaultCameras = context.defaults as SettingsType['cameras'];
  const savedSource =
    savedCameras.sources.find((candidate) => candidate.id === source.id) ??
    savedCameras.sources.find(
      (candidate) => candidate.url === source.url && candidate.name === source.name,
    ) ??
    savedCameras.sources[index];
  return (
    <FieldContext.Provider
      value={{
        ...context,
        saved: savedSource ?? {},
        draft: source,
        defaults: defaultCameras.sources[0] ?? {},
        prefix: '',
        cameraSource: true,
        resetValue: (path, value) => context.resetValue(`sources.${index}.${path}`, value),
      }}
    >
      {children}
    </FieldContext.Provider>
  );
}
