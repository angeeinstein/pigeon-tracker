/**
 * Live camera view with a normalised overlay layer.
 *
 * The image is an MJPEG stream in a plain <img>: it works on every browser
 * including phones, survives a backgrounded tab, and needs no decoding code
 * here. Overlays are an SVG with `viewBox="0 0 1 1"` laid exactly over it, so
 * every coordinate in this app — zones, calibration points, aim points — is in
 * the same normalised [0,1] space the server uses.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import type { Telemetry } from '../api/types';

interface Props {
  telemetry: Telemetry | null;
  /** Camera source to show. Omit for the configured primary camera. */
  cameraId?: string;
  /** Connection and dimensions for a non-primary camera. */
  cameraConnected?: boolean;
  frameSize?: { width: number; height: number };
  /** Server-drawn boxes/zones. Turn off when the page draws its own. */
  serverOverlays?: boolean;
  /** Client-side tracks/target markers. */
  showTracks?: boolean;
  showAimMarkers?: boolean;
  onPick?: (x: number, y: number, event: React.MouseEvent) => void;
  children?: ReactNode;
  className?: string;
  cursor?: string;
  /** Show an in-page fullscreen viewer without opening a second stream. */
  expandable?: boolean;
}

export default function VideoView({
  telemetry,
  cameraId,
  cameraConnected,
  frameSize,
  serverOverlays = true,
  showTracks = true,
  showAimMarkers = true,
  onPick,
  children,
  className = '',
  cursor = 'crosshair',
  expandable = false,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const width = frameSize?.width || telemetry?.frame?.width || 16;
  const height = frameSize?.height || telemetry?.frame?.height || 9;
  const aspectRatio = width / height;

  useEffect(() => {
    if (!expanded) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const keyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setExpanded(false);
      event.preventDefault();
    };
    window.addEventListener('keydown', keyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', keyDown);
    };
  }, [expanded]);

  const handleClick = useCallback(
    (event: React.MouseEvent) => {
      if (!onPick || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width;
      const y = (event.clientY - rect.top) / rect.height;
      if (x < 0 || x > 1 || y < 0 || y > 1) return;
      onPick(Number(x.toFixed(5)), Number(y.toFixed(5)), event);
    },
    [onPick],
  );

  const streamParams = new URLSearchParams({ overlays: String(serverOverlays) });
  if (cameraId) streamParams.set('camera_id', cameraId);
  const streamUrl = `/api/camera/stream.mjpg?${streamParams.toString()}`;
  const isConnected = cameraConnected ?? telemetry?.camera_connected;

  return (
    <div
      className={
        expanded
          ? 'fixed inset-0 z-[60] flex items-center justify-center bg-[#05070b]/95 p-[2vh] backdrop-blur-sm'
          : ''
      }
      role={expanded ? 'dialog' : undefined}
      aria-modal={expanded ? 'true' : undefined}
      aria-label={expanded ? 'Enlarged live camera' : undefined}
    >
      <div
        ref={containerRef}
        onClick={handleClick}
        style={{
          aspectRatio: `${width} / ${height}`,
          cursor: onPick ? cursor : 'default',
          width: expanded ? `min(96vw, ${aspectRatio * 96}vh)` : undefined,
        }}
        className={`relative w-full overflow-hidden rounded-xl border border-edge bg-black ${className}`}
      >
        <img
          src={streamUrl}
          alt="Live camera"
          className="absolute inset-0 h-full w-full object-fill"
          draggable={false}
        />

        {!isConnected && (
          <div className="absolute inset-0 grid place-items-center bg-black/70 text-sm text-muted">
            camera not connected
          </div>
        )}

        <svg
          viewBox="0 0 1 1"
          preserveAspectRatio="none"
          className="pointer-events-none absolute inset-0 h-full w-full"
        >
          {showTracks &&
            telemetry?.tracks.map((track) => {
              const [x1, y1, x2, y2] = track.bbox;
              const isTarget = telemetry.target?.track_id === track.track_id;
              return (
                <g key={track.track_id}>
                  <rect
                    x={x1 / width}
                    y={y1 / height}
                    width={(x2 - x1) / width}
                    height={(y2 - y1) / height}
                    fill="none"
                    stroke={isTarget ? '#f3496a' : '#4c8dff'}
                    strokeWidth={isTarget ? 0.004 : 0.0025}
                    vectorEffect="non-scaling-stroke"
                  />
                </g>
              );
            })}

          {showAimMarkers && telemetry?.target?.aim_norm && (
            <g>
              <circle
                cx={telemetry.target.aim_norm[0]}
                cy={telemetry.target.aim_norm[1]}
                r={0.02}
                fill="none"
                stroke="#f3496a"
                strokeWidth={0.003}
              />
            </g>
          )}

          {showAimMarkers && telemetry?.turret_point && (
            <g stroke="#f5a524" strokeWidth={0.003} fill="none">
              {telemetry.controller_simulated && (
                <rect
                  x={telemetry.turret_point[0] - 0.04}
                  y={telemetry.turret_point[1] - 0.04}
                  width={0.08}
                  height={0.08}
                  rx={0.008}
                  strokeDasharray="0.012 0.008"
                />
              )}
              <circle cx={telemetry.turret_point[0]} cy={telemetry.turret_point[1]} r={0.025} />
              <line
                x1={telemetry.turret_point[0] - 0.04}
                y1={telemetry.turret_point[1]}
                x2={telemetry.turret_point[0] + 0.04}
                y2={telemetry.turret_point[1]}
              />
              <line
                x1={telemetry.turret_point[0]}
                y1={telemetry.turret_point[1] - 0.04}
                x2={telemetry.turret_point[0]}
                y2={telemetry.turret_point[1] + 0.04}
              />
            </g>
          )}

          {children}
        </svg>

        {expandable && (
          <button
            type="button"
            className="btn absolute right-2 top-2 z-10 bg-panel/90 px-2.5 py-1.5 text-xs shadow-lg"
            onClick={(event) => {
              event.stopPropagation();
              setExpanded((value) => !value);
            }}
            aria-label={expanded ? 'Close enlarged camera' : 'Enlarge camera'}
            title={expanded ? 'Close enlarged view (Esc)' : 'Enlarge camera'}
          >
            <span aria-hidden="true">{expanded ? '↙' : '↗'}</span>
            {expanded ? 'Close' : 'Enlarge'}
          </button>
        )}
      </div>
    </div>
  );
}
