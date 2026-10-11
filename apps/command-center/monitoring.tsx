import React, { createContext, useContext, useEffect, useState } from 'react';
import { isFailure, usePoll } from './hooks';
import type { Snapshot } from './lab.d';

const INTERVALS = [2000, 5000, 10000];
export const monitoringIntervals = INTERVALS;
const initialInterval = () => {
  try {
    const n = Number(localStorage.getItem('cc:refresh-ms'));
    return INTERVALS.includes(n) ? n : 2000;
  } catch {
    return 2000;
  }
};

function useMonitoringState() {
  const [interval, setIntervalState] = useState(initialInterval);
  const [paused, setPaused] = useState(false);
  const poll = usePoll(() => window.lab.snapshot(), paused ? null : interval);
  const snapshot = poll.data && !isFailure(poll.data) ? poll.data : null;
  const [samples, setSamples] = useState<(Snapshot & { sampleInterval: number })[]>([]);
  useEffect(() => {
    if (!snapshot) return;
    setSamples((previous) =>
      previous.at(-1)?.at === snapshot.at
        ? previous
        : [
            ...previous.filter((s) => s.at >= snapshot.at - 600000),
            { ...snapshot, sampleInterval: interval },
          ].slice(-301)
    );
  }, [snapshot, interval]);
  const setInterval = (value: number) => {
    if (!INTERVALS.includes(value)) return;
    setIntervalState(value);
    try {
      localStorage.setItem('cc:refresh-ms', String(value));
    } catch {
      /* optional preference */
    }
  };
  return { ...poll, snapshot, samples, interval, setInterval, paused, setPaused };
}
const MonitoringContext = createContext<ReturnType<typeof useMonitoringState> | null>(null);
export function MonitoringProvider({ children }: { children: React.ReactNode }) {
  const value = useMonitoringState();
  return <MonitoringContext.Provider value={value}>{children}</MonitoringContext.Provider>;
}
export function useMonitoring() {
  const value = useContext(MonitoringContext);
  if (!value) throw new Error('Monitoring provider is missing');
  return value;
}
