import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from './server.js';
import type { SysctlDbusClient, SysctlEntry } from './dbusClient.js';

function fakeClient(overrides: Partial<SysctlDbusClient> = {}): SysctlDbusClient {
  return {
    list: async () => [],
    setManaged: async () => '',
    listBackups: async () => [],
    restoreBackup: async () => '',
    ...overrides,
  };
}

test('GET /api/sysctl returns entries from the dbus client', async () => {
  const entries: SysctlEntry[] = [{ key: 'net.ipv4.tcp_syncookies', value: '1', sourceFile: '', isManaged: false }];
  const app = createServer(fakeClient({ list: async () => entries }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl`);
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.deepEqual(body.entries, entries);
  } finally {
    server.close();
  }
});

test('POST /api/sysctl/apply rejects a malformed body', async () => {
  const app = createServer(fakeClient());
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl/apply`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ entries: [{ key: 'x' }] }),
    });
    assert.equal(res.status, 400);
  } finally {
    server.close();
  }
});

test('POST /api/sysctl/apply forwards valid entries and returns output', async () => {
  let received: unknown;
  const app = createServer(fakeClient({
    setManaged: async (entries) => { received = entries; return 'applied ok'; },
  }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl/apply`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ entries: [{ key: 'net.ipv4.tcp_syncookies', value: '1' }] }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.output, 'applied ok');
    assert.deepEqual(received, [{ key: 'net.ipv4.tcp_syncookies', value: '1' }]);
  } finally {
    server.close();
  }
});

test('GET /api/sysctl/backups returns backups from the dbus client', async () => {
  const app = createServer(fakeClient({
    listBackups: async () => [{ timestamp: 123, entries: [{ key: 'kernel.dmesg_restrict', value: '0' }] }],
  }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl/backups`);
    const body = await res.json();
    assert.equal(body.backups.length, 1);
    assert.equal(body.backups[0].timestamp, 123);
  } finally {
    server.close();
  }
});

test('POST /api/sysctl/backups/:timestamp/restore forwards the timestamp', async () => {
  let received: unknown;
  const app = createServer(fakeClient({
    restoreBackup: async (timestamp) => { received = timestamp; return 'restored ok'; },
  }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl/backups/123/restore`, { method: 'POST' });
    const body = await res.json();
    assert.equal(body.output, 'restored ok');
    assert.equal(received, 123);
  } finally {
    server.close();
  }
});

test('errors from the dbus client surface as 502s', async () => {
  const app = createServer(fakeClient({ list: async () => { throw new Error('daemon unreachable'); } }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl`);
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.error, 'daemon unreachable');
  } finally {
    server.close();
  }
});
