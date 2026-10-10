'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');
const P = require('./parse.cjs');

const read = (f) => { try { return fs.readFileSync(f, 'utf8'); } catch { return ''; } };

function run(cmd, args = [], opts = {}) {
  // Desktop sessions often omit /usr/sbin. Internal tools also must not be
  // shadowed by a same-named executable in an arbitrary working directory.
  const executable = cmd.includes('/') ? cmd : ['/usr/bin', '/bin', '/usr/sbin', '/sbin'].map(dir => path.join(dir, cmd)).find(file => { try { fs.accessSync(file, fs.constants.X_OK); return true; } catch { return false; } }) || cmd;
  return new Promise((resolve) => {
    const child = execFile(executable, args, { timeout: opts.timeout ?? 15000, maxBuffer: 16 * 1024 * 1024, env: { ...process.env, LC_ALL: 'C', NO_COLOR: '1' } },
      (err, stdout, stderr) => resolve({ ok: !err, code: err ? (typeof err.code === 'number' ? err.code : -1) : 0, stdout: String(stdout), stderr: String(stderr || (err ? err.message : '')) }));
    child.stdin.on('error', () => {}); // An early exit may close stdin before input is written.
    if (opts.input !== undefined) child.stdin.end(opts.input); else child.stdin.end();
  });
}

// sudo -n never prompts: either passwordless sudo is configured or we report that plainly.
async function sudo(args, opts) {
  const r = await run('sudo', ['-n', ...args], opts);
  if (!r.ok && /password is required|a terminal is required|not in the sudoers/i.test(r.stderr)) {
    r.needsSudo = true;
    r.hint = `sudo ${args.join(' ')}`;
  }
  return r;
}

let prevCpu = null;
let prevNet = null;
let prevNetAt = 0;

function readTemps() {
  const out = [];
  try {
    for (const h of fs.readdirSync('/sys/class/hwmon')) {
      const base = path.join('/sys/class/hwmon', h);
      const name = read(path.join(base, 'name')).trim() || h;
      for (const f of fs.readdirSync(base).filter((n) => /^temp\d+_input$/.test(n))) {
        const v = Number(read(path.join(base, f))) / 1000;
        const label = read(path.join(base, f.replace('_input', '_label'))).trim();
        if (Number.isFinite(v) && v > 0) out.push({ name: label ? `${name} ${label}` : name, c: v });
      }
    }
  } catch { /* no hwmon */ }
  return out.slice(0, 8);
}

function readBattery() {
  try {
    const dir = fs.readdirSync('/sys/class/power_supply').find((d) => read(`/sys/class/power_supply/${d}/type`).trim() === 'Battery');
    if (!dir) return null;
    const b = `/sys/class/power_supply/${dir}`;
    return { pct: Number(read(`${b}/capacity`)), status: read(`${b}/status`).trim() };
  } catch { return null; }
}

async function snapshot() {
  const cpuNow = P.parseCpuStat(read('/proc/stat'));
  const cpu = P.cpuPercent(prevCpu, cpuNow);
  prevCpu = cpuNow;

  const nowMs = Date.now();
  const netNow = P.parseNetDev(read('/proc/net/dev'));
  const dt = (nowMs - prevNetAt) / 1000;
  const net = Object.entries(netNow).map(([name, v]) => {
    const p = prevNet && prevNet[name];
    return { name, rx: v.rx, tx: v.tx, rxRate: p && dt > 0 ? Math.max(0, (v.rx - p.rx) / dt) : 0, txRate: p && dt > 0 ? Math.max(0, (v.tx - p.tx) / dt) : 0 };
  });
  prevNet = netNow; prevNetAt = nowMs;

  const [df, ps] = await Promise.all([
    run('df', ['-PkT', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'squashfs', '-x', 'overlay', '-x', 'efivarfs']),
    run('ps', ['-eo', 'pid,pcpu,pmem,comm', '--sort=-pcpu']),
  ]);

  return {
    at: nowMs,
    cpu,
    cores: os.cpus().length,
    load: os.loadavg(),
    mem: P.parseMeminfo(read('/proc/meminfo')),
    uptime: os.uptime(),
    net,
    disks: P.parseDf(df.stdout).map((d) => ({ ...d, size: d.size * 1024, used: d.used * 1024, avail: d.avail * 1024 })),
    top: P.parsePs(ps.stdout).slice(0, 8),
    temps: readTemps(),
    battery: readBattery(),
  };
}

