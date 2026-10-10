'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const I = require('../inspector.cjs');
const S = require('../system.cjs');

test('storage flattens nested block devices and normalizes null fields', () => {
  const result = I.parseBlocks(JSON.stringify({ blockdevices: [{ name: 'nvme0n1', path: '/dev/nvme0n1', size: '1000', model: ' Disk ', ro: false, children: [{ name: 'crypt', path: '/dev/mapper/crypt', size: 900, mountpoints: [null, '/'], ro: '1' }] }] }));
  assert.equal(result[0].model, 'Disk'); assert.equal(result[0].size, 1000);
  assert.equal(result[1].depth, 1); assert.equal(result[1].readOnly, true); assert.deepEqual(result[1].mounts, ['/']);
  assert.throws(() => I.parseBlocks('{}'), /Unexpected/);
});
test('package inventory ignores removed and unconfigured packages', () => {
  const result = I.parsePackages('bash\t5.2\tamd64\t100\tii \tGNU shell\nold\t1\tall\t0\trc \tOld\nbroken\t1\tall\t12\tiU \tBroken\n');
  assert.equal(result.length, 1); assert.equal(result[0].bytes, 102400); assert.equal(result[0].description, 'GNU shell');
});
test('diagnostics reports unavailable capabilities as unknown, never healthy', async () => {
  const run = S.run, services = S.listServices;
  S.run = async () => ({ ok: false, stdout: '', stderr: 'unavailable' });
  S.listServices = async () => ({ units: [], error: 'No system bus' });
  try {
    const r = await I.diagnostics();
    for (const id of ['services', 'packages', 'disk', 'system']) assert.equal(r.checks.find(c => c.id === id).status, 'unknown');
  } finally { S.run = run; S.listServices = services; }
});
