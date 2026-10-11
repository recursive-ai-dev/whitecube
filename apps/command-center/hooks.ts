import { useCallback, useEffect, useRef, useState } from 'react';
import type { Failure } from './lab.d';

export const isFailure = (v: unknown): v is Failure =>
  !!v &&
  typeof v === 'object' &&
  'error' in v &&
  typeof (v as Failure).error === 'string' &&
  Object.keys(v).length === 1;

// Each filter generation owns its request. Late results cannot replace a newer
// scope, and rejected IPC requests become a visible error rather than a hang.
export function usePoll<T>(fn: () => Promise<T>, ms: number | null, key = '') {
  const [data, setData] = useState<T | Failure | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const dataKey = useRef(key);
  const fnRef = useRef(fn);
  const tickRef = useRef<() => void>(() => {});
  fnRef.current = fn;
  const reload = useCallback(() => tickRef.current(), []);
  useEffect(() => {
    let alive = true,
      busy = false,
      completed = false;
    // Interval changes (including pause) retain the current snapshot. Changing
    // the scope clears it so system results never appear under the user scope.
    if (dataKey.current !== key) {
      setData(null);
      setUpdatedAt(null);
      dataKey.current = key;
      setError(null);
    }
    const tick = async () => {
      if (busy || document.hidden || !alive) return;
      busy = true;
      setLoading(true);
      try {
        const result = await fnRef.current();
        if (alive) {
          if (isFailure(result)) {
            setError(result.error);
            setData((previous) => (previous !== null && !isFailure(previous) ? previous : result));
          } else {
            setData(result);
            setError(null);
            setUpdatedAt(Date.now());
          }
        }
      } catch (e) {
        if (alive) {
          const message = e instanceof Error ? e.message : 'Request failed';
          setError(message);
          setData((previous) =>
            previous !== null && !isFailure(previous) ? previous : { error: message }
          );
        }
      } finally {
        busy = false;
        completed = true;
        if (alive) setLoading(false);
      }
    };
    tickRef.current = tick;
    void tick();
    const timer = ms === null ? null : window.setInterval(tick, ms);
    const visible = () => {
      if (!document.hidden && (ms !== null || !completed)) void tick();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      alive = false;
      if (timer !== null) window.clearInterval(timer);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [ms, key]);
  return { data, loading, reload, error, updatedAt };
}
