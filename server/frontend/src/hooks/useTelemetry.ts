/**
 * Telemetry WebSocket with automatic reconnect.
 *
 * One socket per tab, shared through context by `App`. It also carries the
 * outbound jog channel: routing joystick samples over the open socket avoids a
 * request round-trip per sample.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { wsUrl } from '../api/client';
import type { EventRecord, Telemetry } from '../api/types';

export interface TelemetryChannel {
  telemetry: Telemetry | null;
  events: EventRecord[];
  connected: boolean;
  error: string | null;
  send: (message: Record<string, unknown>) => void;
  jog: (pan: number, tilt: number) => void;
}

const MAX_EVENTS = 60;
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 8000;

export function useTelemetry(): TelemetryChannel {
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(RECONNECT_MIN_MS);
  const closedRef = useRef(false);

  useEffect(() => {
    closedRef.current = false;
    let timer: number | undefined;

    const connect = () => {
      if (closedRef.current) return;
      const socket = new WebSocket(wsUrl('/ws/telemetry'));
      socketRef.current = socket;

      socket.onopen = () => {
        setConnected(true);
        setError(null);
        retryRef.current = RECONNECT_MIN_MS;
      };

      socket.onmessage = (raw) => {
        try {
          const message = JSON.parse(raw.data as string);
          if (message.type === 'telemetry') {
            setTelemetry(message.data as Telemetry);
          } else if (message.type === 'event') {
            setEvents((current) => [message.data as EventRecord, ...current].slice(0, MAX_EVENTS));
          } else if (message.type === 'hello') {
            setEvents(((message.data.recent_events as EventRecord[]) ?? []).slice().reverse());
          } else if (message.type === 'error') {
            setError(String(message.data?.message ?? 'command failed'));
          }
        } catch {
          /* ignore malformed frames */
        }
      };

      socket.onclose = () => {
        setConnected(false);
        socketRef.current = null;
        if (closedRef.current) return;
        // Exponential backoff, capped: a turret left running overnight should
        // reconnect promptly after a server restart without hammering it.
        timer = window.setTimeout(connect, retryRef.current);
        retryRef.current = Math.min(retryRef.current * 2, RECONNECT_MAX_MS);
      };

      socket.onerror = () => setError('telemetry connection lost');
    };

    connect();
    return () => {
      closedRef.current = true;
      if (timer) window.clearTimeout(timer);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, []);

  const send = useCallback((message: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
    }
  }, []);

  const jog = useCallback(
    (pan: number, tilt: number) => send({ type: 'jog', pan, tilt }),
    [send],
  );

  return { telemetry, events, connected, error, send, jog };
}
