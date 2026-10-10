import { ScriptStatus, type ScriptItem, type NautilusAction, type ShortcutGroup } from './types';
import { createId } from './id';
export interface Library { scripts: ScriptItem[]; nautilusActions: NautilusAction[]; shortcutGroups: ShortcutGroup[] }
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const string = (v: unknown, max = 4096): v is string => typeof v === 'string' && v.length <= max;
const categories = new Set(['System', 'Files', 'Network', 'Development', 'Custom']);

export function decodeBackup(content: string): Library {
  if (content.length > 10 * 1024 * 1024) throw new Error('Backup exceeds 10 MiB');
  const data = JSON.parse(content);
  if (!object(data) || data.format !== 'command-center' || data.version !== 1 || !object(data.library)) throw new Error('Unsupported backup format or version');
  const lib = data.library;
  for (const key of ['scripts', 'nautilusActions', 'shortcutGroups']) {
    if (!Array.isArray(lib[key]) || lib[key].length > 1000) throw new Error(`Invalid ${key} collection`);
    const ids = new Set();
    for (const item of lib[key] as unknown[]) {
      if (!object(item) || !string(item.id, 200) || !item.id || ids.has(item.id) || !string(item.name, 200) || !item.name.trim()) throw new Error(`Invalid or duplicate item in ${key}`);
      ids.add(item.id);
    }
  }
  const scripts = (lib.scripts as Record<string, unknown>[]).map(s => {
    if (!string(s.command, 65536) || !s.command.trim() || s.command.includes('\0') || !string(s.description) || !string(s.icon, 100) || !categories.has(String(s.category)) || (s.path !== undefined && !string(s.path))) throw new Error('Invalid command in backup');
    return { id: s.id as string, name: s.name as string, command: s.command, description: s.description, icon: s.icon, category: s.category as ScriptItem['category'], path: s.path as string | undefined, requiresConfirmation: true, lastRun: 'Not run', status: ScriptStatus.IDLE };
  });
  const scriptIds = new Set(scripts.map(s => s.id));
  const nautilusActions = (lib.nautilusActions as Record<string, unknown>[]).map(a => {
    if (!string(a.command, 65536) || !['Selection', 'Directory', 'Background'].includes(String(a.context)) || !string(a.fileTypes)) throw new Error('Invalid file manager action');
    return { id: a.id as string, name: a.name as string, command: a.command, context: a.context as string, fileTypes: a.fileTypes };
  });
  const shortcutGroups = (lib.shortcutGroups as Record<string, unknown>[]).map(g => {
    if (!string(g.shortcut, 100) || !Array.isArray(g.scriptIds) || g.scriptIds.length > 1000 || !g.scriptIds.every(id => typeof id === 'string' && scriptIds.has(id))) throw new Error('Invalid shortcut or missing command reference');
    return { id: g.id as string, name: g.name as string, shortcut: g.shortcut, scriptIds: g.scriptIds as string[] };
  });
  return { scripts, nautilusActions, shortcutGroups };
}
export function encodeBackup(library: Library) {
  return JSON.stringify({ format: 'command-center', version: 1, exportedAt: new Date().toISOString(), library }, null, 2);
}
export function mergeLibrary(current: Library, incoming: Library): Library {
  const ids = new Set([...current.scripts, ...current.nautilusActions, ...current.shortcutGroups].map(x => x.id));
  const remap = new Map<string, string>();
  const fresh = (id: string) => { const value = ids.has(id) ? createId() : id; ids.add(value); return value; };
  const scripts = incoming.scripts.map(s => { const id = fresh(s.id); remap.set(s.id, id); return { ...s, id, requiresConfirmation: true }; });
  return {
    scripts: [...current.scripts, ...scripts],
    nautilusActions: [...current.nautilusActions, ...incoming.nautilusActions.map(a => ({ ...a, id: fresh(a.id) }))],
    shortcutGroups: [...current.shortcutGroups, ...incoming.shortcutGroups.map(g => ({ ...g, id: fresh(g.id), scriptIds: g.scriptIds.map(id => remap.get(id)!) }))],
  };
}
