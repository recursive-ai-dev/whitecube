'use strict';
// Pure parsers for /proc and CLI output. No I/O here so they can be unit tested.

function parseCpuStat(text) {
  const line = text.split('\n').find((l) => l.startsWith('cpu '));
  if (!line) return null;
  const n = line.trim().split(/\s+/).slice(1).map(Number);
  const idle = (n[3] || 0) + (n[4] || 0);
  const total = n.slice(0, 8).reduce((a, b) => a + (b || 0), 0);
  return { idle, total };
}

function cpuPercent(prev, cur) {
  if (!prev || !cur) return 0;
  const dt = cur.total - prev.total;
  if (dt <= 0) return 0;
  return Math.max(0, Math.min(100, (1 - (cur.idle - prev.idle) / dt) * 100));
}

function parseMeminfo(text) {
  const kb = {};
  for (const l of text.split('\n')) {
    const m = l.match(/^(\w+):\s+(\d+)/);
    if (m) kb[m[1]] = Number(m[2]) * 1024;
  }
  const total = kb.MemTotal || 0;
  const available = kb.MemAvailable ?? kb.MemFree ?? 0;
  return {
    total,
    used: total - available,
    available,
    swapTotal: kb.SwapTotal || 0,
    swapUsed: (kb.SwapTotal || 0) - (kb.SwapFree || 0),
  };
}

function parseNetDev(text) {
  const out = {};
  for (const l of text.split('\n').slice(2)) {
    const m = l.match(/^\s*([^:]+):\s*(.*)$/);
    if (!m) continue;
    const f = m[2].trim().split(/\s+/).map(Number);
    if (m[1] === 'lo') continue;
    out[m[1]] = { rx: f[0] || 0, tx: f[8] || 0 };
  }
  return out;
}

function parseDf(text) {
  return text.trim().split('\n').slice(1).map((l) => {
    const p = l.trim().split(/\s+/);
    if (p.length < 6) return null;
    return { fs: p[0], type: p[1], size: Number(p[2]), used: Number(p[3]), avail: Number(p[4]), pct: parseInt(p[5], 10), mount: p.slice(6).join(' ') || p[5] };
  }).filter(Boolean);
}

// `systemctl list-units --type=service --all --no-legend --plain`
function parseUnits(text) {
  return text.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
    const m = l.match(/^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)$/);
    if (!m) return null;
    return { unit: m[1], load: m[2], active: m[3], sub: m[4], description: m[5] };
  }).filter(Boolean);
}

// `apt list --upgradable`
function parseUpgradable(text) {
  return text.split('\n').map((l) => {
    const m = l.match(/^([^/\s]+)\/(\S+)\s+(\S+)\s+(\S+)\s+\[upgradable from: ([^\]]+)\]/);
    return m ? { name: m[1], source: m[2], to: m[3], arch: m[4], from: m[5] } : null;
  }).filter(Boolean);
}

// `ps -eo pid,pcpu,pmem,comm --sort=-pcpu`
function parsePs(text) {
  return text.trim().split('\n').slice(1).map((l) => {
    const m = l.trim().match(/^(\d+)\s+([\d.]+)\s+([\d.]+)\s+(.+)$/);
    return m ? { pid: Number(m[1]), cpu: Number(m[2]), mem: Number(m[3]), name: m[4] } : null;
  }).filter(Boolean);
}

// `journalctl -o json`, one object per line
function parseJournal(text) {
  return text.split('\n').filter(Boolean).map((l) => {
    try {
      const j = JSON.parse(l);
      const ts = Number(j.__REALTIME_TIMESTAMP) / 1000;
      return {
        ts,
        priority: Number(j.PRIORITY ?? 6),
        unit: j._SYSTEMD_UNIT || j.SYSLOG_IDENTIFIER || '',
        message: Array.isArray(j.MESSAGE) ? '[binary]' : String(j.MESSAGE ?? ''),
      };
    } catch { return null; }
  }).filter(Boolean);
}

// `sysctl -a` style "key = value"
function parseSysctl(text) {
  return text.split('\n').map((l) => {
    const i = l.indexOf('=');
    return i > 0 ? { key: l.slice(0, i).trim(), value: l.slice(i + 1).trim() } : null;
  }).filter(Boolean);
}

