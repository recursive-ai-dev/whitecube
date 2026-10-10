'use strict';
const { contextBridge, ipcRenderer } = require('electron');

const invoke = (ch, ...a) => ipcRenderer.invoke(ch, ...a);

contextBridge.exposeInMainWorld('lab', {
  host: () => invoke('host:info'),
  snapshot: () => invoke('host:snapshot'),
  display: () => invoke('host:display'),
  storage: () => invoke('storage:read'),
  packages: () => invoke('packages:list'),
  diagnostics: () => invoke('diagnostics:read'),
  activity: () => invoke('activity:list'),
  clearActivity: () => invoke('activity:clear'),
  appInfo: () => invoke('app:info'),
  openData: () => invoke('app:open-data'),
  saveFile: (name, content) => invoke('file:save', { name, content }),
  importFile: () => invoke('file:import'),
  run: (command, cwd) => invoke('run:start', { command, cwd }),
  kill: (id) => invoke('run:kill', id),
  onRun: (cb) => {
    const h = (_e, ev) => cb(ev);
    ipcRenderer.on('run:event', h);
    return () => ipcRenderer.removeListener('run:event', h);
  },
  services: (scope) => invoke('svc:list', scope),
  serviceAction: (scope, unit, action) => invoke('svc:action', scope, unit, action),
  journal: (opts) => invoke('journal:read', opts),
  updates: () => invoke('apt:upgradable'),
  wallpaper: () => invoke('web:wallpaper'),
  outside: () => invoke('web:outside'),
  network: () => invoke('net:info'),
  probe: (targets) => invoke('net:probe', targets),
  processes: () => invoke('proc:list'),
  signal: (pid, sig) => invoke('proc:signal', pid, sig),
  sysctl: {
    list: () => invoke('sysctl:list'),
    apply: (entries, persist) => invoke('sysctl:apply', entries, persist),
    backups: () => invoke('sysctl:backups'),
    restore: (ts) => invoke('sysctl:restore', ts),
  },
  onNavigate: (cb) => {
    const h = (_e, view) => cb(view);
    ipcRenderer.on('navigate', h);
    return () => ipcRenderer.removeListener('navigate', h);
  },
});
