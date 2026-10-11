export enum ScriptStatus {
  IDLE = 'IDLE',
  RUNNING = 'RUNNING',
  SUCCESS = 'SUCCESS',
  ERROR = 'ERROR'
}

export type Category = 'System' | 'Files' | 'Network' | 'Development' | 'Custom';
export type ViewMode = 'Overview' | 'Dashboard' | 'Processes' | 'Storage' | 'Packages' | 'Diagnostics' | 'Activity' | 'Settings' | 'Network' | 'Services' | 'Journal' | 'Updates' | 'Display' | 'Nautilus' | 'Shortcuts' | 'Sysctl';

export interface ScriptItem {
  id: string;
  name: string;
  description: string;
  command: string; 
  path?: string; // Working directory
  icon: string; 
  category: Category;
  lastRun: string;
  status: ScriptStatus;
  requiresConfirmation?: boolean;
  pinned?: boolean;
  tags?: string[];
}

export interface TerminalLog {
  id: string;
  timestamp: string;
  content: string;
  type: 'info' | 'success' | 'error' | 'command';
  cwd?: string;
}

export interface NautilusAction {
  id: string;
  name: string;
  command: string;
  context: string; // e.g. "Selection", "Background"
  fileTypes: string; // e.g. "*" or ".jpg, .png"
}

export interface ShortcutGroup {
  id: string;
  name: string;
  shortcut: string; // e.g. "Alt+S"
  scriptIds: string[];
}

export interface SysctlEntry {
  key: string;
  value: string;
  sourceFile: string;
  isManaged: boolean;
}

export interface SysctlBackup {
  timestamp: number;
  entries: { key: string; value: string }[];
}

export type SysctlRiskTier = 'info' | 'low' | 'medium' | 'high';

export interface SysctlCatalogEntry {
  description: string;
  risk: SysctlRiskTier;
}
