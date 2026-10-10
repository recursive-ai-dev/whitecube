import { SysctlEntry, SysctlBackup } from './types';

export const fetchSysctlEntries = (): Promise<SysctlEntry[]> => window.lab.sysctl.list();
export const applySysctlEntries = (entries: { key: string; value: string }[], persist = false): Promise<string> => window.lab.sysctl.apply(entries, persist);
export const fetchSysctlBackups = (): Promise<SysctlBackup[]> => window.lab.sysctl.backups();
export const restoreSysctlBackup = (timestamp: number): Promise<string> => window.lab.sysctl.restore(timestamp);
