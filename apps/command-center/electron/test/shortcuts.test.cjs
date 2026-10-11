'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { buildSync } = require('esbuild');
const compiled = buildSync({ entryPoints: [path.join(__dirname, '../../shortcuts.ts')], bundle: true, platform: 'node', format: 'cjs', write: false }).outputFiles[0].text;
const instance = { exports: {} }; new Function('module', 'exports', 'require', compiled)(instance, instance.exports, require);
const { shortcutIssue, normalizeShortcut } = instance.exports;
test('shortcut normalization detects duplicates and permits editing its owner', () => {
  const groups = [{ id: 'a', name: 'Core', shortcut: 'Ctrl+Shift+G' }];
  assert.equal(normalizeShortcut(' ctrl + shift + g '), 'CTRL+SHIFT+G');
  assert.match(shortcutIssue(' ctrl + shift + g ', groups), /Core/);
  assert.equal(shortcutIssue('Ctrl+Shift+G', groups, 'a'), null);
  assert.equal(shortcutIssue('Ctrl+Shift+2', groups), null);
});
test('reserved and unsupported bindings cannot be saved', () => {
  for (const value of ['Ctrl+K', 'Alt+C', 'Alt+2', 'Alt+F4', 'Ctrl+Shift+C', 'Ctrl+C', 'Alt+Shift+M', 'Alt+', 'M', '']) assert.ok(shortcutIssue(value, []));
});

test('available Alt bindings work and desktop bindings explain their conflict', () => {
  assert.equal(shortcutIssue('Alt+M', []), null);
  assert.match(shortcutIssue('Alt+C', []), /Whitecube desktop/);
});
