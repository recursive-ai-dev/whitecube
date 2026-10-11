import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { TerminalLog } from '../types';

interface TerminalProps {
  logs: TerminalLog[];
  onCommand: (cmd: string) => void;
  onInterrupt: () => void;
  onClear: () => void;
  cwd: string;
  prompt: string;
  running: boolean;
  stopping: boolean;
}

const Terminal: React.FC<TerminalProps> = ({ logs, onCommand, onInterrupt, onClear, cwd, prompt, running, stopping }) => {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const [input, setInput] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef(0);
  const draft = useRef('');
  const [collapsed, setCollapsed] = useState(() => { try { const saved = localStorage.getItem('cc:terminal-collapsed'); return saved === null || saved === '1'; } catch { return true; } });
  const [historyIdx, setHistoryIdx] = useState<number | null>(null);
  const displayCwd = cwd || '~';
  const history = logs.filter((l) => l.type === 'command').map((l) => l.content);

  useEffect(() => {
    const focus = () => { setCollapsed(false); requestAnimationFrame(() => document.getElementById('cc-term-input')?.focus()); };
    window.addEventListener('cc:focus-terminal', focus);
    return () => window.removeEventListener('cc:focus-terminal', focus);
  }, []);
  useEffect(() => {
    if (!running) return;
    setCollapsed(false); startedAt.current = Date.now(); setElapsed(0);
    const timer = window.setInterval(() => setElapsed((Date.now() - startedAt.current) / 1000), 250);
    return () => window.clearInterval(timer);
  }, [running]);

  // Follow new output only while the user is parked at the bottom.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [logs]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop <= el.clientHeight + 50;
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || running) return;
    stickRef.current = true;
    onCommand(input);
    setInput('');
    setHistoryIdx(null);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.ctrlKey && e.key.toLowerCase() === 'c' && running) { e.preventDefault(); onInterrupt(); return; }
    if (e.ctrlKey && e.key.toLowerCase() === 'l') { e.preventDefault(); onClear(); return; }
    if (e.key === 'ArrowUp' && history.length) {
      e.preventDefault();
      if (historyIdx === null) draft.current = input;
      const next = historyIdx === null ? history.length - 1 : Math.max(0, historyIdx - 1);
      setHistoryIdx(next);
      setInput(history[next]);
    } else if (e.key === 'ArrowDown' && historyIdx !== null) {
      e.preventDefault();
      const next = historyIdx + 1;
      if (next >= history.length) { setHistoryIdx(null); setInput(draft.current); } else { setHistoryIdx(next); setInput(history[next]); }
    }
  };

  return (
    <div className={`${collapsed ? 'h-10' : 'h-48 md:h-60'} bg-terminal-bg wc-terminal border-t border-white/10 flex flex-col shrink-0 font-mono text-sm relative`}>
      <div className="h-10 bg-[#0f131b] flex items-center justify-between px-4 select-none">
        <div className="flex items-center gap-2">
          <span className="material-symbols-outlined text-sm text-white/50">terminal</span>
          <span className="text-xs text-white/70 font-medium">Terminal — bash</span>
          {running && <span className="text-[10px] text-whitecube-accent animate-pulse">{stopping ? 'Stopping…' : `running · ${elapsed.toFixed(1)}s`}</span>}
        </div>
        <div className="flex gap-3 text-xs text-white/50">
          {running && (
            <button disabled={stopping} onClick={onInterrupt} className="hover:text-white" title="Ctrl+C">{stopping ? 'Stopping…' : 'Stop'}</button>
          )}
          <button onClick={onClear} className="hover:text-white" title="Ctrl+L">Clear</button>
          <button onClick={() => setCollapsed((c) => { try { localStorage.setItem('cc:terminal-collapsed', c ? '0' : '1'); } catch { /* optional preference */ } return !c; })} className="hover:text-white flex items-center" aria-label={collapsed ? 'Expand terminal' : 'Collapse terminal'}>
            <span className="material-symbols-outlined text-[16px]">{collapsed ? 'expand_less' : 'expand_more'}</span>
          </button>
        </div>
      </div>

      <div ref={scrollRef} hidden={collapsed} onScroll={onScroll} onClick={() => document.getElementById('cc-term-input')?.focus()} className="flex-1 p-4 overflow-y-auto text-white/90 space-y-0.5">
        {logs.map((log) => (
          <div key={log.id} className="break-words whitespace-pre-wrap leading-tight">
            {log.type === 'command' ? (
              <div className="mt-2 text-white font-bold">
                <span className="text-green-400">{prompt}</span>
                <span className="text-white">:</span>
                <span className="text-blue-400">{log.cwd || displayCwd}</span>
                <span className="text-white">$ {log.content}</span>
              </div>
            ) : (
              <div className={`${log.type === 'info' ? 'text-gray-300' : ''} ${log.type === 'success' ? 'text-green-400' : ''} ${log.type === 'error' ? 'text-red-400' : ''}`}>
                {log.content}
              </div>
            )}
          </div>
        ))}

        <div className="flex items-center mt-2">
          <span className="text-green-400 mr-0.5">{prompt}</span>
          <span className="text-white mr-0.5">:</span>
          <span className="text-blue-400 mr-2">{displayCwd}</span>
          <span className="text-white mr-2">$</span>
          <form onSubmit={handleSubmit} className="flex-1">
            <input
              id="cc-term-input"
              aria-label="Terminal command"
              placeholder={running ? 'Command running…' : 'Type a command · help for built-ins'}
              className="w-full bg-transparent border-none p-0 text-white focus:ring-0 placeholder:text-white/20 outline-none font-mono read-only:opacity-40"
              type="text"
              value={input}
              readOnly={running}
              onChange={(e) => { setInput(e.target.value); setHistoryIdx(null); }}
              onKeyDown={onKeyDown}
              spellCheck={false}
              autoComplete="off"
            />
          </form>
        </div>
      </div>
    </div>
  );
};

export default Terminal;
