import React, { useMemo, useState } from 'react';
import { isFailure, usePoll } from '../hooks';
import { formatBytes } from '../format';
import { Button, Loading, PanelHeading, Search } from './Panel';
export default function PackagesPanel() {
  const { data, loading, reload } = usePoll(() => window.lab.packages(), null);
  const [query, setQuery] = useState(''), [sort, setSort] = useState('name'), [page, setPage] = useState(0);
  const all = data && !isFailure(data) ? data.packages : [];
  const packages = useMemo(() => all.filter(p => `${p.name} ${p.description}`.toLowerCase().includes(query.toLowerCase())).sort((a, b) => sort === 'size' ? b.bytes - a.bytes : a.name.localeCompare(b.name)), [all, query, sort]);
  const current = Math.min(page, Math.max(0, Math.ceil(packages.length / 60) - 1));
  return <div className="p-6 max-w-7xl mx-auto"><PanelHeading title="Installed packages" description={`${all.length.toLocaleString()} packages · ${formatBytes(all.reduce((sum, p) => sum + p.bytes, 0))} installed size reported by dpkg`}><Button disabled={loading} onClick={reload}>Refresh</Button></PanelHeading><div className="flex gap-3 mb-4"><Search label="Search package names and descriptions…" value={query} onChange={v => { setQuery(v); setPage(0); }} /><select aria-label="Sort packages" className="bg-[#141a24] border border-white/10 rounded-lg px-3 text-sm" value={sort} onChange={e => { setSort(e.target.value); setPage(0); }}><option value="name">Name</option><option value="size">Largest first</option></select></div><Loading data={data} />
    <div className="glass-panel rounded-xl overflow-hidden">{packages.slice(current * 60, (current + 1) * 60).map(p => <div key={p.name} className="flex gap-4 p-3 border-b border-white/5 items-start"><div className="min-w-0 flex-1"><div className="text-sm font-medium">{p.name} <span className="text-xs text-white/30 font-normal">{p.arch}</span></div><div className="text-xs text-white/40 truncate mt-1" title={p.description}>{p.description}</div></div><span className="text-xs text-white/50 break-all max-w-[35%]">{p.version}</span><span className="text-xs text-white/70 w-24 text-right shrink-0">{formatBytes(p.bytes)}</span></div>)}{data && !isFailure(data) && !packages.length && <div className="p-6 text-white/40 text-sm">No packages match your search.</div>}</div>
    <div className="flex items-center justify-between mt-4 text-xs text-white/50"><span>{packages.length.toLocaleString()} matches · Page {current + 1} of {Math.max(1, Math.ceil(packages.length / 60))}</span><div className="flex gap-2"><Button disabled={!current} onClick={() => setPage(current - 1)}>Previous</Button><Button disabled={(current + 1) * 60 >= packages.length} onClick={() => setPage(current + 1)}>Next</Button></div></div>
  </div>;
}
