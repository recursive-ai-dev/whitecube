// Bridge exposed by electron/preload.cjs.
export interface RunEvent {
  id: string;
  kind: 'out' | 'err' | 'exit';
  text?: string;
  code?: number | null;
  signal?: string | null;
  error?: string;
  cwd?: string;
  ms?: number;
}

export interface Snapshot {
  at: number;
  cpu: number;
  cores: number;
  load: number[];
  mem: { total: number; used: number; available: number; swapTotal: number; swapUsed: number };
  uptime: number;
  net: { name: string; rx: number; tx: number; rxRate: number; txRate: number }[];
  disks: { fs: string; type: string; size: number; used: number; avail: number; pct: number; mount: string }[];
  top: { pid: number; cpu: number; mem: number; name: string }[];
  temps: { name: string; c: number }[];
  battery: { pct: number; status: string } | null;
}

export interface HostInfo { home: string; hostname: string; user: string; os: string; kernel: string; cpu: string; arch: string }

export interface DisplayInfo {
  sessionType: string;
  waylandDisplay: string | null;
  sockets: string[];
  desktop: string | null;
  x11Display: string | null;
  outputs: { connector: string; status: string; preferred: string | null; modes: number }[];
  randr: string | null;
}

export interface ServiceUnit { unit: string; load: string; active: string; sub: string; description: string; enabled: string }
export interface JournalEntry { ts: number; priority: number; unit: string; message: string }
export interface UpgradablePkg { name: string; source: string; to: string; arch: string; from: string }
export interface ProcInfo { pid: number; user: string; cpu: number; mem: number; rss: number; age: number; state: string; cmd: string }
export interface NetInfo {
  ifaces: { name: string; state: string; mac: string; addrs: string[] }[];
  gateway: { ip: string; dev: string } | null;
  wireless: { name: string; link: number; signal: number; retries: number }[];
  listeners: { proto: string; state: string; local: string; process: string; pid: number | null }[];
}
export interface ProbeResult { host: string; loss: number; min: number | null; avg: number | null; max: number | null }
export interface WallpaperInfo { id: string; author: string; source: string; dataUrl: string }
export interface OutsideInfo { city: string; region: string; country: string; ip: string; tempC: number; wind: number; code: number; tz: string | null }
export type Failure = { error: string };

export interface StorageInfo {
  at: number; warnings: string[];
  devices: { name: string; path: string; type: string; model: string; size: number; fs: string; mounts: string[]; readOnly: boolean; removable: boolean; depth: number }[];
  filesystems: (Snapshot['disks'][number] & { inodePct: number | null })[];
}
export interface InstalledPackage { name: string; version: string; arch: string; bytes: number; description: string }
export interface DiagnosticReport { at: number; host: HostInfo; checks: { id: string; title: string; status: 'ok' | 'warning' | 'critical' | 'unknown'; detail: string }[] }
export interface ActivityRecord { id: string; command: string; cwd: string; startedAt: number; endedAt?: number; duration?: number; code?: number; signal?: string; status: 'running' | 'success' | 'failed' | 'stopped' | 'interrupted' }
export type SaveResult = { path?: string; canceled?: boolean } | Failure;

export interface Lab {
  storage(): Promise<StorageInfo | Failure>;
  packages(): Promise<{ at: number; packages: InstalledPackage[] } | Failure>;
  diagnostics(): Promise<DiagnosticReport | Failure>;
  activity(): Promise<{ records: ActivityRecord[]; warning: string | null } | Failure>;
  clearActivity(): Promise<{ ok: boolean; error?: string }>;
  appInfo(): Promise<{ version: string; electron: string; dataDir: string; packaged: boolean } | Failure>;
  openData(): Promise<{ ok: boolean } | Failure>;
  saveFile(name: string, content: string): Promise<SaveResult>;
  importFile(): Promise<{ canceled?: boolean; content?: string } | Failure>;
  host(): Promise<HostInfo | Failure>;
  snapshot(): Promise<Snapshot | Failure>;
  display(): Promise<DisplayInfo | Failure>;
  run(command: string, cwd: string): Promise<{ id: string; cwd: string } | Failure>;
  kill(id: string): Promise<boolean>;
  onRun(cb: (e: RunEvent) => void): () => void;
  services(scope: 'system' | 'user'): Promise<{ units: ServiceUnit[]; error?: string } | Failure>;
  serviceAction(scope: 'system' | 'user', unit: string, action: string): Promise<{ ok: boolean; output?: string; needsSudo?: boolean; hint?: string; error?: string }>;
  journal(opts: { lines?: number; priority?: number; unit?: string; scope?: 'system' | 'user' }): Promise<{ entries: JournalEntry[]; error?: string } | Failure>;
  updates(): Promise<{ packages: UpgradablePkg[]; error?: string } | Failure>;
  wallpaper(): Promise<WallpaperInfo | Failure>;
  outside(): Promise<OutsideInfo | Failure>;
  network(): Promise<NetInfo | Failure>;
  probe(targets: string[]): Promise<ProbeResult[] | Failure>;
  processes(): Promise<{ processes: ProcInfo[]; self: number } | Failure>;
  signal(pid: number, signal: string): Promise<{ ok: boolean; error?: string }>;
  sysctl: {
    list(): Promise<{ key: string; value: string; sourceFile: string; isManaged: boolean }[]>;
    apply(entries: { key: string; value: string }[], persist?: boolean): Promise<string>;
    backups(): Promise<{ timestamp: number; entries: { key: string; value: string }[] }[]>;
    restore(timestamp: number): Promise<string>;
  };
  onNavigate(cb: (view: string) => void): () => void;
}

declare global {
  interface Window { lab: Lab }
}
