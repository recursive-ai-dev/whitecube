// Real React hook checks injected only into the isolated end-to-end profile.
import React, { useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { usePoll } from '../hooks';

type State = ReturnType<typeof usePoll<number | { error: string }>>;
(window as any).__runPollingChecks = async () => {
  const results: [string, boolean][] = [];
  const container = document.createElement('div');
  container.hidden = true;
  document.body.append(container);
  const root = createRoot(container);
  let observed: State | null = null,
    hidden = false,
    calls = 0;
  const descriptor = Object.getOwnPropertyDescriptor(document, 'hidden');
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  const until = async (condition: () => boolean) => {
    const deadline = Date.now() + 2000;
    while (!condition() && Date.now() < deadline) await new Promise((r) => setTimeout(r, 10));
    if (!condition()) throw new Error('Polling fixture timed out');
  };
  function Probe({
    fn,
    interval,
    scope,
  }: {
    fn: () => Promise<number | { error: string }>;
    interval: number | null;
    scope: string;
  }) {
    const state = usePoll(fn, interval, scope);
    useEffect(() => {
      observed = state;
    });
    return null;
  }
  const render = (
    fn: () => Promise<number | { error: string }>,
    interval: number | null,
    scope: string
  ) => root.render(<Probe fn={fn} interval={interval} scope={scope} />);
  try {
    let response: number | { error: string } = 42,
      reject = false;
    const request = async () => {
      calls++;
      if (reject) throw new Error('IPC disconnected');
      return response;
    };
    render(request, null, 'one');
    await until(() => observed?.data === 42 && !observed.loading);
    const timestamp = observed!.updatedAt;
    response = { error: 'Inspection failed' };
    observed!.reload();
    await until(() => observed?.error === 'Inspection failed');
    results.push([
      'polling retains cached values and timestamp on backend failure',
      observed!.data === 42 && observed!.updatedAt === timestamp,
    ]);
    reject = true;
    observed!.reload();
    await until(() => observed?.error === 'IPC disconnected');
    results.push(['polling retains cached values on rejected IPC', observed!.data === 42]);
    reject = false;
    response = 77;
    observed!.reload();
    await until(() => observed?.data === 77 && !observed.loading);
    results.push(['polling clears the refresh error after recovery', observed!.error === null]);
    response = 0;
    observed!.reload();
    await until(() => observed?.data === 0 && !observed.loading);
    response = { error: 'Zero sample interrupted' };
    observed!.reload();
    await until(() => observed?.error === 'Zero sample interrupted');
    results.push(['a valid zero reading survives a failed refresh', observed!.data === 0]);
    response = 77;
    let resolveOld!: (value: number) => void;
    render(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
      null,
      'old'
    );
    await until(() => !!resolveOld);
    render(async () => 99, null, 'new');
    await until(() => observed?.data === 99);
    resolveOld(11);
    await new Promise((r) => setTimeout(r, 30));
    results.push([
      'late results from an abandoned scope cannot replace current data',
      observed!.data === 99,
    ]);
    hidden = true;
    calls = 0;
    render(request, null, 'hidden');
    await new Promise((r) => setTimeout(r, 30));
    results.push(['hidden one-shot panels defer inspection', calls === 0]);
    hidden = false;
    document.dispatchEvent(new Event('visibilitychange'));
    await until(() => observed?.data === 77 && !observed.loading);
    results.push(['hidden one-shot panels load when the window becomes visible', calls === 1]);
    document.dispatchEvent(new Event('visibilitychange'));
    await new Promise((r) => setTimeout(r, 30));
    results.push(['completed one-shot panels do not poll on every visibility event', calls === 1]);
    let resolveInterval!: (value: number) => void;
    render(
      () =>
        new Promise((resolve) => {
          resolveInterval = resolve;
        }),
      5000,
      'hidden'
    );
    await until(() => !!resolveInterval);
    results.push(['changing refresh frequency preserves the last snapshot', observed!.data === 77]);
    resolveInterval(88);
    await until(() => observed?.data === 88);
    hidden = true;
    document.dispatchEvent(new Event('visibilitychange'));
    calls = 0;
    render(request, 20, 'background');
    await new Promise((r) => setTimeout(r, 80));
    results.push(['recurring polling is suspended while hidden', calls === 0]);
  } finally {
    root.unmount();
    container.remove();
    if (descriptor) Object.defineProperty(document, 'hidden', descriptor);
    else delete (document as any).hidden;
    document.dispatchEvent(new Event('visibilitychange'));
  }
  return results;
};
