import React, { useState } from 'react';
import { NautilusAction } from '../types';

interface NautilusEditorProps {
  actions: NautilusAction[];
  onAdd: (action: Omit<NautilusAction, 'id'>) => void;
  onDelete: (id: string) => void;
  onEdit: (action: NautilusAction) => void;
}

const NautilusEditor: React.FC<NautilusEditorProps> = ({ actions, onAdd, onDelete, onEdit }) => {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formData, setFormData] = useState<Partial<NautilusAction>>({});

  const startEdit = (action: NautilusAction) => {
    setEditingId(action.id);
    setFormData(action);
  };

  const startNew = () => {
    setEditingId('new');
    setFormData({ name: '', command: '', context: 'Selection', fileTypes: '*' });
  };

  const cancelEdit = () => {
    setEditingId(null);
    setFormData({});
  };

  const save = () => {
    if (editingId === 'new') {
      onAdd(formData as Omit<NautilusAction, 'id'>);
    } else if (editingId) {
      onEdit({ ...formData, id: editingId } as NautilusAction);
    }
    cancelEdit();
  };

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-light text-white mb-1">Nautilus Context Menus</h2>
          <p className="text-white/50 text-sm">Save file manager action definitions with your command library. These definitions are not installed into Nautilus.</p>
        </div>
        <button 
          onClick={startNew}
          className="flex items-center gap-2 px-4 py-2 bg-[#141a24] hover:bg-[#252d3b] border border-white/10 rounded shadow-md transition-all text-sm font-bold text-white"
        >
          <span className="material-symbols-outlined text-[20px]">add</span>
          Add Action
        </button>
      </div>

      <div className="bg-[#1b2230] border border-white/5 rounded-lg overflow-hidden">
        <table className="w-full text-left text-sm">
          <thead className="bg-[#090c12] text-white/40 uppercase font-bold text-xs">
            <tr>
              <th className="px-6 py-4">Label</th>
              <th className="px-6 py-4">Command</th>
              <th className="px-6 py-4">Context</th>
              <th className="px-6 py-4">File Types</th>
              <th className="px-6 py-4 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/5">
            {editingId === 'new' && (
              <tr className="bg-whitecube-accent/10">
                <td className="px-6 py-4">
                  <input className="bg-black/20 border border-white/10 rounded px-2 py-1 text-white w-full" placeholder="Label" value={formData.name || ''} onChange={e => setFormData({...formData, name: e.target.value})} autoFocus />
                </td>
                <td className="px-6 py-4">
                  <input className="bg-black/20 border border-white/10 rounded px-2 py-1 text-white w-full font-mono text-xs" placeholder="Command" value={formData.command || ''} onChange={e => setFormData({...formData, command: e.target.value})} />
                </td>
                <td className="px-6 py-4">
                   <select className="bg-black/20 border border-white/10 rounded px-2 py-1 text-white w-full" value={formData.context} onChange={e => setFormData({...formData, context: e.target.value})}>
                     <option>Selection</option>
                     <option>Directory</option>
                     <option>Background</option>
                   </select>
                </td>
                <td className="px-6 py-4">
                  <input className="bg-black/20 border border-white/10 rounded px-2 py-1 text-white w-full text-xs" placeholder="*" value={formData.fileTypes || ''} onChange={e => setFormData({...formData, fileTypes: e.target.value})} />
                </td>
                <td className="px-6 py-4 text-right">
                  <div className="flex items-center justify-end gap-2">
                    <button onClick={save} className="text-green-400 hover:text-green-300"><span className="material-symbols-outlined">check</span></button>
                    <button onClick={cancelEdit} className="text-white/40 hover:text-white"><span className="material-symbols-outlined">close</span></button>
                  </div>
                </td>
              </tr>
            )}
            {actions.map(action => (
              <tr key={action.id} className="group hover:bg-white/5 transition-colors">
                 {editingId === action.id ? (
                   <>
                    <td className="px-6 py-4">
                      <input className="bg-black/20 border border-white/10 rounded px-2 py-1 text-white w-full" value={formData.name} onChange={e => setFormData({...formData, name: e.target.value})} />
                    </td>
                    <td className="px-6 py-4">
                      <input className="bg-black/20 border border-white/10 rounded px-2 py-1 text-white w-full font-mono text-xs" value={formData.command} onChange={e => setFormData({...formData, command: e.target.value})} />
                    </td>
                    <td className="px-6 py-4">
                       <select className="bg-black/20 border border-white/10 rounded px-2 py-1 text-white w-full" value={formData.context} onChange={e => setFormData({...formData, context: e.target.value})}>
                         <option>Selection</option>
                         <option>Directory</option>
                         <option>Background</option>
                       </select>
                    </td>
                    <td className="px-6 py-4">
                      <input className="bg-black/20 border border-white/10 rounded px-2 py-1 text-white w-full text-xs" value={formData.fileTypes} onChange={e => setFormData({...formData, fileTypes: e.target.value})} />
                    </td>
                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button onClick={save} className="text-green-400 hover:text-green-300"><span className="material-symbols-outlined">check</span></button>
                        <button onClick={cancelEdit} className="text-white/40 hover:text-white"><span className="material-symbols-outlined">close</span></button>
                      </div>
                    </td>
                   </>
                 ) : (
                   <>
                    <td className="px-6 py-4 font-medium text-white">{action.name}</td>
                    <td className="px-6 py-4 font-mono text-white/70 text-xs">{action.command}</td>
                    <td className="px-6 py-4 text-white/70"><span className="bg-white/10 px-2 py-1 rounded text-xs">{action.context}</span></td>
                    <td className="px-6 py-4 text-white/70 text-xs">{action.fileTypes}</td>
                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-2 opacity-70 group-hover:opacity-100 transition-opacity">
                        <button onClick={() => startEdit(action)} className="text-white/50 hover:text-white transition-colors"><span className="material-symbols-outlined text-lg">edit</span></button>
                        <button onClick={() => onDelete(action.id)} className="text-white/50 hover:text-red-400 transition-colors"><span className="material-symbols-outlined text-lg">delete</span></button>
                      </div>
                    </td>
                   </>
                 )}
              </tr>
            ))}
          </tbody>
        </table>
        {actions.length === 0 && editingId !== 'new' && (
          <div className="p-8 text-center text-white/30">
            No context menu actions configured.
          </div>
        )}
      </div>
    </div>
  );
};

export default NautilusEditor;
