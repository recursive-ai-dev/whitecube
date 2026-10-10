import React, { useLayoutEffect, useRef, useState } from 'react';
import { TerminalLog } from '../types';

interface TerminalProps {
  logs: TerminalLog[];
  onCommand: (cmd: string) => void;
  onInterrupt: () => void;
  onClear: () => void;
  cwd: string;
  prompt: string;
  running: boolean;
}

const Terminal: React.FC<TerminalProps> = ({ logs, onCommand, onInterrupt, onClear, cwd, prompt, running }) => {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const [input, setInput] = useState('');
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem('cc:terminal-collapsed') === '1'; } catch { return false; } });
  const [historyIdx, setHistoryIdx] = useState<number | null>(null);
  const displayCwd = cwd || '~';
  const history = logs.filter((l) => l.type === 'command').map((l) => l.content);

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
      const next = historyIdx === null ? history.length - 1 : Math.max(0, historyIdx - 1);
      setHistoryIdx(next);
      setInput(history[next]);
    } else if (e.key === 'ArrowDown' && historyIdx !== null) {
      e.preventDefault();
      const next = historyIdx + 1;
      if (next >= history.length) { setHistoryIdx(null); setInput(''); } else { setHistoryIdx(next); setInput(history[next]); }
    }
  };

  return (
    <div className={`${collapsed ? 'h-8' : 'h-56 md:h-72'} bg-terminal-bg wc-terminal border-t border-white/10 flex flex-col shrink-0 font-mono text-sm relative`}>
      <div className="h-8 bg-[#141a24] flex items-center justify-between px-4 select-none">
        <div className="flex items-center gap-2">
          <span className="material-symbols-outlined text-sm text-white/50">terminal</span>
          <span className="text-xs text-white/70 font-medium">Terminal — bash</span>
          {running && <span className="text-[10px] text-whitecube-accent animate-pulse">running</span>}
        </div>
        <div className="flex gap-3 text-xs text-white/50">
          {running && (
            <button onClick={onInterrupt} className="hover:text-white" title="Ctrl+C">Stop</button>
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
              className="w-full bg-transparent border-none p-0 text-white focus:ring-0 placeholder:text-white/20 outline-none font-mono read-only:opacity-40"
              type="text"
              value={input}
              readOnly={running}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              autoFocus
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
