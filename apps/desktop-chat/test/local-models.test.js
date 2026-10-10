const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { LocalModels, discoverCheckpoints, runtime } = require('../src/local-models');

function fixture(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'checkpoint-picker-'));
  try { fn(dir); } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}

test('discovers individual checkpoint files and displays metadata and saved names', () => fixture(dir => {
  const run = path.join(dir, 'new-run'); fs.mkdirSync(run);
  fs.writeFileSync(path.join(run, 'ckpt_best.pt'), 'best'); fs.writeFileSync(path.join(run, 'ckpt_latest.pt'), 'latest');
  fs.writeFileSync(path.join(run, 'ckpt_step_100.pt'), 'snapshot'); fs.writeFileSync(path.join(run, 'unrelated.pt'), 'ignore');
  fs.writeFileSync(path.join(run, 'run.json'), JSON.stringify({ parameters: 1000000, display_name: 'Named run' }));
  fs.writeFileSync(path.join(run, 'local_config.json'), JSON.stringify({ block_size: 256 }));
  fs.symlinkSync(run, path.join(dir, 'linked-run'));
  fs.symlinkSync(path.join(run, 'ckpt_best.pt'), path.join(run, 'ckpt_link.pt'));
  const items = discoverCheckpoints(dir, { 'new-run/ckpt_best.pt': '<my model>' });
  assert.equal(items.length, 3); assert.equal(items[0].name, '<my model>');
  assert.equal(items[0].contextBytes, 256); assert.equal(items[0].parameters, 1000000);
  assert.equal(items.find(x => x.filename === 'ckpt_latest.pt').kind, 'Latest');
  assert.equal(items.find(x => x.filename === 'ckpt_latest.pt').name, 'Named run · Latest');
  const models = new LocalModels({ directory: dir });
  assert.throws(() => models.find('../outside.pt'), /no longer available/);
  fs.unlinkSync(path.join(run, 'ckpt_best.pt'));
  assert.throws(() => models.find('new-run/ckpt_best.pt'), /no longer available/);
}));

test('missing training folder is an empty list and unfinished metadata is tolerated', () => fixture(dir => {
  assert.deepEqual(discoverCheckpoints(path.join(dir, 'missing')), []);
  fs.mkdirSync(path.join(dir, 'run')); fs.writeFileSync(path.join(dir, 'run/ckpt_latest.pt'), 'x');
  fs.writeFileSync(path.join(dir, 'run/run.json'), '{');
  assert.equal(discoverCheckpoints(dir)[0].parameters, null);
}));

test('finds the training interpreter from a quoted tg-serve launcher', () => fixture(home => {
  const moduleDir = path.join(home, '.local/share/tensegrity'); fs.mkdirSync(moduleDir, { recursive: true });
  fs.writeFileSync(path.join(moduleDir, 'tensegrity_serve.py'), '');
  const python = path.join(home, 'python with spaces'); fs.writeFileSync(python, '');
  fs.mkdirSync(path.join(home, '.local/bin'), { recursive: true });
  fs.writeFileSync(path.join(home, '.local/bin/tg-serve'), `#!/bin/bash\nexec "${python}" -u server.py "$@"\n`);
  assert.equal(runtime(home).interpreter, python);
}));
