import type { ShortcutGroup } from './types';
export const normalizeShortcut = (value: string) => value.replace(/\s+/g, '').toUpperCase();
// These Alt bindings are consumed by Whitecube before reaching application windows.
const desktopKeys = new Set('abcdfghlnqstuvwy123456789'.toUpperCase().split(''));
export const CORE_SHORTCUT = 'Ctrl+Shift+G';
export function shortcutIssue(value: string, groups: Pick<ShortcutGroup, 'id' | 'name' | 'shortcut'>[], id?: string) {
  const key = normalizeShortcut(value);
  if (!/^(ALT|CTRL\+SHIFT)\+[A-Z0-9]$/.test(key)) return 'Use Ctrl+Shift plus a letter or number, for example Ctrl+Shift+G, or an available Alt binding.';
  if (key.startsWith('ALT+') && desktopKeys.has(key.slice(-1))) return `${value.trim()} is reserved by the Whitecube desktop. Use a Ctrl+Shift binding instead.`;
  if (['CTRL+SHIFT+C', 'CTRL+SHIFT+V', 'CTRL+SHIFT+I', 'CTRL+SHIFT+J'].includes(key)) return 'This binding is reserved for clipboard or developer tools. Choose another shortcut.';
  const duplicate = groups.find(g => g.id !== id && normalizeShortcut(g.shortcut) === key);
  return duplicate ? `This shortcut is already used by “${duplicate.name}”. Choose another binding.` : null;
}
