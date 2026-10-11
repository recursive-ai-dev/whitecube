import React from 'react';
import { ScriptItem, ScriptStatus } from '../types';
import { STATUS_LABELS } from '../constants';
interface Props {
  script: ScriptItem;
  onRun: (id: string) => void;
  onEdit: (script: ScriptItem) => void;
  onDelete: (id: string) => void;
  onToggleConfirmation: (id: string, value: boolean) => void;
  onPin: (id: string) => void;
  busy: boolean;
}
export default function ScriptCard({
  script,
  onRun,
  onEdit,
  onDelete,
  onToggleConfirmation,
  onPin,
  busy,
}: Props) {
  const running = script.status === ScriptStatus.RUNNING;
  const last = Date.parse(script.lastRun);
  return (
    <article
      className={`wc-command-card glass-panel rounded-xl p-4 flex flex-col h-full ${running ? 'border-red-400/40' : ''}`}
    >
      <div className="flex items-center justify-between gap-2 mb-4">
        <span
          aria-hidden="true"
          className="material-symbols-outlined text-whitecube-accent text-2xl bg-whitecube-accent/8 p-2 rounded-lg"
        >
          {script.icon || 'terminal'}
        </span>
        <div className="flex gap-1">
          <button
            aria-label={`${script.pinned ? 'Unpin' : 'Pin'} ${script.name}`}
            aria-pressed={!!script.pinned}
            title={script.pinned ? 'Unpin command' : 'Pin to overview'}
            onClick={() => onPin(script.id)}
            className={`wc-icon-button ${script.pinned ? 'text-red-300 bg-red-400/10' : ''}`}
          >
            <span aria-hidden="true" className="material-symbols-outlined text-lg">
              push_pin
            </span>
          </button>
          <button
            aria-label={`Edit ${script.name}`}
            title="Edit command"
            className="wc-icon-button"
            onClick={() => onEdit(script)}
          >
            <span aria-hidden="true" className="material-symbols-outlined text-lg">
              edit
            </span>
          </button>
          <button
            aria-label={`Remove ${script.name}`}
            title="Remove command"
            className="wc-icon-button"
            disabled={running}
            onClick={() => onDelete(script.id)}
          >
            <span aria-hidden="true" className="material-symbols-outlined text-lg">
              delete
            </span>
          </button>
        </div>
      </div>
      <h3 className="font-medium text-white truncate" title={script.name}>
        {script.name}
      </h3>
      <p className="text-xs text-white/50 leading-relaxed mt-2 mb-3 line-clamp-2 min-h-9">
        {script.description || 'No description provided.'}
      </p>
      <code
        className="font-mono text-[11px] text-white/65 bg-black/20 border border-white/5 rounded-md px-2.5 py-2 mb-3 line-clamp-2 break-all"
        title={script.command}
      >
        {script.command}
      </code>
      <div className="flex flex-wrap gap-1.5 text-[10px] mb-3">
        <span className="rounded px-2 py-1 bg-white/5 text-white/50">{script.category}</span>
        {(script.tags || []).map((tag) => (
          <span key={tag} className="rounded px-2 py-1 bg-white/5 text-white/60">
            #{tag}
          </span>
        ))}
      </div>
      {script.path && (
        <p className="text-[11px] font-mono text-white/40 truncate mb-3" title={script.path}>
          {script.path}
        </p>
      )}
      <div className="flex justify-between gap-3 items-center border-t border-white/8 pt-3 mt-auto">
        <div className="min-w-0">
          <span
            className={`block text-[10px] ${script.status === ScriptStatus.SUCCESS ? 'text-emerald-300' : script.status === ScriptStatus.ERROR ? 'text-red-300' : 'text-white/60'}`}
          >
            {STATUS_LABELS[script.status]}
          </span>
          <time className="text-[10px] text-white/40" title={script.lastRun}>
            {Number.isNaN(last)
              ? 'Not run yet'
              : new Date(last).toLocaleString(undefined, {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
          </time>
        </div>
        <button
          onClick={() => onRun(script.id)}
          disabled={busy}
          aria-label={`Execute ${script.name}`}
          className="px-3 py-2 rounded-lg text-xs font-medium bg-white/8 hover:bg-whitecube-accent text-white disabled:opacity-35 disabled:cursor-not-allowed"
        >
          {running ? 'Running…' : 'Execute'}
        </button>
      </div>
      <label className="flex items-center gap-2 text-[11px] text-white/50 mt-3">
        <input
          type="checkbox"
          checked={!!script.requiresConfirmation}
          onChange={(e) => onToggleConfirmation(script.id, e.target.checked)}
        />
        Confirm before running
      </label>
    </article>
  );
}
