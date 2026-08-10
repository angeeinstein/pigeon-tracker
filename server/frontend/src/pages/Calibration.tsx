/**
 * Calibration workflow:
 *   1. click the spot in the image
 *   2. jog the turret until the nozzle points at that spot in the real world
 *   3. save — the pair (pixel, angles) becomes a calibration point
 *
 * Points are grouped by surface (railing / planter / floor), because a balcony
 * is not one plane and the same pixel maps to different angles depending on
 * what the bird is standing on.
 */

import { useState } from 'react';
import { api } from '../api/client';
import Joystick from '../components/Joystick';
import VideoView from '../components/VideoView';
import { Banner, Card, Pill, SelectField, StatusRow, TextField } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { useLive, useToast } from '../state';

const SURFACES = [
  { value: 'default', label: 'default' },
  { value: 'railing', label: 'railing' },
  { value: 'planter', label: 'planter' },
  { value: 'floor', label: 'floor' },
] as const;

export default function Calibration() {
  const { telemetry, jog } = useLive();
  const { attempt, notify } = useToast();
  const points = useAsync(() => api.calibrationPoints(), []);
  const model = useAsync(() => api.calibrationModel(), []);

  const [pending, setPending] = useState<{ x: number; y: number } | null>(null);
  const [surface, setSurface] = useState<string>('default');
  const [label, setLabel] = useState('');

  const connected = telemetry?.controller_connected ?? false;
  const homed = telemetry?.homed ?? false;

  const save = async () => {
    if (!pending) return;
    if (!connected) {
      notify('controller not connected', 'bad');
      return;
    }
    const ok = await attempt(
      () => api.addCalibrationPoint({ x: pending.x, y: pending.y, surface, label }),
      'calibration point saved',
    );
    if (ok) {
      setPending(null);
      setLabel('');
      points.reload();
      model.reload();
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="space-y-4">
        {!homed && (
          <Banner tone="warn">
            The turret is not homed. Calibration points saved now would record angles that do not
            survive a restart — home first.
          </Banner>
        )}

        <VideoView
          telemetry={telemetry}
          showTracks={false}
          onPick={(x, y) => setPending({ x, y })}
        >
          {points.data?.map((point) => (
            <g key={point.id}>
              <circle
                cx={point.cam_x}
                cy={point.cam_y}
                r={0.008}
                fill={point.enabled ? '#3ecf8e' : '#96a0b5'}
              />
              <circle
                cx={point.cam_x}
                cy={point.cam_y}
                r={0.018}
                fill="none"
                stroke={point.enabled ? '#3ecf8e' : '#96a0b5'}
                strokeWidth={0.002}
              />
            </g>
          ))}
          {pending && (
            <g stroke="#f5a524" strokeWidth={0.003} fill="none">
              <circle cx={pending.x} cy={pending.y} r={0.03} />
              <line x1={pending.x - 0.05} y1={pending.y} x2={pending.x + 0.05} y2={pending.y} />
              <line x1={pending.x} y1={pending.y - 0.05} x2={pending.x} y2={pending.y + 0.05} />
            </g>
          )}
        </VideoView>

        <Card title="Step 2 — aim the turret at that spot">
          <div className="flex flex-wrap items-start gap-5">
            <Joystick onChange={jog} disabled={!connected} size={150} />
            <div className="grid flex-1 grid-cols-3 gap-2 sm:max-w-xs">
              <span />
              <button
                className="btn"
                disabled={!connected}
                onClick={() => attempt(() => api.moveRelative(0, 1))}
              >
                ↑
              </button>
              <span />
              <button
                className="btn"
                disabled={!connected}
                onClick={() => attempt(() => api.moveRelative(-1, 0))}
              >
                ←
              </button>
              <button
                className="btn"
                disabled={!connected}
                onClick={() => attempt(() => api.moveRelative(0, -1))}
              >
                ↓
              </button>
              <button
                className="btn"
                disabled={!connected}
                onClick={() => attempt(() => api.moveRelative(1, 0))}
              >
                →
              </button>
              <span className="col-span-3 text-xs text-muted">
                Arrow buttons move 1°; the joystick jogs continuously.
              </span>
            </div>
          </div>
        </Card>
      </div>

      <aside className="space-y-4">
        <Card title="Step 3 — save the point">
          <StatusRow
            label="Image point"
            value={pending ? `${pending.x.toFixed(3)}, ${pending.y.toFixed(3)}` : 'click the video'}
          />
          <StatusRow
            label="Turret"
            value={`${(telemetry?.pan_deg ?? 0).toFixed(2)}° / ${(telemetry?.tilt_deg ?? 0).toFixed(2)}°`}
          />
          <SelectField
            label="Surface"
            value={surface}
            options={SURFACES as unknown as { value: string; label: string }[]}
            onChange={setSurface}
            hint="Group points by the physical surface they sit on."
          />
          <TextField label="Label (optional)" value={label} onChange={setLabel} />
          <button className="btn btn-primary mt-2 w-full" disabled={!pending} onClick={save}>
            Save calibration point
          </button>
        </Card>

        <Card
          title="Model"
          actions={
            <button
              className="btn px-2 py-1 text-xs"
              onClick={() => {
                points.reload();
                model.reload();
              }}
            >
              Refresh
            </button>
          }
        >
          <div className="mb-2 flex gap-2">
            <Pill tone={model.data?.calibrated ? 'good' : 'warn'}>
              {model.data?.calibrated ? 'calibrated' : 'not calibrated'}
            </Pill>
            <Pill tone="idle">{model.data?.strategy ?? '—'}</Pill>
          </div>
          {(model.data?.surfaces as { surface: string; points: number; strategy: string }[])?.map(
            (entry) => (
              <StatusRow
                key={entry.surface}
                label={entry.surface}
                value={`${entry.points} points · ${entry.strategy}`}
              />
            ),
          )}
          {!model.data?.calibrated && (
            <p className="mt-2 text-xs text-muted">
              Three or more points per surface enable interpolation; fewer falls back to
              nearest-point aiming.
            </p>
          )}
        </Card>

        <Card
          title={`Points (${points.data?.length ?? 0})`}
          actions={
            <button
              className="btn px-2 py-1 text-xs"
              onClick={async () => {
                if (!window.confirm('Delete every calibration point?')) return;
                if (await attempt(() => api.clearCalibration(), 'calibration cleared')) {
                  points.reload();
                  model.reload();
                }
              }}
            >
              Clear all
            </button>
          }
        >
          <div className="max-h-80 space-y-1 overflow-y-auto">
            {points.data?.map((point) => (
              <div key={point.id} className="flex items-center gap-2 text-xs">
                <span className="tabular flex-1">
                  {point.surface} · {point.cam_x.toFixed(3)},{point.cam_y.toFixed(3)} →{' '}
                  {point.pan_deg.toFixed(1)}°/{point.tilt_deg.toFixed(1)}°
                  {point.label && ` · ${point.label}`}
                </span>
                <button
                  className="btn px-2 py-0.5"
                  onClick={async () => {
                    if (await attempt(() => api.deleteCalibrationPoint(point.id))) {
                      points.reload();
                      model.reload();
                    }
                  }}
                >
                  ✕
                </button>
              </div>
            ))}
            {points.data?.length === 0 && <p className="text-sm text-muted">No points yet.</p>}
          </div>
        </Card>
      </aside>
    </div>
  );
}
