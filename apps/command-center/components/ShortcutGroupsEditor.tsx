import React, { useState } from 'react';
import { ShortcutGroup, ScriptItem } from '../types';

interface ShortcutGroupsEditorProps {
  groups: ShortcutGroup[];
  availableScripts: ScriptItem[];
  onAddGroup: (group: Omit<ShortcutGroup, 'id'>) => void;
  onUpdateGroup: (group: ShortcutGroup) => void;
  onDeleteGroup: (id: string) => void;
}

const ShortcutGroupsEditor: React.FC<ShortcutGroupsEditorProps> = ({
  groups,
  availableScripts,
  onAddGroup,
  onUpdateGroup,
  onDeleteGroup
}) => {
  const [editingGroup, setEditingGroup] = useState<Partial<ShortcutGroup> | null>(null);

  const handleSave = () => {
    if (!editingGroup?.name || !editingGroup.shortcut) return;

    if (editingGroup.id) {
      onUpdateGroup(editingGroup as ShortcutGroup);
    } else {
      onAddGroup({
        name: editingGroup.name,
        shortcut: editingGroup.shortcut,
        scriptIds: editingGroup.scriptIds || []
      });
    }
    setEditingGroup(null);
  };

  const toggleScriptInGroup = (scriptId: string) => {
    if (!editingGroup) return;
    const currentIds = editingGroup.scriptIds || [];
    const newIds = currentIds.includes(scriptId)
      ? currentIds.filter(id => id !== scriptId)
      : [...currentIds, scriptId];
    setEditingGroup({ ...editingGroup, scriptIds: newIds });
  };

  return (
    <div className="p-6 h-full flex flex-col">
       <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-light text-white mb-1">Shortcut Groups</h2>
          <p className="text-white/50 text-sm">Create HUD menus triggered by global shortcuts.</p>
        </div>
        {!editingGroup && (
          <button
            onClick={() => setEditingGroup({ name: '', shortcut: '', scriptIds: [] })}
            className="flex items-center gap-2 px-4 py-2 bg-[#141a24] hover:bg-[#252d3b] border border-white/10 rounded shadow-md transition-all text-sm font-bold text-white"
          >
            <span className="material-symbols-outlined text-[20px]">add</span>
            New Group
          </button>
        )}
      </div>

      <div className="flex gap-6 h-full overflow-hidden">
        {/* List of Groups */}
        <div className="w-1/3 space-y-3 overflow-y-auto pr-2">
           {groups.map(group => (
             <div
               key={group.id}
               onClick={() => setEditingGroup(group)}
               className={`p-4 rounded-lg border cursor-pointer transition-all ${
                 editingGroup?.id === group.id
                 ? 'bg-whitecube-accent/20 border-whitecube-accent text-white'
                 : 'bg-[#1b2230] border-white/5 hover:bg-[#141a24] text-white/80'
               }`}
             >
               <div className="flex items-center justify-between mb-2">
                 <h3 className="font-bold">{group.name}</h3>
                 <span className="text-xs bg-white/10 px-2 py-1 rounded font-mono">{group.shortcut}</span>
               </div>
               <p className="text-xs opacity-60">{group.scriptIds.length} actions linked</p>
             </div>
           ))}
        </div>

        {/* Editor Panel */}
        <div className="flex-1 bg-[#090c12] border border-white/10 rounded-xl p-6 overflow-hidden flex flex-col">
           {editingGroup ? (
             <>
                <div className="grid grid-cols-2 gap-4 mb-6">
                   <div>
                     <label className="text-xs font-bold text-white/40 uppercase block mb-1">Group Name</label>
                     <input
                       className="w-full bg-black/20 border border-white/10 rounded px-3 py-2 text-white"
                       value={editingGroup.name}
                       onChange={e => setEditingGroup({...editingGroup, name: e.target.value})}
                       placeholder="e.g. Media Controls"
                     />
                   </div>
                   <div>
                     <label className="text-xs font-bold text-white/40 uppercase block mb-1">Trigger Shortcut</label>
                     <input
                       className="w-full bg-black/20 border border-white/10 rounded px-3 py-2 text-white font-mono"
                       value={editingGroup.shortcut}
                       onChange={e => setEditingGroup({...editingGroup, shortcut: e.target.value})}
                       placeholder="e.g. Alt+M"
                     />
                   </div>
                </div>

                <div className="flex-1 overflow-hidden flex flex-col">
                  <label className="text-xs font-bold text-white/40 uppercase block mb-2">Included Actions</label>
                  <div className="flex-1 overflow-y-auto border border-white/5 rounded bg-black/20 p-2 space-y-1">
                     {availableScripts.map(script => {
                       const isSelected = editingGroup.scriptIds?.includes(script.id);
                       return (
                         <div
                           key={script.id}
                           onClick={() => toggleScriptInGroup(script.id)}
                           className={`flex items-center gap-3 p-2 rounded cursor-pointer select-none ${isSelected ? 'bg-whitecube-accent/20 text-white' : 'hover:bg-white/5 text-white/50'}`}
                         >
                            <div className={`w-4 h-4 rounded border flex items-center justify-center ${isSelected ? 'bg-whitecube-accent border-whitecube-accent' : 'border-white/20'}`}>
                               {isSelected && <span className="material-symbols-outlined text-[10px] text-white">check</span>}
                            </div>
                            <span className="material-symbols-outlined text-lg opacity-70">{script.icon}</span>
                            <span className="text-sm font-medium">{script.name}</span>
                         </div>
                       );
                     })}
                  </div>
                </div>

                <div className="mt-6 flex justify-between">
                   {editingGroup.id && (
                     <button
                       onClick={() => { onDeleteGroup(editingGroup.id!); setEditingGroup(null); }}
                       className="text-red-400 hover:text-red-300 text-sm font-bold flex items-center gap-2"
                     >
                       <span className="material-symbols-outlined">delete</span> Delete Group
                     </button>
                   )}
                   <div className="flex gap-3 ml-auto">
                      <button onClick={() => setEditingGroup(null)} className="px-4 py-2 text-white/60 hover:text-white text-sm">Cancel</button>
                      <button onClick={handleSave} className="px-6 py-2 bg-whitecube-accent hover:bg-red-600 rounded text-white text-sm font-bold shadow-md">Save Group</button>
                   </div>
                </div>
             </>
           ) : (
             <div className="flex flex-col items-center justify-center h-full text-white/30">
               <span className="material-symbols-outlined text-4xl mb-4">touch_app</span>
               <p>Select a group to edit or create a new one.</p>
             </div>
           )}
        </div>
      </div>
    </div>
  );
};

export default ShortcutGroupsEditor;
