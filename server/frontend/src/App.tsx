import { useEffect } from 'react';
import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { api } from './api/client';
import { Pill } from './components/ui';
import { useAsync } from './hooks/useAsync';
import Calibration from './pages/Calibration';
import Cameras from './pages/Cameras';
import Dashboard from './pages/Dashboard';
import Detections from './pages/Detections';
import Events from './pages/Events';
import Login from './pages/Login';
import Settings from './pages/Settings';
import System from './pages/System';
import Zones from './pages/Zones';
import { AppProviders, ToastStack, useLive, useToast } from './state';

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/cameras', label: 'Cameras' },
  { to: '/calibration', label: 'Calibration' },
  { to: '/zones', label: 'Zones' },
  { to: '/detections', label: 'Detections' },
  { to: '/settings', label: 'Settings' },
  { to: '/events', label: 'Events' },
  { to: '/system', label: 'System' },
];

export default function App() {
  const auth = useAsync(() => api.me(), []);

  if (auth.loading) {
    return <p className="grid min-h-screen place-items-center text-muted">connecting…</p>;
  }
  if (auth.data?.auth_enabled && !auth.data.authenticated) {
    return <Login onSuccess={auth.reload} />;
  }

  return (
    <AppProviders>
      <Shell />
      <ToastStack />
    </AppProviders>
  );
}

function Shell() {
  return (
    <div className="mx-auto flex min-h-screen w-full max-w-7xl flex-col gap-4 p-3 sm:p-5">
      <TopBar />
      <main className="flex-1">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/cameras" element={<Cameras />} />
          <Route path="/calibration" element={<Calibration />} />
          <Route path="/zones" element={<Zones />} />
          <Route path="/detections" element={<Detections />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/events" element={<Events />} />
          <Route path="/system" element={<System />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <footer className="pb-2 text-center text-xs text-muted">
        turret-control · the water valve is closed whenever the system is disarmed
      </footer>
    </div>
  );
}

function TopBar() {
  const { telemetry, connected } = useLive();
  const { attempt } = useToast();
  const update = useAsync(() => api.systemUpdateOverview(), []);
  const updateRunning = ['starting', 'checking', 'updating', 'restarting', 'verifying'].includes(
    update.data?.state ?? '',
  );

  useEffect(() => {
    const timer = window.setInterval(update.reload, updateRunning ? 2_000 : 15_000);
    return () => window.clearInterval(timer);
    // `reload` deliberately changes identity; polling cadence depends on state only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [updateRunning]);

  useEffect(() => {
    if (!window.sessionStorage.getItem('turret-update-requested')) return;
    if (update.data?.state === 'failed') {
      window.sessionStorage.removeItem('turret-update-requested');
      return;
    }
    if (update.data?.state !== 'succeeded') return;
    window.sessionStorage.removeItem('turret-update-requested');
    const refreshed = new URL(window.location.href);
    refreshed.searchParams.set('updated', Date.now().toString());
    window.location.replace(`${refreshed.pathname}${refreshed.search}${refreshed.hash}`);
  }, [update.data?.state]);

  return (
    <header className="sticky top-0 z-30 -mx-3 mb-1 border-b border-edge bg-[#0f1115]/95 px-3 py-2 backdrop-blur sm:-mx-5 sm:px-5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-2 text-sm font-semibold tracking-wide">TURRET</span>

        <nav className="order-3 -mx-1 grid w-full grid-cols-3 gap-1 pb-1 sm:order-none sm:mx-0 sm:flex sm:w-auto sm:pb-0">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-lg px-2 py-1.5 text-center text-sm transition sm:px-3 ${
                  isActive ? 'bg-panelalt text-ink' : 'text-muted hover:text-ink'
                }`
              }
            >
              <span className="inline-flex items-center gap-1.5">
                {item.label}
                {item.to === '/system' && update.data?.version_check.update_available ? (
                  <span
                    className="h-2 w-2 rounded-full bg-warn shadow-[0_0_0_3px_rgba(247,185,85,0.12)]"
                    title="Server update available"
                    aria-label="Server update available"
                  />
                ) : null}
              </span>
            </NavLink>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <Pill tone={connected ? 'good' : 'bad'} title="Telemetry WebSocket">
            {connected ? 'live' : 'offline'}
          </Pill>
          <Pill tone={telemetry?.armed ? 'bad' : 'idle'}>
            {telemetry?.armed ? 'ARMED' : 'disarmed'}
          </Pill>
          <button
            className="btn btn-danger px-3 py-1.5"
            onClick={() => attempt(() => api.estop(), 'emergency stop sent')}
          >
            E-STOP
          </button>
        </div>
      </div>
    </header>
  );
}
