'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { buildSync } = require('esbuild');
const compiled = buildSync({ entryPoints: [path.join(__dirname, '../../storage.ts')], bundle: true, platform: 'node', format: 'cjs', write: false }).outputFiles[0].text;
function fixture() {
  const values = new Map();
  const window = { localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } };
  const instance = { exports: {} }; new Function('module', 'exports', 'require', 'window', compiled)(instance, instance.exports, require, window);
  return instance.exports;
}
test('legacy default Core Actions binding migrates without changing saved commands', () => {
  const storage = fixture(), library = storage.loadState();
  const before = library.scripts.map(s => ({ name: s.name, command: s.command, path: s.path }));
  library.shortcutGroups[0].shortcut = 'Alt+C';
  storage.saveState(library);
  const restored = storage.loadState();
  assert.equal(restored.shortcutGroups[0].shortcut, 'Ctrl+Shift+G');
  assert.deepEqual(restored.scripts.map(s => ({ name: s.name, command: s.command, path: s.path })), before);
});
test('custom group bindings and valid organization survive a reload', () => {
  const storage = fixture(), library = storage.loadState();
  library.shortcutGroups.push({ id: 'custom', name: 'Custom', shortcut: 'Alt+C', scriptIds: [] });
  library.scripts[0].pinned = true; library.scripts[0].tags = ['daily'];
  storage.saveState(library);
  const restored = storage.loadState();
  assert.equal(restored.shortcutGroups[1].shortcut, 'Alt+C');
  assert.equal(restored.scripts[0].pinned, true);
  assert.deepEqual(restored.scripts[0].tags, ['daily']);
});
test('default migration preserves an existing owner of the new shortcut', () => {
  const storage = fixture(), library = storage.loadState();
  library.shortcutGroups[0].shortcut = 'Alt+C';
  library.shortcutGroups.push({ id: 'owner', name: 'Existing group', shortcut: 'Ctrl+Shift+G', scriptIds: [] });
  storage.saveState(library);
  const restored = storage.loadState();
  assert.equal(restored.shortcutGroups[0].shortcut, '');
  assert.equal(restored.shortcutGroups[1].shortcut, 'Ctrl+Shift+G');
});
