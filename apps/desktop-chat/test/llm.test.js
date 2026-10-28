const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { runAgentTurn, streamChatCompletion, trimHistory, repairToolCalls } = require('../src/llm');

const data = value => `data: ${JSON.stringify(value)}\n\n`;
const choice = (delta, finish_reason = null) => data({ choices: [{ delta, finish_reason }] });
async function endpoint(t, handler) {
  let requests = 0;
  const server = http.createServer((req, res) => handler(req, res, ++requests));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  return { url: `http://127.0.0.1:${server.address().port}/v1`, count: () => requests };
}
const settings = endpoint => ({ endpoint, model: 'test', toolsEnabled: false, maxTokens: 256, temperature: 0.4 });

test('stream accepts chunked UTF-8, terminal events without newline and sends generation settings', async t => {
  let requestBody;
  const service = await endpoint(t, async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    requestBody = JSON.parse(body);
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const response = Buffer.from(choice({ content: 'hello 中文' }) + choice({}, 'stop') + 'data: [DONE]');
    for (let index = 0; index < response.length; index += 7) res.write(response.subarray(index, index + 7));
    res.end();
  });
  const events = [];
  const result = await streamChatCompletion({ ...settings(service.url), messages: [] }, event => events.push(event));
  assert.equal(result.content, 'hello 中文'); assert.equal(result.finishReason, 'stop');
  assert.equal(events.map(event => event.text).join(''), result.content);
  assert.equal(requestBody.temperature, 0.4); assert.equal(requestBody.max_tokens, 256);
  assert.equal(requestBody.tools, undefined);
});

test('HTTP retry respects retry-after and emits a single final answer', async t => {
  const service = await endpoint(t, (req, res, request) => {
    if (request === 1) { res.writeHead(429, { 'Retry-After': '0' }); return res.end('busy'); }
    res.end(choice({ content: 'done' }) + choice({}, 'stop'));
  });
  const messages = [{ role: 'system', content: 'system' }, { role: 'user', content: 'hello' }], events = [];
  await runAgentTurn({ settings: settings(service.url), messages, onEvent: event => events.push(event) });
  assert.equal(service.count(), 2);
  assert.equal(messages.at(-1).content, 'done');
  assert.equal(events.filter(event => event.type === 'text-delta').length, 1);
  assert.equal(events.filter(event => event.type === 'notice').length, 1);
});

test('an interrupted partial stream is preserved without automatic duplicate requests', async t => {
  const service = await endpoint(t, (req, res) => res.end(choice({ content: 'partial answer' })));
  const messages = [{ role: 'system', content: 'system' }, { role: 'user', content: 'hello' }];
  await assert.rejects(runAgentTurn({ settings: settings(service.url), messages, onEvent() {} }), /before the reply finished/);
  assert.equal(service.count(), 1);
  assert.equal(messages.at(-1).role, 'assistant'); assert.equal(messages.at(-1).content, 'partial answer');
});

test('malformed streaming JSON is actionable and never silently swallowed', async t => {
  const service = await endpoint(t, (req, res) => res.end('data: {broken}\n\n'));
  await assert.rejects(streamChatCompletion({ ...settings(service.url), messages: [] }, () => {}), /malformed streaming JSON/);
  assert.equal(service.count(), 1);
});

test('cancel during write approval repairs all tool responses for the next request', async t => {
  const calls = [0, 1].map(index => ({ index, id: `call-${index}`, function: { name: 'write_file', arguments: JSON.stringify({ path: '/unused', content: 'test' }) } }));
  const service = await endpoint(t, (req, res) => res.end(choice({ tool_calls: calls }) + choice({}, 'tool_calls')));
  const messages = [{ role: 'system', content: 'system' }, { role: 'user', content: 'write' }];
  const controller = new AbortController();
  await assert.rejects(runAgentTurn({ settings: { ...settings(service.url), toolsEnabled: true }, messages,
    signal: controller.signal, onEvent() {}, confirmTool: async () => { controller.abort(); return false; } }), { name: 'AbortError' });
  assert.deepEqual(messages.slice(-2).map(message => message.tool_call_id), ['call-0', 'call-1']);
  assert.ok(messages.slice(-2).every(message => /interrupted/.test(message.content)));
});

test('incomplete tool call stopped by token limit is never executed', async t => {
  const service = await endpoint(t, (req, res) => res.end(choice({ tool_calls: [{ index: 0, id: 'call', function: { name: 'write_file', arguments: '{' } }] }) + choice({}, 'length')));
  const messages = [{ role: 'system', content: 'system' }, { role: 'user', content: 'hello' }], events = [];
  await runAgentTurn({ settings: { ...settings(service.url), toolsEnabled: true }, messages, onEvent: event => events.push(event), confirmTool() { assert.fail('must not request approval for truncated tools'); } });
  assert.equal(messages.at(-1).tool_calls, undefined);
  assert.ok(events.some(event => event.type === 'notice' && /token limit/.test(event.text)));
});

test('history trimming preserves the latest user turn and complete tool clusters', () => {
  const messages = [{ role: 'system', content: 'system' }, { role: 'user', content: 'a'.repeat(100) },
    { role: 'assistant', tool_calls: [{ id: 'old' }] }, { role: 'tool', tool_call_id: 'old', content: 'done' },
    { role: 'user', content: 'latest' }, { role: 'assistant', tool_calls: [{ id: 'new' }] }, { role: 'tool', tool_call_id: 'new', content: 'done' }];
  trimHistory(messages, 1);
  assert.equal(messages[1].content, 'latest'); assert.equal(messages.length, 4);
  assert.equal(messages[2].tool_calls[0].id, 'new');
});

test('restored interrupted tool clusters are repaired before the next user message', () => {
  const messages = [{ role: 'assistant', tool_calls: [{ id: 'one' }, { id: 'two' }] },
    { role: 'tool', tool_call_id: 'one', content: 'done' }, { role: 'user', content: 'continue' }];
  repairToolCalls(messages);
  assert.equal(messages[2].tool_call_id, 'two'); assert.equal(messages[3].role, 'user');
  repairToolCalls(messages); assert.equal(messages.length, 4);
});
