'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { buildSync } = require('esbuild');
const compiled = buildSync({ entryPoints: [path.join(__dirname, '../../backup.ts')], bundle: true, platform: 'node', format: 'cjs', write: false }).outputFiles[0].text;
const instance = { exports: {} }; new Function('module', 'exports', 'require', compiled)(instance, instance.exports, require);
const B = instance.exports;
const fixture = () => ({ scripts: [{ id: 'cmd', name: 'Inspect', description: 'Read only', command: 'pwd', icon: 'terminal', category: 'System', status: 'SUCCESS', lastRun: 'yesterday', requiresConfirmation: false }], nautilusActions: [], shortcutGroups: [{ id: 'group', name: 'Core', shortcut: 'Alt+C', scriptIds: ['cmd'] }] });
test('backup round trip resets transient state and requires imported command confirmation', () => {
  const lib = B.decodeBackup(B.encodeBackup(fixture()));
  assert.equal(lib.scripts[0].requiresConfirmation, true); assert.equal(lib.scripts[0].status, 'IDLE');
  assert.deepEqual(lib.shortcutGroups[0].scriptIds, ['cmd']);
});
test('malformed backups and broken references are rejected before merge', () => {
  assert.throws(() => B.decodeBackup('{"version":99}'), /format|version/);
  const duplicate = fixture(); duplicate.scripts.push({ ...duplicate.scripts[0] });
  assert.throws(() => B.decodeBackup(B.encodeBackup(duplicate)), /duplicate/);
  const missing = fixture(); missing.shortcutGroups[0].scriptIds = ['missing'];
  assert.throws(() => B.decodeBackup(B.encodeBackup(missing)), /reference/);
  const invalid = fixture(); invalid.scripts[0].command = null;
  assert.throws(() => B.decodeBackup(B.encodeBackup(invalid)), /Invalid command/);
});
test('merge retains existing items and remaps imported shortcut references', () => {
  const current = fixture(), imported = B.decodeBackup(B.encodeBackup(fixture()));
  const merged = B.mergeLibrary(current, imported);
  assert.equal(merged.scripts.length, 2); assert.equal(merged.scripts[0].id, 'cmd');
  assert.notEqual(merged.scripts[1].id, 'cmd'); assert.equal(merged.shortcutGroups[1].scriptIds[0], merged.scripts[1].id);
  assert.equal(current.scripts.length, 1);
});