// `wlr-randr` / `swaymsg` are optional; /sys/class/drm gives connectors without either.
function parseDrmModes(connector, modesText, status) {
  const modes = modesText.split('\n').filter(Boolean);
  return { connector, status: status.trim(), preferred: modes[0] || null, modes: modes.length };
}

// /proc/net/wireless: per-interface link quality and signal level (dBm).
function parseWireless(text) {
  return text.split('\n').slice(2).map((l) => {
    const m = l.match(/^\s*(\S+):\s+\S+\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+\d+\s+\d+\s+\d+\s+(\d+)/);
    return m ? { name: m[1], link: Number(m[2]), signal: Number(m[3]), retries: Number(m[5]) } : null;
  }).filter(Boolean);
}

// `ss -tulpnH`
function parseSs(text) {
  return text.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
    const m = l.match(/^(tcp|udp)\s+(\S+)\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s*(.*)$/);
    if (!m) return null;
    const proc = m[5].match(/\("([^"]+)",pid=(\d+)/);
    return { proto: m[1], state: m[2], local: m[3], process: proc ? proc[1] : '', pid: proc ? Number(proc[2]) : null };
  }).filter(Boolean);
}

// `ping -c N` summary
function parsePing(text) {
  const loss = text.match(/(\d+(?:\.\d+)?)% packet loss/);
  const rtt = text.match(/= ([\d.]+)\/([\d.]+)\/([\d.]+)/);
  return { loss: loss ? Number(loss[1]) : 100, min: rtt ? Number(rtt[1]) : null, avg: rtt ? Number(rtt[2]) : null, max: rtt ? Number(rtt[3]) : null };
}

// `ps -eo pid,user,pcpu,pmem,rss,etimes,stat,args --sort=-pcpu`
function parsePsFull(text) {
  return text.trim().split('\n').slice(1).map((l) => {
    const m = l.trim().match(/^(\d+)\s+(\S+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$/);
    return m ? { pid: Number(m[1]), user: m[2], cpu: Number(m[3]), mem: Number(m[4]), rss: Number(m[5]) * 1024, age: Number(m[6]), state: m[7], cmd: m[8] } : null;
  }).filter(Boolean);
}

// Merge key/value pairs into an existing sysctl.d file body, preserving unrelated lines.
function mergeSysctlConf(existing, entries) {
  const want = new Map(entries.map((e) => [e.key, e.value]));
  const lines = existing.split('\n').filter((l, i, a) => !(i === a.length - 1 && l === '')).map((l) => {
    const m = l.match(/^\s*([^#=\s]+)\s*=/);
    if (m && want.has(m[1])) { const v = want.get(m[1]); want.delete(m[1]); return `${m[1]} = ${v}`; }
    return l;
  });
  for (const [k, v] of want) lines.push(`${k} = ${v}`);
  return lines.join('\n') + '\n';
}

const SIGNALS = new Set(['SIGTERM', 'SIGKILL', 'SIGSTOP', 'SIGCONT', 'SIGHUP']);

// Single-quote for POSIX shells.
function shQuote(s) { return `'${String(s).replace(/'/g, `'\\''`)}'`; }

const SERVICE_ACTIONS = new Set(['start', 'stop', 'restart', 'enable', 'disable', 'reload']);
const UNIT_RE = /^(?!-)[A-Za-z0-9:_.@\\-]+\.(service|socket|timer|target|mount|path)$/;
const SYSCTL_KEY_RE = /^[a-z0-9_]+(\.[a-z0-9_\-]+)+$/i;
const SYSCTL_VALUE_RE = /^[\w \t.\-:,/]+$/;

module.exports = {
  parseCpuStat, cpuPercent, parseMeminfo, parseNetDev, parseDf, parseUnits, parseUpgradable,
  parsePs, parseWireless, parseSs, parsePing, parsePsFull, mergeSysctlConf, SIGNALS, parseJournal, parseSysctl, parseDrmModes, shQuote,
  SERVICE_ACTIONS, UNIT_RE, SYSCTL_KEY_RE, SYSCTL_VALUE_RE,
};
