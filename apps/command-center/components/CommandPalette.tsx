import React, { useEffect, useMemo, useRef, useState } from 'react';

export interface PaletteItem { id: string; label: string; hint: string; icon: string; run: () => void }

const CommandPalette: React.FC<{ open: boolean; items: PaletteItem[]; onClose: () => void }> = ({ open, items, onClose }) => {
  const [query, setQuery] = useState('');
  const [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => { if (open) { setQuery(''); setIndex(0); requestAnimationFrame(() => input.current?.focus()); } }, [open]);

  const results = useMemo(() => {
    const q = query.toLowerCase().trim();
    if (!q) return items.slice(0, 12);
    const words = q.split(/\s+/);
    return items.filter((i) => words.every((w) => `${i.label} ${i.hint}`.toLowerCase().includes(w))).slice(0, 12);
  }, [items, query]);

  if (!open) return null;

  const choose = (item?: PaletteItem) => { if (item) { onClose(); item.run(); } };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-24 bg-black/50" onMouseDown={onClose}>
      <div role="dialog" aria-label="Command palette" className="w-[36rem] max-w-[92vw] bg-[#090c12] border border-white/10 rounded-xl shadow-2xl overflow-hidden" onMouseDown={(e) => e.stopPropagation()}>
        <input ref={input} value={query} placeholder="Go to a view or run a saved command…" onChange={(e) => { setQuery(e.target.value); setIndex(0); }}
          onKeyDown={(e) => {
            if (e.key === 'Escape') onClose();
            else if (e.key === 'ArrowDown') { e.preventDefault(); setIndex((i) => Math.min(results.length - 1, i + 1)); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setIndex((i) => Math.max(0, i - 1)); }
            else if (e.key === 'Enter') choose(results[index]);
          }}
          className="w-full bg-transparent px-4 py-3 text-white outline-none border-b border-white/10" />
        <div className="max-h-80 overflow-y-auto">
          {results.length === 0 && <div className="px-4 py-3 text-sm text-white/40">No matches.</div>}
          {results.map((r, i) => (
            <button key={r.id} onMouseEnter={() => setIndex(i)} onClick={() => choose(r)}
              className={`w-full flex items-center gap-3 px-4 py-2 text-left text-sm ${i === index ? 'bg-whitecube-accent text-white' : 'text-white/80'}`}>
              <span className="material-symbols-outlined text-[18px]">{r.icon}</span>
              <span className="flex-1 truncate">{r.label}</span>
              <span className={`text-xs truncate max-w-[14rem] ${i === index ? 'text-white/80' : 'text-white/40'}`}>{r.hint}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
};

export default CommandPalette;
