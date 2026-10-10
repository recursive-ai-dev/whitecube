import React from 'react';
import { ScriptItem, ScriptStatus } from '../types';
import { STATUS_LABELS } from '../constants';

interface ScriptCardProps {
  script: ScriptItem;
  onRun: (id: string) => void;
  onEdit: (script: ScriptItem) => void;
  onDelete: (id: string) => void;
  onToggleConfirmation: (id: string, value: boolean) => void;
}

const ScriptCard: React.FC<ScriptCardProps> = ({
  script,
  onRun,
  onEdit,
  onDelete,
  onToggleConfirmation
}) => {
  const isRunning = script.status === ScriptStatus.RUNNING;
  const statusLabel = STATUS_LABELS[script.status];
  const statusClass = script.status === ScriptStatus.SUCCESS
    ? 'text-green-400'
    : script.status === ScriptStatus.ERROR
      ? 'text-red-400'
      : script.status === ScriptStatus.RUNNING
        ? 'text-whitecube-accent'
        : 'text-white/60';

  return (
    <div className="group bg-[#1b2230] hover:bg-[#141a24] border border-white/5 rounded-lg p-4 transition-all duration-200 shadow-sm hover:shadow-elevation flex flex-col h-full relative overflow-hidden">
      {/* Active Indicator Strip */}
      {isRunning && (
        <div className="absolute left-0 top-0 bottom-0 w-1 bg-whitecube-accent animate-pulse"></div>
      )}

      <div className="flex items-start justify-between mb-3">
        <div className="p-2 bg-white/5 rounded-md text-whitecube-accent group-hover:bg-whitecube-accent group-hover:text-white transition-colors duration-300">
          <span className="material-symbols-outlined text-2xl">
            {script.icon || 'terminal'}
          </span>
        </div>
        <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={(e) => { e.stopPropagation(); onToggleConfirmation(script.id, !script.requiresConfirmation); }}
            className={`p-1.5 rounded-md transition-colors ${script.requiresConfirmation ? 'text-whitecube-accent bg-whitecube-accent/10' : 'text-white/60 hover:text-white hover:bg-white/10'}`}
            title={script.requiresConfirmation ? "Confirmation Required" : "No Confirmation Required"}
          >
            <span className="material-symbols-outlined text-lg">
                {script.requiresConfirmation ? 'lock' : 'lock_open_right'}
            </span>
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onEdit(script); }}
            className="p-1.5 hover:bg-white/10 rounded-md text-white/60 hover:text-white transition-colors"
            title="Configure"
          >
            <span className="material-symbols-outlined text-lg">tune</span>
          </button>
          <button
             onClick={(e) => { e.stopPropagation(); onDelete(script.id); }}
             className="p-1.5 hover:bg-red-500/20 rounded-md text-white/60 hover:text-red-400 transition-colors"
             title="Remove"
          >
            <span className="material-symbols-outlined text-lg">delete</span>
          </button>
        </div>
      </div>

      <div className="mb-4 flex-1">
        <h3 className="font-medium text-white text-base mb-1 truncate" title={script.name}>{script.name}</h3>
        <p className="text-sm text-white/50 line-clamp-2 min-h-[2.5em] leading-relaxed">
          {script.description || 'No description provided.'}
        </p>
      </div>

      {script.path && (
         <div className="mb-3 flex items-center gap-1.5 text-xs text-white/40 font-mono truncate">
            <span className="material-symbols-outlined text-[14px]">folder</span>
            <span className="truncate">{script.path}</span>
         </div>
      )}

      <div className="flex items-center justify-between pt-3 border-t border-white/5 mt-auto">
        <div className="flex flex-col">
           <span className="text-[10px] text-white/40 font-medium uppercase tracking-wider">Last Run</span>
           <span className="text-xs text-white/70">{script.lastRun}</span>
           <span className={`text-[10px] uppercase tracking-wide ${statusClass}`}>{statusLabel}</span>
        </div>
        <button
          onClick={() => onRun(script.id)}
          disabled={isRunning}
          className={`px-4 py-1.5 rounded text-xs font-bold uppercase tracking-wide transition-all ${
            isRunning
              ? 'bg-white/10 text-white/50 cursor-not-allowed'
              : 'bg-white/10 hover:bg-whitecube-accent text-white'
          }`}
        >
          {isRunning ? 'Running...' : 'Execute'}
        </button>
      </div>
    </div>
  );
};

export default ScriptCard;
