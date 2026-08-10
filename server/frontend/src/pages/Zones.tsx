/**
 * Polygon zone editor.
 *
 * Click to add vertices, then save. Zone semantics live on the server
 * (app/targeting/zones.py); this page only draws and stores polygons.
 */

import { useState } from 'react';
import { api } from '../api/client';
import type { ZoneRecord, ZoneTypeName } from '../api/types';
import VideoView from '../components/VideoView';
import { Card, Pill, SelectField, TextField } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { useLive, useToast } from '../state';

const ZONE_COLORS: Record<ZoneTypeName, string> = {
  active: '#3ecf8e',
  no_target: '#f3496a',
  no_spray: '#f5a524',
  railing: '#e5e56b',
  planter: '#4fd0b0',
  floor: '#b47ce0',
};

const ZONE_HELP: Record<ZoneTypeName, string> = {
  active: 'Automatic targeting only engages inside active zones (if any exist).',
  no_target: 'Never engage anything here.',
  no_spray: 'Aiming is allowed, but the valve stays shut.',
  railing: 'Surface: calibration points tagged "railing" are used here.',
  planter: 'Surface: calibration points tagged "planter" are used here.',
  floor: 'Surface: calibration points tagged "floor" are used here.',
};

const TYPE_OPTIONS = (Object.keys(ZONE_COLORS) as ZoneTypeName[]).map((value) => ({
  value,
  label: value.replace('_', ' '),
}));

export default function Zones() {
  const { telemetry } = useLive();
  const { attempt, notify } = useToast();
  const zones = useAsync(() => api.zones(), []);

  const [draft, setDraft] = useState<[number, number][]>([]);
  const [name, setName] = useState('');
  const [zoneType, setZoneType] = useState<ZoneTypeName>('active');

  const save = async () => {
    if (draft.length < 3) {
      notify('a zone needs at least three points', 'bad');
      return;
    }
    const ok = await attempt(
      () =>
        api.createZone({
          name: name.trim() || zoneType,
          zone_type: zoneType,
          points: draft,
        }),
      'zone saved',
    );
    if (ok) {
      setDraft([]);
      setName('');
      zones.reload();
    }
  };

  const polygonPoints = (points: [number, number][]) =>
    points.map(([x, y]) => `${x},${y}`).join(' ');

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="space-y-4">
        <VideoView
          telemetry={telemetry}
          serverOverlays={false}
          showTracks
          onPick={(x, y) => setDraft((current) => [...current, [x, y]])}
        >
          {zones.data?.map((zone: ZoneRecord) => (
            <polygon
              key={zone.id}
              points={polygonPoints(zone.points)}
              fill={ZONE_COLORS[zone.zone_type]}
              fillOpacity={zone.enabled ? 0.16 : 0.05}
              stroke={ZONE_COLORS[zone.zone_type]}
              strokeOpacity={zone.enabled ? 0.9 : 0.35}
              strokeWidth={0.003}
            />
          ))}
          {draft.length > 0 && (
            <>
              <polygon
                points={polygonPoints(draft)}
                fill={ZONE_COLORS[zoneType]}
                fillOpacity={0.2}
                stroke={ZONE_COLORS[zoneType]}
                strokeWidth={0.004}
                strokeDasharray="0.01 0.01"
              />
              {draft.map(([x, y], index) => (
                <circle key={index} cx={x} cy={y} r={0.008} fill={ZONE_COLORS[zoneType]} />
              ))}
            </>
          )}
        </VideoView>
        <p className="text-xs text-muted">
          Click the video to add points. {draft.length} point{draft.length === 1 ? '' : 's'} in the
          current draft.
        </p>
      </div>

      <aside className="space-y-4">
        <Card title="New zone">
          <SelectField
            label="Type"
            value={zoneType}
            options={TYPE_OPTIONS}
            onChange={setZoneType}
            hint={ZONE_HELP[zoneType]}
          />
          <TextField label="Name" value={name} onChange={setName} placeholder={zoneType} />
          <div className="mt-2 grid grid-cols-3 gap-2">
            <button
              className="btn"
              disabled={draft.length === 0}
              onClick={() => setDraft((current) => current.slice(0, -1))}
            >
              Undo
            </button>
            <button className="btn" disabled={draft.length === 0} onClick={() => setDraft([])}>
              Clear
            </button>
            <button className="btn btn-primary" disabled={draft.length < 3} onClick={save}>
              Save
            </button>
          </div>
        </Card>

        <Card title={`Zones (${zones.data?.length ?? 0})`}>
          <div className="space-y-2">
            {zones.data?.map((zone) => (
              <div key={zone.id} className="flex items-center gap-2">
                <span
                  className="h-3 w-3 shrink-0 rounded-sm"
                  style={{ background: ZONE_COLORS[zone.zone_type] }}
                />
                <span className="flex-1 truncate text-sm">
                  {zone.name}
                  <span className="ml-2 text-xs text-muted">{zone.zone_type}</span>
                </span>
                <button
                  className="btn px-2 py-0.5 text-xs"
                  onClick={async () => {
                    if (
                      await attempt(() => api.updateZone(zone.id, { enabled: !zone.enabled }))
                    ) {
                      zones.reload();
                    }
                  }}
                >
                  {zone.enabled ? 'on' : 'off'}
                </button>
                <button
                  className="btn px-2 py-0.5 text-xs"
                  onClick={async () => {
                    if (!window.confirm(`Delete zone "${zone.name}"?`)) return;
                    if (await attempt(() => api.deleteZone(zone.id), 'zone deleted')) {
                      zones.reload();
                    }
                  }}
                >
                  ✕
                </button>
              </div>
            ))}
            {zones.data?.length === 0 && <p className="text-sm text-muted">No zones yet.</p>}
          </div>
        </Card>

        <Card title="How zones are used">
          <ul className="space-y-2 text-xs text-muted">
            {(Object.keys(ZONE_HELP) as ZoneTypeName[]).map((type) => (
              <li key={type} className="flex gap-2">
                <Pill tone="idle">{type}</Pill>
                <span className="flex-1">{ZONE_HELP[type]}</span>
              </li>
            ))}
          </ul>
        </Card>
      </aside>
    </div>
  );
}
