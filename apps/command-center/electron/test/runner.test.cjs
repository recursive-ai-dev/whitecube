'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const runner = require('../runner.cjs');

const exec = (command, cwd) => new Promise((resolve) => {
  const events = [];
  runner.start({ command, cwd }, (e) => { events.push(e); if (e.kind === 'exit') resolve(events); });
});

test('streams stdout/stderr and exit code', async () => {
  const ev = await exec('echo hi; echo oops >&2; exit 3', '/tmp');
  assert.ok(ev.some((e) => e.kind === 'out' && e.text.includes('hi')));
  assert.ok(ev.some((e) => e.kind === 'err' && e.text.includes('oops')));
  assert.equal(ev.at(-1).code, 3);
});

test('reports the cwd after cd', async () => {
  const ev = await exec('cd /usr && pwd', '/tmp');
  assert.equal(ev.at(-1).cwd, '/usr');
});

test('kill terminates a running job', async () => {
  const p = new Promise((resolve) => {
    const { id } = runner.start({ command: 'sleep 30', cwd: '/tmp' }, (e) => { if (e.kind === 'exit') resolve(e); });
    setTimeout(() => runner.kill(id), 100);
  });
  const e = await p;
  assert.equal(e.signal, 'SIGTERM');
});

test('signalProcess refuses pid 1, self and unknown signals', () => {
  const S = require('../system.cjs');
  assert.equal(S.signalProcess(1, 'SIGTERM').ok, false);
  assert.equal(S.signalProcess(process.pid, 'SIGTERM').ok, false);
  assert.equal(S.signalProcess(4242424, 'SIGUSR1').ok, false);
  assert.equal(S.signalProcess(4242424, 'SIGTERM').ok, false);
});

test('backend prevents simultaneous jobs even without the renderer guard', async () => {
  const done = new Promise(resolve => {
    const job = runner.start({ command: 'sleep 2', cwd: '/tmp' }, e => { if (e.kind === 'exit') resolve(); });
    assert.throws(() => runner.start({ command: 'echo second', cwd: '/tmp' }, () => {}), /already running/);
    runner.kill(job.id);
  });
  await done;
});
test('large output is bounded while the process still exits normally', async () => {
  const events = await exec('head -c 6000000 /dev/zero', '/tmp');
  const output = events.filter(e => e.text).map(e => e.text).join('');
  assert.ok(output.length < 4200000); assert.match(output, /Output truncated/); assert.equal(events.at(-1).code, 0);
});
