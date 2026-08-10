/** App-wide contexts: telemetry channel and toast notifications. */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { useTelemetry, type TelemetryChannel } from './hooks/useTelemetry';

const TelemetryContext = createContext<TelemetryChannel | null>(null);

export function useLive(): TelemetryChannel {
  const value = useContext(TelemetryContext);
  if (!value) throw new Error('useLive must be used inside <AppProviders>');
  return value;
}

export interface Toast {
  id: number;
  message: string;
  tone: 'good' | 'bad' | 'info';
}

interface ToastApi {
  toasts: Toast[];
  notify: (message: string, tone?: Toast['tone']) => void;
  /** Run an action, surfacing failures as a toast instead of throwing. */
  attempt: (action: () => Promise<unknown>, success?: string) => Promise<boolean>;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const value = useContext(ToastContext);
  if (!value) throw new Error('useToast must be used inside <AppProviders>');
  return value;
}

export function AppProviders({ children }: { children: ReactNode }) {
  const channel = useTelemetry();
  const [toasts, setToasts] = useState<Toast[]>([]);

  const notify = useCallback((message: string, tone: Toast['tone'] = 'info') => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, message, tone }]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((toast) => toast.id !== id));
    }, 5000);
  }, []);

  const attempt = useCallback(
    async (action: () => Promise<unknown>, success?: string) => {
      try {
        await action();
        if (success) notify(success, 'good');
        return true;
      } catch (error) {
        notify(error instanceof Error ? error.message : String(error), 'bad');
        return false;
      }
    },
    [notify],
  );

  const toastApi = useMemo(() => ({ toasts, notify, attempt }), [toasts, notify, attempt]);

  return (
    <ToastContext.Provider value={toastApi}>
      <TelemetryContext.Provider value={channel}>{children}</TelemetryContext.Provider>
    </ToastContext.Provider>
  );
}

export function ToastStack() {
  const { toasts } = useToast();
  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 left-1/2 z-50 flex w-[min(28rem,92vw)] -translate-x-1/2 flex-col gap-2">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`pointer-events-auto rounded-lg border px-3 py-2 text-sm shadow-xl backdrop-blur
            ${
              toast.tone === 'bad'
                ? 'border-bad/50 bg-bad/20 text-[#ffd7de]'
                : toast.tone === 'good'
                  ? 'border-good/50 bg-good/20 text-[#d3f8e8]'
                  : 'border-edge bg-panel/95'
            }`}
        >
          {toast.message}
        </div>
      ))}
    </div>
  );
}
