import { useCallback, useEffect, useRef, useState } from 'react';
import type { Failure } from './lab.d';

export const isFailure = (v: unknown): v is Failure => !!v && typeof v === 'object' && 'error' in v && typeof (v as Failure).error === 'string' && Object.keys(v).length === 1;

// Each filter generation owns its request. Late results cannot replace a newer
// scope, and rejected IPC requests become a visible error rather than a hang.
export function usePoll<T>(fn: () => Promise<T>, ms: number | null, key = '') {
  const [data, setData] = useState<T | Failure | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  const tickRef = useRef<() => void>(() => {});
  fnRef.current = fn;
  const reload = useCallback(() => tickRef.current(), []);
  useEffect(() => {
    let alive = true, busy = false;
    setData(null);
    const tick = async () => {
      if (busy || document.hidden || !alive) return;
      busy = true; setLoading(true);
      try { const result = await fnRef.current(); if (alive) setData(result); }
      catch (e) { if (alive) setData({ error: e instanceof Error ? e.message : 'Request failed' }); }
      finally { busy = false; if (alive) setLoading(false); }
    };
    tickRef.current = tick;
    void tick();
    const timer = ms === null ? null : window.setInterval(tick, ms);
    const visible = () => { if (!document.hidden && ms !== null) void tick(); };
    document.addEventListener('visibilitychange', visible);
    return () => { alive = false; if (timer !== null) window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [ms, key]);
  return { data, loading, reload };
}
