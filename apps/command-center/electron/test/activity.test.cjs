'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createActivity } = require('../activity.cjs');
const fixture = t => { const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-activity-')); t.after(() => fs.rmSync(dir, { recursive: true, force: true })); return path.join(dir, 'activity.json'); };
test('activity persists results and repairs interrupted sessions', t => {
  const file = fixture(t), activity = createActivity(file);
  const id = activity.start('printf ok', '/tmp');
  activity.finish(id, { code: 0, ms: 10 });
  activity.start('sleep 30', '/tmp');
  const saved = createActivity(file).list();
  assert.equal(saved.records[0].status, 'interrupted'); assert.equal(saved.records[1].status, 'success');
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});
test('history is bounded and clearing preserves the active command', t => {
  const history = createActivity(fixture(t));
  for (let i = 0; i < 205; i++) { const id = history.start(String(i), '/'); history.finish(id, { code: 7 }); }
  assert.equal(history.list().records.length, 200);
  const id = history.start('sleep 1', '/tmp'); history.clear();
  assert.equal(history.list().records.length, 1); assert.equal(history.list().records[0].id, id);
});
test('a damaged history reports a warning without crashing', t => {
  const file = fixture(t); fs.writeFileSync(file, '{broken');
  const state = createActivity(file).list(); assert.equal(state.records.length, 0); assert.ok(state.warning);
});
