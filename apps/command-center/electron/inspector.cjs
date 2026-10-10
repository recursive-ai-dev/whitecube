'use strict';
const fs = require('node:fs');
const S = require('./system.cjs');
const P = require('./parse.cjs');

function parseBlocks(text) {
  const data = JSON.parse(text);
  if (!Array.isArray(data.blockdevices)) throw new Error('Unexpected lsblk response');
  const flatten = (items, depth = 0) => items.flatMap(d => [{
    name: String(d.name || ''), path: String(d.path || ''), type: String(d.type || ''),
    model: String(d.model || '').trim(), size: Number(d.size) || 0, fs: d.fstype || '',
    mounts: (d.mountpoints || []).filter(v => typeof v === 'string'),
    readOnly: d.ro === true || d.ro === 1 || d.ro === '1', removable: d.rm === true || d.rm === 1 || d.rm === '1', depth,
  }, ...flatten(Array.isArray(d.children) ? d.children : [], depth + 1)]);
  return flatten(data.blockdevices);
}

async function storage() {
  const [blocks, space, inode] = await Promise.all([
    S.run('lsblk', ['--json', '--bytes', '--output', 'NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,RO,RM']),
    S.run('df', ['-PkT', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'squashfs']),
    S.run('df', ['-Pi', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'squashfs']),
  ]);
  const warnings = [];
  let devices = [];
  try { if (!blocks.ok) throw new Error(blocks.stderr || 'lsblk unavailable'); devices = parseBlocks(blocks.stdout); }
  catch (e) { warnings.push(e.message); }
  if (!space.ok) warnings.push(space.stderr || 'Filesystem usage unavailable');
  if (!inode.ok) warnings.push(inode.stderr || 'Inode usage unavailable');
  const inodes = Object.fromEntries(inode.stdout.split('\n').slice(1).map(l => l.trim().match(/^\S+\s+\S+\s+\S+\s+\S+\s+(\d+)%\s+(.+)$/)).filter(Boolean).map(m => [m[2], Number(m[1])]));
  return { at: Date.now(), devices, warnings, filesystems: P.parseDf(space.stdout).map(d => ({ ...d, size: d.size * 1024, used: d.used * 1024, avail: d.avail * 1024, inodePct: inodes[d.mount] ?? null })) };
}

function parsePackages(text) {
  return text.split('\n').filter(Boolean).map(line => {
    const [name, version, arch, size, status, ...description] = line.split('\t');
    return { name, version, arch, bytes: (Number(size) || 0) * 1024, status, description: description.join(' ') };
  }).filter(p => p.status === 'ii ');
}
async function packages() {
  const r = await S.run('dpkg-query', ['-W', '-f=${binary:Package}\t${Version}\t${Architecture}\t${Installed-Size}\t${db:Status-Abbrev}\t${binary:Summary}\n']);
  if (!r.ok) throw new Error(r.stderr.trim() || 'dpkg-query is unavailable');
  return { at: Date.now(), packages: parsePackages(r.stdout) };
}

async function diagnostics() {
  const [disk, services, audit, system] = await Promise.all([
    storage(), S.listServices('system'), S.run('dpkg', ['--audit'], { timeout: 10000 }),
    S.run('systemctl', ['is-system-running']),
  ]);
  const checks = [];
  const add = (id, title, status, detail) => checks.push({ id, title, status, detail });
  const failed = services.units?.filter(u => u.active === 'failed') || [];
  add('services', 'System services', services.error ? 'unknown' : failed.length ? 'warning' : 'ok', services.error || (failed.length ? failed.map(u => u.unit).join(', ') : 'No failed service units.'));
  for (const d of disk.filesystems) {
    const pct = Math.max(d.pct, d.inodePct || 0);
    add(`disk:${d.mount}`, `Storage · ${d.mount}`, pct >= 95 ? 'critical' : pct >= 85 ? 'warning' : 'ok', `${d.pct}% capacity used${d.inodePct === null ? '' : ` · ${d.inodePct}% inodes used`}`);
  }
  if (!disk.filesystems.length) add('disk', 'Storage', 'unknown', disk.warnings.join('; ') || 'No filesystem data available.');
  add('packages', 'Package consistency', !audit.ok ? 'unknown' : audit.stdout.trim() ? 'warning' : 'ok', (audit.stdout || audit.stderr).trim() || 'No incomplete package installations reported.');
  const reboot = fs.existsSync('/run/reboot-required');
  add('reboot', 'Restart requirement', reboot ? 'warning' : 'ok', reboot ? 'The system requests a reboot to complete updates.' : 'No reboot-required marker present.');
  const state = system.stdout.trim();
  add('system', 'System state', state === 'running' ? 'ok' : state === 'degraded' ? 'warning' : 'unknown', state || system.stderr.trim() || 'systemd state unavailable');
  return { at: Date.now(), host: S.hostInfo(), checks };
}

module.exports = { storage, packages, diagnostics, parseBlocks, parsePackages };
