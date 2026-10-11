import React, { useState, useEffect } from 'react';
import { ScriptItem, Category } from '../types';
import { createId } from '../id';
import { useDialog } from './useDialog';

interface ConfigModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (script: Omit<ScriptItem, 'lastRun' | 'status'>) => void;
  initialData?: ScriptItem;
}

const ICONS = ['terminal', 'folder', 'folder_open', 'settings', 'code', 'bug_report', 'wifi', 'security', 'database', 'language', 'extension'];

const ConfigModal: React.FC<ConfigModalProps> = ({ isOpen, onClose, onSave, initialData }) => {
  const [formData, setFormData] = useState<Partial<ScriptItem>>({
    name: '',
    description: '',
    command: '',
    path: '~/',
    category: 'Custom',
    icon: 'terminal'
  });
  const dialogRef = useDialog(isOpen, onClose);
  const [pathNotice, setPathNotice] = useState<string | null>(null);

  useEffect(() => {
    if (initialData) {
      setFormData(initialData);
      setPathNotice(null);
    } else {
      setFormData({
        name: '',
        description: '',
        command: '',
        path: '~/',
        category: 'Custom',
        icon: 'terminal'
      });
      setPathNotice(null);
    }
  }, [initialData, isOpen]);

  if (!isOpen) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.command?.trim() || !formData.name?.trim()) {
      setPathNotice('Name and command are required before saving.');
      return;
    }
    onSave({
      id: initialData?.id || createId(),
      name: formData.name,
      description: formData.description || '',
      command: formData.command,
      path: formData.path,
      icon: formData.icon || 'terminal',
      category: formData.category as Category,
      tags: [...new Set((formData.tags || []).map(t => t.trim().slice(0, 32)).filter(Boolean))].slice(0, 12),
      pinned: formData.pinned === true,
      requiresConfirmation: formData.requiresConfirmation ?? /\b(sudo|pkexec)\b/.test(formData.command),
    });
    onClose();
  };

  const handleBrowsePath = async () => {
    setPathNotice(null);
    try {
      const result = await window.lab.chooseDirectory();
      if ('error' in result) setPathNotice(result.error);
      else if (result.path) setFormData(previous => ({ ...previous, path: result.path }));
    } catch (e) { setPathNotice((e as Error).message); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="config-title" tabIndex={-1} className="bg-[#10151e] w-full max-w-lg rounded-xl shadow-2xl border border-white/10 flex flex-col max-h-[90vh]">
        <div className="flex items-center justify-between p-4 border-b border-white/10">
          <h3 id="config-title" className="text-lg font-bold text-white">
            {initialData ? 'Edit Configuration' : 'New Command Sequence'}
          </h3>
          <button aria-label="Close command editor" onClick={onClose} className="text-white/50 hover:text-white">
            <span className="material-symbols-outlined">close</span>
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 overflow-y-auto space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label className="text-xs font-bold text-white/60 uppercase">Name</label>
              <input
                required
                data-autofocus
                aria-label="Command name"
                className="w-full bg-[#06080d] border border-white/10 rounded px-3 py-2 text-white focus:border-whitecube-accent focus:ring-1 focus:ring-whitecube-accent text-sm"
                value={formData.name || ''}
                onChange={e => setFormData({...formData, name: e.target.value})}
                placeholder="e.g. Update System"
              />
            </div>
            <div className="space-y-1.5">
               <label htmlFor="command-category" className="text-xs font-bold text-white/60 uppercase">Category</label>
               <select id="command-category"
                 className="w-full bg-[#06080d] border border-white/10 rounded px-3 py-2 text-white focus:border-whitecube-accent focus:ring-1 focus:ring-whitecube-accent text-sm"
                 value={formData.category}
                 onChange={e => setFormData({...formData, category: e.target.value as Category})}
               >
                 <option value="System">System</option>
                 <option value="Files">Files</option>
                 <option value="Network">Network</option>
                 <option value="Development">Development</option>
                 <option value="Custom">Custom</option>
               </select>
            </div>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="command-description" className="text-xs font-bold text-white/60 uppercase">Description</label>
            <textarea id="command-description"
              className="w-full bg-[#06080d] border border-white/10 rounded px-3 py-2 text-white focus:border-whitecube-accent focus:ring-1 focus:ring-whitecube-accent text-sm h-20 resize-none"
              value={formData.description || ''}
              onChange={e => setFormData({...formData, description: e.target.value})}
              placeholder="Describe what this script does..."
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="command-executable" className="text-xs font-bold text-white/60 uppercase">Command / Executable</label>
            <div className="relative">
              <span className="absolute left-3 top-2 text-white/30 text-sm font-mono">$</span>
              <input id="command-executable"
                required
                className="w-full bg-[#06080d] border border-white/10 rounded pl-7 pr-3 py-2 text-white font-mono text-sm focus:border-whitecube-accent focus:ring-1 focus:ring-whitecube-accent"
                value={formData.command || ''}
                onChange={e => setFormData({...formData, command: e.target.value})}
                placeholder="sudo apt update"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="command-path" className="text-xs font-bold text-white/60 uppercase">Working Path</label>
            <div className="flex gap-2">
                <input id="command-path"
                  className="flex-1 bg-[#06080d] border border-white/10 rounded px-3 py-2 text-white font-mono text-sm focus:border-whitecube-accent focus:ring-1 focus:ring-whitecube-accent"
                  value={formData.path || ''}
                  onChange={e => setFormData({...formData, path: e.target.value})}
                  placeholder="/home/user/scripts"
                />
                <button
                    type="button"
                    onClick={handleBrowsePath}
                    className="px-3 bg-[#1b2230] border border-white/10 rounded hover:bg-white/10 text-white/70 hover:text-whitecube-accent transition-colors"
                    aria-label="Browse working directory"
                    title="Browse Directory"
                >
                    <span className="material-symbols-outlined text-xl">folder_open</span>
                </button>
            </div>
            {pathNotice && (
              <p className="text-xs text-white/50">{pathNotice}</p>
            )}
          </div>

          <div className="space-y-1.5"><label htmlFor="command-tags" className="text-xs font-bold text-white/60 uppercase">Tags</label><input id="command-tags" value={(formData.tags || []).join(',')} onChange={e => setFormData({ ...formData, tags: e.target.value.split(',') })} maxLength={395} placeholder="maintenance, network, daily" className="w-full bg-[#06080d] border border-white/10 rounded px-3 py-2 text-white text-sm" /><p className="text-xs text-white/40">Comma separated · up to 12 tags, 32 characters each</p></div>
          <label className="flex items-center gap-2 text-sm text-white/70"><input type="checkbox" checked={!!formData.pinned} onChange={e => setFormData({ ...formData, pinned: e.target.checked })} />Pin to overview</label>
          <label className="flex items-center gap-2 text-sm text-white/70"><input type="checkbox" checked={formData.requiresConfirmation ?? /\b(sudo|pkexec)\b/.test(formData.command || '')} onChange={e => setFormData({ ...formData, requiresConfirmation: e.target.checked })} />Confirm before running</label>
          <div className="space-y-1.5">
            <label className="text-xs font-bold text-white/60 uppercase">Icon</label>
            <div className="flex gap-2 flex-wrap bg-[#06080d] p-2 rounded border border-white/10">
              {ICONS.map(icon => (
                <button
                  type="button"
                  key={icon}
                  aria-label={`Use ${icon} icon`}
                  aria-pressed={formData.icon === icon}
                  onClick={() => setFormData({...formData, icon})}
                  className={`p-1.5 rounded hover:bg-white/10 transition-colors ${formData.icon === icon ? 'bg-whitecube-accent text-white' : 'text-white/50'}`}
                >
                  <span className="material-symbols-outlined text-xl">{icon}</span>
                </button>
              ))}
            </div>
          </div>
        </form>

        <div className="p-4 border-t border-white/10 flex justify-end gap-3 bg-[#090c12] rounded-b-xl">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-sm text-white/70 hover:text-white transition-colors font-medium"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            className="px-6 py-2 bg-whitecube-accent hover:bg-red-600 text-white text-sm font-bold rounded shadow-md transition-all transform active:scale-95"
          >
            Save Configuration
          </button>
        </div>
      </div>
    </div>
  );
};

export default ConfigModal;
