import { useState } from 'react';
import { api } from '../api/client';
import { Banner, Card } from '../components/ui';

export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      onSuccess();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'login failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid min-h-screen place-items-center p-4">
      <Card title="Sign in" className="w-full max-w-sm">
        <form onSubmit={submit} className="space-y-3">
          {error && <Banner>{error}</Banner>}
          <label className="block">
            <span className="label">Username</span>
            <input
              className="field"
              value={username}
              autoComplete="username"
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <label className="block">
            <span className="label">Password</span>
            <input
              className="field"
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <button className="btn btn-primary w-full" disabled={busy}>
            {busy ? 'signing in…' : 'Sign in'}
          </button>
        </form>
      </Card>
    </div>
  );
}
