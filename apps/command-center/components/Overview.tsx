import React, { useState } from 'react';
import type { HostInfo, Snapshot } from '../lab.d';
import { formatBytes, formatRate, formatUptime } from '../format';
import { isFailure, usePoll } from '../hooks';
import { useMonitoring } from '../monitoring';
import { ScriptItem, ViewMode } from '../types';
import { Notice, PollNotice } from './Panel';

const Icon = ({ name, className = '' }: { name: string; className?: string }) => (
  <span aria-hidden="true" className={`material-symbols-outlined ${className}`}>
    {name}
  </span>
);
const Meter = ({
  label,
  value,
  detail,
  pct,
  icon,
  onClick,
}: {
  label: string;
  value: string;
  detail: string;
  pct: number;
  icon: string;
  onClick: () => void;
}) => (
  <button onClick={onClick} className="wc-metric text-left">
    <div className="flex items-center justify-between mb-4">
      <span className="wc-eyebrow">{label}</span>
      <Icon name={icon} className="text-white/40 text-xl" />
    </div>
    <div className="wc-metric-value tabular-nums">{value}</div>
    <p className="text-xs text-white/55 mt-2 mb-4 truncate" title={detail}>
      {detail}
    </p>
    <div
      className="h-1 rounded-full bg-white/8 overflow-hidden"
      role="progressbar"
      aria-label={`${label} utilization`}
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full"
        style={{
          width: `${Math.max(0, Math.min(100, pct))}%`,
          background: pct >= 90 ? '#ff717b' : pct >= 75 ? '#efbc66' : '#ff555e',
        }}
      />
    </div>
  </button>
);
type Metric = 'CPU' | 'Memory' | 'Network';
const sampleValue = (s: Snapshot, metric: Metric) =>
  metric === 'CPU'
    ? s.cpu
    : metric === 'Memory'
      ? s.mem.total
        ? (s.mem.used / s.mem.total) * 100
        : 0
      : s.net.reduce((sum, n) => sum + n.rxRate + n.txRate, 0);
