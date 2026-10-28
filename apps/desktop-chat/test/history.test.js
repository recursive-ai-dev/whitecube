const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { History, exportMarkdown } = require('../src/history');
const { randomUUID } = require('node:crypto');
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

test('large conversation libraries search off-thread, paginate and detect external edits and deletion', async t => {
  const { history } = fixture(t);
  t.after(() => history.close());
  const template = history.create({ model: 'library' });
  const ids = [];
  for (let index = 0; index < 1000; index++) {
    const id = randomUUID(); ids.push(id);
    const chat = { ...template, id, title: `Conversation ${index}`,
      messages: [{ role: 'user', content: 'x'.repeat(12000) + ` deep-needle-${index}` }] };
    fs.writeFileSync(history.filename(id), JSON.stringify(chat));
  }
  let eventLoopAdvanced = false;
  setTimeout(() => { eventLoopAdvanced = true; }, 0);
  const first = await history.listAsync('', { offset: 0, limit: 50 });
  assert.equal(eventLoopAdvanced, true, 'search must release the main event loop');
  assert.equal(first.total, 1001); assert.equal(first.items.length, 50); assert.equal(first.hasMore, true);
  const second = await history.listAsync('', { offset: 50, limit: 50 });
  assert.equal(new Set([...first.items, ...second.items].map(chat => chat.id)).size, 100);
  assert.equal((await history.listAsync('DEEP-NEEDLE-999', { limit: 50 })).items[0].id, ids[999]);
  const changed = history.get(ids[999]);
  changed.messages[0].content = 'A new external searchable transcript';
  history.save(changed);
  assert.equal((await history.listAsync('deep-needle-999')).length, 0);
  assert.equal((await history.listAsync('external searchable'))[0].id, changed.id);
  history.rename(changed.id, 'Renamed externally');
  assert.equal((await history.listAsync('renamed externally'))[0].id, changed.id);
  history.delete(changed.id);
  assert.equal((await history.listAsync('external searchable')).length, 0);
  fs.writeFileSync(history.filename(ids[998]), '{broken');
  assert.equal((await history.listAsync('', { limit: 1 })).total, 999);
  assert.equal(fs.readFileSync(history.filename(ids[998]), 'utf8'), '{broken');
});

test('conversation page bounds are checked and a closed search worker can restart', async t => {
  const { history } = fixture(t);
  t.after(() => history.close());
  const chat = history.create({});
  for (const options of [{ limit: 0 }, { limit: 101 }, { offset: -1 }, { offset: 1.2 }, []]) {
    await assert.rejects(history.listAsync('', options), /Invalid conversation page/);
  }
  assert.equal((await history.listAsync())[0].id, chat.id);
  await history.close();
  assert.equal((await history.listAsync())[0].id, chat.id);
});

test('closing an in-flight history worker settles its request and permits a fresh search', async t => {
  const { history } = fixture(t);
  t.after(() => history.close());
  const chat = history.create({});
  const pending = history.listAsync('', { limit: 50 });
  const rejected = assert.rejects(pending, /search closed/);
  await history.close();
  await rejected;
  assert.equal((await history.listAsync())[0].id, chat.id);
});
