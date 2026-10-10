import React, { useEffect, useState } from 'react';
import type { UpgradablePkg } from '../lab.d';
import { isFailure } from '../hooks';

interface Props { onRunCommand: (cmd: string) => void }

const UpdatesPanel: React.FC<Props> = ({ onRunCommand }) => {
  const [pkgs, setPkgs] = useState<UpgradablePkg[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);

  const check = async () => {
    setPkgs(null); setError(null);
    const r = await window.lab.updates().catch(e => ({ error: String(e.message || 'Could not check updates') }));
    if (isFailure(r)) setError(r.error); else { setPkgs(r.packages); setError(r.error ?? null); setCheckedAt(new Date()); }
  };
  useEffect(() => { check(); }, []);

  const security = (pkgs ?? []).filter((p) => /security/i.test(p.source));

  return (
    <div className="h-full flex flex-col">
      <div className="p-4 border-b border-white/5 flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-48">
          <div className="text-white text-lg font-light">{error ? 'Update check unavailable' : pkgs === null ? 'Checking…' : pkgs.length === 0 ? 'Up to date' : `${pkgs.length} updates available`}</div>
          <div className="text-xs text-white/50">{security.length > 0 ? `${security.length} security · ` : ''}{checkedAt ? `checked ${checkedAt.toLocaleTimeString()} from the local package index` : ''}</div>
        </div>
        <button onClick={() => onRunCommand('sudo apt update')} className="px-4 py-2 rounded text-sm text-white bg-white/10 hover:bg-white/15">Refresh index</button>
        <button onClick={check} className="px-4 py-2 rounded text-sm text-white bg-white/10 hover:bg-white/15">Re-check</button>
        <button disabled={!pkgs?.length} onClick={() => onRunCommand('sudo apt upgrade -y')} className="px-4 py-2 rounded text-sm font-bold text-white bg-whitecube-accent hover:bg-red-600 disabled:opacity-30">Upgrade all</button>
      </div>
      {error && <div className="p-4 text-sm text-red-400">{error}</div>}
      <div className="flex-1 overflow-y-auto">
        {pkgs?.map((p) => (
          <div key={p.name} className="flex items-center gap-4 px-4 py-2 border-b border-white/5 text-sm">
            <span className="text-white w-64 truncate">{p.name}</span>
            <span className="text-white/40 flex-1 truncate tabular-nums">{p.from} → <span className="text-white/80">{p.to}</span></span>
            {/security/i.test(p.source) && <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-500/20 text-red-300">security</span>}
            <span className="text-xs text-white/40 w-40 text-right truncate">{p.source}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

export default UpdatesPanel;
