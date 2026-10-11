import React from 'react';
import type { HostInfo } from '../lab.d';

const Header: React.FC<{ host: HostInfo | null; session: string; onPalette: () => void }> = ({ host, session, onPalette }) => (
  <header className="wc-header h-14 bg-[#090c12]/95 border-b border-white/10 flex items-center justify-between px-4 select-none shadow-md shrink-0">
    <span className="text-sm font-semibold tracking-tight text-white/90 flex items-center gap-2">
      <span className="material-symbols-outlined text-[24px] text-whitecube-accent">deployed_code</span>
      Whitecube <span className="text-white/45 font-normal hidden lg:inline">/ Command Center</span>
    </span>
    <button onClick={onPalette} className="flex items-center gap-3 px-3 py-1 rounded-md bg-white/5 border border-white/10 text-xs text-white/50 hover:text-white" aria-label="Open command palette"><span className="material-symbols-outlined text-base">search</span> Find a command or view <kbd className="text-white/45 border border-white/10 rounded px-1.5 py-0.5">Ctrl K</kbd></button>
    <div className="flex items-center gap-3 text-xs font-mono text-white/60">
      {host && <span className="hidden xl:inline">{host.user}@{host.hostname}</span>}
      <span className="flex items-center gap-2 px-3 py-1 bg-white/5 rounded-full border border-white/5" title="Display session">
        <span className={`w-2 h-2 rounded-full ${session === 'wayland' ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]' : 'bg-red-500'}`} />
        {session || 'unknown'}
      </span>
    </div>
  </header>
);

export default Header;
