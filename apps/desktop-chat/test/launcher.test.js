const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const launcher = path.resolve(__dirname, '..', 'desktop-chat.sh');

function environment(extra = {}) {
  return { ...process.env, PATH: '/usr/bin:/bin', DISPLAY: '', WAYLAND_DISPLAY: '', XDG_SESSION_TYPE: '', ELECTRON_OZONE_PLATFORM_HINT: '', DESKTOP_CHAT_OZONE_PLATFORM: '', DESKTOP_CHAT_RUNTIME: '', ...extra };
}
function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-chat-launch-'));
  const runtime = path.join(directory, 'electron');
  fs.writeFileSync(runtime, '#!/bin/sh\nprintf "%s\\0" "$PWD" "$ELECTRON_OZONE_PLATFORM_HINT" "$DISPLAY" "${ELECTRON_RUN_AS_NODE:-}" "$@" > "$DESKTOP_CHAT_LAUNCH_CAPTURE"\n', { mode: 0o755 });
  const capture = path.join(directory, 'argv');
  return { directory, runtime, capture, cleanup: () => fs.rmSync(directory, { recursive: true, force: true }) };
}
test('launcher works from another directory and preserves literal arguments', () => {
  const f = fixture();
  try {
    const result = spawnSync(launcher, ['--settings', 'literal $(touch should-not-exist)', ''], {
      cwd: f.directory,
      env: environment({ DESKTOP_CHAT_RUNTIME: f.runtime, DESKTOP_CHAT_LAUNCH_CAPTURE: f.capture, WAYLAND_DISPLAY: 'wayland-0', DISPLAY: ':1', ELECTRON_RUN_AS_NODE: '1' }), encoding: 'utf8'
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(fs.readFileSync(f.capture, 'utf8').split('\0'), [
      path.dirname(launcher), 'wayland', ':1', '', '--ozone-platform=wayland', path.dirname(launcher),
      '--settings', 'literal $(touch should-not-exist)', '', ''
    ]);
    assert.equal(fs.existsSync(path.join(path.dirname(launcher), 'should-not-exist')), false);
  } finally { f.cleanup(); }
});

test('launcher selects X11 and accepts an explicit platform override', () => {
  const f = fixture();
  try {
    for (const [extra, expected] of [[{ DISPLAY: ':42' }, 'x11'], [{}, 'auto'], [{ WAYLAND_DISPLAY: 'wayland-0', DESKTOP_CHAT_OZONE_PLATFORM: 'x11' }, 'x11']]) {
      const result = spawnSync(launcher, ['--show'], { env: environment({ DESKTOP_CHAT_RUNTIME: f.runtime, DESKTOP_CHAT_LAUNCH_CAPTURE: f.capture, ...extra }), encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      assert.equal(fs.readFileSync(f.capture, 'utf8').split('\0')[1], expected);
    }
    const invalid = spawnSync(launcher, [], { env: environment({ DESKTOP_CHAT_OZONE_PLATFORM: 'bad-platform' }), encoding: 'utf8' });
    assert.equal(invalid.status, 2); assert.match(invalid.stderr, /platform must/);
  } finally { f.cleanup(); }
});

test('existing Whitecube launcher finds private runtime without npm on PATH', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-chat-package-'));
  try {
    const app = path.join(directory, 'usr/share/whitecube/desktop-chat');
    const runtime = path.join(directory, 'usr/lib/whitecube/desktop-chat-runtime');
    const bin = path.join(directory, 'usr/bin');
    fs.mkdirSync(app, { recursive: true }); fs.mkdirSync(runtime, { recursive: true }); fs.mkdirSync(bin, { recursive: true });
    fs.copyFileSync(launcher, path.join(app, 'desktop-chat.sh')); fs.chmodSync(path.join(app, 'desktop-chat.sh'), 0o755);
    fs.symlinkSync('../share/whitecube/desktop-chat/desktop-chat.sh', path.join(bin, 'desktop-chat'));
    fs.writeFileSync(path.join(runtime, 'electron'), '#!/bin/sh\nprintf "%s\\n" "$ELECTRON_OZONE_PLATFORM_HINT" "$@"\n', { mode: 0o755 });
    const result = spawnSync(path.join(bin, 'desktop-chat'), ['--show'], { cwd: directory, env: environment({ WAYLAND_DISPLAY: 'wayland-0' }), encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(result.stdout.trim().split('\n'), ['wayland', '--ozone-platform=wayland', app, '--show']);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('standalone package launcher uses sibling runtime and help needs no Electron', () => {
  const f = fixture();
  try {
    const app = path.join(f.directory, 'usr/lib/whitecube-desktop-chat/app');
    const runtime = path.join(f.directory, 'usr/lib/whitecube-desktop-chat/runtime');
    const bin = path.join(f.directory, 'usr/bin');
    fs.mkdirSync(app, { recursive: true }); fs.mkdirSync(runtime, { recursive: true }); fs.mkdirSync(bin, { recursive: true });
    fs.copyFileSync(launcher, path.join(app, 'desktop-chat.sh')); fs.chmodSync(path.join(app, 'desktop-chat.sh'), 0o755);
    fs.writeFileSync(path.join(app, 'VERSION'), '1.0.0\n');
    fs.symlinkSync('../lib/whitecube-desktop-chat/app/desktop-chat.sh', path.join(bin, 'whitecube-chat'));
    const command = path.join(bin, 'whitecube-chat');
    for (const flag of ['--help', '--version']) {
      const result = spawnSync(command, [flag], { env: environment(), encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr); assert.match(result.stdout, /Desktop Chat|whitecube-chat/);
    }
    fs.copyFileSync(f.runtime, path.join(runtime, 'electron')); fs.chmodSync(path.join(runtime, 'electron'), 0o755);
    const result = spawnSync(command, ['--show'], { env: environment({ DISPLAY: ':1', DESKTOP_CHAT_LAUNCH_CAPTURE: f.capture }), encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(fs.readFileSync(f.capture, 'utf8').split('\0').slice(4), ['--ozone-platform=x11', app, '--show', '']);
  } finally { f.cleanup(); }
});

test('launcher --cli supports headless doctor, models, chats, and export without Electron or creating empty state', () => {
  const f = fixture();
  try {
    const userData = path.join(f.directory, 'user-data');
    const trainOut = path.join(f.directory, 'train-out');
    const cliEnv = {
      ...process.env,
      DISPLAY: '',
      WAYLAND_DISPLAY: '',
      XDG_SESSION_TYPE: '',
      NODE: process.execPath,
      DESKTOP_CHAT_USER_DATA: userData,
      WHITECUBE_TRAIN_OUT: trainOut
    };
    const emptyDoc = spawnSync(launcher, ['--cli', 'doctor', '--json'], { env: cliEnv, encoding: 'utf8' });
    assert.equal(emptyDoc.status, 0, emptyDoc.stderr);
    const parsedEmpty = JSON.parse(emptyDoc.stdout);
    assert.equal(parsedEmpty.ok, true);
    assert.equal(parsedEmpty.config.present, false);
    assert.equal(parsedEmpty.conversations.count, 0);
    assert.equal(fs.existsSync(userData), false);

    fs.mkdirSync(path.join(userData, 'conversations'), { recursive: true });
    fs.writeFileSync(path.join(userData, 'config.json'), JSON.stringify({
      endpoint: 'http://127.0.0.1:11434/v1',
      model: 'qwen-local',
      apiKeyPlain: 'secret-token',
      modelAliases: { 'run1/ckpt_best.pt': 'Best Local Run' }
    }));
    const id = '11111111-2222-3333-4444-555555555555';
    fs.writeFileSync(path.join(userData, 'conversations', `${id}.json`), JSON.stringify({
      id,
      title: 'CLI Export Test',
      customTitle: true,
      createdAt: '2026-03-01T10:00:00.000Z',
      updatedAt: '2026-03-01T10:01:00.000Z',
      source: { chatSource: 'api', endpoint: 'http://127.0.0.1:11434/v1', model: 'qwen-local', checkpointId: '' },
      messages: [{ role: 'user', content: 'Hello CLI needle' }, { role: 'assistant', content: 'Ready.' }],
      context: []
    }));
    fs.mkdirSync(path.join(trainOut, 'run1'), { recursive: true });
    fs.writeFileSync(path.join(trainOut, 'run1', 'run.json'), JSON.stringify({ display_name: 'Run 1', parameters: 1234 }));
    fs.writeFileSync(path.join(trainOut, 'run1', 'ckpt_best.pt'), 'stub');

    const popDoc = spawnSync(launcher, ['--cli', 'doctor', '--json'], { env: cliEnv, encoding: 'utf8' });
    assert.equal(popDoc.status, 0, popDoc.stderr);
    assert.equal(popDoc.stdout.includes('secret-token'), false);
    const parsedPop = JSON.parse(popDoc.stdout);
    assert.equal(parsedPop.config.hasApiKey, true);
    assert.equal(parsedPop.conversations.count, 1);
    assert.equal(parsedPop.checkpoints.count, 1);

    const models = spawnSync(launcher, ['--cli', 'models', '--json'], { env: cliEnv, encoding: 'utf8' });
    assert.equal(models.status, 0, models.stderr);
    assert.equal(JSON.parse(models.stdout)[0].name, 'Best Local Run');

    const chats = spawnSync(launcher, ['--cli', 'chats', '--query', 'needle', '--json'], { env: cliEnv, encoding: 'utf8' });
    assert.equal(chats.status, 0, chats.stderr);
    assert.equal(JSON.parse(chats.stdout).length, 1);

    const md = spawnSync(launcher, ['--cli', 'export', id, '--format', 'markdown'], { env: cliEnv, encoding: 'utf8' });
    assert.equal(md.status, 0, md.stderr);
    assert.match(md.stdout, /^# CLI Export Test/);
    assert.match(md.stdout, /Hello CLI needle/);

    // Verify Electron fallback when node/nodejs/NODE are unavailable sets ELECTRON_RUN_AS_NODE=1
    const noNodeBin = path.join(f.directory, 'no-node-bin');
    fs.mkdirSync(noNodeBin);
    for (const tool of ['sh', 'readlink', 'cat', 'sed']) {
      for (const dir of ['/bin', '/usr/bin']) {
        const candidate = path.join(dir, tool);
        if (fs.existsSync(candidate)) { fs.symlinkSync(candidate, path.join(noNodeBin, tool)); break; }
      }
    }
    fs.writeFileSync(f.runtime, '#!/bin/sh\nprintf "%s\\n" "$ELECTRON_RUN_AS_NODE" "$@" > "$DESKTOP_CHAT_LAUNCH_CAPTURE"\n', { mode: 0o755 });
    const fallback = spawnSync(launcher, ['--cli', '--version'], {
      env: environment({ PATH: noNodeBin, NODE: '', DESKTOP_CHAT_RUNTIME: f.runtime, DESKTOP_CHAT_LAUNCH_CAPTURE: f.capture }),
      encoding: 'utf8'
    });
    assert.equal(fallback.status, 0, fallback.stderr);
    assert.deepEqual(fs.readFileSync(f.capture, 'utf8').trim().split('\n'), [
      '1',
      path.join(path.dirname(launcher), 'src', 'cli.js'),
      '--version'
    ]);
  } finally { f.cleanup(); }
});
