'use strict';
const { app, BrowserWindow, ipcMain, session, Menu, dialog, shell } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { waylandProblem, enforce } = require('./wayland.cjs');
const { trustedUrl, validateDevUrl } = require('./security.cjs');
const { createActivity } = require('./activity.cjs');
const inspector = require('./inspector.cjs');

const problem = waylandProblem();
if (problem) { process.stderr.write(`Whitecube Command Center: ${problem}\n`); process.exit(1); }
enforce(app);
app.setDesktopName('whitecube-command-center.desktop');
// Keep the existing profile and library through the product rename. Explicit
// test/development profiles still take precedence.
const profileOverride = app.commandLine.getSwitchValue('user-data-dir');
app.setPath('userData', profileOverride ? path.resolve(profileOverride) : path.join(app.getPath('appData'), 'Command Center'));
const S = require('./system.cjs');
const runner = require('./runner.cjs');
const sysctl = require('./sysctl.cjs');
const web = require('./web.cjs');
const DEV_URL = validateDevUrl(process.env.WHITECUBE_COMMAND_CENTER_DEV_URL || process.env.COMMAND_CENTER_DEV_URL);
const DIST_INDEX = path.join(__dirname, '..', 'dist', 'index.html');
if (!app.requestSingleInstanceLock()) { app.quit(); process.exit(0); }
let win = null, activity;
const settingsFile = path.join(app.getPath('userData'), 'window.json');
const validSize = (v, fallback, min, max) => Number.isInteger(v) && v >= min && v <= max ? v : fallback;

function createWindow() {
  let bounds = {};
  try { bounds = JSON.parse(fs.readFileSync(settingsFile, 'utf8')) || {}; } catch { /* first launch */ }
  win = new BrowserWindow({
    width: validSize(bounds.width, 1360, 960, 7680), height: validSize(bounds.height, 860, 600, 4320), minWidth: 960, minHeight: 600,
    title: 'Whitecube Command Center', backgroundColor: '#04070d', show: false,
    icon: path.join(__dirname, '..', 'assets', 'whitecube-command-center.png'),
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true, spellcheck: false },
  });
  win.once('ready-to-show', () => { if (bounds.maximized) win.maximize(); win.show(); });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (e, url) => { if (!trustedUrl(url, DIST_INDEX, DEV_URL)) e.preventDefault(); });
  win.webContents.on('will-attach-webview', e => e.preventDefault());
  win.on('close', () => {
    try { fs.mkdirSync(path.dirname(settingsFile), { recursive: true }); fs.writeFileSync(settingsFile, JSON.stringify({ ...win.getNormalBounds(), maximized: win.isMaximized() })); } catch { /* optional window state */ }
  });
  if (DEV_URL) win.loadURL(DEV_URL); else win.loadFile(DIST_INDEX);
  win.on('closed', () => { win = null; });
}

function csp() {
  const dev = DEV_URL ? ` ${new URL(DEV_URL).origin} ws://${new URL(DEV_URL).host}` : '';
  const policy = `default-src 'self'${dev}; script-src 'self'${DEV_URL ? " 'unsafe-inline'" : ''}; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self'${dev}; object-src 'none'; base-uri 'none'; form-action 'none'`;
  session.defaultSession.webRequest.onHeadersReceived((d, cb) => cb({ responseHeaders: { ...d.responseHeaders, 'Content-Security-Policy': [policy] } }));
}

