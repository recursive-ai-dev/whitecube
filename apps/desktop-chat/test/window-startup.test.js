const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const os = require('node:os');
const directories = [];
test.after(() => { for (const directory of directories) fs.rmSync(directory, { recursive: true, force: true }); });

async function start(args, overrides = {}) {
  const windows = [];
  class Window extends EventEmitter {
    constructor() {
      super();
      this.visible = false;
      this.sent = [];
      this.webContents = new EventEmitter();
      this.webContents.send = (channel) => this.sent.push(channel);
      windows.push(this);
    }
    loadFile() {}
    show() { this.visible = true; }
    hide() { this.visible = false; }
    isVisible() { return this.visible; }
    getSize() { return [760, 640]; }
    setPosition() {}
    focus() {}
  }
  class Tray extends EventEmitter {
    setToolTip() {}
    setContextMenu() {}
  }
  const app = new EventEmitter();
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-chat-main-'));
  directories.push(directory);
  app.getPath = () => directory;
  app.commandLine = { appendSwitch() {} };
  app.requestSingleInstanceLock = data => { app.launchData = data; return true; };
  app.whenReady = () => Promise.resolve();
  app.quit = () => {};
  const ipcMain = new EventEmitter();
  const handlers = new Map();
  const settings = { endpoint: 'https://example.com/v1', model: 'test-model', hotkey: 'Control+Shift+T', apiKey: '', chatSource: 'api', checkpointId: '', modelAliases: {}, ...overrides.settings };
  const rawHandlers = new Map();
  ipcMain.handle = (name, callback) => {
    rawHandlers.set(name, callback);
    handlers.set(name, (event = {}, ...args) => {
      if (event.sender) windows[0].webContents.send = event.sender.send;
      return callback({ ...event, sender: windows[0].webContents }, ...args);
    });
  };
  const electron = {
    app, BrowserWindow: Window, Tray, ipcMain, clipboard: {},
    Menu: { buildFromTemplate: (value) => value },
    globalShortcut: { register: () => true, unregisterAll() {} },
    screen: {
      getCursorScreenPoint: () => ({ x: 0, y: 0 }),
      getDisplayNearestPoint: () => ({ workArea: { x: 0, y: 0, width: 1280 } })
    },
    nativeImage: { createFromPath: () => ({ isEmpty: () => false }) }
  };
  const context = vm.createContext({
    require(name) {
      if (name === 'electron') return electron;
      if (name === 'path') return path;
      if (name === 'node:fs') return fs;
      if (name === './src/history') return require('../src/history');
      if (name === './src/config') return { load: () => settings, save: value => Object.assign(settings, value), configPath: () => '/test/config.json' };
      if (name === './src/local-models') return { LocalModels: class {
        constructor() { this.directory = '/test/train-out'; Object.assign(this, overrides.localModels); }
        list() { return [{ id: 'run/ckpt_best.pt', name: 'Best' }]; }
        find(id) { const item = this.list().find(item => item.id === id); if (!item) throw new Error('Checkpoint unavailable'); return item; }
        stop() {}
      } };
      if (name === './src/llm') return { runAgentTurn: overrides.runAgentTurn || (() => { throw new Error('No network expected'); }) };
      if (name === './src/endpoint') return require('../src/endpoint');
      throw new Error(`Unexpected import: ${name}`);
    },
    __dirname: path.join(__dirname, '..'),
    process: { argv: ['electron', 'desktop-chat', ...args], env: {} },
    console, setTimeout, clearTimeout, AbortController, fetch: overrides.fetch
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8'), context);
  await new Promise((resolve) => setImmediate(resolve));
  return { app, win: windows[0], handlers, rawHandlers, ipcMain, directory };
}

test('first-launch settings wait for renderer listeners', async () => {
  const { win } = await start(['--settings']);
  assert.equal(win.isVisible(), false);
  assert.deepEqual(win.sent, []);
  win.webContents.emit('did-finish-load');
  assert.equal(win.isVisible(), true);
  assert.deepEqual(win.sent, ['panel:shown', 'settings:open']);
});

test('settings on an already loaded instance show immediately', async () => {
  const { app, win } = await start(['--hide']);
  win.webContents.emit('did-finish-load');
  app.emit('second-instance', {}, ['electron', 'desktop-chat', '--settings']);
  assert.equal(win.isVisible(), true);
  assert.deepEqual(win.sent, ['panel:shown', 'settings:open']);
});

test('hide cancels a pending first-launch settings request', async () => {
  const { app, win } = await start(['--settings']);
  app.emit('second-instance', {}, ['electron', 'desktop-chat', '--hide']);
  win.webContents.emit('did-finish-load');
  assert.equal(win.isVisible(), false);
  assert.deepEqual(win.sent, []);
});

test('a second toggle during loading cancels the pending show', async () => {
  const { app, win } = await start(['--toggle']);
  app.emit('second-instance', {}, ['electron', 'desktop-chat', '--toggle']);
  win.webContents.emit('did-finish-load');
  assert.equal(win.isVisible(), false);
  assert.deepEqual(win.sent, []);
});

test('ordinary app-menu launch shows a window', async () => {
  const { win } = await start([]);
  win.webContents.emit('did-finish-load');
  assert.equal(win.isVisible(), true);
});

test('trainer checkpoint launches select the model on first and existing instances', async () => {
  const first = await start(['--show', '--checkpoint', 'run/ckpt_best.pt']);
  first.win.webContents.emit('did-finish-load');
  assert.equal(first.handlers.get('settings:get')().checkpointId, 'run/ckpt_best.pt');
  assert.equal(first.handlers.get('settings:get')().chatSource, 'local');
  assert.equal(first.app.launchData.whitecubeArgs.at(-1), 'run/ckpt_best.pt');
  const existing = await start(['--hide']);
  existing.win.webContents.emit('did-finish-load');
  existing.app.emit('second-instance', {}, ['electron', 'desktop-chat', '--checkpoint=run/ckpt_best.pt']);
  assert.equal(existing.win.isVisible(), true);
  assert.ok(existing.win.sent.includes('models:changed'));
  existing.handlers.get('models:select')({ sender: { send() {} } }, 'api');
  existing.app.emit('second-instance', {}, ['electron', 'desktop-chat', '--checkpoint', '/normalized/ckpt_best.pt'], '/other',
                   { whitecubeArgs: ['desktop-chat', '--show', '--checkpoint', 'run/ckpt_best.pt'] });
  assert.equal(existing.handlers.get('settings:get')().checkpointId, 'run/ckpt_best.pt');
  assert.equal(existing.handlers.get('settings:get')().chatSource, 'local');
});

test('missing key emits an error and always completes the send', async () => {
  const { handlers } = await start(['--hide']);
  const events = [];
  const result = await handlers.get('chat:send')({ sender: { send: (...args) => events.push(args) } }, 'hello');
  assert.equal(result.ok, false);
  assert.match(result.error, /No API key/);
  assert.equal(events[0][0], 'chat:event');
  assert.match(events[0][1].message, /No API key/);
  assert.equal(events.at(-1)[0], 'chat:done');
});

test('stop resolves a pending confirmation and completes the turn', async () => {
  let reachedConfirmation = false;
  const { handlers } = await start(['--hide'], {
    settings: { endpoint: 'http://localhost:11434/v1' },
    async runAgentTurn({ confirmTool, signal }) {
      reachedConfirmation = true;
      await confirmTool({ name: 'write_file', args: {} });
      signal.throwIfAborted();
      assert.fail('a cancelled tool must never execute');
    }
  });
  const events = [];
  const pending = handlers.get('chat:send')({ sender: { send: (...args) => events.push(args) } }, 'hello');
  assert.equal(reachedConfirmation, true);
  handlers.get('chat:cancel')();
  await pending;
  assert.ok(events.some(([name]) => name === 'chat:confirm-cancelled'));
  assert.equal(events.at(-1)[0], 'chat:done');
});

test('connection test uses edited settings and matches exact model IDs', async () => {
  let requested;
  const { handlers } = await start(['--hide'], {
    fetch: async url => { requested = url; return { ok: true, text: async () => JSON.stringify({ data: [{ id: 'test-model-longer' }] }) }; }
  });
  const result = await handlers.get('settings:test')({}, { endpoint: 'http://localhost:9999/v1/chat/completions', model: 'test-model' });
  assert.equal(requested, 'http://localhost:9999/v1/models');
  assert.match(result.warning, /not listed/);
});

test('checkpoint selection and renaming preserve API settings and reject unknown files', async () => {
  const { handlers } = await start(['--hide'], { settings: { apiKey: 'keep-this-key' } });
  const sender = { send() {} };
  assert.throws(() => handlers.get('models:select')({ sender }, '../../outside.pt'), /unavailable/);
  const selected = handlers.get('models:select')({ sender }, 'run/ckpt_best.pt');
  assert.equal(selected.chatSource, 'local');
  assert.equal(handlers.get('settings:get')().needsKey, false);
  handlers.get('models:rename')({}, { id: 'run/ckpt_best.pt', name: 'My model' });
  handlers.get('models:select')({ sender }, 'api');
  const s = handlers.get('settings:get')();
  assert.equal(s.endpoint, 'https://example.com/v1'); assert.equal(s.model, 'test-model');
  assert.equal(s.apiKey, 'keep-this-key');
  assert.throws(() => handlers.get('models:rename')({}, { id: 'run/ckpt_best.pt', name: 'bad\nname' }), /80 characters/);
});

test('local checkpoint requests use the exact file ID without leaking the API key', async () => {
  let actual;
  const { handlers } = await start(['--hide'], {
    settings: { chatSource: 'local', checkpointId: 'run/ckpt_best.pt', apiKey: 'private-api-key' },
    localModels: { ensure: async () => 'http://127.0.0.1:43210/v1' },
    runAgentTurn: async ({ settings }) => { actual = settings; }
  });
  await handlers.get('chat:send')({ sender: { send() {} } }, 'hello');
  assert.equal(actual.endpoint, 'http://127.0.0.1:43210/v1');
  assert.equal(actual.model, 'run/ckpt_best.pt'); assert.equal(actual.apiKey, '');
  assert.equal(actual.toolsEnabled, false); assert.equal(actual.maxTokens, 120);
});

test('Stop cancels local startup and model changes are blocked during a reply', async () => {
  const { handlers } = await start(['--hide'], {
    settings: { chatSource: 'local', checkpointId: 'run/ckpt_best.pt' },
    localModels: { ensure: signal => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')))) }
  });
  const events = [], sender = { send: (...args) => events.push(args) };
  const pending = handlers.get('chat:send')({ sender }, 'hello');
  assert.throws(() => handlers.get('models:select')({ sender }, 'api'), /Stop the reply/);
  handlers.get('chat:cancel')(); await pending;
  assert.ok(events.some(([name, data]) => name === 'chat:event' && data.message === 'Cancelled.'));
  assert.equal(events.at(-1)[0], 'chat:done');
});

test('deleting an API chat restores the local source before sending the fallback transcript', async () => {
  const endpoints = [];
  const { handlers } = await start(['--hide'], {
    settings: { chatSource: 'local', checkpointId: 'run/ckpt_best.pt', apiKey: 'external-key' },
    localModels: { ensure: async () => 'http://127.0.0.1:43210/v1' },
    runAgentTurn: async ({ settings }) => endpoints.push(settings.endpoint)
  });
  const sender = { send() {} };
  await handlers.get('chat:send')({ sender }, 'private local conversation');
  const local = handlers.get('chats:current')();
  handlers.get('models:select')({ sender }, 'api');
  const api = handlers.get('chats:current')();
  const result = handlers.get('chats:delete')({}, api.id);
  assert.equal(result.activeChat.id, local.id);
  assert.equal(handlers.get('settings:get')().chatSource, 'local');
  await handlers.get('chat:send')({ sender }, 'continue locally');
  assert.deepEqual(endpoints, ['http://127.0.0.1:43210/v1', 'http://127.0.0.1:43210/v1']);
});

test('changing the API endpoint cannot transfer an existing chat to the new provider', async () => {
  let calls = 0;
  const { handlers } = await start(['--hide'], {
    settings: { endpoint: 'http://localhost:11434/v1' },
    runAgentTurn: async () => { calls++; }
  });
  const sender = { send() {} };
  await handlers.get('chat:send')({ sender }, 'private conversation');
  handlers.get('settings:save')({}, { endpoint: 'http://localhost:8888/v1' });
  const result = await handlers.get('chat:send')({ sender }, 'keep talking');
  assert.equal(result.ok, false); assert.match(result.error, /different endpoint or model/);
  assert.equal(calls, 1);
});

test('quit snapshots visible partial text into model context before the engine completes', async () => {
  const partial = 'The reply in progress';
  const { app, handlers, directory } = await start(['--hide'], {
    settings: { endpoint: 'http://localhost:11434/v1' },
    async runAgentTurn({ messages, onEvent, signal }) {
      onEvent({ type: 'text-delta', text: partial });
      await new Promise(resolve => signal.addEventListener('abort', resolve, { once: true }));
      messages.push({ role: 'assistant', content: partial });
      signal.throwIfAborted();
    }
  });
  const pending = handlers.get('chat:send')({ sender: { send() {} } }, 'start a reply');
  const chat = handlers.get('chats:current')();
  app.emit('will-quit');
  const saved = JSON.parse(fs.readFileSync(path.join(directory, 'conversations', `${chat.id}.json`), 'utf8'));
  assert.equal(saved.context.at(-1).content, partial);
  assert.equal(saved.messages.at(-1).content, partial);
  await pending;
});

test('IPC refuses other windows and subframes before reading secrets or changing state', async () => {
  const { rawHandlers, win } = await start(['--hide']);
  assert.throws(() => rawHandlers.get('settings:get')({ sender: { send() {} } }), /chat window/);
  assert.throws(() => rawHandlers.get('settings:get')({ sender: win.webContents, senderFrame: {} }), /chat window/);
  assert.equal(rawHandlers.get('settings:get')({ sender: win.webContents }).model, 'test-model');
});

test('destructive approval requires a boolean response from the chat window', async () => {
  let approved;
  const { handlers, ipcMain, win } = await start(['--hide'], {
    settings: { endpoint: 'http://localhost:11434/v1' },
    async runAgentTurn({ confirmTool }) { approved = await confirmTool({ name: 'write_file', args: {} }); }
  });
  const events = [];
  const pending = handlers.get('chat:send')({ sender: { send: (channel, payload) => events.push([channel, payload]) } }, 'write something');
  const requestId = events.find(([channel]) => channel === 'chat:confirm-request')[1].requestId;
  ipcMain.emit('chat:confirm-response', { sender: win.webContents }, { requestId, approved: 'true' });
  await Promise.resolve();
  assert.equal(approved, undefined);
  ipcMain.emit('chat:confirm-response', { sender: win.webContents }, { requestId, approved: false });
  await pending;
  assert.equal(approved, false);
});
