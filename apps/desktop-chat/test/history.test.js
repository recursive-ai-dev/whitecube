const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { History, exportMarkdown } = require('../src/history');
function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-chat-history-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { history: new History(directory), directory };
}
test('saved chat restores model context and searchable full transcript without credentials', t => {
  const { history, directory } = fixture(t);
  const chat = history.create({ endpoint: 'https://example.com/v1', model: 'test', apiKey: 'never-save-me' });
  chat.messages = [{ role: 'user', content: 'Find my amber notebook' }, { role: 'assistant', content: 'Here it is.' }];
  chat.context = [{ role: 'assistant', content: 'trimmed context' }];
  history.save(chat);
  const restored = new History(directory);
  assert.deepEqual(restored.current(), chat);
  assert.equal(restored.list('AMBER')[0].id, chat.id);
  assert.equal(restored.list('missing').length, 0);
  assert.equal(fs.readFileSync(history.filename(chat.id), 'utf8').includes('never-save-me'), false);
  assert.equal(fs.statSync(history.filename(chat.id)).mode & 0o777, 0o600);
  assert.equal(fs.statSync(directory).mode & 0o777, 0o700);
});
test('opaque IDs block traversal and invalid rename leaves history intact', t => {
  const { history } = fixture(t);
  const chat = history.create({});
  for (const bad of ['../../config', '', null, chat.id + '/..']) assert.throws(() => history.get(bad), /Invalid conversation/);
  assert.throws(() => history.rename(chat.id, 'bad\nname'));
  assert.equal(history.get(chat.id).title, 'New conversation');
  history.rename(chat.id, 'Notebook');
  assert.equal(history.get(chat.id).customTitle, true);
  assert.equal(history.list('notebook').length, 1);
  history.delete(chat.id);
  assert.equal(history.current(), null);
});
test('corrupt chats are preserved and do not prevent opening good history', t => {
  const { history, directory } = fixture(t);
  const good = history.create({ model: 'one' });
  const bad = history.create({});
  fs.writeFileSync(history.filename(bad.id), '{broken');
  assert.equal(history.current().id, good.id);
  assert.equal(fs.readFileSync(history.filename(bad.id), 'utf8'), '{broken');
  assert.equal(fs.readdirSync(directory).filter(n => n.endsWith('.tmp')).length, 0);
});
test('markdown exports visible messages including tool activity', t => {
  const { history } = fixture(t);
  const chat = history.create({});
  chat.title = 'A useful chat';
  chat.messages = [{ role: 'user', content: 'hello' }, { role: 'tool', content: 'read_file: first\nsecond' }, { role: 'assistant', content: '```js\n1+1\n```' }];
  const text = exportMarkdown(chat);
  assert.match(text, /^# A useful chat/);
  assert.match(text, /## You\n\nhello/);
  assert.match(text, /> tool: read_file: first\n> second/);
  assert.match(text, /```js\n1\+1\n```/);
});

test('malformed timestamps and transcript entries are isolated without changing original files', t => {
  const { history } = fixture(t);
  const good = history.create({});
  const damaged = [];
  const mutations = [
    chat => { delete chat.updatedAt; },
    chat => { chat.updatedAt = 42; },
    chat => { chat.createdAt = '2026-02-30T00:00:00.000Z'; },
    chat => { chat.messages = [null]; },
    chat => { chat.messages = [{ role: 'assistant', content: {} }]; },
    chat => { chat.context = [null]; },
    chat => { chat.context = [{ role: 'system', content: 'unexpected persisted instructions' }]; },
    chat => { chat.context = [{ role: 'tool', content: 'missing call ID' }]; },
    chat => { chat.context = [{ role: 'assistant', content: null, tool_calls: [{ id: 'call', type: 'function', function: { name: 'read_file', arguments: {} } }] }]; }
  ];
  for (const mutate of mutations) {
    const bad = history.create({}); mutate(bad);
    const original = JSON.stringify(bad);
    fs.writeFileSync(history.filename(bad.id), original);
    assert.throws(() => history.get(bad.id), /damaged/);
    assert.equal(fs.readFileSync(history.filename(bad.id), 'utf8'), original);
    damaged.push(bad.id);
  }
  assert.deepEqual(history.list().map(chat => chat.id), [good.id]);
  assert.equal(history.current().id, good.id);
  assert.equal(damaged.length, mutations.length);
});

test('valid interrupted tool context remains loadable for engine repair', t => {
  const { history } = fixture(t);
  const chat = history.create({});
  chat.messages = [{ role: 'user', content: 'read this' }, { role: 'assistant', content: 'partial response' }];
  chat.context = [{ role: 'user', content: 'read this' }, { role: 'assistant', content: null,
    tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'read_file', arguments: '{"path":"/tmp/example"}' } }] }];
  history.save(chat);
  assert.deepEqual(history.get(chat.id), chat);
});
