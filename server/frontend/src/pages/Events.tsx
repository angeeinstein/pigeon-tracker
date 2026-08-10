import { useState } from 'react';
import { api } from '../api/client';
import type { EventRecord } from '../api/types';
import { Banner, Card, Spinner } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { useLive } from '../state';

const LEVEL_CLASS: Record<string, string> = {
  error: 'text-bad',
  warning: 'text-warn',
  info: 'text-ink',
  debug: 'text-muted',
};

export default function Events() {
  const { events: liveEvents } = useLive();
  const [category, setCategory] = useState('');
  const [level, setLevel] = useState('');
  const history = useAsync(
    () => api.events({ limit: 300, category: category || undefined, level: level || undefined }),
    [category, level],
  );
  const categories = useAsync(() => api.eventCategories(), []);

  // Live events arrive over the telemetry socket; the stored history is the
  // authoritative list. Showing live ones first means the page is never stale.
  const merged: EventRecord[] = [
    ...liveEvents.filter(
      (event) =>
        (!category || event.category === category) && (!level || event.level === level),
    ),
    ...(history.data ?? []),
  ].slice(0, 400);

  return (
    <div className="space-y-4">
      <Card
        title="Filters"
        actions={
          <button className="btn px-2 py-1 text-xs" onClick={history.reload}>
            Reload
          </button>
        }
      >
        <div className="flex flex-wrap gap-2">
          <select
            className="field max-w-[12rem]"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
          >
            <option value="">all categories</option>
            {categories.data?.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <select
            className="field max-w-[10rem]"
            value={level}
            onChange={(event) => setLevel(event.target.value)}
          >
            <option value="">all levels</option>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="error">error</option>
          </select>
        </div>
      </Card>

      <Card title={`Events (${merged.length})`}>
        {history.loading && <Spinner />}
        {history.error && <Banner>{history.error}</Banner>}
        <div className="max-h-[70vh] overflow-y-auto">
          <table className="w-full text-sm">
            <tbody>
              {merged.map((event, index) => (
                <tr key={`${event.id ?? 'live'}-${index}`} className="border-b border-edge/50">
                  <td className="tabular whitespace-nowrap py-1.5 pr-3 align-top text-xs text-muted">
                    {new Date(event.ts).toLocaleString()}
                  </td>
                  <td className="whitespace-nowrap py-1.5 pr-3 align-top text-xs text-muted">
                    {event.category}
                  </td>
                  <td className={`py-1.5 pr-3 align-top ${LEVEL_CLASS[event.level] ?? ''}`}>
                    {event.message}
                    {event.data && Object.keys(event.data).length > 0 && (
                      <span className="tabular ml-2 text-xs text-muted">
                        {Object.entries(event.data)
                          .map(([key, value]) => `${key}=${String(value)}`)
                          .join(' ')}
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 align-top">
                    {event.snapshot && (
                      <a
                        className="text-xs text-accent"
                        href={`/api/snapshots/${event.snapshot.split(/[\\/]/).pop()}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        snapshot
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {merged.length === 0 && !history.loading && (
            <p className="py-6 text-center text-sm text-muted">No events match this filter.</p>
          )}
        </div>
      </Card>
    </div>
  );
}
