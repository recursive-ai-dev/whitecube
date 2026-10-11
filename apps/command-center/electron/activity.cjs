'use strict';
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');
const { writeAtomic } = require('./persistence.cjs');

// Persist only command metadata, never captured stdout/stderr. Interrupted sessions
// are marked explicitly on the next launch rather than remaining "running" forever.
function createActivity(file) {
  let records = [];
  let warning = null;
  try {
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (Array.isArray(saved)) records = saved.filter(r => r && typeof r.command === 'string' && typeof r.id === 'string').slice(0, 200);
  } catch (e) { if (e.code !== 'ENOENT') warning = 'Previous activity could not be read.'; }
  records = records.map(r => r.status === 'running' ? { ...r, status: 'interrupted', endedAt: Date.now() } : r);
  const save = () => {
    try {
      writeAtomic(file, JSON.stringify(records));
      warning = null;
    } catch (e) { warning = `Activity could not be saved: ${e.message}`; }
  };
  return {
    list: () => ({ records: records.map(r => ({ ...r })), warning }),
    start(command, cwd) {
      const id = randomUUID();
      records.unshift({ id, command, cwd, startedAt: Date.now(), status: 'running' });
      records = records.slice(0, 200); save(); return id;
    },
    finish(id, event) {
      records = records.map(r => r.id === id ? { ...r, endedAt: Date.now(), duration: event.ms, code: event.code, signal: event.signal, status: event.error ? 'failed' : event.signal ? 'stopped' : event.code === 0 ? 'success' : 'failed' } : r);
      save();
    },
    clear() { records = records.filter(r => r.status === 'running'); save(); return { ok: !warning, error: warning }; },
  };
}
module.exports = { createActivity };
