#!/usr/bin/env node
'use strict';
// Install a verified .deb payload for this account without changing dpkg state.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const root = path.resolve(__dirname, '..');
const pkg = require(path.join(root, 'package.json'));
const arch = { x64: 'amd64', arm64: 'arm64' }[process.arch];
if (process.platform !== 'linux' || !arch) throw new Error('Install on Linux amd64 or arm64');
const artifact = path.resolve(process.argv[2] || path.join(root, 'release', `${pkg.name}_${pkg.version}_${arch}.deb`));
const name = execFileSync('dpkg-deb', ['-f', artifact, 'Package'], { encoding: 'utf8' }).trim();
const version = execFileSync('dpkg-deb', ['-f', artifact, 'Version'], { encoding: 'utf8' }).trim();
const artifactArch = execFileSync('dpkg-deb', ['-f', artifact, 'Architecture'], { encoding: 'utf8' }).trim();
if (name !== pkg.name || version !== pkg.version || artifactArch !== arch) throw new Error('Unexpected package, version, or architecture');
const hash = createHash('sha256').update(fs.readFileSync(artifact)).digest('hex');
const expected = fs.readFileSync(`${artifact}.sha256`, 'utf8').trim();
if (expected !== `${hash}  ${path.basename(artifact)}`) throw new Error('Package checksum does not match');
const namespaceSandbox = spawnSync('unshare', ['--user', '--map-root-user', 'true'], { stdio: 'ignore' }).status === 0;
const home = os.homedir(), base = path.join(home, '.local/opt/whitecube-command-center');
fs.mkdirSync(path.join(base, 'versions'), { recursive: true });
const destination = path.join(base, 'versions', `${version}-${hash.slice(0, 12)}`);
const scratch = fs.mkdtempSync(path.join(base, '.install-'));
const write = (file, data, mode = 0o644) => { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, data, { mode }); fs.chmodSync(file, mode); };
const quote = text => `'${text.replace(/'/g, `'\\''`)}'`;
function systemHelper(runtimeVersion) {
  for (const directory of ['/opt/whitecube-command-center', '/opt/ubuntu-command-center']) {
    try {
      const helper = fs.realpathSync(path.join(directory, 'chrome-sandbox'));
      const stat = fs.statSync(helper);
      const parent = fs.statSync(path.dirname(helper));
      if (stat.isFile() && stat.uid === 0 && (stat.mode & 0o7777) === 0o4755 && parent.uid === 0 && !(parent.mode & 0o022) && fs.readFileSync(path.join(directory, 'version'), 'utf8').trim() === runtimeVersion) return helper;
    } catch { /* not installed, or not a trusted matching helper */ }
  }
  throw new Error('User namespaces are unavailable and no matching root-owned sandbox helper is installed. Use the system .deb installer.');
}
try {
  execFileSync('dpkg-deb', ['--extract', artifact, scratch]);
  const payload = path.join(scratch, 'opt/whitecube-command-center');
  const runtimeVersion = fs.readFileSync(path.join(payload, 'version'), 'utf8').trim();
  const helper = namespaceSandbox ? null : systemHelper(runtimeVersion);
  fs.chmodSync(path.join(payload, 'chrome-sandbox'), 0o755);
  if (!fs.existsSync(destination)) fs.renameSync(payload, destination);
  if (helper) {
    const sandbox = path.join(destination, 'chrome-sandbox');
    fs.unlinkSync(sandbox);
    fs.symlinkSync(helper, sandbox);
  }
  const next = path.join(base, `current-${process.pid}`);
  fs.symlinkSync(path.relative(base, destination), next);
  fs.renameSync(next, path.join(base, 'current'));
  const launcher = path.join(home, '.local/bin/whitecube-command-center');
  // Check a trusted system helper at launch too: a later system package rename
  // may remove the legacy helper while installing the new one.
  const sandboxSetup = namespaceSandbox ? '' : `sandbox_helper=\nfor sandbox_dir in /opt/whitecube-command-center /opt/ubuntu-command-center; do\n if [ -f "$sandbox_dir/chrome-sandbox" ] && [ "$(stat -Lc '%u:%a' "$sandbox_dir/chrome-sandbox" 2>/dev/null)" = '0:4755' ] && [ "$(cat "$sandbox_dir/version" 2>/dev/null)" = ${quote(runtimeVersion)} ]; then\n  sandbox_helper="$sandbox_dir/chrome-sandbox"; break\n fi\ndone\n[ -n "$sandbox_helper" ] || { printf '%s\\n' 'Whitecube Command Center: matching system sandbox helper is missing; reinstall the system package.' >&2; exit 1; }\nln -sfn -- "$sandbox_helper" ${quote(path.join(base, 'current/chrome-sandbox'))} || exit 1\n`;
  write(launcher, `#!/bin/sh\ncase "$1" in\n --version) printf '%s\\n' ${quote(`Whitecube Command Center ${version} (user installation)`)}; exit 0;;\n --help) printf '%s\\n' 'Whitecube Command Center — Wayland desktop tools' 'Usage: whitecube-command-center [Electron options]' 'Compatibility aliases: command-center, ubuntu-lab.'; exit 0;;\nesac\nunset ELECTRON_RUN_AS_NODE DISPLAY CHROME_DEVEL_SANDBOX\nexport ELECTRON_OZONE_PLATFORM_HINT=wayland\n${sandboxSetup}exec ${quote(path.join(base, 'current/whitecube-command-center-bin'))}${namespaceSandbox ? ' --disable-setuid-sandbox' : ''} "$@"\n`, 0o755);
  for (const name of ['command-center', 'ubuntu-lab']) {
    const alias = path.join(home, '.local/bin', name);
    let owned = false;
    try {
      const stat = fs.lstatSync(alias);
      if (stat.isSymbolicLink()) owned = ['command-center', 'whitecube-command-center'].includes(fs.readlinkSync(alias));
      else if (stat.isFile()) owned = fs.readFileSync(alias, 'utf8').includes('.local/opt/ubuntu-command-center/current/command-center-bin');
    } catch (e) { if (e.code !== 'ENOENT') throw e; }
    if (owned) fs.unlinkSync(alias);
    if (!fs.existsSync(alias) && !fs.lstatSync(path.dirname(alias)).isSymbolicLink()) {
      try { fs.symlinkSync('whitecube-command-center', alias); } catch (e) { if (e.code !== 'EEXIST') throw e; }
    }
  }
  const desktop = fs.readFileSync(path.join(scratch, 'usr/share/applications/whitecube-command-center.desktop'), 'utf8')
    .replace('Exec=whitecube-command-center', `Exec="${launcher.replace(/(["`$\\])/g, '\\$1')}"`).replace('TryExec=whitecube-command-center', `TryExec=${launcher}`);
  write(path.join(home, '.local/share/applications/whitecube-command-center.desktop'), desktop);
  // Mask the old system desktop entry so menus show a single rebranded app.
  write(path.join(home, '.local/share/applications/ubuntu-command-center.desktop'), '[Desktop Entry]\nType=Application\nName=Whitecube Command Center (legacy entry)\nHidden=true\n');
  for (const [dir, ext] of [['256x256', 'png'], ['scalable', 'svg']]) {
    const name = `icons/hicolor/${dir}/apps/whitecube-command-center.${ext}`;
    write(path.join(home, '.local/share', name), fs.readFileSync(path.join(scratch, 'usr/share', name)));
  }
  write(path.join(base, 'installation.json'), JSON.stringify({ version, architecture: arch, artifact, sha256: hash, installedAt: new Date().toISOString(), scope: 'user', sandbox: namespaceSandbox ? 'user-namespace' : 'system-setuid-helper', sandboxHelper: helper, systemPackageRegistered: false }, null, 2) + '\n', 0o600);
  spawnSync('update-desktop-database', [path.join(home, '.local/share/applications')], { stdio: 'ignore' });
  spawnSync('gtk-update-icon-cache', ['-f', '-t', path.join(home, '.local/share/icons/hicolor')], { stdio: 'ignore' });
  console.log(`Installed Whitecube Command Center ${version} for this account: ${destination}\nLauncher: ${launcher}\nSandbox: ${namespaceSandbox ? 'user namespaces' : helper}\nSystem dpkg registration still requires root authentication.`);
} finally { fs.rmSync(scratch, { recursive: true, force: true }); }
