const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('sidebar coalesces slow searches to one in flight and the latest query', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../renderer/renderer.js'), 'utf8');
  const search = { value: 'first' }, requests = [], paints = [], errors = [];
  const context = vm.createContext({
    document: { getElementById: () => search, createElement: () => ({}) },
    window: { api: { listChatsPage: (query, options) => new Promise(resolve => requests.push({ query, options, resolve })) } },
    chatList: { replaceChildren: () => paints.push('clear'), appendChild: () => paints.push('row') },
    chatMore: {}, currentChat: null, chatListRequest: 0, chatSearchTimer: null,
    chatListRunning: false, chatListPending: null, chatPageOffset: 0,
    clearTimeout, updateChatControls() {}, showHistoryError: error => errors.push(error)
  });
  vm.runInContext(source.slice(source.indexOf('async function refreshChats('), source.indexOf('function renderChat(')), context);
  const first = context.refreshChats();
  search.value = 'superseded'; await context.refreshChats();
  search.value = 'latest'; await context.refreshChats();
  assert.equal(requests.length, 1, 'a slow search must not accumulate worker requests');
  requests[0].resolve({ items: [], total: 0, hasMore: false });
  await first;
  assert.equal(requests.length, 2);
  assert.equal(requests[1].query, 'latest');
  assert.equal(paints.length, 0, 'obsolete responses must not repaint the sidebar');
  requests[1].resolve({ items: [], total: 0, hasMore: false });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(paints, ['clear', 'row']);
  assert.deepEqual(errors, []);
  assert.equal(context.chatListRunning, false);
});