function Trend({ samples }: { samples: (Snapshot & { sampleInterval: number })[] }) {
  const [metric, setMetric] = useState<Metric>('CPU'),
    [minutes, setMinutes] = useState(2);
  const end = samples.at(-1)?.at ?? Date.now(),
    start = end - minutes * 60000;
  const selected = samples.filter((s) => s.at >= start);
  const maximum =
    metric === 'Network' ? Math.max(1024, ...selected.map((s) => sampleValue(s, metric))) : 100;
  const points = selected.map((s) => ({
    x: ((s.at - start) / (end - start)) * 800,
    y: 144 - (Math.max(0, Math.min(maximum, sampleValue(s, metric))) / maximum) * 136,
    at: s.at,
    interval: s.sampleInterval,
  }));
  const line = points
    .map(
      (p, i) =>
        `${i === 0 || p.at - points[i - 1].at > Math.max(p.interval, points[i - 1].interval) * 2.5 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`
    )
    .join(' ');
  const latest = selected.at(-1),
    value = latest ? sampleValue(latest, metric) : null;
  return (
    <section className="glass-panel rounded-xl p-5 min-w-0" aria-label="Resource trends">
      <div className="flex flex-wrap justify-between items-start gap-3 mb-5">
        <div>
          <h3 className="wc-section-title">Resource trends</h3>
          <p className="text-xs text-white/45 mt-1">
            Last {minutes} minutes · gaps show missing samples
          </p>
        </div>
        <div className="wc-segmented" aria-label="Trend time window">
          {[2, 5, 10].map((n) => (
            <button key={n} aria-pressed={minutes === n} onClick={() => setMinutes(n)}>
              {n}m
            </button>
          ))}
        </div>
      </div>
      <div className="flex justify-between items-center mb-4 gap-2">
        <div className="wc-segmented" aria-label="Trend metric">
          {(['CPU', 'Memory', 'Network'] as const).map((m) => (
            <button key={m} aria-pressed={metric === m} onClick={() => setMetric(m)}>
              {m}
            </button>
          ))}
        </div>
        <span className="text-sm tabular-nums text-white/80">
          {value === null
            ? 'Waiting for samples'
            : metric === 'Network'
              ? formatRate(value)
              : `${value.toFixed(1)}%`}
        </span>
      </div>
      <div className="relative pl-9">
        <div className="absolute left-0 top-0 h-36 flex flex-col justify-between text-[10px] text-white/40 tabular-nums">
          <span>{metric === 'Network' ? formatRate(maximum) : '100%'}</span>
          <span>{metric === 'Network' ? formatRate(maximum / 2) : '50%'}</span>
          <span>0</span>
        </div>
        <svg
          viewBox="0 0 800 152"
          preserveAspectRatio="none"
          className="w-full h-36"
          role="img"
          aria-label={`${metric} history, last ${minutes} minutes, ${selected.length} samples`}
        >
          {[8, 76, 144].map((y) => (
            <line
              key={y}
              x1="0"
              x2="800"
              y1={y}
              y2={y}
              stroke="rgba(255,255,255,.08)"
              strokeDasharray="4 6"
            />
          ))}
          <path
            d={line}
            fill="none"
            stroke={metric === 'CPU' ? '#ff646c' : metric === 'Memory' ? '#c2a5ef' : '#74cbbc'}
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
            strokeLinejoin="round"
          />
          {points.at(-1) && (
            <circle cx={points.at(-1)!.x} cy={points.at(-1)!.y} r="3" fill="#f7e6e8" />
          )}
        </svg>
        <div className="flex justify-between text-[10px] text-white/40 mt-2">
          <span>{minutes} min ago</span>
          <span>{selected.length} samples collected</span>
          <span>Latest</span>
        </div>
      </div>
    </section>
  );
}
interface Props {
  host: HostInfo | null;
  webApis: boolean;
  onToggleWeb: (v: boolean) => void;
  onNavigate: (v: ViewMode) => void;
  scripts: ScriptItem[];
  running: boolean;
  onRun: (id: string) => void;
  onNew: () => void;
}
export default function Overview({
  host,
  webApis,
  onToggleWeb,
  onNavigate,
  scripts,
  running,
  onRun,
  onNew,
}: Props) {
  const {
    snapshot: snap,
    samples,
    loading,
    reload,
    error,
    updatedAt,
    paused,
    setPaused,
    interval,
  } = useMonitoring();
  const diagnostics = usePoll(() => window.lab.diagnostics(), 60000),
    updates = usePoll(
      () =>
        window.lab.updates().then((r) => {
          if (r.error) throw new Error(r.error);
          return r;
        }),
      60000
    ),
    activity = usePoll(() => window.lab.activity(), 5000);
  const report = diagnostics.data && !isFailure(diagnostics.data) ? diagnostics.data : null;
  const packages =
    updates.data && !isFailure(updates.data) && !updates.data.error ? updates.data.packages : null;
  const records =
    activity.data && !isFailure(activity.data) ? activity.data.records.slice(0, 3) : [];
  const pinned = scripts.filter((s) => s.pinned).slice(0, 4),
    attention = report?.checks.filter((c) => c.status !== 'ok') ?? [];
  const rootDisk = snap?.disks.find((d) => d.mount === '/') || snap?.disks[0];
  const go = (view: ViewMode, label: string) => (
    <button className="wc-text-button" onClick={() => onNavigate(view)}>
      {label}
      <Icon name="arrow_forward" className="text-base" />
    </button>
  );
  return (
    <div className="wc-overview p-5 lg:p-7">
      <div className="max-w-7xl mx-auto space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="wc-eyebrow mb-2">Your machine, at a glance</div>
            <h2 className="text-3xl tracking-tight font-semibold text-white">
              System overview<span className="text-whitecube-accent">.</span>
            </h2>
            <p className="text-sm text-white/50 mt-2">
              {host?.hostname || 'This machine'}
              <span className="mx-2 text-white/20">/</span>
              {host?.os || 'Reading host information'}
              {snap && <span className="ml-2">· up {formatUptime(snap.uptime)}</span>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span
              className={`wc-status ${error ? 'wc-status-warning' : paused ? '' : 'wc-status-success'}`}
            >
              <span className="wc-status-dot" />
              {error
                ? 'Refresh failed'
                : paused
                  ? 'Paused'
                  : snap
                    ? 'Live monitoring'
                    : 'Connecting'}
            </span>
            <button
              className="wc-icon-button"
              aria-label={paused ? 'Resume monitoring' : 'Pause monitoring'}
              title={paused ? 'Resume monitoring' : 'Pause monitoring'}
              onClick={() => setPaused(!paused)}
            >
              <Icon name={paused ? 'play_arrow' : 'pause'} />
            </button>
            <button
              className="wc-icon-button"
              aria-label="Refresh monitoring"
              title="Refresh monitoring"
              disabled={loading}
              onClick={reload}
            >
              <Icon name="refresh" className={loading ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>
        <PollNotice error={error} updatedAt={updatedAt} />
        {!snap && (
          <Notice>
            {error
              ? 'System information is unavailable. Use Refresh to try again.'
              : 'Reading live system information…'}
          </Notice>
        )}
        {snap && (
          <>
            <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
              <Meter
                label="CPU"
                value={`${snap.cpu.toFixed(1)}%`}
                pct={snap.cpu}
                icon="memory"
                detail={`${snap.cores} cores · load ${snap.load[0]?.toFixed(2)}`}
                onClick={() => onNavigate('Processes')}
              />
              <Meter
                label="Memory"
                value={`${(snap.mem.total ? (snap.mem.used / snap.mem.total) * 100 : 0).toFixed(1)}%`}
                pct={snap.mem.total ? (snap.mem.used / snap.mem.total) * 100 : 0}
                icon="developer_board"
                detail={`${formatBytes(snap.mem.used)} / ${formatBytes(snap.mem.total)}`}
                onClick={() => onNavigate('Processes')}
              />
              <Meter
                label="Storage"
                value={rootDisk ? `${rootDisk.pct}%` : '—'}
                pct={rootDisk?.pct ?? 0}
                icon="hard_drive"
                detail={
                  rootDisk
                    ? `${formatBytes(rootDisk.avail)} available · ${snap.disks.length} mounts`
                    : 'No filesystem information'
                }
                onClick={() => onNavigate('Storage')}
              />
              <button onClick={() => onNavigate('Network')} className="wc-metric text-left">
                <div className="flex justify-between items-center mb-4">
                  <span className="wc-eyebrow">Network</span>
                  <Icon name="lan" className="text-white/40 text-xl" />
                </div>
                <div className="wc-metric-value tabular-nums text-[1.8rem]">
                  {formatRate(snap.net.reduce((n, s) => n + s.rxRate, 0))}
                  <span className="text-sm text-emerald-300 ml-2">↓</span>
                </div>
                <div className="text-xs text-white/55 mt-2">
                  ↑ {formatRate(snap.net.reduce((n, s) => n + s.txRate, 0))} upload
                </div>
                <div className="text-xs text-white/45 mt-4 truncate">
                  {snap.net.map((n) => n.name).join(' · ') || 'No active interfaces'}
                </div>
              </button>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.8fr)_minmax(0,1fr)] gap-4">
              <Trend samples={samples} />
              <section className="glass-panel rounded-xl p-5">
                <div className="flex justify-between gap-2 items-center mb-4">
                  <h3 className="wc-section-title">Attention center</h3>
                  <Icon name="health_and_safety" className="text-white/40 text-xl" />
                </div>
                <PollNotice
                  error={diagnostics.error || updates.error}
                  updatedAt={diagnostics.updatedAt}
                />
                {!report && (
                  <p className="text-xs text-white/50 mb-4">
                    {diagnostics.error ? 'Diagnostics unavailable' : 'Checking system health…'}
                  </p>
                )}
                {report && !attention.length && (
                  <div className="flex items-center gap-3 p-3 rounded-lg bg-emerald-400/5 border border-emerald-400/15 mb-3">
                    <Icon name="check_circle" className="text-emerald-300" />
                    <div>
                      <p className="text-sm text-emerald-200">All checks passed</p>
                      <p className="text-xs text-white/50 mt-1">Services, packages and storage</p>
                    </div>
                  </div>
                )}
                {attention.slice(0, 3).map((c) => (
                  <button
                    key={c.id}
                    onClick={() => onNavigate('Diagnostics')}
                    className="wc-attention-row"
                  >
                    <Icon
                      name={c.status === 'unknown' ? 'help' : 'warning'}
                      className={
                        c.status === 'critical'
                          ? 'text-red-300'
                          : c.status === 'unknown'
                            ? 'text-white/50'
                            : 'text-amber-300'
                      }
                    />
                    <div className="min-w-0 flex-1">
                      <div className="text-sm">{c.title}</div>
                      <div className="text-xs text-white/50 truncate mt-1" title={c.detail}>
                        {c.detail}
                      </div>
                    </div>
                    <span className="text-[10px] uppercase text-white/60">{c.status}</span>
                  </button>
                ))}
                <button onClick={() => onNavigate('Updates')} className="wc-attention-row">
                  <Icon name="system_update" className="text-white/50" />
                  <div className="flex-1 text-sm">
                    {packages
                      ? packages.length
                        ? `${packages.length} package updates`
                        : 'Packages up to date'
                      : updates.loading
                        ? 'Checking package index…'
                        : 'Update check unavailable'}
                    <p className="text-xs text-white/45 mt-1">From the local package index</p>
                  </div>
                  <Icon name="chevron_right" className="text-white/40" />
                </button>
                <div className="mt-3">{go('Diagnostics', 'View diagnostics')}</div>
              </section>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <section className="glass-panel rounded-xl p-5">
                <div className="flex justify-between items-center mb-4">
                  <h3 className="wc-section-title">Pinned commands</h3>
                  {go('Dashboard', 'Open library')}
                </div>
                {pinned.length ? (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    {pinned.map((s) => (
                      <button
                        key={s.id}
                        disabled={running}
                        onClick={() => onRun(s.id)}
                        className="wc-quick-command"
                        title={s.command}
                      >
                        <Icon name={s.icon} className="text-whitecube-accent" />
                        <div className="min-w-0 flex-1">
                          <div className="text-sm truncate">{s.name}</div>
                          <div className="text-[11px] text-white/45 mt-1 truncate font-mono">
                            {s.command}
                          </div>
                        </div>
                        <Icon
                          name={s.requiresConfirmation ? 'lock' : 'play_arrow'}
                          className="text-white/40 text-base"
                        />
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="p-4 border border-dashed border-white/15 rounded-lg">
                    <p className="text-sm text-white/70">Keep your everyday commands close.</p>
                    <p className="text-xs text-white/45 mt-2">
                      Pin commands in your library to run them from here.
                    </p>
                    <button onClick={onNew} className="wc-text-button mt-3">
                      <Icon name="add" className="text-base" />
                      Create a command
                    </button>
                  </div>
                )}
              </section>
              <section className="glass-panel rounded-xl p-5">
                <div className="flex justify-between items-center mb-4">
                  <h3 className="wc-section-title">Recent activity</h3>
                  {go('Activity', 'View all')}
                </div>
                <PollNotice error={activity.error} updatedAt={activity.updatedAt} />
                {records.length ? (
                  records.map((r) => (
                    <div
                      key={r.id}
                      className="flex gap-3 items-center py-3 border-t border-white/6"
                    >
                      <Icon
                        name={
                          r.status === 'success'
                            ? 'check_circle'
                            : r.status === 'running'
                              ? 'pending'
                              : 'info'
                        }
                        className={
                          r.status === 'success'
                            ? 'text-emerald-300 text-lg'
                            : 'text-white/50 text-lg'
                        }
                      />
                      <div className="min-w-0 flex-1">
                        <p className="font-mono text-xs truncate" title={r.command}>
                          {r.command}
                        </p>
                        <p className="text-[11px] text-white/45 mt-1">
                          {new Date(r.startedAt).toLocaleTimeString()} · {r.status}
                          {r.duration !== undefined ? ` · ${(r.duration / 1000).toFixed(1)}s` : ''}
                        </p>
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="text-sm text-white/50 py-4">
                    {activity.loading ? 'Reading activity…' : 'Your next command will appear here.'}
                  </p>
                )}
              </section>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <section className="glass-panel rounded-xl p-5 min-w-0">
                <div className="flex justify-between items-center mb-3">
                  <h3 className="wc-section-title">
                    Top processes{' '}
                    <span className="text-xs text-white/40 font-normal">· lifetime CPU</span>
                  </h3>
                  {go('Processes', 'Inspect')}
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-white/45 text-left">
                        <th className="font-normal pb-3">Process</th>
                        <th className="font-normal text-right">PID</th>
                        <th className="font-normal text-right">CPU</th>
                        <th className="font-normal text-right">Memory</th>
                      </tr>
                    </thead>
                    <tbody>
                      {snap.top.slice(0, 5).map((p) => (
                        <tr key={p.pid} className="border-t border-white/6">
                          <td className="py-2.5 max-w-48 truncate">{p.name}</td>
                          <td className="text-right text-white/50 tabular-nums">{p.pid}</td>
                          <td className="text-right tabular-nums">{p.cpu.toFixed(1)}%</td>
                          <td className="text-right tabular-nums">{p.mem.toFixed(1)}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
              <section className="glass-panel rounded-xl p-5">
                <h3 className="wc-section-title mb-4">Machine details</h3>
                <dl className="space-y-3 text-xs">
                  {[
                    ['Kernel', host?.kernel ?? 'Reading…'],
                    ['Architecture', host?.arch ?? 'Reading…'],
                    [
                      'Swap',
                      snap.mem.swapTotal
                        ? `${formatBytes(snap.mem.swapUsed)} / ${formatBytes(snap.mem.swapTotal)}`
                        : 'Not configured',
                    ],
                    ...snap.temps.slice(0, 2).map((t) => [t.name, `${t.c.toFixed(0)} °C`]),
                    ...(snap.battery
                      ? [['Battery', `${snap.battery.pct}% · ${snap.battery.status}`]]
                      : []),
                  ].map(([label, value]) => (
                    <div key={label} className="flex justify-between gap-4">
                      <dt className="text-white/50">{label}</dt>
                      <dd className="text-right break-all">{value}</dd>
                    </div>
                  ))}
                </dl>
                <div className="pt-4 mt-4 border-t border-white/6">
                  {go('Display', 'Display session')}
                </div>
              </section>
            </div>
          </>
        )}
        {webApis && <Weather onDisable={() => onToggleWeb(false)} />}
        <div className="flex flex-wrap justify-between gap-2 text-[11px] text-white/40 pb-1">
          <span>
            Local system data · refresh every {interval / 1000}s
            {updatedAt ? ` · updated ${new Date(updatedAt).toLocaleTimeString()}` : ''}
          </span>
          <button onClick={() => onNavigate('Settings')} className="hover:text-white/80">
            Monitoring preferences ↗
          </button>
        </div>
      </div>
    </div>
  );
}
function Weather({ onDisable }: { onDisable: () => void }) {
  const { data, error, updatedAt } = usePoll(() => window.lab.outside(), 600000);
  const weather = data && !isFailure(data) ? data : null;
  return (
    <section className="glass-panel rounded-xl p-5">
      <div className="flex justify-between">
        <h3 className="wc-section-title">Local weather</h3>
        <button onClick={onDisable} className="wc-text-button">
          Disable
        </button>
      </div>
      <PollNotice error={error} updatedAt={updatedAt} />
      <p className="text-xs text-white/50 mt-3">
        {weather
          ? `${weather.tempC.toFixed(0)} °C · ${weather.city}, ${weather.country} · wind ${weather.wind.toFixed(0)} km/h`
          : 'Loading weather…'}
      </p>
    </section>
  );
}
