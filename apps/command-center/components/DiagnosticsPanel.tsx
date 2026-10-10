import React, { useState } from 'react';
import { isFailure, usePoll } from '../hooks';
import { Button, Loading, Notice, PanelHeading } from './Panel';
const styles = { ok: 'text-emerald-300 bg-emerald-400/10', warning: 'text-amber-300 bg-amber-400/10', critical: 'text-red-300 bg-red-400/10', unknown: 'text-white/40 bg-white/5' };
export default function DiagnosticsPanel() {
  const { data, loading, reload } = usePoll(() => window.lab.diagnostics(), null);
  const [message, setMessage] = useState('');
  const report = data && !isFailure(data) ? data : null;
  const exportReport = async () => {
    if (!report) return;
    try {
      const r = await window.lab.saveFile(`whitecube-command-center-diagnostics-${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(report, null, 2));
      setMessage(isFailure(r) ? r.error : r.path ? `Saved to ${r.path}` : '');
    } catch (e) { setMessage((e as Error).message); }
  };
  return <div className="p-6 max-w-6xl mx-auto"><PanelHeading title="System diagnostics" description="A read-only health check of this machine. Reports include your hostname, user, home path, OS, service names, and mount points."><Button disabled={!report} onClick={exportReport}>Export report</Button><Button disabled={loading} onClick={reload}>{loading ? 'Checking…' : 'Run checks'}</Button></PanelHeading><Loading data={data} />{message && <Notice>{message}</Notice>}
    {report && <><div className="grid grid-cols-4 gap-3 mb-6">{(['ok', 'warning', 'critical', 'unknown'] as const).map(s => <div key={s} className={`p-4 rounded-xl ${styles[s]}`}><div className="text-3xl font-light mb-1">{report.checks.filter(c => c.status === s).length}</div><div className="text-xs uppercase tracking-wide">{s === 'ok' ? 'Passed' : s}</div></div>)}</div><div className="glass-panel rounded-xl divide-y divide-white/5">{report.checks.map(c => <div key={c.id} className="flex gap-4 p-4 items-start"><span className={`text-xs rounded-full px-2 py-1 min-w-20 text-center ${styles[c.status]}`}>{c.status}</span><div className="min-w-0"><div className="text-sm font-medium mb-1">{c.title}</div><p className="text-xs text-white/50 whitespace-pre-wrap break-words">{c.detail}</p></div></div>)}</div><p className="text-xs text-white/30 mt-4">Checked {new Date(report.at).toLocaleString()} · Unknown means the check was unavailable, not that it passed.</p></>}
  </div>;
}
