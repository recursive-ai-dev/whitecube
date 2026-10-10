import React from 'react';
import type { HostInfo } from '../lab.d';

const Header: React.FC<{ host: HostInfo | null; session: string; onPalette: () => void }> = ({ host, session, onPalette }) => (
  <header className="h-12 bg-[#06080d]/85 border-b border-white/10 flex items-center justify-between px-4 select-none shadow-md shrink-0">
    <span className="text-sm font-bold tracking-wide text-whitecube-accent flex items-center gap-2">
      <span className="material-symbols-outlined text-[20px]">deployed_code</span>
      Whitecube Command Center
    </span>
    <button onClick={onPalette} className="flex items-center gap-3 px-3 py-1 rounded-md bg-white/5 border border-white/10 text-xs text-white/50 hover:text-white" aria-label="Open command palette"><span className="material-symbols-outlined text-base">search</span> Find a command or view <kbd className="text-white/30">Ctrl K</kbd></button>
    <div className="flex items-center gap-3 text-xs font-mono text-white/70">
      {host && <span>{host.user}@{host.hostname}</span>}
      <span className="flex items-center gap-2 px-3 py-1 bg-white/5 rounded-full border border-white/5" title="Display session">
        <span className={`w-2 h-2 rounded-full ${session === 'wayland' ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]' : 'bg-red-500'}`} />
        {session || 'unknown'}
      </span>
    </div>
  </header>
);

export default Header;
