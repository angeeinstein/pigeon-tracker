import { useState } from 'react';
import { api } from '../api/client';
import type { Preset, Settings } from '../api/types';
import Joystick from '../components/Joystick';
import VideoView from '../components/VideoView';
import { Banner, Card, Pill, StatusRow, Toggle } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { useLive, useToast } from '../state';

const STATE_TONE: Record<string, 'good' | 'warn' | 'bad' | 'info' | 'idle'> = {
  DISARMED: 'idle',
  IDLE: 'good',
  DETECTED: 'info',
  TRACKING: 'info',
  AIMING: 'warn',
  VERIFY_TARGET: 'warn',
  SPRAY: 'bad',
  VERIFY_RESULT: 'warn',
  COOLDOWN: 'idle',
  ERROR: 'bad',
};

export default function Dashboard() {
  const { telemetry, jog } = useLive();
  const { attempt, notify } = useToast();
  const settings = useAsync(() => api.settings(), []);
  const presets = useAsync(() => api.presets(), []);
  const [clickToAim, setClickToAim] = useState(true);

  const connected = telemetry?.controller_connected ?? false;
  const canMove = connected && (telemetry?.homed ?? false);

  const patch = async <S extends keyof Settings>(section: S, body: Partial<Settings[S]>) => {
    const ok = await attempt(() => api.patchSettings(section, body));
    if (ok) settings.reload();
  };

  const onPick = async (x: number, y: number) => {
    if (!clickToAim) return;
    if (!canMove) {
      notify('turret must be connected and homed before aiming', 'bad');
      return;
    }
    await attempt(async () => {
      const result = await api.aim(x, y);
      notify(
        `aiming at pan ${result.pan_deg.toFixed(1)}° tilt ${result.tilt_deg.toFixed(1)}°` +
          (result.extrapolated ? ' (extrapolated — add calibration points here)' : ''),
        result.extrapolated ? 'info' : 'good',
      );
    });
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="space-y-4">
        {telemetry?.controller_fault && (
          <Banner tone="warn">Controller: {telemetry.controller_fault}</Banner>
        )}
        {telemetry?.estop && (
          <Banner tone="bad">
            Emergency stop is latched. Clear it, then re-home before moving.{' '}
            <button
              className="btn ml-2 px-2 py-1"
              onClick={() => attempt(() => api.clearEstop(), 'emergency stop cleared')}
            >
              Clear
            </button>
          </Banner>
        )}

        <VideoView telemetry={telemetry} onPick={onPick} />

        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={STATE_TONE[telemetry?.system_state ?? 'DISARMED'] ?? 'idle'}>
            {telemetry?.system_state ?? '—'}
          </Pill>
          {telemetry?.state_reason && (
            <span className="text-xs text-muted">{telemetry.state_reason}</span>
          )}
          <span className="ml-auto text-xs text-muted">
            {telemetry?.frame
              ? `${telemetry.frame.width}×${telemetry.frame.height} · ${telemetry.frame.inference_ms.toFixed(0)} ms inference`
              : 'no frames'}
          </span>
        </div>

        <Card title="Manual control">
          <div className="flex flex-wrap items-start gap-5">
            <Joystick onChange={jog} disabled={!connected} />

            <div className="grid flex-1 grid-cols-2 gap-2 sm:grid-cols-3">
              <button
                className={`btn ${telemetry?.armed ? 'btn-danger' : 'btn-good'}`}
                disabled={!connected}
                onClick={() =>
                  attempt(
                    () => api.arm(!telemetry?.armed),
                    telemetry?.armed ? 'disarmed' : 'armed',
                  )
                }
              >
                {telemetry?.armed ? 'Disarm' : 'Arm'}
              </button>
              <button
                className="btn"
                disabled={!connected}
                onClick={() => attempt(() => api.home(), 'homing complete')}
              >
                Home
              </button>
              <button className="btn" disabled={!canMove} onClick={() => attempt(() => api.center())}>
                Center
              </button>
              <button className="btn" disabled={!connected} onClick={() => attempt(() => api.stop())}>
                Stop
              </button>
              <button
                className="btn btn-danger col-span-2 sm:col-span-1"
                disabled={!telemetry?.armed || !telemetry?.spray_enabled}
                onClick={() => attempt(() => api.spray(), 'spray triggered')}
                title={
                  telemetry?.spray_enabled
                    ? 'Manual spray (subject to the same budget as automatic)'
                    : 'Water output is disabled in settings'
                }
              >
                Spray
              </button>
            </div>
          </div>

          <div className="mt-4 grid grid-cols-3 gap-2 sm:max-w-md">
            <span />
            <button
              className="btn"
              disabled={!canMove}
              onClick={() => attempt(() => api.moveRelative(0, 5))}
            >
              ↑
            </button>
            <span />
            <button
              className="btn"
              disabled={!canMove}
              onClick={() => attempt(() => api.moveRelative(-5, 0))}
            >
              ←
            </button>
            <button
              className="btn"
              disabled={!canMove}
              onClick={() => attempt(() => api.moveRelative(0, -5))}
            >
              ↓
            </button>
            <button
              className="btn"
              disabled={!canMove}
              onClick={() => attempt(() => api.moveRelative(5, 0))}
            >
              →
            </button>
          </div>
        </Card>
      </div>

      <aside className="space-y-4">
        <Card title="Status">
          <div className="mb-3 flex flex-wrap gap-2">
            <Pill tone={telemetry?.camera_connected ? 'good' : 'bad'}>camera</Pill>
            <Pill tone={connected ? 'good' : 'bad'}>controller</Pill>
            <Pill tone={telemetry?.detection_enabled ? 'good' : 'idle'}>AI</Pill>
            <Pill tone={telemetry?.homed ? 'good' : 'warn'}>
              {telemetry?.homed ? 'homed' : 'not homed'}
            </Pill>
            <Pill tone={telemetry?.valve_open ? 'bad' : 'idle'}>
              valve {telemetry?.valve_open ? 'open' : 'closed'}
            </Pill>
          </div>
          <StatusRow label="Pan" value={`${(telemetry?.pan_deg ?? 0).toFixed(2)}°`} />
          <StatusRow label="Tilt" value={`${(telemetry?.tilt_deg ?? 0).toFixed(2)}°`} />
          <StatusRow label="Moving" value={telemetry?.moving ? 'yes' : 'no'} />
          <StatusRow
            label="Endstops"
            value={
              Object.entries(telemetry?.limits ?? {})
                .filter(([, hit]) => hit)
                .map(([name]) => name)
                .join(', ') || 'clear'
            }
          />
          <StatusRow
            label="Target"
            value={
              telemetry?.target
                ? `#${telemetry.target.track_id} ${telemetry.target.class} ${(
                    telemetry.target.confidence * 100
                  ).toFixed(0)}%`
                : '—'
            }
          />
          <StatusRow label="Tracks" value={telemetry?.tracks.length ?? 0} />
          <StatusRow
            label="Water budget"
            value={
              telemetry?.spray
                ? `${telemetry.spray.used_ms} / ${telemetry.spray.budget_ms} ms`
                : '—'
            }
          />
        </Card>

        <Card title="Modes">
          <Toggle
            label="Automatic targeting"
            hint="Runs the engagement state machine. Still requires the system to be armed."
            checked={settings.data?.targeting.auto_enabled ?? false}
            onChange={(value) => patch('targeting', { auto_enabled: value })}
            disabled={!settings.data}
          />
          <Toggle
            label="Detection"
            hint="Turn off for a pure manual camera turret."
            checked={settings.data?.detector.enabled ?? false}
            onChange={(value) => patch('detector', { enabled: value })}
            disabled={!settings.data}
          />
          <Toggle
            label="Water output"
            hint="Master switch for the valve. Off means the turret can never spray."
            checked={settings.data?.spray.enabled ?? false}
            onChange={(value) => patch('spray', { enabled: value })}
            disabled={!settings.data}
          />
          <Toggle
            label="Click-to-aim"
            hint="Click the video to point the turret at that spot."
            checked={clickToAim}
            onChange={setClickToAim}
          />
        </Card>

        <Card
          title="Presets"
          actions={
            <button
              className="btn px-2 py-1 text-xs"
              disabled={!connected}
              onClick={async () => {
                const name = window.prompt('Preset name');
                if (!name) return;
                if (await attempt(() => api.createPreset(name), 'preset saved')) presets.reload();
              }}
            >
              Save current
            </button>
          }
        >
          {presets.data?.length ? (
            <ul className="space-y-1">
              {presets.data.map((preset: Preset) => (
                <li key={preset.id} className="flex items-center gap-2">
                  <button
                    className="btn flex-1 justify-start px-2 py-1 text-sm"
                    disabled={!canMove}
                    onClick={() => attempt(() => api.gotoPreset(preset.id))}
                  >
                    {preset.name}
                    <span className="tabular ml-auto text-xs text-muted">
                      {preset.pan_deg.toFixed(1)}° / {preset.tilt_deg.toFixed(1)}°
                    </span>
                  </button>
                  <button
                    className="btn px-2 py-1 text-xs"
                    onClick={async () => {
                      if (await attempt(() => api.deletePreset(preset.id))) presets.reload();
                    }}
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">No presets yet.</p>
          )}
        </Card>
      </aside>
    </div>
  );
}
