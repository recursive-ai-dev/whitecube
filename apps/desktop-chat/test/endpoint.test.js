const test = require('node:test');
const assert = require('node:assert');
const { isLocalEndpoint, needsApiKey, validateSettings } = require('../src/endpoint');

test('loopback endpoints are local and need no key', () => {
  for (const u of [
    'http://localhost:1234/v1',
    'http://127.0.0.1:11434/v1',
    'http://127.1.2.3:8080',
    'http://[::1]:8080/v1',
    'http://llm.localhost:9000/v1',
    '  http://LOCALHOST:1234/v1  '
  ]) {
    assert.strictEqual(isLocalEndpoint(u), true, u);
    assert.strictEqual(needsApiKey(u), false, u);
  }
});

test('remote, LAN and look-alike hosts still need a key', () => {
  for (const u of [
    'https://api.openai.com/v1',
    'http://192.168.1.20:1234/v1',
    'http://10.0.0.5/v1',
    'https://localhost.evil.com/v1',
    'http://127.0.0.1.evil.com/v1',
    'https://example.com/localhost',
    'http://user@evil.com:1234@localhost/v1x'.replace('@localhost', '@evil.org')
  ]) {
    assert.strictEqual(isLocalEndpoint(u), false, u);
    assert.strictEqual(needsApiKey(u), true, u);
  }
});

test('garbage input is treated as remote (key required)', () => {
  for (const u of ['', null, undefined, 'not a url', '://', 42]) {
    assert.strictEqual(needsApiKey(u), true, String(u));
  }
});

test('settings normalize completion URLs and reject unusable endpoints or models', () => {
  const result = validateSettings({ endpoint: ' http://localhost:1234/v1/chat/completions/ ', model: ' model ' });
  assert.strictEqual(result.endpoint, 'http://localhost:1234/v1');
  assert.strictEqual(result.model, 'model');
  for (const endpoint of ['', 'not a URL', 'file:///etc/passwd', 'https://user:pass@example.com/v1', 'https://example.com/v1?key=secret']) {
    assert.throws(() => validateSettings({ endpoint, model: 'model' }));
  }
  assert.throws(() => validateSettings({ endpoint: 'http://localhost/v1', model: '  ' }), /model name/);
});
