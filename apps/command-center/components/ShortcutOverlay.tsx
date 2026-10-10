import React, { useEffect } from 'react';
import { ShortcutGroup, ScriptItem } from '../types';

interface ShortcutOverlayProps {
  group: ShortcutGroup;
  scripts: ScriptItem[];
  onClose: () => void;
  onExecute: (scriptId: string) => void;
}

const ShortcutOverlay: React.FC<ShortcutOverlayProps> = ({ group, scripts, onClose, onExecute }) => {
  // Close on Escape
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const groupScripts = scripts.filter(s => group.scriptIds.includes(s.id));

  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/40 backdrop-blur-md animate-in fade-in duration-200">
      <div 
        className="absolute inset-0" 
        onClick={onClose}
      ></div>
      
      <div className="relative bg-[#10151e]/90 p-8 rounded-2xl shadow-2xl border border-white/10 max-w-4xl w-full mx-4">
        <div className="flex items-center justify-between mb-8">
           <div className="flex items-center gap-3">
              <span className="bg-whitecube-accent px-2 py-1 rounded text-white font-mono font-bold text-sm">{group.shortcut}</span>
              <h2 className="text-3xl text-white font-light">{group.name}</h2>
           </div>
           <button onClick={onClose} className="text-white/40 hover:text-white">
             <span className="material-symbols-outlined text-3xl">close</span>
           </button>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {groupScripts.map(script => (
            <button
              key={script.id}
              onClick={() => {
                 onExecute(script.id);
                 onClose();
              }}
              className="group flex flex-col items-center justify-center gap-3 p-6 bg-white/5 hover:bg-whitecube-accent hover:shadow-lg rounded-xl transition-all border border-white/5 aspect-square"
            >
              <span className="material-symbols-outlined text-4xl text-white/80 group-hover:text-white transition-colors">{script.icon}</span>
              <span className="text-white font-medium text-center text-sm">{script.name}</span>
            </button>
          ))}
          {groupScripts.length === 0 && (
            <div className="col-span-full text-center text-white/40 py-10">
              No actions configured for this group.
            </div>
          )}
        </div>
        
        <div className="mt-8 text-center text-white/30 text-xs">
          Press ESC to dismiss
        </div>
      </div>
    </div>
  );
};

export default ShortcutOverlay;
