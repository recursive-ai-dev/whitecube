'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const S = require('../system.cjs');
const sysctl = require('../sysctl.cjs');
const P = require('../parse.cjs');
test('sysctl recovery exists before a partially failing privileged write', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-sysctl-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  sysctl.init(dir);
  const run = S.run, sudo = S.sudo;
  S.run = async () => ({ ok: true, stdout: 'vm.swappiness = 60\n', stderr: '' });
  S.sudo = async () => {
    assert.equal(sysctl.backups()[0].entries[0].value, '60');
    return { ok: false, stderr: 'Partial apply failed' };
  };
  try {
    await assert.rejects(sysctl.apply([{ key: 'vm.swappiness', value: '10' }]), /Partial apply/);
    assert.equal(sysctl.backups().length, 1);
    await assert.rejects(sysctl.apply([{ key: 'vm.swappiness', value: '10\nvm.other 1' }]), /Invalid/);
    await assert.rejects(sysctl.apply([{ key: 'vm.swappiness', value: '10' }, { key: 'vm.swappiness', value: '20' }]), /Duplicate/);
  } finally { S.run = run; S.sudo = sudo; }
});
test('argument validators reject option injection and newline persistence injection', async () => {
  assert.equal(P.UNIT_RE.test('--help.service'), false);
  assert.equal(P.SYSCTL_VALUE_RE.test('1\nvm.other 1'), false);
  assert.equal(P.SYSCTL_VALUE_RE.test('4096\t87380\t6291456'), true);
  assert.deepEqual(await S.probe(['-f', '--help', null, {}]), []);
});
