import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { api } from './api/client';
import { Pill } from './components/ui';
import { useAsync } from './hooks/useAsync';
import Calibration from './pages/Calibration';
import Dashboard from './pages/Dashboard';
import Events from './pages/Events';
import Login from './pages/Login';
import Settings from './pages/Settings';
import System from './pages/System';
import Zones from './pages/Zones';
import { AppProviders, ToastStack, useLive, useToast } from './state';

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/calibration', label: 'Calibration' },
  { to: '/zones', label: 'Zones' },
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
          <Route path="/calibration" element={<Calibration />} />
          <Route path="/zones" element={<Zones />} />
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
              {item.label}
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
