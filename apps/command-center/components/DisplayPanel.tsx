import React from 'react';
import { isFailure, usePoll } from '../hooks';

const Row: React.FC<{ k: string; v: React.ReactNode; ok?: boolean }> = ({ k, v, ok }) => (
  <div className="flex justify-between gap-4 py-2 border-b border-white/5 text-sm">
    <span className="text-white/50">{k}</span>
    <span className={`text-right ${ok === undefined ? 'text-white' : ok ? 'text-green-300' : 'text-red-300'}`}>{v}</span>
  </div>
);

const DisplayPanel: React.FC = () => {
  const { data } = usePoll(() => window.lab.display(), 10000);
  if (!data) return <div className="p-6 text-white/40 text-sm">Reading session…</div>;
  if (isFailure(data)) return <div className="p-6 text-red-400 text-sm">{data.error}</div>;
  const wayland = data.sessionType === 'wayland' && !!data.waylandDisplay;

  return (
    <div className="p-6 overflow-y-auto h-full">
      <div className="max-w-3xl mx-auto space-y-4">
        <div>
          <h2 className="text-2xl font-light text-white mb-1">Display Session</h2>
          <p className="text-white/50 text-sm">This app refuses to start outside Wayland and never falls back to X11.</p>
        </div>
        <div className="glass-panel rounded-xl p-4">
          <Row k="Session type" v={data.sessionType} ok={wayland} />
          <Row k="WAYLAND_DISPLAY" v={data.waylandDisplay ?? 'not set'} ok={!!data.waylandDisplay} />
          <Row k="Wayland sockets" v={data.sockets.join(', ') || 'none'} />
          <Row k="DISPLAY (X11)" v={data.x11Display ?? 'hidden from this app'} ok={!data.x11Display} />
          <Row k="Desktop" v={data.desktop ?? 'unknown'} />
          <Row k="Electron backend" v="ozone: wayland" ok />
        </div>
        <div className="glass-panel rounded-xl p-4">
          <div className="text-xs uppercase tracking-wider text-white/50 mb-2">Connected outputs</div>
          {data.outputs.length === 0 && <div className="text-sm text-white/40">No connected DRM connectors visible.</div>}
          {data.outputs.map((o) => <Row key={o.connector} k={o.connector} v={`${o.preferred ?? '—'} · ${o.modes} modes`} />)}
        </div>
        {data.randr && (
          <pre className="glass-panel rounded-xl p-4 text-xs text-white/70 font-mono whitespace-pre-wrap">{data.randr}</pre>
        )}
      </div>
    </div>
  );
};

export default DisplayPanel;
