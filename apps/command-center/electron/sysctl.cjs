'use strict';
const fs = require('node:fs');
const path = require('node:path');
const S = require('./system.cjs');
const P = require('./parse.cjs');

const MAX_BACKUPS = 20;
const CONF = '/etc/sysctl.d/90-command-center.conf';
let backupFile = null;
const init = (userDataDir) => { backupFile = path.join(userDataDir, 'sysctl-backups.json'); };

function loadBackups() {
  try { const value = JSON.parse(fs.readFileSync(backupFile, 'utf8')); return Array.isArray(value) ? value : []; } catch { return []; }
}

async function list() {
  const r = await S.run('sysctl', ['-a'], { timeout: 10000 });
  if (!r.stdout) throw new Error((r.stderr || 'sysctl failed').trim().split('\n')[0]);
  const managed = new Set(P.parseSysctl(S.read(CONF).split('\n').filter(l => !/^\s*#/.test(l)).join('\n')).map(e => e.key));
  return P.parseSysctl(r.stdout).map((e) => ({ ...e, sourceFile: managed.has(e.key) ? CONF : '', isManaged: managed.has(e.key) }));
}

function validate(entries) {
  if (!Array.isArray(entries) || !entries.length) throw new Error('No changes to apply');
  for (const e of entries) {
    if (!e || typeof e.key !== 'string' || typeof e.value !== 'string' || !P.SYSCTL_KEY_RE.test(e.key) || !P.SYSCTL_VALUE_RE.test(e.value)) throw new Error('Invalid kernel setting');
  }
  if (new Set(entries.map(e => e.key)).size !== entries.length) throw new Error('Duplicate kernel setting');
}

// Live apply only (`sysctl -w`), so a bad value is undone by a reboot or by restoring a backup.
async function apply(entries, persist = false) {
  validate(entries);
  const before = await list();
  const old = new Map(before.map((e) => [e.key, e.value]));
  const backup = { timestamp: Date.now(), entries: entries.filter((e) => old.has(e.key)).map((e) => ({ key: e.key, value: old.get(e.key) })) };
  if (backup.entries.length !== entries.length) throw new Error('One or more kernel settings no longer exist. Refresh before applying.');
  // A multi-key sysctl call may partially apply before failing. Save recovery data first.
  fs.mkdirSync(path.dirname(backupFile), { recursive: true, mode: 0o700 });
  fs.writeFileSync(`${backupFile}.tmp`, JSON.stringify([backup, ...loadBackups()].slice(0, MAX_BACKUPS)), { mode: 0o600 });
  fs.renameSync(`${backupFile}.tmp`, backupFile);
  const r = await S.sudo(['sysctl', '-w', ...entries.map((e) => `${e.key}=${e.value}`)]);
  if (r.needsSudo) throw new Error(`Passwordless sudo is required. Run in a terminal: ${r.hint}`);
  if (!r.ok) throw new Error((r.stderr || 'sysctl -w failed').trim());
  let note = '';
  if (persist) {
    let existing = '';
    try { existing = fs.readFileSync(CONF, 'utf8'); } catch { /* new file */ }
    const w = await S.run('sudo', ['-n', 'tee', CONF], { input: P.mergeSysctlConf(existing, entries) });
    note = w.ok ? ` (saved to ${CONF})` : ` (applied, but could not persist: ${(w.stderr || 'sudo tee failed').trim()})`;
  }
  return r.stdout.trim() + note;
}

const backups = () => loadBackups();

async function restore(timestamp) {
  const b = loadBackups().find((x) => x.timestamp === timestamp);
  if (!b) throw new Error('Backup not found');
  validate(b.entries);
  const r = await S.sudo(['sysctl', '-w', ...b.entries.map((e) => `${e.key}=${e.value}`)]);
  if (r.needsSudo) throw new Error(`Passwordless sudo is required. Run in a terminal: ${r.hint}`);
  if (!r.ok) throw new Error((r.stderr || 'restore failed').trim());
  return r.stdout.trim();
}

module.exports = { init, list, apply, backups, restore };
