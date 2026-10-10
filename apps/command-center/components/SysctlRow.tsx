// components/SysctlRow.tsx
import React from 'react';
import { SysctlEntry, SysctlCatalogEntry } from '../types';

interface SysctlRowProps {
  entry: SysctlEntry;
  catalogInfo?: SysctlCatalogEntry;
  pendingValue?: string;
  onChange: (key: string, value: string) => void;
}

const RISK_STYLES: Record<string, string> = {
  info: 'bg-white/10 text-white/60',
  low: 'bg-blue-500/20 text-blue-300',
  medium: 'bg-yellow-500/20 text-yellow-300',
  high: 'bg-red-500/20 text-red-300',
};

const SysctlRow: React.FC<SysctlRowProps> = ({ entry, catalogInfo, pendingValue, onChange }) => {
  const displayValue = pendingValue ?? entry.value;
  const isDirty = pendingValue !== undefined && pendingValue !== entry.value;

  return (
    <div className={`flex items-center gap-4 px-4 py-3 border-b border-white/5 ${isDirty ? 'bg-whitecube-accent/10' : ''}`}>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <code className="text-sm text-white font-mono truncate">{entry.key}</code>
          {catalogInfo && (
            <span className={`text-[10px] px-2 py-0.5 rounded-full uppercase font-bold ${RISK_STYLES[catalogInfo.risk]}`}>
              {catalogInfo.risk}
            </span>
          )}
        </div>
        {catalogInfo && <p className="text-xs text-white/40 mt-0.5">{catalogInfo.description}</p>}
        <p className="text-[10px] text-white/30 mt-0.5">
          {entry.isManaged
            ? 'Managed by Whitecube Command Center'
            : entry.sourceFile
            ? `Set by ${entry.sourceFile}`
            : 'Runtime only (no boot-persisted file)'}
        </p>
      </div>
      <input
        type="text"
        value={displayValue}
        onChange={(e) => onChange(entry.key, e.target.value)}
        className={`w-40 bg-black/20 border rounded px-2 py-1 text-sm font-mono text-white outline-none ${
          isDirty ? 'border-whitecube-accent' : 'border-white/10'
        }`}
      />
      {isDirty && <span className="text-[10px] text-white/30 font-mono">was {entry.value}</span>}
    </div>
  );
};

export default SysctlRow;
