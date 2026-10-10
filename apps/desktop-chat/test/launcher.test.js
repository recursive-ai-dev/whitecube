const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

test('launcher works from another directory and preserves literal arguments', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-chat-launch-'));
  try {
    const bin = path.join(directory, 'bin');
    fs.mkdirSync(bin);
    const capture = path.join(directory, 'argv');
    fs.writeFileSync(path.join(bin, 'electron'), '#!/bin/sh\nprintf "%s\\0" "$PWD" "$@" > "$DESKTOP_CHAT_LAUNCH_CAPTURE"\n', { mode: 0o755 });
    const launcher = path.resolve(__dirname, '..', 'desktop-chat.sh');
    const result = spawnSync(launcher, ['--settings', 'literal $(touch should-not-exist)', ''], {
      cwd: directory,
      env: { ...process.env, PATH: `${bin}:/usr/bin:/bin`, DESKTOP_CHAT_LAUNCH_CAPTURE: capture },
      encoding: 'utf8'
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(fs.readFileSync(capture, 'utf8').split('\0'), [
      path.dirname(launcher), '--ozone-platform=wayland', path.dirname(launcher),
      '--settings', 'literal $(touch should-not-exist)', '', ''
    ]);
    assert.equal(fs.existsSync(path.join(path.dirname(launcher), 'should-not-exist')), false);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('packaged launcher finds its private runtime without npm or nvm on PATH', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-chat-package-'));
  try {
    const app = path.join(directory, 'usr/share/whitecube/desktop-chat');
    const runtime = path.join(directory, 'usr/lib/whitecube/desktop-chat-runtime');
    const bin = path.join(directory, 'usr/bin');
    fs.mkdirSync(app, { recursive: true }); fs.mkdirSync(runtime, { recursive: true }); fs.mkdirSync(bin, { recursive: true });
    fs.copyFileSync(path.resolve(__dirname, '..', 'desktop-chat.sh'), path.join(app, 'desktop-chat.sh'));
    fs.chmodSync(path.join(app, 'desktop-chat.sh'), 0o755);
    fs.symlinkSync('../share/whitecube/desktop-chat/desktop-chat.sh', path.join(bin, 'desktop-chat'));
    fs.writeFileSync(path.join(runtime, 'electron'), '#!/bin/sh\nprintf "%s\\n" "$ELECTRON_OZONE_PLATFORM_HINT" "$@"\n', { mode: 0o755 });
    const result = spawnSync(path.join(bin, 'desktop-chat'), ['--show'], {
      cwd: directory, env: { ...process.env, PATH: '/usr/bin:/bin', DESKTOP_CHAT_RUNTIME: '' }, encoding: 'utf8'
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(result.stdout.trim().split('\n'), ['wayland', '--ozone-platform=wayland', app, '--show']);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
