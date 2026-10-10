import React, { useEffect, useRef, useState } from 'react';
import type { HostInfo, Snapshot } from '../lab.d';
import { formatBytes, formatRate, formatUptime } from '../format';
import { isFailure, usePoll } from '../hooks';
import type { ViewMode } from '../types';

const HISTORY = 60;

const barColor = (pct: number) => (pct >= 90 ? 'bg-red-500' : pct >= 70 ? 'bg-yellow-400' : 'bg-whitecube-accent');

const Meter: React.FC<{ label: string; pct: number; detail: string }> = ({ label, pct, detail }) => (
  <div className="glass-panel rounded-xl p-4">
    <div className="flex justify-between items-baseline mb-2">
      <span className="text-xs uppercase tracking-wider text-white/50">{label}</span>
      <span className="text-xl font-light text-white tabular-nums">{pct.toFixed(0)}%</span>
    </div>
    <div className="h-1.5 rounded-full bg-white/10 overflow-hidden" role="progressbar" aria-label={label} aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <div className={`h-full ${barColor(pct)} transition-all duration-500`} style={{ width: `${Math.min(100, pct)}%` }} />
    </div>
    <div className="text-xs text-white/50 mt-2 tabular-nums">{detail}</div>
  </div>
);

const Spark: React.FC<{ values: number[] }> = ({ values }) => {
  const w = 240, h = 48;
  const pts = values.map((v, i) => `${(i / (HISTORY - 1)) * w + (HISTORY - values.length) * (w / (HISTORY - 1))},${h - (Math.min(100, v) / 100) * h}`).join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-12" role="img" aria-label="CPU history">
      <polyline points={pts} fill="none" stroke="#ff3b3b" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
};

const Outside: React.FC<{ enabled: boolean; onToggle: (v: boolean) => void }> = ({ enabled, onToggle }) => {
  const { data } = usePoll(async () => (enabled ? window.lab.outside() : null), enabled ? 600000 : null, String(enabled));
  const o = data && !isFailure(data) ? data : null;
  return (
    <div className="glass-panel rounded-xl p-4">
      <div className="flex justify-between items-center mb-2">
        <span className="text-xs uppercase tracking-wider text-white/50">Outside</span>
        <label className="flex items-center gap-2 text-xs text-white/50"><input type="checkbox" checked={enabled} onChange={(e) => onToggle(e.target.checked)} /> free web APIs</label>
      </div>
      {!enabled && <div className="text-xs text-white/40">Off. Turning on sends your public IP to ipwho.is and open-meteo.com to show local weather.</div>}
      {enabled && !o && <div className="text-xs text-white/40">{data && isFailure(data) ? data.error : 'Loading…'}</div>}
      {o && <><div className="text-xl font-light text-white tabular-nums">{o.tempC.toFixed(0)} °C</div><div className="text-xs text-white/50">{o.city}, {o.country} · wind {o.wind.toFixed(0)} km/h</div></>}
    </div>
  );
};

