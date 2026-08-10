/**
 * Live camera view with a normalised overlay layer.
 *
 * The image is an MJPEG stream in a plain <img>: it works on every browser
 * including phones, survives a backgrounded tab, and needs no decoding code
 * here. Overlays are an SVG with `viewBox="0 0 1 1"` laid exactly over it, so
 * every coordinate in this app — zones, calibration points, aim points — is in
 * the same normalised [0,1] space the server uses.
 */

import { useCallback, useRef, type ReactNode } from 'react';
import type { Telemetry } from '../api/types';

interface Props {
  telemetry: Telemetry | null;
  /** Server-drawn boxes/zones. Turn off when the page draws its own. */
  serverOverlays?: boolean;
  /** Client-side tracks/target markers. */
  showTracks?: boolean;
  onPick?: (x: number, y: number, event: React.MouseEvent) => void;
  children?: ReactNode;
  className?: string;
  cursor?: string;
}

export default function VideoView({
  telemetry,
  serverOverlays = true,
  showTracks = true,
  onPick,
  children,
  className = '',
  cursor = 'crosshair',
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const width = telemetry?.frame?.width ?? 16;
  const height = telemetry?.frame?.height ?? 9;

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

  const streamUrl = `/api/camera/stream.mjpg?overlays=${serverOverlays ? 'true' : 'false'}`;

  return (
    <div
      ref={containerRef}
      onClick={handleClick}
      style={{ aspectRatio: `${width} / ${height}`, cursor: onPick ? cursor : 'default' }}
      className={`relative w-full overflow-hidden rounded-xl border border-edge bg-black ${className}`}
    >
      <img
        src={streamUrl}
        alt="Live camera"
        className="absolute inset-0 h-full w-full object-fill"
        draggable={false}
      />

      {!telemetry?.camera_connected && (
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

        {telemetry?.target?.aim_norm && (
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

        {telemetry?.turret_point && (
          <g stroke="#f5a524" strokeWidth={0.003} fill="none">
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
    </div>
  );
}
