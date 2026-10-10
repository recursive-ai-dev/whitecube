'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const p = require('../parse.cjs');

test('cpu percent from two samples', () => {
  const a = p.parseCpuStat('cpu  100 0 100 800 0 0 0 0 0 0\n');
  const b = p.parseCpuStat('cpu  150 0 150 900 0 0 0 0 0 0\n');
  assert.equal(p.cpuPercent(a, b), 50);
  assert.equal(p.cpuPercent(a, a), 0);
});

test('meminfo uses MemAvailable', () => {
  const m = p.parseMeminfo('MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 100 kB\nSwapFree: 40 kB\n');
  assert.equal(m.used, 600 * 1024);
  assert.equal(m.swapUsed, 60 * 1024);
});

test('net dev skips lo', () => {
  const t = 'h\nh\n    lo: 1 0 0 0 0 0 0 0 2 0\n  wlan0: 500 0 0 0 0 0 0 0 700 0\n';
  assert.deepEqual(p.parseNetDev(t), { wlan0: { rx: 500, tx: 700 } });
});

test('units and upgradable', () => {
  const u = p.parseUnits('ssh.service loaded active running OpenBSD Secure Shell server\nfoo.service not-found inactive dead foo.service\n');
  assert.equal(u[0].description, 'OpenBSD Secure Shell server');
  assert.equal(u[1].load, 'not-found');
  const g = p.parseUpgradable('Listing...\ncurl/stable-security 8.1-2 amd64 [upgradable from: 8.1-1]\n');
  assert.deepEqual(g[0], { name: 'curl', source: 'stable-security', to: '8.1-2', arch: 'amd64', from: '8.1-1' });
});

test('journal json lines tolerate garbage', () => {
  const j = p.parseJournal('{"__REALTIME_TIMESTAMP":"2000000","PRIORITY":"3","_SYSTEMD_UNIT":"a.service","MESSAGE":"boom"}\nnot json\n');
  assert.equal(j.length, 1);
  assert.equal(j[0].priority, 3);
  assert.equal(j[0].ts, 2000);
});

test('validators reject injection', () => {
  assert.ok(p.UNIT_RE.test('NetworkManager.service'));
  assert.ok(!p.UNIT_RE.test('x.service; rm -rf ~'));
  assert.ok(p.SYSCTL_KEY_RE.test('net.ipv4.ip_forward'));
  assert.ok(!p.SYSCTL_KEY_RE.test('a b.c'));
  assert.ok(!p.SYSCTL_VALUE_RE.test('1; reboot'));
  assert.equal(p.shQuote("it's"), `'it'\\''s'`);
});

test('wireless, ss, ping, ps parsers', () => {
  const w = p.parseWireless('h\nh\n wlan0: 0000   49.  -61.  -256   0 0 0 0 696 0\n');
  assert.deepEqual(w[0], { name: 'wlan0', link: 49, signal: -61, retries: 0 });
  const s = p.parseSs('tcp LISTEN 0 128 127.0.0.1:34205 0.0.0.0:* users:(("codex",pid=10049,fd=37))\nudp UNCONN 0 0 10.0.0.133:68 0.0.0.0:*\n');
  assert.equal(s[0].process, 'codex'); assert.equal(s[1].pid, null);
  const g = p.parsePing('3 packets transmitted, 3 received, 0% packet loss\nrtt min/avg/max/mdev = 1.1/2.2/3.3/0.5 ms');
  assert.deepEqual(g, { loss: 0, min: 1.1, avg: 2.2, max: 3.3 });
  assert.equal(p.parsePing('100% packet loss').avg, null);
  const ps = p.parsePsFull('PID USER %CPU %MEM RSS ELAPSED STAT COMMAND\n 12 woez 1.5 0.2 2048 90 S+ /usr/bin/foo --bar\n');
  assert.equal(ps[0].cmd, '/usr/bin/foo --bar'); assert.equal(ps[0].rss, 2048 * 1024);
});

test('sysctl.d merge keeps unrelated lines and updates in place', () => {
  const out = p.mergeSysctlConf('# hi\nvm.swappiness = 60\nkernel.x=1\n', [{ key: 'vm.swappiness', value: '10' }, { key: 'net.a.b', value: '1' }]);
  assert.equal(out, '# hi\nvm.swappiness = 10\nkernel.x=1\nnet.a.b = 1\n');
});
