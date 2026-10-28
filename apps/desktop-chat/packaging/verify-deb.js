#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, ...options });
  if (result.error || result.status !== 0) throw new Error(`${command}: ${result.error?.message || result.stderr || result.status}`);
  return result.stdout;
}
function verifyDeb(filename) {
  filename = path.resolve(filename);
  assert.equal(run('dpkg-deb', ['-f', filename, 'Package']).trim(), 'whitecube-desktop-chat');
  const version = run('dpkg-deb', ['-f', filename, 'Version']).trim();
  const architecture = run('dpkg-deb', ['-f', filename, 'Architecture']).trim();
  assert.ok(['amd64', 'arm64'].includes(architecture), 'Unsupported package architecture');
  const listing = run('dpkg-deb', ['--contents', filename]);
  assert.match(listing, /-rwsr-xr-x root\/root\s+\d+ .*\.\/usr\/lib\/whitecube-desktop-chat\/runtime\/chrome-sandbox/);
  assert.doesNotMatch(listing, /\/node_modules\/|\/test\/|\/dist\/|\/\.git\/|\/config\.json$|\/\.env(?:\s|$)/m);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'whitecube-chat-verify-'));
  try {
    run('dpkg-deb', ['--extract', filename, directory]);
    const control = path.join(directory, 'control'); run('dpkg-deb', ['--control', filename, control]);
    const install = path.join(directory, 'usr/lib/whitecube-desktop-chat');
    const app = path.join(install, 'app');
    for (const file of ['main.js', 'preload.js', 'renderer/index.html', 'renderer/renderer.js', 'src/llm.js', 'assets/icon.png', 'local-model-server.py']) assert.ok(fs.statSync(path.join(app, file)).isFile(), `Missing ${file}`);
    assert.equal(require(path.join(app, 'package.json')).version, version);
    assert.equal(fs.readFileSync(path.join(app, 'VERSION'), 'utf8').trim(), version);
    assert.equal(fs.readlinkSync(path.join(directory, 'usr/bin/whitecube-chat')), '../lib/whitecube-desktop-chat/app/desktop-chat.sh');
    const runtime = JSON.parse(fs.readFileSync(path.join(directory, 'usr/share/doc/whitecube-desktop-chat/runtime.json'), 'utf8'));
    assert.equal(runtime.architecture, architecture); assert.equal(runtime.appVersion, version);
    assert.equal(fs.readFileSync(path.join(install, 'runtime/version'), 'utf8').trim().replace(/^v/, ''), runtime.electron);
    assert.ok(fs.statSync(path.join(install, 'runtime/electron')).mode & 0o111);
    const executable = fs.readFileSync(path.join(install, 'runtime/electron'));
    assert.equal(executable.toString('hex', 0, 4), '7f454c46');
    assert.equal(({ 62: 'amd64', 183: 'arm64' })[executable.readUInt16LE(18)], architecture);
    for (const line of fs.readFileSync(path.join(control, 'md5sums'), 'utf8').trim().split('\n')) {
      const match = line.match(/^([a-f\d]{32})  (usr\/.+)$/); assert.ok(match, 'Invalid md5sums entry');
      const file = path.resolve(directory, match[2]); assert.ok(file.startsWith(directory + path.sep));
      assert.equal(crypto.createHash('md5').update(fs.readFileSync(file)).digest('hex'), match[1], `Checksum mismatch: ${match[2]}`);
    }
    const launcher = path.join(directory, 'usr/bin/whitecube-chat');
    assert.match(run(launcher, ['--help'], { cwd: os.tmpdir(), env: { PATH: '/usr/bin:/bin' } }), /Usage: whitecube-chat/);
    assert.equal(run(launcher, ['--version'], { cwd: os.tmpdir(), env: { PATH: '/usr/bin:/bin' } }).trim(), `Desktop Chat ${version}`);
    const desktop = fs.readFileSync(path.join(directory, 'usr/share/applications/org.whitecube.DesktopChat.Standalone.desktop'), 'utf8');
    assert.match(desktop, /^Exec=whitecube-chat --show$/m);
    assert.match(desktop, /^Icon=org\.whitecube\.DesktopChat\.Standalone$/m);
    assert.ok(fs.statSync(path.join(directory, 'usr/share/icons/hicolor/256x256/apps/org.whitecube.DesktopChat.Standalone.png')).isFile());
    assert.ok(fs.statSync(path.join(install, 'runtime/LICENSES.chromium.html')).size > 1000);
    return { filename, version, architecture, electron: runtime.electron };
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
}
module.exports = { verifyDeb };
if (require.main === module) {
  try {
    const filename = process.argv[2];
    if (!filename) throw new Error('Usage: npm run verify:deb -- PATH_TO_DEB');
    const info = verifyDeb(filename);
    console.log(`Verified ${info.filename}\nVersion ${info.version}; ${info.architecture}; Electron ${info.electron}; payload checksums, sandbox, launcher and desktop integration passed.`);
  } catch (error) { console.error(`verify-deb: ${error.message}`); process.exitCode = 1; }
}