const Overview: React.FC<{ host: HostInfo | null; webApis: boolean; onToggleWeb: (v: boolean) => void; onNavigate: (v: ViewMode) => void }> = ({ host, webApis, onToggleWeb, onNavigate }) => {
  const { data } = usePoll(() => window.lab.snapshot(), 2000);
  const [cpuHist, setCpuHist] = useState<number[]>([]);
  const lastAt = useRef(0);
  const snap: Snapshot | null = data && !isFailure(data) ? data : null;

  useEffect(() => {
    if (snap && snap.at !== lastAt.current) {
      lastAt.current = snap.at;
      setCpuHist((h) => [...h, snap.cpu].slice(-HISTORY));
    }
  }, [snap]);

  if (data && isFailure(data)) return <div className="p-6 text-red-400 text-sm">Could not read system stats: {data.error}</div>;
  if (!snap) return <div className="p-6 text-white/40 text-sm">Reading system…</div>;

  const memPct = snap.mem.total ? (snap.mem.used / snap.mem.total) * 100 : 0;
  const swapPct = snap.mem.swapTotal ? (snap.mem.swapUsed / snap.mem.swapTotal) * 100 : 0;
  const rx = snap.net.reduce((a, n) => a + n.rxRate, 0);
  const tx = snap.net.reduce((a, n) => a + n.txRate, 0);

  return (
    <div className="p-6">
      <div className="max-w-7xl mx-auto space-y-4">
        <div className="mb-4">
          <h2 className="text-2xl font-light text-white mb-1">{host ? host.hostname : 'System'} <span className="text-white/40 text-base">· up {formatUptime(snap.uptime)}</span></h2>
          <p className="text-white/50 text-sm">{host ? `${host.os} · kernel ${host.kernel} · ${host.cpu}` : ''}</p>
        </div>

        <div className="grid grid-cols-2 xl:grid-cols-4 gap-4">
          <Meter label="CPU" pct={snap.cpu} detail={`${snap.cores} cores · load ${snap.load.map((l) => l.toFixed(2)).join(' ')}`} />
          <Meter label="Memory" pct={memPct} detail={`${formatBytes(snap.mem.used)} of ${formatBytes(snap.mem.total)}`} />
          <Meter label="Swap" pct={swapPct} detail={snap.mem.swapTotal ? `${formatBytes(snap.mem.swapUsed)} of ${formatBytes(snap.mem.swapTotal)}` : 'No swap'} />
          <div className="glass-panel rounded-xl p-4">
            <div className="text-xs uppercase tracking-wider text-white/50 mb-2">Network</div>
            <div className="text-sm text-white tabular-nums">↓ {formatRate(rx)}</div>
            <div className="text-sm text-white tabular-nums">↑ {formatRate(tx)}</div>
            <div className="text-xs text-white/50 mt-2 truncate">{snap.net.map((n) => n.name).join(', ') || 'No interfaces'}</div>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="glass-panel rounded-xl p-4">
            <div className="text-xs uppercase tracking-wider text-white/50 mb-2">CPU history · 2 min</div>
            <Spark values={cpuHist} />
            {(snap.temps.length > 0 || snap.battery) && (
              <div className="mt-3 pt-3 border-t border-white/10 space-y-1 text-xs text-white/60">
                {snap.temps.slice(0, 4).map((t) => <div key={t.name} className="flex justify-between"><span>{t.name}</span><span className="tabular-nums">{t.c.toFixed(0)} °C</span></div>)}
                {snap.battery && <div className="flex justify-between"><span>Battery</span><span>{snap.battery.pct}% · {snap.battery.status}</span></div>}
              </div>
            )}
          </div>

          <div className="glass-panel rounded-xl p-4 lg:col-span-2">
            <div className="text-xs uppercase tracking-wider text-white/50 mb-2">Top processes</div>
            <table className="w-full text-sm">
              <thead><tr className="text-left text-xs text-white/40"><th className="font-normal py-1">Process</th><th className="font-normal text-right">PID</th><th className="font-normal text-right">CPU</th><th className="font-normal text-right">Mem</th></tr></thead>
              <tbody>
                {snap.top.map((p) => (
                  <tr key={p.pid} className="border-t border-white/5 text-white/80">
                    <td className="py-1 truncate max-w-[16rem]">{p.name}</td>
                    <td className="text-right tabular-nums text-white/50">{p.pid}</td>
                    <td className="text-right tabular-nums">{p.cpu.toFixed(1)}%</td>
                    <td className="text-right tabular-nums">{p.mem.toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-3">{([{ view: 'Diagnostics', icon: 'health_and_safety', title: 'System diagnostics', detail: 'Check services, packages & storage' }, { view: 'Activity', icon: 'history', title: 'Command activity', detail: 'Review and repeat recent commands' }, { view: 'Settings', icon: 'backup', title: 'Back up your library', detail: 'Export commands and shortcuts' }] as const).map(item => <button key={item.view} onClick={() => onNavigate(item.view)} className="glass-panel rounded-xl p-4 text-left hover:bg-white/10 transition-colors"><span className="material-symbols-outlined text-whitecube-accent mb-2">{item.icon}</span><div className="text-sm">{item.title}</div><div className="text-xs text-white/40 mt-1">{item.detail}</div></button>)}</div>
        <Outside enabled={webApis} onToggle={onToggleWeb} />

        <div className="glass-panel rounded-xl p-4">
          <div className="text-xs uppercase tracking-wider text-white/50 mb-3">Storage</div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-3">
            {snap.disks.map((d) => (
              <div key={d.mount}>
                <div className="flex justify-between text-sm text-white/80"><span className="truncate">{d.mount}</span><span className="tabular-nums text-white/50">{formatBytes(d.used)} / {formatBytes(d.size)}</span></div>
                <div className="h-1.5 rounded-full bg-white/10 overflow-hidden mt-1"><div className={`h-full ${barColor(d.pct)}`} style={{ width: `${d.pct}%` }} /></div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

export default Overview;
