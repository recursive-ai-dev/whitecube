#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const root = path.resolve(__dirname, '..');
const pkg = require(path.join(root, 'package.json'));
const arch = { x64: 'amd64', arm64: 'arm64' }[process.arch];
if (process.platform !== 'linux' || !arch) throw new Error('Build on Linux amd64 or arm64');
const runtime = path.join(root, 'node_modules/electron/dist');
for (const f of ['dist/index.html', 'assets/whitecube-command-center.png', 'node_modules/electron/dist/electron']) {
  if (!fs.existsSync(path.join(root, f))) throw new Error(`Missing ${f}. Run npm run setup:electron and npm run build.`);
}
const stage = path.join(root, '.build/deb-stage');
fs.rmSync(stage, { recursive: true, force: true });
const appDir = path.join(stage, 'opt/whitecube-command-center');
fs.mkdirSync(appDir, { recursive: true });
fs.cpSync(runtime, appDir, { recursive: true });
fs.renameSync(path.join(appDir, 'electron'), path.join(appDir, 'whitecube-command-center-bin'));
fs.rmSync(path.join(appDir, 'resources/default_app.asar'), { force: true });
const source = path.join(appDir, 'resources/app');
fs.mkdirSync(source, { recursive: true });
for (const dir of ['dist', 'assets']) fs.cpSync(path.join(root, dir), path.join(source, dir), { recursive: true });
fs.mkdirSync(path.join(source, 'electron'));
for (const name of fs.readdirSync(path.join(root, 'electron')).filter(n => n.endsWith('.cjs'))) fs.copyFileSync(path.join(root, 'electron', name), path.join(source, 'electron', name));
fs.writeFileSync(path.join(source, 'package.json'), JSON.stringify({ name: pkg.name, productName: pkg.productName, version: pkg.version, main: pkg.main, description: pkg.description }, null, 2));
const write = (name, content, mode = 0o644) => { const file = path.join(stage, name); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, content, { mode }); };
write('usr/bin/whitecube-command-center', `#!/bin/sh\ncase "$1" in\n  --version) printf '%s\\n' 'Whitecube Command Center ${pkg.version}'; exit 0;;\n  --help) printf '%s\\n' 'Whitecube Command Center — Wayland desktop tools' 'Usage: whitecube-command-center [Electron options]' 'Compatibility aliases: command-center, ubuntu-lab.'; exit 0;;\nesac\nunset ELECTRON_RUN_AS_NODE DISPLAY\nexport ELECTRON_OZONE_PLATFORM_HINT=wayland\nexec /opt/whitecube-command-center/whitecube-command-center-bin "$@"\n`, 0o755);
for (const alias of ['command-center', 'ubuntu-lab']) fs.symlinkSync('whitecube-command-center', path.join(stage, 'usr/bin', alias));
write('usr/share/applications/whitecube-command-center.desktop', `[Desktop Entry]\nType=Application\nName=Whitecube Command Center\nGenericName=System Monitor and Command Library\nComment=Monitor, diagnose, and manage your Whitecube desktop\nExec=whitecube-command-center\nTryExec=whitecube-command-center\nIcon=whitecube-command-center\nTerminal=false\nCategories=System;Monitor;\nKeywords=whitecube;ubuntu;debian;system;terminal;services;diagnostics;\nStartupNotify=true\nStartupWMClass=whitecube-command-center\n`);
for (const ext of ['png', 'svg']) { const dest = `usr/share/icons/hicolor/${ext === 'png' ? '256x256/apps' : 'scalable/apps'}/whitecube-command-center.${ext}`; write(dest, fs.readFileSync(path.join(root, `assets/whitecube-command-center.${ext}`))); }
write('usr/share/doc/whitecube-command-center/README.md', fs.readFileSync(path.join(root, 'README.md')));
write('usr/share/doc/whitecube-command-center/IMPROVEMENTS.txt', fs.readFileSync(path.join(root, 'IMPROVEMENTS.txt')));
write('usr/share/doc/whitecube-command-center/changelog', fs.readFileSync(path.join(root, 'CHANGELOG.md')));
write('usr/share/doc/whitecube-command-center/copyright', 'Whitecube Command Center application source is supplied by its project owner.\nElectron and bundled third-party notices: /opt/whitecube-command-center/LICENSE and LICENSES.chromium.html.\nUI dependency notices are included in third-party-licenses.txt.\n');
const notices = [];
for (const dependency of Object.keys(pkg.dependencies)) {
  const directory = path.join(root, 'node_modules', dependency);
  const licenses = fs.readdirSync(directory).filter(name => /^(LICENSE|OFL|COPYING)(\.|$)/i.test(name));
  for (const file of licenses) notices.push(`${dependency} — ${file}\n${fs.readFileSync(path.join(directory, file), 'utf8')}`);
}
write('usr/share/doc/whitecube-command-center/third-party-licenses.txt', notices.join('\n\n'));
// Runtime libraries, including X11 client libraries linked by Chromium even when
// the app uses only Wayland. Installing them does not start an X server.
const depends = [
  'libc6 (>= 2.31)', 'libstdc++6', 'libgcc-s1', 'libgtk-3-0t64 | libgtk-3-0', 'libnss3', 'libnspr4',
  'libatk1.0-0t64 | libatk1.0-0', 'libatk-bridge2.0-0t64 | libatk-bridge2.0-0',
  'libcups2t64 | libcups2', 'libdrm2', 'libdbus-1-3', 'libx11-6', 'libxcb1', 'libxcomposite1',
  'libxdamage1', 'libxext6', 'libxfixes3', 'libxrandr2', 'libgbm1', 'libxkbcommon0',
  'libasound2t64 | libasound2', 'libpango-1.0-0', 'libcairo2', 'libudev1', 'libwayland-client0',
  'libatomic1', 'libexpat1', 'libglib2.0-0t64 | libglib2.0-0',
  'procps', 'iproute2', 'util-linux', 'coreutils', 'bash', 'apt', 'dpkg',
];
const size = Number(execFileSync('du', ['-sk', appDir], { encoding: 'utf8' }).split(/\s/)[0]);
write('DEBIAN/control', `Package: whitecube-command-center\nVersion: ${pkg.version}\nSection: utils\nPriority: optional\nArchitecture: ${arch}\nMaintainer: Whitecube Project <whitecube@localhost>\nInstalled-Size: ${size}\nDepends: ${depends.join(', ')}\nRecommends: iputils-ping, sudo, xdg-utils\nProvides: ubuntu-command-center, ubuntu-lab\nConflicts: ubuntu-command-center\nReplaces: ubuntu-command-center\nDescription: Whitecube Wayland desktop command center for Ubuntu and Debian\n Live monitoring, storage inspection, systemd and journal tools, package\n inventory, diagnostics, command history, and portable command libraries.\n Includes its Electron runtime; Node.js is not required to run the app.\n`);
// Only the Chromium sandbox helper is setuid. dpkg assigns its owner to root.
// Normalize staging permissions even when the caller uses a private umask.
function permissions(dir) {
  fs.chmodSync(dir, 0o755);
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) permissions(file);
    else if (entry.isFile()) fs.chmodSync(file, fs.statSync(file).mode & 0o111 ? 0o755 : 0o644);
  }
}
permissions(stage);
fs.chmodSync(path.join(appDir, 'chrome-sandbox'), 0o4755);
const release = path.join(root, 'release'); fs.mkdirSync(release, { recursive: true });
const output = path.join(release, `whitecube-command-center_${pkg.version}_${arch}.deb`);
execFileSync('dpkg-deb', ['--root-owner-group', '-Zxz', '-z3', '--build', stage, output], { stdio: 'inherit' });
const hash = createHash('sha256').update(fs.readFileSync(output)).digest('hex');
fs.writeFileSync(`${output}.sha256`, `${hash}  ${path.basename(output)}\n`);
console.log(`Built ${output}`);
