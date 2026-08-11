import { useState } from 'react';
import { api } from '../api/client';
import type { DetectionCapture, DetectionReviewStatus } from '../api/types';
import { Banner, Card, Pill, Spinner } from '../components/ui';
import { useAsync } from '../hooks/useAsync';

type StatusFilter = '' | DetectionReviewStatus;

const STATUS_TONE: Record<DetectionReviewStatus, 'idle' | 'good' | 'bad'> = {
  unreviewed: 'idle',
  training: 'good',
  rejected: 'bad',
};

export default function Detections() {
  const [status, setStatus] = useState<StatusFilter>('unreviewed');
  const [className, setClassName] = useState('');
  const [busy, setBusy] = useState<number | 'manual' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const captures = useAsync(
    () =>
      api.detectionCaptures({
        limit: 200,
        review_status: status || undefined,
        class_name: className.trim() || undefined,
      }),
    [status, className],
  );

  async function act(id: number, operation: () => Promise<unknown>) {
    setBusy(id);
    setActionError(null);
    try {
      await operation();
      captures.reload();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  async function saveCurrentFrame() {
    setBusy('manual');
    setActionError(null);
    try {
      await api.saveDetectionCapture();
      setStatus('unreviewed');
      captures.reload();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      <Card
        title="Detection evidence"
        actions={
          <button
            className="btn px-3 py-1.5 text-xs"
            disabled={busy !== null}
            onClick={saveCurrentFrame}
          >
            {busy === 'manual' ? 'Saving…' : 'Save current frame'}
          </button>
        }
      >
        <p className="mb-3 text-sm text-muted">
          Automatic captures contain the original camera frame and every model proposal. Save the
          current frame manually when a visible bird was missed.
        </p>
        <div className="flex flex-wrap gap-2">
          <select
            className="field max-w-[12rem]"
            value={status}
            onChange={(event) => setStatus(event.target.value as StatusFilter)}
          >
            <option value="">all review states</option>
            <option value="unreviewed">unreviewed</option>
            <option value="training">kept for training</option>
            <option value="rejected">rejected</option>
          </select>
          <input
            className="field max-w-[14rem]"
            placeholder="filter class, e.g. bird"
            value={className}
            onChange={(event) => setClassName(event.target.value)}
          />
          <button className="btn px-3 py-1.5 text-xs" onClick={captures.reload}>
            Reload
          </button>
        </div>
      </Card>

      {(captures.error || actionError) && <Banner>{captures.error || actionError}</Banner>}
      {captures.loading && <Spinner />}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {captures.data?.map((capture) => (
          <CaptureCard
            key={capture.id}
            capture={capture}
            disabled={busy !== null}
            onReview={(reviewStatus, label) =>
              act(capture.id, () =>
                api.reviewDetectionCapture(capture.id, reviewStatus, label),
              )
            }
            onDelete={() =>
              act(capture.id, () => api.deleteDetectionCapture(capture.id))
            }
          />
        ))}
      </div>

      {!captures.loading && captures.data?.length === 0 && (
        <Card>
          <p className="py-6 text-center text-sm text-muted">
            No captures match these filters yet.
          </p>
        </Card>
      )}
    </div>
  );
}

function CaptureCard({
  capture,
  disabled,
  onReview,
  onDelete,
}: {
  capture: DetectionCapture;
  disabled: boolean;
  onReview: (status: DetectionReviewStatus, label: string) => void;
  onDelete: () => void;
}) {
  return (
    <Card
      title={`${capture.class_name || 'unlabelled'} · ${
        capture.confidence === null ? 'manual' : capture.confidence.toFixed(2)
      }`}
      actions={<Pill tone={STATUS_TONE[capture.review_status]}>{capture.review_status}</Pill>}
    >
      <div className="relative overflow-hidden rounded-lg border border-edge bg-black">
        <img
          className="block h-auto w-full"
          src={api.detectionCaptureImage(capture.id)}
          alt={`${capture.class_name || 'manual'} detection`}
          loading="lazy"
        />
        <svg
          className="pointer-events-none absolute inset-0 h-full w-full"
          viewBox={`0 0 ${capture.frame_width} ${capture.frame_height}`}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          {capture.detections.map((detection, index) => {
            const [x1, y1, x2, y2] = detection.bbox;
            return (
              <g key={`${detection.class_name}-${index}`}>
                <rect
                  x={x1}
                  y={y1}
                  width={Math.max(1, x2 - x1)}
                  height={Math.max(1, y2 - y1)}
                  fill="none"
                  stroke="#f7b955"
                  strokeWidth={Math.max(2, capture.frame_width / 400)}
                />
              </g>
            );
          })}
        </svg>
      </div>

      <div className="mt-3 space-y-1 text-xs text-muted">
        <p>{new Date(capture.ts).toLocaleString()}</p>
        <p>
          camera={capture.camera_id} trigger={capture.trigger} proposals=
          {capture.detections.length}
        </p>
        <p className="truncate" title={capture.model_name}>
          model={capture.model_name}
        </p>
        {capture.detections.length > 0 && (
          <p>
            {capture.detections
              .map((item) => `${item.class_name} ${item.confidence.toFixed(2)}`)
              .join(' · ')}
          </p>
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          className="btn px-2 py-1 text-xs"
          disabled={disabled}
          onClick={() => onReview('training', 'bird')}
        >
          Confirm bird
        </button>
        <button
          className="btn px-2 py-1 text-xs"
          disabled={disabled}
          onClick={() => onReview('rejected', 'not-bird')}
        >
          Not a bird
        </button>
        {capture.review_status !== 'unreviewed' && (
          <button
            className="btn px-2 py-1 text-xs"
            disabled={disabled}
            onClick={() => onReview('unreviewed', '')}
          >
            Reset
          </button>
        )}
        <button
          className="btn ml-auto px-2 py-1 text-xs text-bad"
          disabled={disabled}
          onClick={() => {
            if (window.confirm('Delete this detection image permanently?')) onDelete();
          }}
        >
          Delete
        </button>
      </div>
    </Card>
  );
}
