'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { writeAtomic } = require('../persistence.cjs');
test('atomic replacement is private and leaves no scratch files', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-state-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'nested/window.json');
  writeAtomic(file, '{"width":1360}'); writeAtomic(file, '{"width":1200}');
  assert.deepEqual(JSON.parse(fs.readFileSync(file)), { width: 1200 });
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ['window.json']);
});
test('a failed replacement leaves the prior state intact and cleans up', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-state-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'state.json'); writeAtomic(file, 'previous');
  // Invalid input fails after the temporary file is opened.
  assert.throws(() => writeAtomic(file, Symbol('invalid')), /data|type/i);
  assert.equal(fs.readFileSync(file, 'utf8'), 'previous');
  assert.deepEqual(fs.readdirSync(dir), ['state.json']);
});