function hostInfo() {
  const rel = Object.fromEntries(read('/etc/os-release').split('\n').map((l) => l.match(/^(\w+)="?([^"]*)"?$/)).filter(Boolean).map((m) => [m[1], m[2]]));
  const cpuModel = (read('/proc/cpuinfo').match(/model name\s*:\s*(.+)/) || [])[1] || os.cpus()[0]?.model || 'unknown';
  return { home: os.homedir(), hostname: os.hostname(), user: os.userInfo().username, os: rel.PRETTY_NAME || 'Linux', kernel: os.release(), cpu: cpuModel.trim(), arch: os.arch() };
}

async function displayInfo() {
  const runtime = process.env.XDG_RUNTIME_DIR || '';
  let sockets = [];
  try { sockets = fs.readdirSync(runtime).filter((n) => /^wayland-\d+$/.test(n)); } catch { /* none */ }
  const outputs = [];
  try {
    for (const c of fs.readdirSync('/sys/class/drm').filter((n) => /^card\d+-/.test(n))) {
      const d = `/sys/class/drm/${c}`;
      outputs.push(P.parseDrmModes(c.replace(/^card\d+-/, ''), read(`${d}/modes`), read(`${d}/status`)));
    }
  } catch { /* no drm */ }
  const randr = await run('wlr-randr', [], { timeout: 3000 });
  return {
    sessionType: process.env.XDG_SESSION_TYPE || 'unknown',
    waylandDisplay: process.env.WAYLAND_DISPLAY || null,
    sockets,
    desktop: process.env.XDG_CURRENT_DESKTOP || process.env.XDG_SESSION_DESKTOP || null,
    x11Display: process.env.DISPLAY || null,
    ozone: 'wayland',
    outputs: outputs.filter((o) => o.status === 'connected'),
    randr: randr.ok ? randr.stdout.trim() : null,
  };
}

async function listServices(scope) {
  const args = scope === 'user' ? ['--user'] : [];
  const [units, enabled] = await Promise.all([
    run('systemctl', [...args, 'list-units', '--type=service', '--all', '--no-legend', '--plain', '--no-pager']),
    run('systemctl', [...args, 'list-unit-files', '--type=service', '--no-legend', '--plain', '--no-pager']),
  ]);
  if (!units.ok) return { error: (units.stderr || 'systemctl failed').trim().split('\n')[0], units: [] };
  const state = new Map(enabled.stdout.split('\n').map((l) => l.trim().split(/\s+/)).filter((p) => p.length >= 2).map((p) => [p[0], p[1]]));
  return { units: P.parseUnits(units.stdout).filter((u) => u.load !== 'not-found').map((u) => ({ ...u, enabled: state.get(u.unit) || '' })) };
}

async function serviceAction(scope, unit, action) {
  if (!P.UNIT_RE.test(unit)) return { ok: false, error: 'Invalid unit name' };
  if (!P.SERVICE_ACTIONS.has(action)) return { ok: false, error: 'Invalid action' };
  const r = scope === 'user' ? await run('systemctl', ['--user', action, unit]) : await sudo(['systemctl', action, unit]);
  return { ok: r.ok, output: (r.stdout + r.stderr).trim(), needsSudo: r.needsSudo, hint: r.hint };
}

async function journal({ lines = 200, priority = 6, unit = '', scope = 'system' } = {}) {
  const args = ['-o', 'json', '--no-pager', '-n', String(Math.min(Math.max(lines, 10), 1000)), '-p', String(Math.min(Math.max(priority, 0), 7))];
  if (scope === 'user') args.unshift('--user');
  if (unit) { if (!P.UNIT_RE.test(unit)) return { error: 'Invalid unit', entries: [] }; args.push('-u', unit); }
  const r = await run('journalctl', args, { timeout: 10000 });
  if (!r.ok && !r.stdout) return { error: (r.stderr || 'journalctl failed').trim().split('\n')[0], entries: [] };
  return { entries: P.parseJournal(r.stdout).reverse() };
}

async function checkUpdates() {
  const r = await run('apt', ['list', '--upgradable'], { timeout: 30000 });
  if (!r.ok) return { error: r.stderr.trim() || 'apt is not available', packages: [] };
  return { packages: P.parseUpgradable(r.stdout) };
}

async function network() {
  const [addr, route, ss] = await Promise.all([
    run('ip', ['-j', 'addr']), run('ip', ['-j', 'route']), run('ss', ['-tulpnH']),
  ]);
  let ifaces = [], routes = [];
  try { ifaces = JSON.parse(addr.stdout).filter((i) => i.ifname !== 'lo').map((i) => ({ name: i.ifname, state: i.operstate, mac: i.address || '', addrs: (i.addr_info || []).map((a) => `${a.local}/${a.prefixlen}`) })); } catch { /* no ip */ }
  try { routes = JSON.parse(route.stdout); } catch { /* no routes */ }
  const gw = routes.find((r) => r.dst === 'default');
  return {
    ifaces,
    gateway: gw ? { ip: gw.gateway, dev: gw.dev } : null,
    wireless: P.parseWireless(read('/proc/net/wireless')),
    listeners: P.parseSs(ss.stdout),
  };
}

// Latency/loss to the gateway and one external anchor, so "Wi-Fi is up" and "internet is up" are told apart.
async function probe(targets) {
  const list = (Array.isArray(targets) ? targets : []).filter((t) => typeof t === 'string' && /^(?!-)[0-9a-zA-Z.:-]{1,253}$/.test(t)).slice(0, 4);
  return Promise.all(list.map(async (host) => {
    const r = await run('ping', ['-n', '-c', '3', '-W', '1', '-i', '0.2', '--', host], { timeout: 8000 });
    return { host, ...P.parsePing(r.stdout) };
  }));
}

async function processes() {
  const r = await run('ps', ['-eo', 'pid,user,pcpu,pmem,rss,etimes,stat,args', '--sort=-pcpu']);
  if (!r.ok) throw new Error(r.stderr.trim() || 'Process list unavailable');
  return { processes: P.parsePsFull(r.stdout).slice(0, 1000), self: process.pid };
}

function signalProcess(pid, signal) {
  if (!Number.isInteger(pid) || pid <= 1 || pid === process.pid) return { ok: false, error: 'Refusing to signal that process' };
  if (!P.SIGNALS.has(signal)) return { ok: false, error: 'Unsupported signal' };
  try { process.kill(pid, signal); return { ok: true }; } catch (e) {
    return { ok: false, error: e.code === 'EPERM' ? 'Permission denied: the process belongs to another user' : e.message };
  }
}

module.exports = { network, probe, processes, signalProcess, run, sudo, snapshot, hostInfo, displayInfo, listServices, serviceAction, journal, checkUpdates, read };
