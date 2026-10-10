import { ScriptItem, TerminalLog, NautilusAction, ShortcutGroup, ScriptStatus, Category } from './types';
import { LOG_HISTORY_LIMIT } from './constants';
import { createId, createStableId } from './id';

export interface AppState {
  scripts: ScriptItem[];
  logs: TerminalLog[];
  nautilusActions: NautilusAction[];
  shortcutGroups: ShortcutGroup[];
}

const STORAGE_KEY = 'command-center:v2';

interface Template { key: string; name: string; description: string; command: string; icon: string; category: Category }

const TEMPLATES: Template[] = [
  { key: 'system-update', name: 'System Update', description: 'Refresh package metadata and apply updates.', command: 'sudo apt update && sudo apt upgrade -y', icon: 'system_update', category: 'System' },
  { key: 'system-clean', name: 'System Cleanup', description: 'Remove unused packages and clear the apt cache.', command: 'sudo apt autoremove -y && sudo apt clean', icon: 'cleaning_services', category: 'System' },
  { key: 'disk-usage', name: 'Disk Usage', description: 'Usage across mounted filesystems.', command: 'df -h -x tmpfs -x devtmpfs', icon: 'database', category: 'System' },
  { key: 'failed-units', name: 'Failed Units', description: 'systemd units that are in a failed state.', command: 'systemctl --failed --no-pager', icon: 'monitor_heart', category: 'System' },
  { key: 'memory-hogs', name: 'Memory Hogs', description: 'Processes using the most memory.', command: 'ps -eo pid,pcpu,pmem,comm --sort=-pmem | head -15', icon: 'monitor_heart', category: 'System' },
  { key: 'wayland-audit', name: 'Wayland Audit', description: 'Confirm the session is Wayland and no X server is running.', command: 'echo "session: $XDG_SESSION_TYPE  display: $WAYLAND_DISPLAY"; ls "$XDG_RUNTIME_DIR" | grep -E "^wayland-[0-9]+$"; ps -eo pid,comm | grep -Ei "Xorg|Xwayland" || echo "No X server running"', icon: 'security', category: 'System' },
  { key: 'config-folder', name: 'Config Folder', description: 'List the user configuration directory.', command: 'ls -la ~/.config', icon: 'folder_open', category: 'Files' },
  { key: 'addresses', name: 'Addresses', description: 'Network interfaces and addresses.', command: 'ip -br addr', icon: 'lan', category: 'Network' },
  { key: 'listening', name: 'Listening Ports', description: 'Sockets accepting connections.', command: 'ss -tulpn', icon: 'wifi_tethering', category: 'Network' },
  { key: 'network-restart', name: 'Restart Network', description: 'Restart the NetworkManager service.', command: 'sudo systemctl restart NetworkManager', icon: 'wifi_tethering', category: 'Network' },
  { key: 'firewall-status', name: 'Firewall Status', description: 'Current firewall policy and rules.', command: 'sudo ufw status verbose', icon: 'security', category: 'Network' },
  { key: 'docker-status', name: 'Docker Status', description: 'Containers and their health.', command: 'docker ps -a', icon: 'deployed_code', category: 'Development' },
];

const buildDefaultScripts = (): ScriptItem[] => TEMPLATES.map((t) => ({
  id: createStableId(`linux-${t.key}`),
  name: t.name,
  description: t.description,
  command: t.command,
  icon: t.icon,
  category: t.category,
  lastRun: 'Not run',
  status: ScriptStatus.IDLE,
  requiresConfirmation: /\bsudo\b/.test(t.command),
}));

const buildDefaultLogs = (): TerminalLog[] => [{
  id: createId(),
  timestamp: new Date().toLocaleTimeString(),
  content: 'Whitecube Command Center ready. Commands run in a real bash; type help for built-ins.',
  type: 'info',
}];

const buildDefaultNautilusActions = (): NautilusAction[] => [
  { id: createStableId('nautilus-open-code'), name: 'Open in VS Code', command: 'code --ozone-platform=wayland %f', context: 'Directory', fileTypes: '*' },
  { id: createStableId('nautilus-copy-path'), name: 'Copy Path', command: 'printf "%s" "%f" | wl-copy', context: 'Selection', fileTypes: '*' },
];

const buildDefaultShortcutGroups = (scripts: ScriptItem[]): ShortcutGroup[] => scripts.length === 0 ? [] : [{
  id: createStableId('shortcuts-core'),
  name: 'Core Actions',
  shortcut: 'Alt+C',
  scriptIds: scripts.slice(0, 4).map((s) => s.id),
}];

const buildDefaultState = (): AppState => {
  const scripts = buildDefaultScripts();
  return { scripts, logs: buildDefaultLogs(), nautilusActions: buildDefaultNautilusActions(), shortcutGroups: buildDefaultShortcutGroups(scripts) };
};

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object';
const isValidScript = (v: unknown): v is ScriptItem =>
  isObj(v) && typeof v.id === 'string' && typeof v.name === 'string' && typeof v.command === 'string' && typeof v.category === 'string' && typeof v.icon === 'string';
const isValidLog = (v: unknown): v is TerminalLog => isObj(v) && typeof v.id === 'string' && typeof v.content === 'string' && ['info', 'success', 'error', 'command'].includes(String(v.type));
const isValidNautilusAction = (v: unknown): v is NautilusAction => isObj(v) && ['id', 'name', 'command', 'context', 'fileTypes'].every(key => typeof v[key] === 'string');
const isValidShortcutGroup = (v: unknown): v is ShortcutGroup => isObj(v) && ['id', 'name', 'shortcut'].every(key => typeof v[key] === 'string') && Array.isArray(v.scriptIds) && v.scriptIds.every(id => typeof id === 'string');

export const loadState = (): AppState => {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return buildDefaultState();
    const parsed = JSON.parse(raw) as Partial<AppState>;
    const scripts = Array.isArray(parsed.scripts) ? parsed.scripts.filter(isValidScript).map((s) => ({ ...s, description: typeof s.description === 'string' ? s.description : '', path: typeof s.path === 'string' ? s.path : undefined, lastRun: typeof s.lastRun === 'string' ? s.lastRun : 'Not run', status: s.status === ScriptStatus.RUNNING || !Object.values(ScriptStatus).includes(s.status) ? ScriptStatus.IDLE : s.status })) : [];
    const logs = Array.isArray(parsed.logs) ? parsed.logs.filter(isValidLog) : [];
    const nautilusActions = Array.isArray(parsed.nautilusActions) ? parsed.nautilusActions.filter(isValidNautilusAction) : [];
    const shortcutGroups = Array.isArray(parsed.shortcutGroups) ? parsed.shortcutGroups.filter(isValidShortcutGroup) : [];
    return { scripts, logs, nautilusActions, shortcutGroups };
  } catch (error) {
    console.warn('Failed to load stored state', error);
    return buildDefaultState();
  }
};

export const saveState = (state: AppState): void => {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...state, logs: state.logs.slice(-LOG_HISTORY_LIMIT) }));
  } catch (error) {
    console.warn('Failed to save state', error);
  }
};
