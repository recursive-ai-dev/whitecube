// components/SysctlPanel.tsx
import React, { useEffect, useMemo, useState } from 'react';
import { SysctlEntry } from '../types';
import { SYSCTL_CATALOG } from '../sysctlCatalog';
import { fetchSysctlEntries } from '../sysctlClient';
import SysctlRow from './SysctlRow';

interface SysctlPanelProps {
  onRequestApply: (pending: { key: string; value: string }[]) => void;
  refreshKey: number;
}

const SysctlPanel: React.FC<SysctlPanelProps> = ({ onRequestApply, refreshKey }) => {
  const [entries, setEntries] = useState<SysctlEntry[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [pending, setPending] = useState<Record<string, string>>({});

  useEffect(() => {
    setPending({});
    fetchSysctlEntries()
      .then((result) => {
        setEntries(result);
        setLoadError(null);
      })
      .catch((err) => setLoadError(err.message));
  }, [refreshKey]);

  const filtered = useMemo(() => {
    if (!searchQuery) return entries;
    const q = searchQuery.toLowerCase();
    return entries.filter((e) => e.key.toLowerCase().includes(q));
  }, [entries, searchQuery]);

  const handleChange = (key: string, value: string) => {
    setPending((prev) => ({ ...prev, [key]: value }));
  };

  const pendingList = Object.entries(pending)
    .filter(([key, value]) => entries.find((e) => e.key === key)?.value !== value)
    .map(([key, value]) => ({ key, value }));

  return (
    <div className="h-full flex flex-col">
      <div className="p-4 border-b border-white/5 flex items-center gap-4">
        <input
          type="text"
          placeholder="Search sysctl keys..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="flex-1 bg-black/20 border border-white/10 rounded-full px-4 py-2 text-sm text-white outline-none"
        />
        <button
          disabled={pendingList.length === 0}
          onClick={() => onRequestApply(pendingList)}
          className="px-4 py-2 bg-whitecube-accent rounded text-sm font-bold text-white disabled:opacity-30 disabled:cursor-not-allowed"
        >
          Apply {pendingList.length > 0 ? `(${pendingList.length})` : ''}
        </button>
      </div>

      {loadError && (
        <div className="p-4 text-sm text-red-400">
          Could not reach the relay: {loadError}. Is the daemon installed and running?
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {filtered.map((entry) => (
          <SysctlRow
            key={entry.key}
            entry={entry}
            catalogInfo={SYSCTL_CATALOG[entry.key]}
            pendingValue={pending[entry.key]}
            onChange={handleChange}
          />
        ))}
      </div>
    </div>
  );
};

export default SysctlPanel;
