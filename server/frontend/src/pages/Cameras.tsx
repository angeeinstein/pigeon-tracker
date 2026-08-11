import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { CameraStatus } from '../api/types';
import VideoView from '../components/VideoView';
import { Banner, Card, Pill, Spinner, StatusRow } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { useLive } from '../state';

export default function Cameras() {
  const { telemetry } = useLive();
  const cameras = useAsync(() => api.cameras(), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setInterval(cameras.reload, 5000);
    return () => window.clearInterval(timer);
    // The loader is intentionally polled independently of render cycles.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!cameras.data) return;
    if (!selectedId || !cameras.data.cameras.some((camera) => camera.camera_id === selectedId)) {
      setSelectedId(cameras.data.primary_id || cameras.data.cameras[0]?.camera_id || null);
    }
  }, [cameras.data, selectedId]);

  if (cameras.loading && !cameras.data) return <Spinner />;
  if (cameras.error || !cameras.data) return <Banner>{cameras.error ?? 'no cameras'}</Banner>;

  const cameraData = cameras.data;
  const selected =
    cameraData.cameras.find((camera) => camera.camera_id === selectedId) ??
    cameraData.cameras[0];
  const isPrimary = selected?.camera_id === cameraData.primary_id;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-ink">Camera feeds</h1>
          <p className="mt-1 text-sm text-muted">
            Inspect every configured camera without changing the detection source.
          </p>
        </div>
        <Link className="btn px-3 py-1.5 text-xs" to="/settings">
          Manage cameras
        </Link>
      </div>

      {!isPrimary && selected && (
        <Banner tone="info">
          Viewing an auxiliary feed. Detection, tracking, calibration and automatic targeting
          continue to use <strong>{cameraName(cameraData.cameras, cameraData.primary_id)}</strong>.
        </Banner>
      )}

      {selected ? (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_19rem]">
          <Card
            title={selected.name}
            titleClassName="text-base font-semibold text-ink"
            actions={isPrimary ? <Pill tone="info">primary / AI source</Pill> : <Pill tone="idle">view only</Pill>}
          >
            <VideoView
              telemetry={telemetry}
              cameraId={selected.camera_id}
              cameraConnected={selected.connected}
              frameSize={
                selected.width && selected.height
                  ? { width: selected.width, height: selected.height }
                  : undefined
              }
              serverOverlays={isPrimary}
              showTracks={isPrimary}
              showAimMarkers={isPrimary}
              cursor="default"
            />
          </Card>

          <aside className="space-y-4">
            <Card title="Selected camera">
              <div className="mb-3 flex flex-wrap gap-2">
                <Pill tone={selected.connected ? 'good' : selected.enabled ? 'bad' : 'idle'}>
                  {selected.connected ? 'connected' : selected.enabled ? 'unavailable' : 'disabled'}
                </Pill>
                <Pill tone="idle">{selected.backend}</Pill>
              </div>
              <StatusRow label="Id" value={selected.camera_id} />
              <StatusRow
                label="Resolution"
                value={selected.width ? `${selected.width} x ${selected.height}` : '-'}
              />
              <StatusRow label="Frame rate" value={`${selected.fps.toFixed(1)} fps`} />
              <StatusRow label="Frames" value={selected.frames.toLocaleString()} />
              <StatusRow label="Reconnects" value={selected.reconnects} />
              {selected.error && <div className="mt-3"><Banner tone="warn">{selected.error}</Banner></div>}
            </Card>
          </aside>
        </div>
      ) : (
        <Banner tone="warn">No cameras are configured.</Banner>
      )}

      {cameraData.cameras.length > 0 && (
        <section>
          <h2 className="mb-2 text-sm font-semibold text-ink">All cameras</h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {cameraData.cameras.map((camera) => (
              <button
                key={camera.camera_id}
                type="button"
                onClick={() => setSelectedId(camera.camera_id)}
                className={`overflow-hidden rounded-xl border-2 text-left shadow-lg shadow-black/20 transition hover:-translate-y-0.5 hover:border-accent/60 ${
                  camera.camera_id === selected?.camera_id
                    ? 'border-accent bg-accent/10'
                    : 'border-edge bg-panel'
                }`}
              >
                <CameraThumbnail camera={camera} />
                <div className="p-3">
                  <div className="flex items-start justify-between gap-2">
                    <span className="truncate text-sm font-semibold text-ink">{camera.name}</span>
                    <span
                      className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
                        camera.connected ? 'bg-good' : camera.enabled ? 'bg-bad' : 'bg-muted'
                      }`}
                    />
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                    {camera.camera_id === cameraData.primary_id && (
                      <span className="text-accent">primary</span>
                    )}
                    <span>{camera.width ? `${camera.width} x ${camera.height}` : camera.camera_id}</span>
                    {camera.connected && <span>/ {camera.fps.toFixed(1)} fps</span>}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function CameraThumbnail({ camera }: { camera: CameraStatus }) {
  const [nonce, setNonce] = useState(() => Date.now());

  useEffect(() => {
    if (!camera.enabled || !camera.connected) return undefined;
    const timer = window.setInterval(() => setNonce(Date.now()), 3000);
    return () => window.clearInterval(timer);
  }, [camera.enabled, camera.connected]);

  if (!camera.enabled || !camera.connected) {
    return (
      <div className="grid aspect-video place-items-center bg-black/50 text-xs text-muted">
        {camera.enabled ? 'feed unavailable' : 'camera disabled'}
      </div>
    );
  }

  const params = new URLSearchParams({
    camera_id: camera.camera_id,
    overlays: 'false',
    t: String(nonce),
  });
  return (
    <img
      src={`/api/camera/snapshot.jpg?${params.toString()}`}
      alt={`${camera.name} preview`}
      className="aspect-video w-full bg-black object-cover"
      loading="lazy"
    />
  );
}

function cameraName(cameras: CameraStatus[], cameraId: string): string {
  return cameras.find((camera) => camera.camera_id === cameraId)?.name ?? cameraId;
}
