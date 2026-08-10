/**
 * Thin API client.
 *
 * Every call goes through `request`, so error handling, the JSON content type
 * and credential handling are defined once. Errors carry the server's `detail`
 * message, which is what the UI shows the operator.
 */

import type {
  CalibrationPoint,
  EventRecord,
  Health,
  OnvifDevice,
  OnvifProfileResult,
  Preset,
  Settings,
  SettingsSection,
  ZoneRecord,
  ZoneTypeName,
} from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  const controller = timeoutMs ? new AbortController() : null;
  const timeout = controller
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : undefined;
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      signal: controller?.signal ?? init?.signal,
      credentials: 'same-origin',
      headers: {
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch (error) {
    if (controller?.signal.aborted) {
      throw new ApiError(
        `request timed out after ${Math.round((timeoutMs ?? 0) / 1000)} seconds`,
        408,
      );
    }
    throw error;
  } finally {
    if (timeout !== undefined) window.clearTimeout(timeout);
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : detail;
    } catch {
      /* non-JSON error body; keep the status text */
    }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const post = <T>(path: string, body?: unknown, timeoutMs?: number) =>
  request<T>(
    path,
    { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) },
    timeoutMs,
  );

export const api = {
  health: () => request<Health>('/api/health'),
  system: () => request<Record<string, unknown>>('/api/system'),

  // --- auth ---------------------------------------------------------
  me: () =>
    request<{ auth_enabled: boolean; authenticated: boolean; username: string | null }>(
      '/api/auth/me',
    ),
  login: (username: string, password: string) =>
    post<{ authenticated: boolean }>('/api/auth/login', { username, password }),
  logout: () => post<{ ok: boolean }>('/api/auth/logout'),

  // --- settings -----------------------------------------------------
  settings: () => request<Settings>('/api/settings'),
  settingsDefaults: () => request<Settings>('/api/settings-defaults'),
  patchAllSettings: (patch: Partial<Settings>) =>
    request<Settings>('/api/settings', {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  patchSettings: <S extends SettingsSection>(section: S, patch: Partial<Settings[S]>) =>
    request<Settings[S]>(`/api/settings/${section}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  resetSettings: (section: SettingsSection) => post<unknown>(`/api/settings/${section}/reset`),

  // --- control ------------------------------------------------------
  arm: (armed: boolean) => post<{ armed: boolean }>('/api/control/arm', { armed }),
  estop: () => post<{ armed: boolean }>('/api/control/estop'),
  clearEstop: () => post<{ ok: boolean }>('/api/control/estop/clear'),
  home: (axes: 'both' | 'pan' | 'tilt' = 'both') => post<unknown>('/api/control/home', { axes }),
  center: () => post<unknown>('/api/control/center'),
  move: (pan_deg: number, tilt_deg: number, speed_deg_s?: number) =>
    post<unknown>('/api/control/move', { pan_deg, tilt_deg, speed_deg_s }),
  moveRelative: (pan_delta_deg: number, tilt_delta_deg: number, speed_deg_s?: number) =>
    post<unknown>('/api/control/move_relative', { pan_delta_deg, tilt_delta_deg, speed_deg_s }),
  jog: (pan: number, tilt: number) => post<unknown>('/api/control/jog', { pan, tilt }),
  stop: () => post<unknown>('/api/control/stop'),
  spray: (duration_ms?: number) => post<{ duration_ms: number }>('/api/control/spray', { duration_ms }),
  sprayStop: () => post<unknown>('/api/control/spray/stop'),
  aim: (x: number, y: number, surface?: string) =>
    post<{ pan_deg: number; tilt_deg: number; extrapolated: boolean }>('/api/control/aim', {
      x,
      y,
      surface,
    }),
  pushControllerConfig: () => post<unknown>('/api/control/config/push'),

  // --- calibration --------------------------------------------------
  calibrationPoints: () => request<CalibrationPoint[]>('/api/calibration/points'),
  addCalibrationPoint: (body: {
    x: number;
    y: number;
    surface?: string;
    label?: string;
    pan_deg?: number;
    tilt_deg?: number;
  }) => post<CalibrationPoint>('/api/calibration/points', body),
  deleteCalibrationPoint: (id: number) =>
    request<void>(`/api/calibration/points/${id}`, { method: 'DELETE' }),
  clearCalibration: () => request<{ removed: number }>('/api/calibration/points', { method: 'DELETE' }),
  calibrationModel: () =>
    request<{ camera_id: string; calibrated: boolean; strategy: string; surfaces: unknown[] }>(
      '/api/calibration/model',
    ),
  solve: (x: number, y: number, surface?: string) =>
    request<{ pan_deg: number; tilt_deg: number; extrapolated: boolean; surface: string }>(
      `/api/calibration/solve?x=${x}&y=${y}${surface ? `&surface=${encodeURIComponent(surface)}` : ''}`,
    ),

  // --- zones --------------------------------------------------------
  zones: () => request<ZoneRecord[]>('/api/zones'),
  zoneTypes: () => request<{ value: ZoneTypeName; is_surface: boolean }[]>('/api/zones/types'),
  createZone: (body: {
    name: string;
    zone_type: ZoneTypeName;
    points: [number, number][];
    priority?: number;
  }) => post<ZoneRecord>('/api/zones', body),
  updateZone: (id: number, body: Partial<ZoneRecord>) =>
    request<ZoneRecord>(`/api/zones/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteZone: (id: number) => request<void>(`/api/zones/${id}`, { method: 'DELETE' }),

  // --- presets ------------------------------------------------------
  presets: () => request<Preset[]>('/api/presets'),
  createPreset: (name: string) => post<Preset>('/api/presets', { name }),
  deletePreset: (id: number) => request<void>(`/api/presets/${id}`, { method: 'DELETE' }),
  gotoPreset: (id: number) => post<unknown>(`/api/presets/${id}/goto`),

  // --- events -------------------------------------------------------
  events: (params: { limit?: number; category?: string; level?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.limit) query.set('limit', String(params.limit));
    if (params.category) query.set('category', params.category);
    if (params.level) query.set('level', params.level);
    return request<EventRecord[]>(`/api/events?${query.toString()}`);
  },
  eventCategories: () => request<string[]>('/api/events/categories'),
  cameras: () => request<{ primary_id: string; connected: boolean; cameras: unknown[] }>('/api/cameras'),
  discoverCameras: (timeout_s = 4) =>
    post<{ devices: OnvifDevice[]; note: string }>(
      `/api/cameras/discover?timeout_s=${timeout_s}`,
      undefined,
      (timeout_s + 5) * 1000,
    ),
  onvifProfiles: (body: { xaddr: string; username: string; password: string }) =>
    post<OnvifProfileResult>('/api/cameras/onvif/profiles', body, 22_000),
  onboardCamera: (body: {
    id: string;
    name: string;
    role: 'overview' | 'turret' | 'aux';
    uri: string;
    username: string;
    password: string;
    make_primary: boolean;
  }) => post<Settings['cameras']>('/api/cameras/onboard', body),
};

/** WebSocket URL for the current origin (works behind a reverse proxy). */
export function wsUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${path}`;
}
