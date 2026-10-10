import React, { useMemo, useState } from 'react';
import type { ServiceUnit } from '../lab.d';
import { isFailure, usePoll } from '../hooks';
import ConfirmationModal from './ConfirmationModal';

interface Props { onLog: (text: string, type: 'info' | 'success' | 'error') => void }

const ACTIVE_STYLE: Record<string, string> = {
  active: 'bg-green-500/20 text-green-300',
  failed: 'bg-red-500/20 text-red-300',
  inactive: 'bg-white/10 text-white/50',
  activating: 'bg-yellow-500/20 text-yellow-300',
};

const ServicesPanel: React.FC<Props> = ({ onLog }) => {
  const [scope, setScope] = useState<'system' | 'user'>('system');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<'all' | 'running' | 'failed'>('all');
  const [busy, setBusy] = useState<string | null>(null);
  const [pending, setPending] = useState<{ unit: ServiceUnit; action: string } | null>(null);
  const { data, reload } = usePoll(() => window.lab.services(scope), 5000, scope);
  const result = data && !isFailure(data) ? data : null;
  const rawError = data && isFailure(data) ? data.error : result?.error;
  const error = rawError && /Failed to connect|System has not been booted|bus/i.test(rawError)
    ? `${rawError}. systemd is not reachable for the ${scope} scope on this machine${scope === 'system' ? ' (no system D-Bus socket); try the user scope' : ''}.`
    : rawError;

  const units = useMemo(() => {
    const q = query.toLowerCase();
    return (result?.units ?? [])
      .filter((u) => (filter === 'running' ? u.sub === 'running' : filter === 'failed' ? u.active === 'failed' : true))
      .filter((u) => !q || u.unit.toLowerCase().includes(q) || u.description.toLowerCase().includes(q))
      .sort((a, b) => Number(b.active === 'failed') - Number(a.active === 'failed') || a.unit.localeCompare(b.unit));
  }, [result, query, filter]);

  const act = async (u: ServiceUnit, action: string) => {
    setBusy(`${u.unit}:${action}`);
    const r = await window.lab.serviceAction(scope, u.unit, action).catch(e => ({ ok: false, error: String(e.message), output: '', needsSudo: false, hint: '' }));
    setBusy(null);
    if (r.ok) onLog(`[systemctl] ${action} ${u.unit} ok`, 'success');
    else onLog(r.needsSudo ? `[systemctl] needs passwordless sudo. Run: ${r.hint}` : `[systemctl] ${action} ${u.unit} failed: ${r.output || r.error || 'unknown error'}`, 'error');
    reload();
  };

  const Btn: React.FC<{ u: ServiceUnit; action: string; icon: string }> = ({ u, action, icon }) => (
    <button title={action} aria-label={`${action} ${u.unit}`} disabled={busy !== null} onClick={() => setPending({ unit: u, action })}
      className="p-1 rounded text-white/50 hover:text-white hover:bg-white/10 disabled:opacity-30">
      <span className="material-symbols-outlined text-[18px]">{icon}</span>
    </button>
  );

  return (
    <div className="h-full flex flex-col">
      <ConfirmationModal isOpen={!!pending} title="Change service" message={`Apply this change to the ${scope} service? Stopping or restarting a service may interrupt its work.`} command={pending ? `systemctl ${scope === 'user' ? '--user ' : ''}${pending.action} ${pending.unit.unit}` : ''} onCancel={() => setPending(null)} onConfirm={() => { const p = pending; setPending(null); if (p) void act(p.unit, p.action); }} />
      <div className="p-4 border-b border-white/5 flex flex-wrap items-center gap-3">
        <div className="flex rounded-full bg-black/20 border border-white/10 overflow-hidden text-sm">
          {(['system', 'user'] as const).map((s) => (
            <button key={s} onClick={() => setScope(s)} className={`px-4 py-1.5 ${scope === s ? 'bg-whitecube-accent text-white' : 'text-white/60 hover:text-white'}`}>{s}</button>
          ))}
        </div>
        <input type="text" placeholder="Filter services…" value={query} onChange={(e) => setQuery(e.target.value)}
          className="flex-1 min-w-48 bg-black/20 border border-white/10 rounded-full px-4 py-2 text-sm text-white outline-none" />
        <div className="flex gap-1 text-sm">
          {(['all', 'running', 'failed'] as const).map((f) => (
            <button key={f} onClick={() => setFilter(f)} className={`px-3 py-1.5 rounded ${filter === f ? 'bg-white/15 text-white' : 'text-white/50 hover:text-white'}`}>{f}</button>
          ))}
        </div>
      </div>
      {error && <div className="p-4 text-sm text-red-400">{error}</div>}
      {!result && !error && <div className="p-4 text-sm text-white/40">Loading services…</div>}
      <div className="flex-1 overflow-y-auto">
        {units.map((u) => (
          <div key={u.unit} className="flex items-center gap-3 px-4 py-2 border-b border-white/5 hover:bg-white/5">
            <span className={`text-[10px] px-2 py-0.5 rounded-full w-16 text-center ${ACTIVE_STYLE[u.active] || 'bg-white/10 text-white/50'}`}>{u.sub}</span>
            <div className="flex-1 min-w-0">
              <div className="text-sm text-white truncate">{u.unit}</div>
              <div className="text-xs text-white/40 truncate">{u.description}</div>
            </div>
            <span className="text-xs text-white/40 w-20 text-right">{u.enabled}</span>
            <div className="flex">
              {u.active === 'active' ? <Btn u={u} action="stop" icon="stop" /> : <Btn u={u} action="start" icon="play_arrow" />}
              <Btn u={u} action="restart" icon="restart_alt" />
              {u.enabled === 'enabled' ? <Btn u={u} action="disable" icon="toggle_on" /> : u.enabled === 'disabled' ? <Btn u={u} action="enable" icon="toggle_off" /> : null}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default ServicesPanel;