function registerIpc() {
  const send = ev => { if (win && !win.isDestroyed()) win.webContents.send('run:event', ev); };
  const authorized = e => {
    if (!win || e.sender !== win.webContents || e.senderFrame !== win.webContents.mainFrame || !trustedUrl(e.senderFrame.url, DIST_INDEX, DEV_URL)) throw new Error('Untrusted IPC sender');
  };
  const guard = fn => async (e, ...args) => {
    try { authorized(e); return await fn(...args); } catch (err) { return { error: err.message || String(err) }; }
  };
  const handle = (channel, fn) => ipcMain.handle(channel, guard(fn));
  handle('host:info', () => S.hostInfo());
  handle('host:snapshot', () => S.snapshot());
  handle('host:display', () => S.displayInfo());
  handle('run:start', ({ command, cwd }) => {
    const dir = runner.resolveCwd(typeof cwd === 'string' ? cwd : '~');
    let recordId;
    const job = runner.start({ command, cwd: dir }, ev => { if (ev.kind === 'exit' && recordId) activity.finish(recordId, ev); send(ev); });
    recordId = activity.start(command, dir);
    return job;
  });
  handle('run:kill', id => runner.kill(String(id)));
  handle('svc:list', scope => S.listServices(scope === 'user' ? 'user' : 'system'));
  handle('svc:action', (scope, unit, action) => S.serviceAction(scope === 'user' ? 'user' : 'system', unit, action));
  handle('journal:read', o => S.journal(o || {}));
  handle('apt:upgradable', () => S.checkUpdates());
  handle('net:info', () => S.network());
  handle('net:probe', t => S.probe(t));
  handle('proc:list', () => S.processes());
  handle('proc:signal', (pid, sig) => S.signalProcess(pid, sig));
  handle('web:wallpaper', () => web.wallpaper());
  handle('web:outside', () => web.outside());
  handle('storage:read', () => inspector.storage());
  handle('packages:list', () => inspector.packages());
  handle('diagnostics:read', () => inspector.diagnostics());
  handle('activity:list', () => activity.list());
  handle('activity:clear', () => activity.clear());
  handle('app:info', () => ({ version: app.getVersion(), electron: process.versions.electron, dataDir: app.getPath('userData'), packaged: app.isPackaged }));
  handle('app:open-data', async () => { const error = await shell.openPath(app.getPath('userData')); return error ? { error } : { ok: true }; });
  handle('file:save', async ({ name, content }) => {
    if (typeof content !== 'string' || Buffer.byteLength(content) > 10 * 1024 * 1024) throw new Error('Export must be at most 10 MiB');
    if (typeof name !== 'string' || !/^[a-zA-Z0-9_.-]+\.(json|txt)$/.test(name)) throw new Error('Invalid export filename');
    const r = await dialog.showSaveDialog(win, { title: 'Export from Whitecube Command Center', defaultPath: path.join(app.getPath('documents'), name), filters: [{ name: 'Export', extensions: [name.split('.').at(-1)] }] });
    if (r.canceled || !r.filePath) return { canceled: true };
    await fs.promises.writeFile(r.filePath, content, { mode: 0o600 });
    return { path: r.filePath };
  });
  handle('file:import', async () => {
    const r = await dialog.showOpenDialog(win, { title: 'Import Whitecube Command Center backup', properties: ['openFile'], filters: [{ name: 'Whitecube Command Center backup', extensions: ['json'] }] });
    if (r.canceled || !r.filePaths[0]) return { canceled: true };
    const file = await fs.promises.open(r.filePaths[0], 'r');
    try {
      const stat = await file.stat();
      if (!stat.isFile() || stat.size > 10 * 1024 * 1024) throw new Error('Choose a JSON file smaller than 10 MiB');
      return { content: await file.readFile('utf8') };
    } finally { await file.close(); }
  });
  // Keep rejection semantics for the existing kernel settings panel.
  ipcMain.handle('sysctl:list', e => { authorized(e); return sysctl.list(); });
  ipcMain.handle('sysctl:apply', (e, entries, persist) => { authorized(e); return sysctl.apply(entries, persist === true); });
  ipcMain.handle('sysctl:backups', e => { authorized(e); return sysctl.backups(); });
  ipcMain.handle('sysctl:restore', (e, ts) => { authorized(e); return sysctl.restore(Number(ts)); });
}

app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => runner.killAll());
app.whenReady().then(() => {
  sysctl.init(app.getPath('userData'));
  activity = createActivity(path.join(app.getPath('userData'), 'activity.json'));
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  csp(); registerIpc(); Menu.setApplicationMenu(null); createWindow();
});
