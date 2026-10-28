const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');

function fixture(fn) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-chat-config-'));
  const context = vm.createContext({ module: { exports: {} }, Buffer, require(name) {
    if (name === 'fs') return fs;
    if (name === 'path') return path;
    if (name === 'crypto') return crypto;
    if (name === './endpoint') return require('../src/endpoint');
    if (name === 'electron') return { app: { getPath: () => directory }, safeStorage: {
      isEncryptionAvailable: () => true, getSelectedStorageBackend: () => 'basic_text',
      encryptString() { assert.fail('basic_text must not be represented as OS keyring encryption'); }
    } };
    throw new Error(`Unexpected import ${name}`);
  } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/config.js'), 'utf8'), context);
  try { fn(context.module.exports, directory); }
  finally { fs.rmSync(directory, { recursive: true, force: true }); }
}

test('clearing a key preserves all other preferences', () => fixture((config, directory) => {
  config.save({ endpoint: 'http://localhost:11434/v1', model: 'custom', apiKey: 'sample', confirmDestructive: false, hotkey: 'Alt+Y' });
  config.save({ apiKey: '' });
  const saved = config.load();
  assert.equal(saved.endpoint, 'http://localhost:11434/v1');
  assert.equal(saved.model, 'custom'); assert.equal(saved.hotkey, 'Alt+Y');
  assert.equal(saved.confirmDestructive, false); assert.equal(saved.apiKey, '');
  assert.equal(fs.statSync(path.join(directory, 'config.json')).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(directory), ['config.json']);
}));

test('invalid settings leave the previous file unchanged and basic_text is disclosed', () => fixture((config, directory) => {
  config.save({ endpoint: 'http://localhost/v1', model: 'custom', apiKey: 'sample' });
  const previous = fs.readFileSync(path.join(directory, 'config.json'), 'utf8');
  assert.throws(() => config.save({ endpoint: 'file:///tmp/test' }));
  assert.equal(fs.readFileSync(path.join(directory, 'config.json'), 'utf8'), previous);
  assert.equal(config.load().keyStorage, 'plaintext');
}));

test('checkpoint IDs and friendly names survive restart and partial API changes', () => fixture((config, directory) => {
  config.save({ apiKey: 'external-key', chatSource: 'local', checkpointId: 'run/ckpt_latest.pt', modelAliases: { 'run/ckpt_latest.pt': 'New experiment' } });
  config.save({ model: 'other-api-model' });
  const saved = config.load();
  assert.equal(saved.chatSource, 'local'); assert.equal(saved.checkpointId, 'run/ckpt_latest.pt');
  assert.equal(saved.modelAliases['run/ckpt_latest.pt'], 'New experiment');
  assert.equal(saved.apiKey, 'external-key');
  const disk = JSON.parse(fs.readFileSync(path.join(directory, 'config.json'), 'utf8'));
  assert.equal(disk.checkpointId, saved.checkpointId);
}));

test('malformed confirmation state falls back to requiring confirmation', () => fixture((config, directory) => {
  fs.writeFileSync(path.join(directory, 'config.json'), JSON.stringify({ confirmDestructive:'false', hotkey:42 }));
  assert.equal(config.load().confirmDestructive, true);
  assert.equal(config.load().hotkey, 'CommandOrControl+Shift+T');
}));

test('invalid settings types never replace a saved configuration', () => fixture((config, directory) => {
  config.save({ model:'my-model' });
  const before = fs.readFileSync(path.join(directory, 'config.json'), 'utf8');
  for (const bad of [null, [], { confirmDestructive:'false' }, { hotkey:42 }, { endpoint:42 }, { model:'bad\nmodel' }]) {
    assert.throws(() => config.save(bad));
    assert.equal(fs.readFileSync(path.join(directory, 'config.json'), 'utf8'), before);
  }
}));

test('generation preferences persist across partial settings writes and reject invalid values', () => fixture((config, directory) => {
  config.save({ toolsEnabled: false, temperature: 1.2, maxTokens: 512 });
  config.save({ model: 'new-model' });
  const saved = config.load();
  assert.equal(saved.toolsEnabled, false); assert.equal(saved.temperature, 1.2); assert.equal(saved.maxTokens, 512);
  const before = fs.readFileSync(path.join(directory, 'config.json'), 'utf8');
  for (const bad of [{ toolsEnabled: 'false' }, { temperature: -1 }, { temperature: NaN }, { maxTokens: 0 }, { maxTokens: 32769 }, { maxTokens: 1.5 }, { apiKey: 42 }, { apiKey: 'invalid\nkey' }]) {
    assert.throws(() => config.save(bad));
    assert.equal(fs.readFileSync(path.join(directory, 'config.json'), 'utf8'), before);
  }
}));

test('malformed generation preferences fall back to usable defaults', () => fixture((config, directory) => {
  fs.writeFileSync(path.join(directory, 'config.json'), JSON.stringify({ toolsEnabled: 'false', temperature: -1, maxTokens: 999999 }));
  const settings = config.load();
  assert.equal(settings.toolsEnabled, true); assert.equal(settings.temperature, 0.7); assert.equal(settings.maxTokens, 8192);
}));
