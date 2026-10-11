import React, { useState } from 'react';
import type { ActivityRecord } from '../lab.d';
import { isFailure, usePoll } from '../hooks';
import { PollNotice } from './Panel';
import { Button, Loading, Notice, PanelHeading, Search } from './Panel';
const colors: Record<string, string> = { success: 'text-emerald-300', failed: 'text-red-300', running: 'text-amber-300', stopped: 'text-white/50', interrupted: 'text-amber-300' };
export default function ActivityPanel({ onRepeat }: { onRepeat: (record: ActivityRecord) => void }) {
  const { data, reload, error: pollError, updatedAt } = usePoll(() => window.lab.activity(), 2000);
  const [query, setQuery] = useState(''), [status, setStatus] = useState('all'), [clear, setClear] = useState(false), [message, setMessage] = useState('');
  const result = data && !isFailure(data) ? data : null;
  const rows = result?.records.filter(r => (status === 'all' || r.status === status) && `${r.command} ${r.cwd}`.toLowerCase().includes(query.toLowerCase())) || [];
  const clearHistory = async () => { const r = await window.lab.clearActivity(); setMessage(r.ok ? '' : r.error || 'Could not clear history'); setClear(false); reload(); };
  return <div className="p-6 max-w-6xl mx-auto"><PollNotice error={pollError} updatedAt={updatedAt} /><PanelHeading title="Command activity" description="The last 200 commands, their working directories, and exit status. Output stays in the terminal; command text can contain sensitive values."><Button onClick={() => setClear(!clear)}>Clear history</Button></PanelHeading>
    {clear && <Notice>Remove completed entries from activity history? Saved commands and terminal output are kept.<div className="flex gap-2 mt-3"><Button onClick={clearHistory}>Confirm clear</Button><Button onClick={() => setClear(false)}>Cancel</Button></div></Notice>}{(message || result?.warning) && <Notice error>{message || result?.warning}</Notice>}
    <div className="flex gap-3 mb-4"><Search label="Search commands or directories…" value={query} onChange={setQuery} /><select aria-label="Filter activity status" className="bg-[#141a24] border border-white/10 rounded-lg px-3 text-sm" value={status} onChange={e => setStatus(e.target.value)}>{['all', 'success', 'failed', 'running', 'stopped', 'interrupted'].map(s => <option key={s}>{s}</option>)}</select></div><Loading data={data} />
    <div className="space-y-3">{rows.map(r => <div key={r.id} className="glass-panel p-4 rounded-xl"><div className="flex items-center gap-3 text-xs mb-3"><span className={colors[r.status]}>{r.status}</span><span className="text-white/40">{new Date(r.startedAt).toLocaleString()}</span><span className="text-white/40">{r.duration !== undefined ? `${(r.duration / 1000).toFixed(1)}s` : ''}{r.code !== undefined && r.code !== null ? ` · exit ${r.code}` : ''}</span><Button className="ml-auto" disabled={r.status === 'running'} onClick={() => onRepeat(r)}>Review & repeat</Button></div><pre className="text-sm whitespace-pre-wrap break-all">{r.command}</pre><div className="text-xs text-white/40 mt-2 break-all">{r.cwd}</div></div>)}</div>{result && !rows.length && <div className="p-8 text-center text-white/40 text-sm">No matching activity. Run a command to start your history.</div>}
  </div>;
}
