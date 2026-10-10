'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { trustedUrl, validateDevUrl } = require('../security.cjs');
test('only the exact application page is trusted in production', () => {
  const index = '/opt/Command Center/dist/index.html';
  assert.equal(trustedUrl('file:///opt/Command%20Center/dist/index.html', index), true);
  assert.equal(trustedUrl('file:///opt/Command%20Center/dist/index.html#view', index), true);
  for (const url of ['file:///etc/passwd', 'file:///opt/Command%20Center/dist/index.html.evil', 'https://example.com', 'not a URL']) assert.equal(trustedUrl(url, index), false);
});
test('development server validation rejects remote origins and prefix attacks', () => {
  assert.equal(validateDevUrl('http://127.0.0.1:3000'), 'http://127.0.0.1:3000/');
  assert.throws(() => validateDevUrl('https://example.com'));
  assert.throws(() => validateDevUrl('http://localhost.evil:3000'));
  assert.throws(() => validateDevUrl('http://user:pass@localhost:3000'));
  assert.equal(trustedUrl('http://localhost:3000.evil', '', 'http://localhost:3000'), false);
  assert.equal(trustedUrl('http://localhost:3001/', '', 'http://localhost:3000'), false);
});
