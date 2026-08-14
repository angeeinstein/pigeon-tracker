import {
  useEffect,
  useMemo,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { api } from '../api/client';
import type {
  DetectionAnnotationStatus,
  DetectionCapture,
  DetectionReviewStatus,
} from '../api/types';
import { Banner, Card, Pill, Spinner } from '../components/ui';
import { useAsync } from '../hooks/useAsync';

type StatusFilter = '' | DetectionReviewStatus;
type Point = [number, number];

const STATUS_TONE: Record<DetectionReviewStatus, 'idle' | 'good' | 'bad'> = {
  unreviewed: 'idle',
  training: 'good',
  rejected: 'bad',
};

const BOX_COLOUR: Record<DetectionAnnotationStatus, string> = {
  unreviewed: '#f7b955',
  accepted: '#38d996',
  rejected: '#f3496a',
};

export default function Detections() {
  const [status, setStatus] = useState<StatusFilter>('unreviewed');
  const [className, setClassName] = useState('');
  const [busy, setBusy] = useState<number | 'manual' | 'export' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [viewerCapture, setViewerCapture] = useState<DetectionCapture | null>(null);
  const captures = useAsync(
    () =>
      api.detectionCaptures({
        limit: 500,
        review_status: status || undefined,
        class_name: className.trim() || undefined,
      }),
    [status, className],
  );

  useEffect(() => {
    if (!viewerCapture || !captures.data) return;
    const fresh = captures.data.find((capture) => capture.id === viewerCapture.id);
    if (fresh) setViewerCapture(fresh);
    // Only refresh the open record when the server list changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [captures.data]);

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

  async function updateViewer(
    operation: () => Promise<DetectionCapture>,
  ): Promise<DetectionCapture | null> {
    if (!viewerCapture) return null;
    setBusy(viewerCapture.id);
    setActionError(null);
    try {
      const updated = await operation();
      setViewerCapture(updated);
      captures.reload();
      return updated;
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function saveCurrentFrame() {
    setBusy('manual');
    setActionError(null);
    try {
      const capture = await api.saveDetectionCapture();
      setStatus('unreviewed');
      setViewerCapture(capture);
      captures.reload();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  async function exportDataset() {
    setBusy('export');
    setActionError(null);
    try {
      const { blob, filename } = await api.downloadDetectionDataset();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  function moveViewer(direction: -1 | 1) {
    if (!captures.data?.length || !viewerCapture) return;
    const current = captures.data.findIndex((capture) => capture.id === viewerCapture.id);
    const next =
      current < 0 ? (direction > 0 ? 0 : captures.data.length - 1) : current + direction;
    if (next >= 0 && next < captures.data.length) setViewerCapture(captures.data[next]);
  }

  const viewerIndex =
    viewerCapture && captures.data
      ? captures.data.findIndex((capture) => capture.id === viewerCapture.id)
      : -1;

  return (
    <div className="space-y-4">
      <Card
        title="Detection evidence"
        actions={
          <div className="flex flex-wrap gap-2">
            <button
              className="btn px-3 py-1.5 text-xs"
              disabled={busy !== null}
              onClick={() => void exportDataset()}
            >
              {busy === 'export' ? 'Exporting...' : 'Export YOLO dataset'}
            </button>
            <button
              className="btn px-3 py-1.5 text-xs"
              disabled={busy !== null}
              onClick={saveCurrentFrame}
            >
              {busy === 'manual' ? 'Saving...' : 'Save current frame'}
            </button>
          </div>
        }
      >
        <p className="mb-3 text-sm text-muted">
          Open a capture to review each proposed box. Accepted bird boxes become training labels;
          rejected boxes remain as evidence but are excluded from the exported dataset.
        </p>
        <div className="flex flex-wrap gap-2">
          <select
            className="field max-w-[12rem]"
            value={status}
            onChange={(event) => setStatus(event.target.value as StatusFilter)}
          >
            <option value="">all review states</option>
            <option value="unreviewed">needs box review</option>
            <option value="training">ready for training</option>
            <option value="rejected">negative / no bird</option>
          </select>
          <input
            className="field max-w-[14rem]"
            placeholder="filter trigger class"
            value={className}
            onChange={(event) => setClassName(event.target.value)}
          />
          <button className="btn px-3 py-1.5 text-xs" onClick={captures.reload}>
            Reload
          </button>
        </div>
      </Card>

      {(captures.error || actionError) && <Banner>{captures.error || actionError}</Banner>}
      {captures.loading && !captures.data && <Spinner />}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {captures.data?.map((capture) => (
          <CaptureCard
            key={capture.id}
            capture={capture}
            disabled={busy !== null}
            onOpen={() => setViewerCapture(capture)}
            onDelete={() => act(capture.id, () => api.deleteDetectionCapture(capture.id))}
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

      {viewerCapture && (
        <CaptureReviewer
          capture={viewerCapture}
          busy={busy !== null}
          error={actionError}
          position={viewerIndex >= 0 ? `${viewerIndex + 1} / ${captures.data?.length ?? 0}` : ''}
          canPrevious={viewerIndex > 0}
          canNext={viewerIndex < (captures.data?.length ?? 0) - 1 || viewerIndex < 0}
          onClose={() => setViewerCapture(null)}
          onPrevious={() => moveViewer(-1)}
          onNext={() => moveViewer(1)}
          onReview={(index, reviewStatus) =>
            updateViewer(() =>
              api.reviewDetectionAnnotation(
                viewerCapture.id,
                index,
                reviewStatus,
                reviewStatus === 'accepted' ? 'bird' : '',
              ),
            )
          }
          onAdd={(bbox) =>
            updateViewer(() => api.addDetectionAnnotation(viewerCapture.id, bbox, 'bird'))
          }
          onDeleteAnnotation={(index) =>
            updateViewer(() => api.deleteDetectionAnnotation(viewerCapture.id, index))
          }
          onRejectRemaining={() =>
            updateViewer(() => api.rejectUnreviewedDetectionAnnotations(viewerCapture.id))
          }
          onRejectImage={() =>
            updateViewer(() => api.reviewDetectionCapture(viewerCapture.id, 'rejected', 'not-bird'))
          }
        />
      )}
    </div>
  );
}

function CaptureCard({
  capture,
  disabled,
  onOpen,
  onDelete,
}: {
  capture: DetectionCapture;
  disabled: boolean;
  onOpen: () => void;
  onDelete: () => void;
}) {
  const reviewed = capture.detections.filter(
    (annotation) => annotation.review_status !== 'unreviewed',
  ).length;
  const legacyImageLabel = capture.review_status === 'training' && reviewed === 0;
  return (
    <Card
      title={`${capture.class_name || 'unlabelled'} / ${
        capture.confidence === null ? 'manual' : capture.confidence.toFixed(2)
      }`}
      actions={<Pill tone={STATUS_TONE[capture.review_status]}>{capture.review_status}</Pill>}
    >
      <button
        type="button"
        className="relative block w-full overflow-hidden rounded-lg border border-edge bg-black"
        onClick={onOpen}
      >
        <img
          className="block h-auto w-full"
          src={api.detectionCaptureImage(capture.id)}
          alt={`${capture.class_name || 'manual'} detection`}
          loading="lazy"
        />
        <BoxOverlay capture={capture} />
      </button>

      <div className="mt-3 space-y-1 text-xs text-muted">
        <p>{new Date(capture.ts).toLocaleString()}</p>
        <p>
          {reviewed}/{capture.detections.length} boxes reviewed / camera={capture.camera_id}
        </p>
        {legacyImageLabel && (
          <p className="text-warn">Legacy image label: review its boxes before export.</p>
        )}
        {capture.detections.length > 0 && (
          <p>
            {capture.detections
              .map(
                (item) =>
                  `${item.class_name} ${item.confidence === null ? 'manual' : item.confidence.toFixed(2)}`,
              )
              .join(' / ')}
          </p>
        )}
      </div>

      <div className="mt-3 flex gap-2">
        <button className="btn px-2 py-1 text-xs" disabled={disabled} onClick={onOpen}>
          Review boxes
        </button>
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

function BoxOverlay({ capture }: { capture: DetectionCapture }) {
  return (
    <svg
      className="pointer-events-none absolute inset-0 h-full w-full"
      viewBox={`0 0 ${capture.frame_width} ${capture.frame_height}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      {capture.detections.map((annotation, index) => {
        const [x1, y1, x2, y2] = annotation.bbox;
        return (
          <rect
            key={`${annotation.class_name}-${index}`}
            x={x1}
            y={y1}
            width={Math.max(1, x2 - x1)}
            height={Math.max(1, y2 - y1)}
            fill="none"
            stroke={BOX_COLOUR[annotation.review_status]}
            strokeWidth={Math.max(2, capture.frame_width / 400)}
            strokeDasharray={annotation.review_status === 'rejected' ? '10 7' : undefined}
          />
        );
      })}
    </svg>
  );
}

function CaptureReviewer({
  capture,
  busy,
  error,
  position,
  canPrevious,
  canNext,
  onClose,
  onPrevious,
  onNext,
  onReview,
  onAdd,
  onDeleteAnnotation,
  onRejectRemaining,
  onRejectImage,
}: {
  capture: DetectionCapture;
  busy: boolean;
  error: string | null;
  position: string;
  canPrevious: boolean;
  canNext: boolean;
  onClose: () => void;
  onPrevious: () => void;
  onNext: () => void;
  onReview: (
    index: number,
    status: DetectionAnnotationStatus,
  ) => Promise<DetectionCapture | null>;
  onAdd: (bbox: [number, number, number, number]) => Promise<DetectionCapture | null>;
  onDeleteAnnotation: (index: number) => Promise<DetectionCapture | null>;
  onRejectRemaining: () => Promise<DetectionCapture | null>;
  onRejectImage: () => Promise<DetectionCapture | null>;
}) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [drawMode, setDrawMode] = useState(false);
  const [draft, setDraft] = useState<{ start: Point; end: Point } | null>(null);
  const selected = capture.detections[selectedIndex];
  const legacyImageLabel =
    capture.review_status === 'training' &&
    capture.detections.every((annotation) => annotation.review_status === 'unreviewed');

  useEffect(() => {
    const firstUnreviewed = capture.detections.findIndex(
      (annotation) => annotation.review_status === 'unreviewed',
    );
    setSelectedIndex(firstUnreviewed >= 0 ? firstUnreviewed : 0);
    setDrawMode(false);
    setDraft(null);
  }, [capture.id]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  async function reviewSelected(status: DetectionAnnotationStatus) {
    if (!selected || busy) return;
    const updated = await onReview(selectedIndex, status);
    if (!updated) return;
    const nextUnreviewed = updated.detections.findIndex(
      (annotation, index) =>
        index !== selectedIndex && annotation.review_status === 'unreviewed',
    );
    if (nextUnreviewed >= 0) setSelectedIndex(nextUnreviewed);
    else if (status !== 'unreviewed' && updated.review_status !== 'unreviewed' && canNext) onNext();
  }

  async function rejectRemaining() {
    if (busy) return;
    const updated = await onRejectRemaining();
    if (updated?.review_status !== 'unreviewed' && canNext) onNext();
  }

  async function rejectImage() {
    if (busy) return;
    const updated = await onRejectImage();
    if (updated && canNext) onNext();
  }

  function cycleBox(direction: -1 | 1) {
    if (!capture.detections.length) return;
    setSelectedIndex(
      (current) =>
        (current + direction + capture.detections.length) % capture.detections.length,
    );
  }

  useEffect(() => {
    function keyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target?.matches('input, textarea, select')) return;
      const key = event.key.toLowerCase();
      if (key === 'arrowleft') onPrevious();
      else if (key === 'arrowright') onNext();
      else if (key === 'a' || key === 'enter') void reviewSelected('accepted');
      else if (key === 'x') void reviewSelected('rejected');
      else if (key === 'u') void reviewSelected('unreviewed');
      else if (key === 'b') setDrawMode((active) => !active);
      else if (key === 'n') void rejectImage();
      else if (key === '[') cycleBox(-1);
      else if (key === ']') cycleBox(1);
      else if (key === 'escape') {
        if (drawMode) setDrawMode(false);
        else onClose();
      } else return;
      event.preventDefault();
    }
    window.addEventListener('keydown', keyDown);
    return () => window.removeEventListener('keydown', keyDown);
  });

  function imagePoint(event: ReactPointerEvent<SVGSVGElement>): Point {
    const rect = event.currentTarget.getBoundingClientRect();
    return [
      ((event.clientX - rect.left) / rect.width) * capture.frame_width,
      ((event.clientY - rect.top) / rect.height) * capture.frame_height,
    ];
  }

  function pointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    if (!drawMode || busy) return;
    const point = imagePoint(event);
    setDraft({ start: point, end: point });
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function pointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    if (!draft) return;
    setDraft({ ...draft, end: imagePoint(event) });
  }

  async function pointerUp(event: ReactPointerEvent<SVGSVGElement>) {
    if (!draft) return;
    const end = imagePoint(event);
    const bbox: [number, number, number, number] = [
      Math.min(draft.start[0], end[0]),
      Math.min(draft.start[1], end[1]),
      Math.max(draft.start[0], end[0]),
      Math.max(draft.start[1], end[1]),
    ];
    setDraft(null);
    setDrawMode(false);
    const updated = await onAdd(bbox);
    if (updated) setSelectedIndex(updated.detections.length - 1);
  }

  const draftBox = useMemo(() => {
    if (!draft) return null;
    return {
      x: Math.min(draft.start[0], draft.end[0]),
      y: Math.min(draft.start[1], draft.end[1]),
      width: Math.abs(draft.end[0] - draft.start[0]),
      height: Math.abs(draft.end[1] - draft.start[1]),
    };
  }, [draft]);

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-[#080b11]/95 p-3 backdrop-blur-sm md:p-5"
      role="dialog"
      aria-modal="true"
      aria-label="Detection annotation viewer"
    >
      <header className="mb-3 flex flex-wrap items-center gap-2">
        <button className="btn px-3 py-1.5" disabled={!canPrevious} onClick={onPrevious}>
          Previous
        </button>
        <button className="btn px-3 py-1.5" disabled={!canNext} onClick={onNext}>
          Next
        </button>
        <span className="text-sm text-muted">{position}</span>
        <span className="ml-auto text-sm font-semibold text-ink">
          {new Date(capture.ts).toLocaleString()}
        </span>
        <button className="btn px-3 py-1.5" onClick={onClose}>
          Close
        </button>
      </header>

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_21rem]">
        <div className="grid min-h-0 place-items-center overflow-auto rounded-xl border border-edge bg-black">
          <div
            className="relative w-full max-w-full"
            style={{ aspectRatio: `${capture.frame_width} / ${capture.frame_height}` }}
          >
            <img
              className="block h-full w-full object-contain"
              src={api.detectionCaptureImage(capture.id)}
              alt="Detection review"
              draggable={false}
            />
            <svg
              className={`absolute inset-0 h-full w-full touch-none ${drawMode ? 'cursor-crosshair' : 'cursor-default'}`}
              viewBox={`0 0 ${capture.frame_width} ${capture.frame_height}`}
              preserveAspectRatio="none"
              onPointerDown={pointerDown}
              onPointerMove={pointerMove}
              onPointerUp={(event) => void pointerUp(event)}
            >
              {capture.detections.map((annotation, index) => {
                const [x1, y1, x2, y2] = annotation.bbox;
                const active = index === selectedIndex;
                return (
                  <g key={`${annotation.source}-${index}`}>
                    <rect
                      x={x1}
                      y={y1}
                      width={Math.max(1, x2 - x1)}
                      height={Math.max(1, y2 - y1)}
                      fill={active ? `${BOX_COLOUR[annotation.review_status]}22` : 'transparent'}
                      stroke={BOX_COLOUR[annotation.review_status]}
                      strokeWidth={active ? 6 : 3}
                      strokeDasharray={annotation.review_status === 'rejected' ? '14 9' : undefined}
                      className={drawMode ? 'pointer-events-none' : 'cursor-pointer'}
                      onPointerDown={(event) => event.stopPropagation()}
                      onClick={() => setSelectedIndex(index)}
                    />
                    <text
                      x={x1 + 5}
                      y={Math.max(18, y1 + 20)}
                      fill={BOX_COLOUR[annotation.review_status]}
                      fontSize={Math.max(16, capture.frame_width / 70)}
                      fontWeight="bold"
                      className="pointer-events-none"
                    >
                      {index + 1} {annotation.class_name}
                    </text>
                  </g>
                );
              })}
              {draftBox && (
                <rect
                  {...draftBox}
                  fill="#38d99622"
                  stroke="#38d996"
                  strokeWidth={4}
                  strokeDasharray="12 8"
                />
              )}
            </svg>
          </div>
        </div>

        <aside className="min-h-0 space-y-3 overflow-y-auto rounded-xl border border-edge bg-panel p-4">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-base font-semibold text-ink">Review boxes</h2>
            <Pill tone={STATUS_TONE[capture.review_status]}>{capture.review_status}</Pill>
          </div>

          {error && <Banner>{error}</Banner>}
          {legacyImageLabel && (
            <Banner tone="warn">
              This capture has an older whole-image label. Review its individual boxes before it
              can be exported.
            </Banner>
          )}

          {selected ? (
            <div className="rounded-lg border border-edge bg-panelalt p-3 text-sm">
              <div className="mb-2 flex items-center justify-between gap-2">
                <strong className="text-ink">Box {selectedIndex + 1}</strong>
                <span style={{ color: BOX_COLOUR[selected.review_status] }}>
                  {selected.review_status}
                </span>
              </div>
              <p className="text-muted">
                Model: {selected.class_name}
                {selected.confidence === null ? '' : ` (${selected.confidence.toFixed(2)})`}
              </p>
              <p className="text-muted">Source: {selected.source}</p>
            </div>
          ) : (
            <Banner tone="info">No proposal boxes. Draw a bird box if the model missed one.</Banner>
          )}

          <div className="grid grid-cols-2 gap-2">
            <button
              className="btn border-good/50 bg-good/10 px-3 py-2 text-good"
              disabled={!selected || busy}
              onClick={() => void reviewSelected('accepted')}
            >
              Correct bird (A)
            </button>
            <button
              className="btn border-bad/50 bg-bad/10 px-3 py-2 text-bad"
              disabled={!selected || busy}
              onClick={() => void reviewSelected('rejected')}
            >
              Wrong box (X)
            </button>
            <button
              className="btn px-3 py-2"
              disabled={!selected || busy}
              onClick={() => void reviewSelected('unreviewed')}
            >
              Reset box (U)
            </button>
            <button
              className={`btn px-3 py-2 ${drawMode ? 'border-good text-good' : ''}`}
              disabled={busy}
              onClick={() => setDrawMode((active) => !active)}
            >
              {drawMode ? 'Drag on image...' : 'Add bird box (B)'}
            </button>
          </div>

          {selected?.source === 'manual' && (
            <button
              className="btn w-full px-3 py-2 text-bad"
              disabled={busy}
              onClick={() => void onDeleteAnnotation(selectedIndex)}
            >
              Remove manual box
            </button>
          )}

          <button
            className="btn w-full px-3 py-2"
            disabled={
              busy || !capture.detections.some((item) => item.review_status === 'unreviewed')
            }
            onClick={() => void rejectRemaining()}
          >
            Reject remaining boxes and continue
          </button>
          <button
            className="btn w-full border-bad/50 px-3 py-2 text-bad"
            disabled={busy}
            onClick={() => void rejectImage()}
          >
            No bird in image (N)
          </button>

          <div className="rounded-lg border border-edge p-3 text-xs leading-5 text-muted">
            <strong className="text-ink">Keyboard</strong>
            <p>A/Enter correct bird / X wrong / U reset</p>
            <p>[ and ] select box / B draw box / N no bird</p>
            <p>Left/Right image / Esc close</p>
          </div>
        </aside>
      </div>
    </div>
  );
}
