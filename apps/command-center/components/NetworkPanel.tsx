import React, { useState } from 'react';
import { isFailure, usePoll } from '../hooks';
import { PollNotice } from './Panel';

const quality = (dbm: number) => (dbm >= -55 ? ['Excellent', 'text-green-300'] : dbm >= -67 ? ['Good', 'text-green-300'] : dbm >= -75 ? ['Fair', 'text-yellow-300'] : ['Weak', 'text-red-300']);
const latencyColor = (ms: number | null, loss: number) => (loss >= 100 ? 'text-red-300' : loss > 0 || (ms ?? 0) > 150 ? 'text-yellow-300' : 'text-green-300');

const NetworkPanel: React.FC = () => {
  const { data, error: pollError, updatedAt } = usePoll(() => window.lab.network(), 4000);
  const net = data && !isFailure(data) ? data : null;
  const gw = net?.gateway?.ip;
  const [anchor, setAnchor] = useState('1.1.1.1');
  const [probes, setProbes] = useState<{ host: string; loss: number; avg: number | null; max: number | null }[] | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState('');

  const runProbe = async () => {
    setProbing(true); setProbeError('');
    try { const r = await window.lab.probe([...(gw ? [gw] : []), anchor]); if (isFailure(r)) setProbeError(r.error); else setProbes(r); }
    catch (e) { setProbeError((e as Error).message); } finally { setProbing(false); }
  };

  if (data && isFailure(data)) return <div className="p-6 text-red-400 text-sm">{data.error}</div>;
  if (!net) return <div className="p-6 text-white/40 text-sm">Reading network…</div>;

  return (
    <div className="p-6 overflow-y-auto h-full"><PollNotice error={pollError} updatedAt={updatedAt} />
      <div className="max-w-5xl mx-auto space-y-4">
        <h2 className="text-2xl font-light text-white">Network</h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {net.ifaces.map((i) => {
            const w = net.wireless.find((x) => x.name === i.name);
            const [label, color] = w ? quality(w.signal) : ['', ''];
            return (
              <div key={i.name} className="glass-panel rounded-xl p-4">
                <div className="flex justify-between items-baseline">
                  <span className="text-white font-medium">{i.name}</span>
                  <span className={`text-xs ${i.state === 'UP' ? 'text-green-300' : 'text-white/40'}`}>{i.state}</span>
                </div>
                <div className="text-xs text-white/40 font-mono mb-2">{i.mac}</div>
                {i.addrs.map((a) => <div key={a} className="text-sm text-white/80 font-mono break-all">{a}</div>)}
                {w && (
                  <div className="mt-3 pt-3 border-t border-white/10 text-sm flex justify-between">
                    <span className="text-white/50">Signal</span>
                    <span className={color}>{w.signal} dBm · {label} · link {w.link}/70</span>
                  </div>
                )}
                {w && w.retries > 0 && <div className="text-xs text-white/40 mt-1">{w.retries} retries since boot</div>}
              </div>
            );
          })}
        </div>

        <div className="glass-panel rounded-xl p-4">
          <div className="flex flex-wrap items-center gap-3 mb-3">
            <div className="flex-1 min-w-48">
              <div className="text-xs uppercase tracking-wider text-white/50">Connectivity probe</div>
              <div className="text-xs text-white/40">Gateway {gw ?? 'none'} separates a Wi-Fi fault from an internet fault.</div>
            </div>
            <input value={anchor} onChange={(e) => setAnchor(e.target.value.trim())} aria-label="External host" className="w-40 bg-black/20 border border-white/10 rounded px-3 py-1.5 text-sm text-white outline-none font-mono" />
            <button disabled={probing || !anchor} onClick={runProbe} className="px-4 py-2 rounded text-sm font-bold text-white bg-whitecube-accent hover:bg-red-600 disabled:opacity-40">{probing ? 'Probing…' : 'Run probe'}</button>
          </div>
          {probeError && <div role="alert" className="text-xs text-amber-200 mb-3">{probeError}</div>}
          {probes?.map((p) => (
            <div key={p.host} className="flex justify-between py-1.5 border-t border-white/5 text-sm">
              <span className="text-white/80 font-mono">{p.host}{p.host === gw ? ' (gateway)' : ''}</span>
              <span className={`tabular-nums ${latencyColor(p.avg, p.loss)}`}>{p.loss >= 100 ? 'unreachable' : `${p.avg?.toFixed(0)} ms avg · ${p.max?.toFixed(0)} ms max · ${p.loss}% loss`}</span>
            </div>
          ))}
        </div>

        <div className="glass-panel rounded-xl p-4">
          <div className="text-xs uppercase tracking-wider text-white/50 mb-2">Listening sockets</div>
          <table className="w-full text-sm">
            <tbody>
              {net.listeners.map((l, i) => (
                <tr key={`${l.proto}${l.local}${i}`} className="border-t border-white/5 text-white/80">
                  <td className="py-1 w-14 text-white/50 uppercase text-xs">{l.proto}</td>
                  <td className="font-mono">{l.local}</td>
                  <td className="text-right text-white/60">{l.process || '—'}{l.pid ? <span className="text-white/30"> · {l.pid}</span> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};

export default NetworkPanel;
