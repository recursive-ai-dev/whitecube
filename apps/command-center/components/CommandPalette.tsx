import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useDialog } from './useDialog';
export interface PaletteItem {
  id: string;
  label: string;
  hint: string;
  icon: string;
  run: () => void;
}
export default function CommandPalette({
  open,
  items,
  onClose,
}: {
  open: boolean;
  items: PaletteItem[];
  onClose: () => void;
}) {
  const [query, setQuery] = useState(''),
    [index, setIndex] = useState(0);
  const dialog = useDialog(open, onClose),
    list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open) {
      setQuery('');
      setIndex(0);
    }
  }, [open]);
  const results = useMemo(() => {
    const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
    return items
      .filter((i) => words.every((w) => `${i.label} ${i.hint}`.toLowerCase().includes(w)))
      .slice(0, 60);
  }, [items, query]);
  const selected = Math.max(0, Math.min(index, results.length - 1));
  useEffect(() => {
    list.current
      ?.querySelector(`[data-result="${selected}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [selected]);
  if (!open) return null;
  const choose = (item?: PaletteItem) => {
    if (item) {
      onClose();
      item.run();
    }
  };
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[12vh] bg-black/65 backdrop-blur-sm p-4"
      onMouseDown={onClose}
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        tabIndex={-1}
        className="w-[42rem] max-w-[94vw] bg-[#10131b] border border-white/15 rounded-2xl shadow-2xl overflow-hidden"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center px-5 border-b border-white/10">
          <span aria-hidden="true" className="material-symbols-outlined text-whitecube-accent">
            search
          </span>
          <input
            data-autofocus
            role="combobox"
            aria-label="Search commands and views"
            aria-expanded="true"
            aria-autocomplete="list"
            aria-controls="palette-results"
            aria-activedescendant={
              results[selected] ? `palette-${results[selected].id}` : undefined
            }
            value={query}
            placeholder="Search views, commands, or tags…"
            onChange={(e) => {
              setQuery(e.target.value);
              setIndex(0);
            }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                setIndex(Math.min(results.length - 1, selected + 1));
              } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setIndex(Math.max(0, selected - 1));
              } else if (e.key === 'Enter') {
                e.preventDefault();
                choose(results[selected]);
              }
            }}
            className="flex-1 min-w-0 bg-transparent px-4 py-5 text-sm text-white outline-none"
          />
          <button
            aria-label="Close command palette"
            onClick={onClose}
            className="text-xs text-white/50 border border-white/10 rounded px-2 py-1"
          >
            Esc
          </button>
        </div>
        <div
          ref={list}
          id="palette-results"
          role="listbox"
          aria-label="Matching commands and views"
          className="max-h-[52vh] overflow-y-auto p-2"
        >
          {!results.length && (
            <p className="p-6 text-sm text-white/50">
              No matches. Try a command name, tag, or view.
            </p>
          )}
          {results.map((r, i) => (
            <button
              role="option"
              aria-selected={i === selected}
              id={`palette-${r.id}`}
              data-result={i}
              key={r.id}
              onMouseEnter={() => setIndex(i)}
              onClick={() => choose(r)}
              className={`w-full flex items-center gap-3 p-3 rounded-lg text-left text-sm ${i === selected ? 'bg-whitecube-accent/12 text-white ring-1 ring-inset ring-whitecube-accent/20' : 'text-white/75 hover:bg-white/5'}`}
            >
              <span
                aria-hidden="true"
                className={`material-symbols-outlined text-xl ${i === selected ? 'text-red-300' : 'text-white/40'}`}
              >
                {r.icon}
              </span>
              <span className="flex-1 min-w-0">
                <span className="block truncate">{r.label}</span>
                <span className="block text-[11px] text-white/45 font-mono truncate mt-1">
                  {r.hint}
                </span>
              </span>
              {i === selected && (
                <span aria-hidden="true" className="text-white/40">
                  ↵
                </span>
              )}
            </button>
          ))}
        </div>
        <div className="flex justify-between gap-3 border-t border-white/8 px-5 py-3 text-[11px] text-white/45">
          <span>↑ ↓ Navigate · Enter Select · Esc Close</span>
          <span>{results.length} results</span>
        </div>
      </div>
    </div>
  );
}
