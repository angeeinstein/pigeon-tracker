/**
 * Virtual joystick for pan/tilt.
 *
 * Emits deflection in [-1, 1] per axis at a fixed rate while held, and one
 * explicit zero on release. The server-side jog command expires on its own if
 * samples stop arriving, so a dropped connection mid-drag stops the turret
 * instead of running it into a limit.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

interface Props {
  onChange: (pan: number, tilt: number) => void;
  disabled?: boolean;
  /** How often to repeat the current deflection, milliseconds. */
  intervalMs?: number;
  size?: number;
}

export default function Joystick({ onChange, disabled, intervalMs = 120, size = 180 }: Props) {
  const areaRef = useRef<HTMLDivElement>(null);
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const [active, setActive] = useState(false);
  const valueRef = useRef({ pan: 0, tilt: 0 });

  const compute = useCallback((clientX: number, clientY: number) => {
    const area = areaRef.current;
    if (!area) return;
    const rect = area.getBoundingClientRect();
    const radius = rect.width / 2;
    let dx = (clientX - (rect.left + radius)) / radius;
    let dy = (clientY - (rect.top + radius)) / radius;
    const magnitude = Math.hypot(dx, dy);
    if (magnitude > 1) {
      dx /= magnitude;
      dy /= magnitude;
    }
    // Small dead zone so a resting thumb does not creep.
    const deadzone = 0.08;
    const pan = Math.abs(dx) < deadzone ? 0 : dx;
    const tilt = Math.abs(dy) < deadzone ? 0 : -dy;
    setKnob({ x: dx, y: dy });
    valueRef.current = { pan, tilt };
  }, []);

  const stop = useCallback(() => {
    setActive(false);
    setKnob({ x: 0, y: 0 });
    valueRef.current = { pan: 0, tilt: 0 };
    onChange(0, 0);
  }, [onChange]);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      onChange(valueRef.current.pan, valueRef.current.tilt);
    }, intervalMs);
    onChange(valueRef.current.pan, valueRef.current.tilt);
    return () => window.clearInterval(timer);
  }, [active, intervalMs, onChange]);

  useEffect(() => {
    if (!active) return;
    const move = (event: PointerEvent) => compute(event.clientX, event.clientY);
    const up = () => stop();
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
    };
  }, [active, compute, stop]);

  return (
    <div
      ref={areaRef}
      onPointerDown={(event) => {
        if (disabled) return;
        (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
        setActive(true);
        compute(event.clientX, event.clientY);
      }}
      style={{ width: size, height: size }}
      className={`relative touch-none select-none rounded-full border border-edge bg-panelalt
        ${disabled ? 'opacity-40' : 'cursor-grab active:cursor-grabbing'}`}
    >
      <div className="absolute inset-0 rounded-full border border-edge/60" />
      <div className="absolute left-1/2 top-0 h-full w-px -translate-x-1/2 bg-edge/60" />
      <div className="absolute top-1/2 left-0 h-px w-full -translate-y-1/2 bg-edge/60" />
      <div
        className="absolute rounded-full bg-accent/80 shadow-lg shadow-accent/20 transition-transform"
        style={{
          width: size * 0.28,
          height: size * 0.28,
          left: `calc(50% + ${(knob.x * size) / 2.6}px)`,
          top: `calc(50% + ${(knob.y * size) / 2.6}px)`,
          transform: 'translate(-50%, -50%)',
        }}
      />
      <span className="absolute bottom-2 left-1/2 -translate-x-1/2 text-[10px] uppercase tracking-widest text-muted">
        jog
      </span>
    </div>
  );
}
