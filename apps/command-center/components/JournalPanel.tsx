import React, { useState } from 'react';
import { isFailure, usePoll } from '../hooks';
import { PollNotice } from './Panel';
import { Button, Notice, Search } from './Panel';

const LEVELS: { label: string; value: number }[] = [
  { label: 'Errors', value: 3 }, { label: 'Warnings', value: 4 }, { label: 'Notices', value: 5 }, { label: 'Info', value: 6 }, { label: 'Debug', value: 7 },
];
const PRIO_COLOR = (p: number) => (p <= 3 ? 'text-red-400' : p === 4 ? 'text-yellow-300' : 'text-white/80');

const JournalPanel: React.FC = () => {
  const [priority, setPriority] = useState(4);
  const [unit, setUnit] = useState('');
  const [scope, setScope] = useState<'system' | 'user'>('system');
  const [follow, setFollow] = useState(true);
  const [query, setQuery] = useState('');
  const [message, setMessage] = useState('');
  const { data, reload, error: pollError, updatedAt } = usePoll(() => window.lab.journal({ lines: 300, priority, unit: /\.\w+$/.test(unit) ? unit : unit ? `${unit}.service` : '', scope }).then(r => { if (r.error) throw new Error(r.error); return r; }), follow ? 3000 : null, `${scope}:${priority}:${unit}`);
  const result = data && !isFailure(data) ? data : null;
  const error = data && isFailure(data) ? data.error : result?.error;
  const entries = result?.entries.filter(e => `${e.unit} ${e.message}`.toLowerCase().includes(query.toLowerCase())) || [];
  const exportLog = async () => {
    const r = await window.lab.saveFile('command-center-journal.txt', entries.map(e => `${new Date(e.ts).toISOString()} [${e.priority}] ${e.unit}: ${e.message}`).join('\n'));
    setMessage(isFailure(r) ? r.error : r.path ? `Saved ${entries.length} entries to ${r.path}` : '');
  };

  return (
    <div className="h-full flex flex-col"><PollNotice error={pollError} updatedAt={updatedAt} />
      <div className="p-4 border-b border-white/5 flex flex-wrap items-center gap-3">
        <select aria-label="Journal priority" value={priority} onChange={(e) => setPriority(Number(e.target.value))} className="bg-black/30 border border-white/10 rounded px-3 py-1.5 text-sm text-white">
          {LEVELS.map((l) => <option key={l.value} value={l.value}>{l.label} and above</option>)}
        </select>
        <input type="text" aria-label="Journal unit" placeholder="Unit (e.g. NetworkManager)" value={unit} onChange={(e) => setUnit(e.target.value.trim())}
          className="flex-1 min-w-48 bg-black/20 border border-white/10 rounded-full px-4 py-2 text-sm text-white outline-none" />
        <select aria-label="Journal scope" value={scope} onChange={(e) => setScope(e.target.value as 'system' | 'user')} className="bg-black/30 border border-white/10 rounded px-3 py-1.5 text-sm text-white">
          <option value="system">system</option><option value="user">user</option>
        </select>
        <label className="flex items-center gap-2 text-sm text-white/60"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> live</label>
        <Button onClick={reload}>Refresh</Button><Button disabled={!entries.length} onClick={exportLog}>Export</Button>
      </div>
      <div className="px-4 py-3"><Search label="Search journal messages…" value={query} onChange={setQuery} /></div>
      {message && <Notice>{message}</Notice>}
      {error && <div className="p-4 text-sm text-red-400">{error}. Reading the system journal may need membership of the systemd-journal or adm group.</div>}
      <div className="flex-1 overflow-y-auto font-mono text-xs">
        {!data && <div className="p-4 text-white/40">Reading journal…</div>}
        {result && entries.length === 0 && <div className="p-4 text-white/40">No entries match these filters.</div>}
        {entries.map((e, i) => (
          <div key={`${e.ts}-${i}`} className={`flex gap-3 px-4 py-1 border-b border-white/5 ${PRIO_COLOR(e.priority)}`}>
            <span className="text-white/40 shrink-0 tabular-nums">{new Date(e.ts).toLocaleTimeString()}</span>
            <span className="text-whitecube-warm-grey shrink-0 w-40 truncate">{e.unit}</span>
            <span className="break-words min-w-0">{e.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

export default JournalPanel;
