import React, { useEffect, useRef, useState } from 'react';

export interface BootStage { id: string; label: string; run: () => Promise<string> }
type Status = 'wait' | 'run' | 'ok' | 'fail' | 'skip';

const MARK: Record<Status, string> = { wait: '·', run: '›', ok: '✓', fail: '✗', skip: '–' };
const COLOR: Record<Status, string> = { wait: 'text-white/25', run: 'text-[#ff7a7a]', ok: 'text-[#ff3b3b]', fail: 'text-yellow-300', skip: 'text-white/30' };

// Runs stages in order. A failing stage is reported and the chain continues: the app must come up either way.
const BootScreen: React.FC<{ stages: BootStage[]; onDone: () => void }> = ({ stages, onDone }) => {
  const [state, setState] = useState<{ status: Status; detail: string }[]>(() => stages.map(() => ({ status: 'wait', detail: '' })));
  const [leaving, setLeaving] = useState(false);
  const skipped = useRef(false);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    let alive = true;
    const set = (i: number, status: Status, detail = '') => alive && setState((s) => s.map((x, j) => (j === i ? { status, detail } : x)));
    (async () => {
      for (let i = 0; i < stages.length; i++) {
        if (skipped.current || !alive) break;
        set(i, 'run');
        try {
          const detail = await stages[i].run();
          set(i, detail.startsWith('skipped') ? 'skip' : 'ok', detail);
        } catch (e) {
          set(i, 'fail', (e as Error).message);
        }
      }
      if (!alive) return;
      setLeaving(true);
      window.setTimeout(() => alive && doneRef.current(), 350);
    })();
    return () => { alive = false; };
  }, [stages]);

  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') { skipped.current = true; doneRef.current(); } };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, []);

  return (
    <div role="status" aria-label="Starting Whitecube Command Center" className={`fixed inset-0 z-[60] bg-[#04070d] flex items-center justify-center transition-opacity duration-300 ${leaving ? 'opacity-0' : 'opacity-100'}`}>
      <div className="w-[34rem] max-w-[90vw] font-mono text-sm">
        <div className="flex items-center gap-3 mb-6">
          <svg width="36" height="36" viewBox="0 0 36 36" aria-hidden="true">
            <path d="M18 3 32 10.5v15L18 33 4 25.5v-15z" fill="none" stroke="#ff3b3b" strokeWidth="1.5" />
            <path d="M18 3v15m0 0 14-7.5M18 18 4 10.5M18 18v15" fill="none" stroke="#ff3b3b" strokeWidth="1" opacity=".6" />
          </svg>
          <div><div className="text-[#ff3b3b] text-lg tracking-widest">WHITECUBE</div><div className="text-white/30 text-xs">command center · boot chain</div></div>
        </div>
        <ul className="space-y-1.5">
          {stages.map((s, i) => (
            <li key={s.id} className="flex gap-3">
              <span className={`w-4 text-center ${COLOR[state[i].status]}`}>{MARK[state[i].status]}</span>
              <span className={`w-56 shrink-0 ${state[i].status === 'wait' ? 'text-white/30' : 'text-[#ff7a7a]'}`}>{s.label}</span>
              <span className="text-white/40 truncate">{state[i].detail}</span>
            </li>
          ))}
        </ul>
        <div className="mt-6 text-xs text-white/25">Esc to skip</div>
      </div>
    </div>
  );
};

export default BootScreen;
