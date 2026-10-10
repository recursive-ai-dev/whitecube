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

export interface SysctlDbusClient {
  list(): Promise<SysctlEntry[]>;
  setManaged(entries: { key: string; value: string }[]): Promise<string>;
  listBackups(): Promise<SysctlBackup[]>;
  restoreBackup(timestamp: number): Promise<string>;
}
