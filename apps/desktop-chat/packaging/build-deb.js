#!/usr/bin/env node
'use strict';
// Explicit runtime allowlists keep development files and user data out of builds.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { verifyDeb } = require('./verify-deb');
const root = path.resolve(__dirname, '..');
const metadata = require('../package.json');

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: 'utf8', cwd: root, ...options });
  if (result.error || result.status !== 0) {
    throw new Error(`${command}: ${result.error?.message || result.stderr || `exited ${result.status}`}`);
  }
  return result.stdout;
}
function main() {
  const args = process.argv.slice(2);
  let out = path.join(root, 'dist'), tests = true;
  while (args.length) {
    const flag = args.shift();
    if (flag === '--no-tests') tests = false;
    else if (flag === '--output-directory' && args[0]) out = path.resolve(args.shift());
    else if (flag === '--help' || flag === '-h') {
      console.log('Usage: npm run deb -- [--no-tests] [--output-directory PATH]\nBuild a standalone Debian package; nothing is installed. Tests run by default.\nUse ELECTRON_DIST to supply the exact pinned Electron distribution instead of node_modules.');
      return;
    } else throw new Error(`Unknown or incomplete option: ${flag}`);
  }
  if (process.platform !== 'linux') throw new Error('Build on Linux with dpkg-deb installed.');
  for (const command of ['dpkg', 'dpkg-deb']) run(command, ['--version']);
  const version = fs.readFileSync(path.join(__dirname, 'VERSION'), 'utf8').trim();
  if (version !== metadata.version) throw new Error('package.json and packaging/VERSION differ.');
  run('dpkg', ['--validate-version', version]);
  const runtime = path.resolve(process.env.ELECTRON_DIST || path.join(root, 'node_modules/electron/dist'));
  for (const file of ['electron', 'chrome-sandbox', 'LICENSE', 'LICENSES.chromium.html', 'version']) {
    if (!fs.statSync(path.join(runtime, file), { throwIfNoEntry: false })?.isFile()) {
      throw new Error(`Missing runtime file ${runtime}/${file}. Run npm ci first.`);
    }
  }
  const electronVersion = fs.readFileSync(path.join(runtime, 'version'), 'utf8').trim().replace(/^v/, '');
  if (electronVersion !== metadata.devDependencies.electron) throw new Error(`Electron ${electronVersion} differs from pinned ${metadata.devDependencies.electron}.`);
  const executable = fs.readFileSync(path.join(runtime, 'electron'));
  if (executable.toString('hex', 0, 4) !== '7f454c46' || executable[5] !== 1) throw new Error('Expected a Linux little-endian ELF runtime.');
  const architecture = ({ 62: 'amd64', 183: 'arm64' })[executable.readUInt16LE(18)];
  if (!architecture) throw new Error('Only amd64 and arm64 Electron packages are supported.');
  if (tests) {
    const result = spawnSync('npm', ['test'], { cwd: root, stdio: 'inherit' });
    if (result.status !== 0) throw new Error('Tests failed; package was not built.');
  }
  fs.mkdirSync(out, { recursive: true });
  const work = fs.mkdtempSync(path.join(out, '.build-'));
  try {
    const stage = path.join(work, 'stage');
    const app = path.join(stage, 'usr/lib/whitecube-desktop-chat/app');
    const bundled = path.join(stage, 'usr/lib/whitecube-desktop-chat/runtime');
    const copy = (source, target, mode = 0o644) => {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(source, target); fs.chmodSync(target, mode);
    };
    fs.mkdirSync(app, { recursive: true });
    for (const file of ['main.js', 'preload.js', 'package.json', 'local-model-server.py']) copy(path.join(root, file), path.join(app, file));
    copy(path.join(root, 'desktop-chat.sh'), path.join(app, 'desktop-chat.sh'), 0o755);
    copy(path.join(__dirname, 'VERSION'), path.join(app, 'VERSION'));
    const copyTree = (source, destination) => {
      fs.mkdirSync(destination, { recursive: true });
      for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
        if (entry.isSymbolicLink()) throw new Error(`Refusing symlink in runtime tree: ${entry.name}`);
        if (entry.isDirectory()) {
          if (entry.name !== '__pycache__') copyTree(path.join(source, entry.name), path.join(destination, entry.name));
        } else if (entry.isFile()) copy(path.join(source, entry.name), path.join(destination, entry.name));
      }
    };
    for (const directory of ['src', 'renderer', 'assets']) copyTree(path.join(root, directory), path.join(app, directory));
    fs.cpSync(runtime, bundled, { recursive: true, dereference: false });
    // Clear developer umasks and preserve only the binaries' executable bit.
    function normalize(directory) {
      fs.chmodSync(directory, 0o755);
      for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
        const filename = path.join(directory, item.name);
        if (item.isDirectory()) normalize(filename);
        else if (item.isSymbolicLink()) {
          const target = fs.readlinkSync(filename);
          if (path.isAbsolute(target) || path.relative(bundled, path.resolve(directory, target)).startsWith('..')) throw new Error(`Runtime symlink escapes bundle: ${filename}`);
        } else fs.chmodSync(filename, fs.statSync(filename).mode & 0o111 ? 0o755 : 0o644);
      }
    }
    normalize(bundled);
    fs.chmodSync(path.join(bundled, 'electron'), 0o755);
    fs.chmodSync(path.join(bundled, 'chrome-sandbox'), 0o4755);
    const bin = path.join(stage, 'usr/bin'); fs.mkdirSync(bin, { recursive: true });
    fs.symlinkSync('../lib/whitecube-desktop-chat/app/desktop-chat.sh', path.join(bin, 'whitecube-chat'));
    copy(path.join(__dirname, 'org.whitecube.DesktopChat.Standalone.desktop'), path.join(stage, 'usr/share/applications/org.whitecube.DesktopChat.Standalone.desktop'));
    copy(path.join(root, 'assets/icon.png'), path.join(stage, 'usr/share/icons/hicolor/256x256/apps/org.whitecube.DesktopChat.Standalone.png'));
    const doc = path.join(stage, 'usr/share/doc/whitecube-desktop-chat');
    for (const file of ['LICENSE', 'NOTICE']) copy(path.join(root, '../..', file), path.join(doc, file));
    if (fs.existsSync(path.join(root, 'README.md'))) copy(path.join(root, 'README.md'), path.join(doc, 'README.md'));
    copy(path.join(__dirname, 'copyright'), path.join(doc, 'copyright'));
    fs.writeFileSync(path.join(doc, 'runtime.json'), JSON.stringify({ electron: electronVersion, architecture, appVersion: version }, null, 2) + '\n');
    const debian = path.join(stage, 'DEBIAN'); fs.mkdirSync(debian);
    let size = 0;
    const files = [];
    function walk(directory, callback) {
      callback(directory);
      if (fs.lstatSync(directory).isDirectory()) for (const file of fs.readdirSync(directory).sort()) walk(path.join(directory, file), callback);
    }
    walk(path.join(stage, 'usr'), file => {
      const stat = fs.lstatSync(file); size += Math.max(1, Math.ceil(stat.size / 1024));
      if (stat.isFile()) files.push(file);
    });
    fs.writeFileSync(path.join(debian, 'control'), `Package: whitecube-desktop-chat\nVersion: ${version}\nSection: utils\nPriority: optional\nArchitecture: ${architecture}\nMaintainer: Whitecube contributors <recursive-ai-dev@users.noreply.github.com>\nInstalled-Size: ${size}\nDepends: libc6 (>= 2.35), libgcc-s1, libstdc++6, libglib2.0-0t64 | libglib2.0-0, libnss3, libnspr4, libatk1.0-0t64 | libatk1.0-0, libatk-bridge2.0-0t64 | libatk-bridge2.0-0, libdrm2, libcups2t64 | libcups2, libudev1, libatspi2.0-0t64 | libatspi2.0-0, libatomic1, libdbus-1-3, libx11-6, libxcb1, libxcomposite1, libxdamage1, libxext6, libxfixes3, libxrandr2, libgbm1, libexpat1, libpango-1.0-0, libcairo2, libasound2t64 | libasound2, libgtk-3-0t64 | libgtk-3-0, libxkbcommon0\nRecommends: ca-certificates\nSuggests: python3\nHomepage: https://github.com/recursive-ai-dev/whitecube\nDescription: Desktop AI workspace with saved chats and local models\n Chat with OpenAI-compatible endpoints, keep conversations, export transcripts\n and use approved filesystem tools. Includes a private Electron runtime and\n the whitecube-chat launcher for both Wayland and X11 desktops.\n Local trained checkpoints optionally use the Whitecube training runtime.\n`);
    fs.writeFileSync(path.join(debian, 'triggers'), 'activate-noawait /usr/share/applications\nactivate-noawait /usr/share/icons/hicolor\n');
    fs.writeFileSync(path.join(debian, 'md5sums'), files.sort().map(file => `${crypto.createHash('md5').update(fs.readFileSync(file)).digest('hex')}  ${path.relative(stage, file)}\n`).join(''));
    for (const file of fs.readdirSync(debian)) fs.chmodSync(path.join(debian, file), 0o644);
    const epoch = process.env.SOURCE_DATE_EPOCH || run('git', ['log', '-1', '--format=%ct']).trim();
    if (!/^\d+$/.test(epoch)) throw new Error('SOURCE_DATE_EPOCH must be an integer.');
    const timestamp = new Date(Number(epoch) * 1000);
    walk(stage, file => {
      if (fs.lstatSync(file).isDirectory()) fs.chmodSync(file, 0o755);
      fs.lutimesSync(file, timestamp, timestamp);
    });
    const temporary = path.join(work, 'package.deb');
    run('dpkg-deb', ['--root-owner-group', '--threads-max=2', '-Zxz', '-z6', '--build', stage, temporary], { env: { ...process.env, SOURCE_DATE_EPOCH: epoch } });
    verifyDeb(temporary);
    const filename = `whitecube-desktop-chat_${version}_${architecture}.deb`;
    const target = path.join(out, filename);
    fs.renameSync(temporary, target); fs.chmodSync(target, 0o644);
    fs.writeFileSync(`${target}.sha256`, `${crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex')}  ${filename}\n`, { mode: 0o644 });
    fs.chmodSync(`${target}.sha256`, 0o644);
    console.log(`Built ${target}\nInstall with: sudo apt install '${target.replace(/'/g, `'\\''`)}'\nLaunch with: whitecube-chat`);
  } finally { fs.rmSync(work, { recursive: true, force: true }); }
}
if (require.main === module) {
  try { main(); } catch (error) { console.error(`build-deb: ${error.message}`); process.exitCode = 1; }
}
