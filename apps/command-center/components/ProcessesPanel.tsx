import React, { useMemo, useState } from 'react';
import { formatBytes, formatUptime } from '../format';
import { isFailure, usePoll } from '../hooks';
import { PollNotice } from './Panel';

interface Props { onLog: (text: string, type: 'info' | 'success' | 'error') => void }

type SortKey = 'cpu' | 'mem' | 'pid';

const ProcessesPanel: React.FC<Props> = ({ onLog }) => {
  const [updatesPaused, setUpdatesPaused] = useState(false);
  const { data, reload, loading, error: pollError, updatedAt } = usePoll(() => window.lab.processes(), updatesPaused ? null : 3000);
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<SortKey>('cpu');
  const [paused, setPaused] = useState<number | null>(null);
  const [armed, setArmed] = useState<number | null>(null);
  const result = data && !isFailure(data) ? data : null;

  const rows = useMemo(() => {
    const q = query.toLowerCase();
    return (result?.processes ?? [])
      .filter((p) => !q || p.cmd.toLowerCase().includes(q) || p.user.toLowerCase().includes(q) || String(p.pid) === q)
      .sort((a, b) => (sort === 'pid' ? a.pid - b.pid : b[sort] - a[sort]))
      .slice(0, 150);
  }, [result, query, sort]);

  const send = async (pid: number, name: string, signal: string) => {
    const r = await window.lab.signal(pid, signal);
    onLog(r.ok ? `[kill] ${signal} → ${name} (${pid})` : `[kill] ${name} (${pid}): ${r.error}`, r.ok ? 'success' : 'error');
    setArmed(null);
    if (signal === 'SIGSTOP' && r.ok) setPaused(pid);
    if (signal === 'SIGCONT' && r.ok) setPaused(null);
    reload();
  };

  const head = (key: SortKey, label: string, cls = '') => (
    <button onClick={() => setSort(key)} className={`${cls} ${sort === key ? 'text-white' : 'text-white/40 hover:text-white'}`}>{label}{sort === key ? ' ↓' : ''}</button>
  );

  return (
    <div className="h-full flex flex-col"><PollNotice error={pollError} updatedAt={updatedAt} />
      <div className="p-4 border-b border-white/5 flex items-center gap-3">
        <input type="text" aria-label="Filter processes" placeholder="Filter by command, user or PID…" value={query} onChange={(e) => setQuery(e.target.value)}
          className="flex-1 bg-black/20 border border-white/10 rounded-full px-4 py-2 text-sm text-white outline-none" />
        <button aria-pressed={updatesPaused} onClick={() => setUpdatesPaused(!updatesPaused)} className="text-xs text-white/60 px-2 py-2 bg-white/5 rounded">{updatesPaused ? 'Resume updates' : 'Pause updates'}</button><button disabled={loading} onClick={reload} className="text-xs text-white/60 px-2 py-2 bg-white/5 rounded">Refresh</button>
        <span className="text-xs text-white/40">{result ? `${result.processes.length} processes` : ''}</span>
      </div>
      {data && isFailure(data) && <div className="p-4 text-sm text-red-400">{data.error}</div>}
      <div className="grid grid-cols-[4rem_6rem_1fr_4rem_4rem_5rem_5rem_9rem] gap-2 px-4 py-2 text-xs border-b border-white/10">
        {head('pid', 'PID', 'text-left')}<span className="text-white/40">User</span><span className="text-white/40">Command</span>
        {head('cpu', 'CPU avg', 'text-right')}{head('mem', 'Mem', 'text-right')}<span className="text-white/40 text-right">RSS</span><span className="text-white/40 text-right">Age</span><span />
      </div>
      <div className="flex-1 overflow-y-auto">
        {rows.map((p) => (
          <div key={p.pid} className="group grid grid-cols-[4rem_6rem_1fr_4rem_4rem_5rem_5rem_9rem] gap-2 px-4 py-1.5 border-b border-white/5 hover:bg-white/5 text-sm items-center">
            <span className="text-white/50 tabular-nums">{p.pid}</span>
            <span className="text-white/60 truncate">{p.user}</span>
            <span className="text-white truncate" title={p.cmd}>{p.cmd}</span>
            <span className="text-right tabular-nums text-white/80">{p.cpu.toFixed(1)}</span>
            <span className="text-right tabular-nums text-white/80">{p.mem.toFixed(1)}</span>
            <span className="text-right tabular-nums text-white/50">{formatBytes(p.rss)}</span>
            <span className="text-right tabular-nums text-white/50">{formatUptime(p.age)}</span>
            <span className="flex justify-end gap-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100">
              {armed === p.pid ? (
                <>
                  <button onClick={() => send(p.pid, p.cmd.split(' ')[0], 'SIGKILL')} className="px-2 text-xs rounded bg-red-600 text-white">Force kill</button>
                  <button onClick={() => setArmed(null)} className="px-2 text-xs rounded bg-white/10 text-white/70">Cancel</button>
                </>
              ) : (
                <>
                  <button title="Pause / resume" aria-label={`Pause or resume ${p.pid}`} onClick={() => send(p.pid, p.cmd.split(' ')[0], paused === p.pid || p.state.startsWith('T') ? 'SIGCONT' : 'SIGSTOP')} className="p-1 text-white/50 hover:text-white"><span className="material-symbols-outlined text-[18px]">{p.state.startsWith('T') ? 'play_arrow' : 'pause'}</span></button>
                  <button title="Terminate (SIGTERM)" aria-label={`Terminate ${p.pid}`} onClick={() => send(p.pid, p.cmd.split(' ')[0], 'SIGTERM')} className="p-1 text-white/50 hover:text-white"><span className="material-symbols-outlined text-[18px]">stop</span></button>
                  <button title="Force kill…" aria-label={`Force kill ${p.pid}`} onClick={() => setArmed(p.pid)} className="p-1 text-white/50 hover:text-red-400"><span className="material-symbols-outlined text-[18px]">close</span></button>
                </>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};

export default ProcessesPanel;
