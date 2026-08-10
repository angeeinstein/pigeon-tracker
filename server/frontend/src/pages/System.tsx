import { api } from '../api/client';
import { Banner, Card, Pill, Spinner, StatusRow } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { useToast } from '../state';

export default function System() {
  const health = useAsync(() => api.health(), []);
  const system = useAsync(() => api.system(), []);
  const { attempt } = useToast();

  if (health.loading) return <Spinner />;
  if (health.error || !health.data) return <Banner>{health.error ?? 'no health data'}</Banner>;

  const info = health.data;
  const controller = info.controller;
  const gpu = (system.data?.gpu ?? {}) as { torch?: boolean; cuda?: boolean; devices?: string[] };
  const paths = (system.data?.paths ?? {}) as Record<string, string>;

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card
        title="Health"
        actions={
          <button
            className="btn px-2 py-1 text-xs"
            onClick={() => {
              health.reload();
              system.reload();
            }}
          >
            Refresh
          </button>
        }
      >
        <div className="mb-3 flex flex-wrap gap-2">
          <Pill tone={info.status === 'ok' ? 'good' : 'warn'}>{info.status}</Pill>
          {Object.entries(info.checks).map(([name, ok]) => (
            <Pill key={name} tone={ok ? 'good' : 'bad'}>
              {name}
            </Pill>
          ))}
        </div>
        <StatusRow label="Uptime" value={`${Math.round(info.uptime_s)} s`} />
        <StatusRow label="State" value={info.system_state} />
        <StatusRow label="Armed" value={info.armed ? 'yes' : 'no'} />
        <StatusRow label="Telemetry clients" value={info.telemetry_clients} />
      </Card>

      <Card title="Versions">
        <StatusRow label="Server" value={info.version.server_version} />
        <StatusRow label="Git commit" value={info.version.git_commit} />
        <StatusRow label="Protocol (server)" value={info.version.protocol_version} />
        <StatusRow
          label="Firmware"
          value={controller.controller.firmware_version || '—'}
        />
        <StatusRow
          label="Protocol (firmware)"
          value={
            controller.controller.protocol_version ? (
              <span
                className={
                  controller.controller.protocol_version === info.version.protocol_version
                    ? ''
                    : 'text-bad'
                }
              >
                {controller.controller.protocol_version}
              </span>
            ) : (
              '—'
            )
          }
        />
        {controller.fault && <Banner tone="warn">{controller.fault}</Banner>}
      </Card>

      <Card title="Controller link">
        <StatusRow label="Link" value={controller.link} />
        <StatusRow label="Controller id" value={controller.controller.controller_id || '—'} />
        <StatusRow label="Round trip" value={controller.rtt_ms ? `${controller.rtt_ms} ms` : '—'} />
        <StatusRow label="Commands sent" value={controller.commands_sent} />
        <StatusRow label="Commands failed" value={controller.commands_failed} />
        <StatusRow
          label="Capabilities"
          value={controller.controller.capabilities.join(', ') || '—'}
        />
        <button
          className="btn mt-3 w-full"
          disabled={!controller.connected}
          onClick={() => attempt(() => api.pushControllerConfig(), 'configuration pushed')}
        >
          Push configuration to controller
        </button>
      </Card>

      <Card title="Cameras">
        {info.camera.cameras.map((camera) => (
          <div key={camera.camera_id} className="mb-3 last:mb-0">
            <div className="mb-1 flex items-center gap-2">
              <Pill tone={camera.connected ? 'good' : camera.enabled ? 'bad' : 'idle'}>
                {camera.camera_id}
              </Pill>
              <span className="text-xs text-muted">{camera.backend}</span>
            </div>
            <StatusRow
              label="Resolution"
              value={camera.width ? `${camera.width}×${camera.height}` : '—'}
            />
            <StatusRow label="Frame rate" value={`${camera.fps.toFixed(1)} fps`} />
            <StatusRow label="Reconnects" value={camera.reconnects} />
            {camera.error && <Banner tone="warn">{camera.error}</Banner>}
          </div>
        ))}
      </Card>

      <Card title="AI">
        <pre className="tabular overflow-x-auto whitespace-pre-wrap text-xs text-muted">
          {JSON.stringify(info.vision, null, 2)}
        </pre>
      </Card>

      <Card title="Environment">
        <StatusRow label="Torch installed" value={gpu.torch ? 'yes' : 'no'} />
        <StatusRow label="CUDA" value={gpu.cuda ? (gpu.devices?.join(', ') ?? 'yes') : 'no'} />
        <StatusRow
          label="Authentication"
          value={system.data?.auth_enabled ? 'enabled' : 'disabled'}
        />
        <StatusRow
          label="Controller token"
          value={system.data?.controller_token_configured ? 'configured' : 'not set'}
        />
        {Object.entries(paths).map(([key, value]) => (
          <StatusRow key={key} label={key} value={<span className="text-xs">{value}</span>} />
        ))}
      </Card>
    </div>
  );
}
