import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { Banner, Card, Pill, Spinner, StatusRow } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { useToast } from '../state';

export default function System() {
  const health = useAsync(() => api.health(), []);
  const system = useAsync(() => api.system(), []);
  const update = useAsync(() => api.systemUpdateStatus(), []);
  const { attempt } = useToast();
  const [updateAction, setUpdateAction] = useState<'check' | 'start' | null>(null);
  const [updateRequested, setUpdateRequested] = useState(
    () => window.sessionStorage.getItem('turret-update-requested') !== null,
  );

  const updateRunning = ['starting', 'checking', 'updating', 'restarting', 'verifying'].includes(
    update.data?.state ?? '',
  );

  useEffect(() => {
    const timer = window.setInterval(update.reload, updateRunning ? 1_000 : 30_000);
    return () => window.clearInterval(timer);
    // `reload` deliberately changes identity; polling cadence depends on state only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [updateRunning]);

  useEffect(() => {
    if (update.data?.state !== 'failed') return;
    window.sessionStorage.removeItem('turret-update-requested');
    setUpdateRequested(false);
  }, [update.data?.state]);

  async function checkForUpdate() {
    setUpdateAction('check');
    try {
      await attempt(async () => {
        await api.checkSystemUpdate();
        update.reload();
      }, 'version check completed');
    } finally {
      setUpdateAction(null);
    }
  }

  async function startUpdate() {
    const target = update.data?.version_check.latest_commit?.slice(0, 7) ?? 'the latest version';
    if (
      !window.confirm(
        `Update the server to ${target}?\n\nThe turret will be disarmed. The web interface will disconnect briefly and refresh automatically after endpoint verification.`,
      )
    ) {
      return;
    }
    setUpdateAction('start');
    try {
      await attempt(async () => {
        await api.startSystemUpdate();
        window.sessionStorage.setItem('turret-update-requested', Date.now().toString());
        setUpdateRequested(true);
        update.reload();
      }, 'update started');
    } finally {
      setUpdateAction(null);
    }
  }

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
              update.reload();
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
        <StatusRow label="Firmware" value={controller.controller.firmware_version || '—'} />
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
        <StatusRow label="Mode" value={controller.mode} />
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

      <UpdateCard
        status={update.data}
        error={update.error}
        busy={updateAction}
        updateRequested={updateRequested}
        onCheck={() => void checkForUpdate()}
        onStart={() => void startUpdate()}
      />
    </div>
  );
}

function UpdateCard({
  status,
  error,
  busy,
  updateRequested,
  onCheck,
  onStart,
}: {
  status: Awaited<ReturnType<typeof api.systemUpdateStatus>> | null;
  error: string | null;
  busy: 'check' | 'start' | null;
  updateRequested: boolean;
  onCheck: () => void;
  onStart: () => void;
}) {
  const version = status?.version_check;
  const running = ['starting', 'checking', 'updating', 'restarting', 'verifying'].includes(
    status?.state ?? '',
  );
  const short = (commit: string | null | undefined) => commit?.slice(0, 7) || 'unknown';
  const tone: 'bad' | 'good' | 'info' =
    status?.state === 'failed' ? 'bad' : status?.state === 'succeeded' ? 'good' : 'info';

  return (
    <Card
      title="Server update"
      className="md:col-span-2"
      actions={
        <div className="flex gap-2">
          <button
            className="btn px-3 py-1.5 text-xs"
            disabled={running || busy !== null}
            onClick={onCheck}
          >
            {busy === 'check' || version?.checking ? 'Checking...' : 'Check now'}
          </button>
          <button
            className="btn btn-primary px-3 py-1.5 text-xs"
            disabled={
              running ||
              busy !== null ||
              !status?.updater_available ||
              version?.update_available !== true
            }
            onClick={onStart}
          >
            {busy === 'start' ? 'Starting...' : running ? 'Updating...' : 'Update server'}
          </button>
        </div>
      }
    >
      {error ? <Banner tone="warn">{error}</Banner> : null}
      <div className="mb-3 flex flex-wrap gap-2">
        {version?.update_available === true ? (
          <Pill tone="warn">update available</Pill>
        ) : version?.update_available === false ? (
          <Pill tone="good">up to date</Pill>
        ) : (
          <Pill tone="idle">version unknown</Pill>
        )}
        <Pill tone={status?.updater_available ? 'good' : 'bad'}>
          web updater {status?.updater_available ? 'ready' : 'not installed'}
        </Pill>
        {running || status?.state === 'failed' || status?.state === 'succeeded' ? (
          <Pill tone={tone}>{status?.state}</Pill>
        ) : null}
      </div>

      <div className="grid gap-x-8 md:grid-cols-2">
        <StatusRow label="Installed commit" value={short(version?.current_commit)} />
        <StatusRow label="Latest commit" value={short(version?.latest_commit)} />
        <StatusRow label="Branch" value={version?.branch ?? 'unknown'} />
        <StatusRow
          label="Last checked"
          value={version?.checked_at ? new Date(version.checked_at).toLocaleString() : 'not yet'}
        />
      </div>

      {version?.check_error ? <Banner tone="warn">{version.check_error}</Banner> : null}
      {status && !status.updater_available ? (
        <Banner tone="warn">
          Run the shell updater once to install the restricted update service and request path.
          Future updates can then be run here without shell access.
        </Banner>
      ) : null}

      {status?.state === 'succeeded' ? (
        <div className="mt-4 rounded-lg border border-edge bg-panelalt/35 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="font-medium text-good">✓ Update finished successfully</span>
            {status.finished_at ? (
              <span className="text-muted">
                Finished: {new Date(status.finished_at).toLocaleString()}
              </span>
            ) : null}
          </div>
          <p className="mt-2 text-xs text-muted">{status.message}</p>
          {updateRequested ? (
            <p className="mt-2 text-xs text-good">
              Endpoints passed. Reconnecting and refreshing this page...
            </p>
          ) : null}
          <details className="mt-3 border-t border-edge pt-2">
            <summary className="cursor-pointer select-none text-xs font-medium text-muted hover:text-ink">
              Show update progress and logs
            </summary>
            <UpdateProgress status={status} />
          </details>
        </div>
      ) : status && status.state !== 'idle' ? (
        <div className="mt-4 rounded-lg border border-edge bg-panelalt/35 p-3">
          {status.state === 'failed' ? (
            <Banner tone="warn">
              The updater failed. Its final log output is preserved below.
            </Banner>
          ) : null}
          <UpdateProgress status={status} showMessage />
        </div>
      ) : (
        <p className="mt-3 text-xs text-muted">
          Version checks run in the background every five minutes. Updates make a backup, rebuild
          dependencies and the UI, restart the service, then verify the real HTTP endpoints.
        </p>
      )}
    </Card>
  );
}

function UpdateProgress({
  status,
  showMessage = false,
}: {
  status: NonNullable<Awaited<ReturnType<typeof api.systemUpdateStatus>>>;
  showMessage?: boolean;
}) {
  return (
    <div className={showMessage ? '' : 'mt-3'}>
      {showMessage ? (
        <div className="mb-2 flex items-center justify-between gap-3 text-xs">
          <span className="font-medium text-ink">{status.message}</span>
          <span className="tabular text-muted">{status.progress}%</span>
        </div>
      ) : null}
      <div className="h-2 overflow-hidden rounded-full bg-edge/70">
        <div
          className={`h-full transition-[width] duration-500 ${status.state === 'failed' ? 'bg-bad' : 'bg-accent'}`}
          style={{ width: `${Math.max(1, Math.min(100, status.progress))}%` }}
        />
      </div>
      <div className="mt-2 flex flex-wrap justify-between gap-2 text-xs text-muted">
        <span>
          Phase: {status.phase} · {status.progress}%
        </span>
        {status.started_at ? (
          <span>Started: {new Date(status.started_at).toLocaleString()}</span>
        ) : null}
      </div>
      {status.log_tail.length ? (
        <pre className="mt-3 max-h-80 overflow-auto rounded border border-edge bg-[#090c11] p-3 text-xs leading-relaxed text-muted">
          {status.log_tail.join('\n')}
        </pre>
      ) : null}
    </div>
  );
}
