// Optional integration check: PLAYWRIGHT_MODULE=/path/to/playwright node test/gui-smoke.cjs
// Requires an X11 display (Xvfb works). No real API, user settings, or system install.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const appPath = path.resolve(process.env.CHAT_APP_PATH || path.join(__dirname, '..'));
const executablePath = process.env.CHAT_ELECTRON_PATH || require('electron');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'whitecube-chat-smoke-'));
const requests = [];
const errors = [];
let app;
const server = http.createServer(async (req, res) => {
  if (req.url === '/v1/models') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'smoke-model' }] })); return; }
  let body = ''; for await (const chunk of req) body += chunk;
  const data = JSON.parse(body); requests.push(data);
  const lastUser = data.messages.filter(m => m.role === 'user').at(-1).content;
  res.setHeader('Content-Type', 'text/event-stream');
  const emit = value => res.write(`data: ${JSON.stringify(value)}\n\n`);
  const chunk = (delta, finish_reason = null) => emit({ choices: [{ delta, finish_reason }] });
  if (lastUser === 'cancel this response') {
    chunk({ content: 'Beginning a slow reply' });
    const timer = setInterval(() => res.write(': waiting\n\n'), 500);
    res.on('close', () => clearInterval(timer)); return;
  }
  if (lastUser === 'write a file' && data.messages.at(-1).role === 'user') {
    chunk({ tool_calls: [{ index: 0, id: 'smoke-write', function: { name: 'write_file', arguments: JSON.stringify({ path: path.join(directory, 'declined.txt'), content: 'must not be written' }) } }] }, 'tool_calls');
  } else chunk({ content: lastUser === 'write a file' ? 'The file operation was declined.' : 'Hello **workspace**.\n\n```js\nconsole.log("hello");\n```' }, 'stop');
  res.end('data: [DONE]\n\n');
});
async function launch() {
  app = await electron.launch({ executablePath, args: [appPath, `--user-data-dir=${path.join(directory, 'profile')}`, '--ozone-platform=x11', '--disable-gpu'],
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '', ELECTRON_OZONE_PLATFORM_HINT: 'x11' }, timeout: 30000 });
  const page = await app.firstWindow();
  page.setDefaultTimeout(15000);
  page.on('pageerror', e => errors.push(e.message));
  await page.waitForFunction(() => window.api && !document.getElementById('btn-sidebar-new').disabled, null, { timeout: 20000 });
  return page;
}
async function send(page, text) {
  await page.locator('#input').fill(text); await page.locator('#btn-send').click();
  await page.waitForFunction(() => document.querySelectorAll('.msg.assistant').length && document.getElementById('btn-stop').classList.contains('hidden'), null, { timeout: 15000 });
}
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let page = await launch();
  await page.locator('#btn-settings').click();
  await page.locator('#s-endpoint').fill(`http://127.0.0.1:${server.address().port}/v1`);
  await page.locator('#s-model').fill('smoke-model');
  await page.locator('#s-temperature').fill('0.4');
  await page.locator('#s-max-tokens').fill('512');
  await page.locator('#s-test').click();
  await page.waitForFunction(() => document.getElementById('s-test-result').textContent.includes('OK'));
  await page.locator('#s-save').click();
  await send(page, 'hello amber notebook');
  assert.equal(await page.locator('.msg.assistant strong').innerText(), 'workspace');
  assert.equal(requests[0].temperature, 0.4); assert.equal(requests[0].max_tokens, 512);
  await page.locator('.code-block .copy-btn').click();
  assert.equal(await app.evaluate(({ clipboard }) => clipboard.readText()), 'console.log("hello");');
  const first = await page.evaluate(() => window.api.getCurrentChat());
  await page.locator('#btn-chat-rename').click(); await page.locator('#chat-name').fill('Amber notebook'); await page.locator('#chat-dialog-save').click();
  await page.waitForFunction(() => document.getElementById('chat-title').textContent === 'Amber notebook');
  const exported = path.join(directory, 'export.md');
  await app.evaluate(({ dialog }, filename) => { dialog.showSaveDialog = async () => ({ canceled: false, filePath: filename }); }, exported);
  await page.locator('#btn-export').click();
  await page.waitForFunction(() => document.getElementById('activity').textContent.startsWith('Saved to'));
  assert.match(fs.readFileSync(exported, 'utf8'), /# Amber notebook/);
  await page.locator('#btn-sidebar-new').click();
  await page.waitForFunction(() => document.getElementById('chat-title').textContent === 'New conversation');
  await page.locator('#chat-search').fill('amber');
  await page.waitForFunction(() => document.querySelectorAll('.chat-item').length === 1);
  await page.locator('.chat-item').click();
  await page.waitForFunction(() => document.getElementById('chat-title').textContent === 'Amber notebook');
  assert.equal(await page.locator('.msg.user').innerText(), 'hello amber notebook\nCopy message');
  await page.locator('#chat-search').fill('');
  await page.locator('#input').fill('write a file'); await page.locator('#btn-send').click();
  await page.locator('#confirm-deny').click();
  await page.waitForFunction(() => document.getElementById('btn-stop').classList.contains('hidden'));
  assert.equal(fs.existsSync(path.join(directory, 'declined.txt')), false);
  assert.match(requests.at(-1).messages.at(-1).content, /User declined/);
  await page.locator('#input').fill('cancel this response'); await page.locator('#btn-send').click();
  await page.waitForFunction(() => document.getElementById('messages').textContent.includes('Beginning a slow reply'));
  await page.locator('#btn-stop').click();
  await page.waitForFunction(() => document.getElementById('btn-stop').classList.contains('hidden'));
  await page.locator('#input').fill('saved per-chat draft');
  await page.screenshot({ path: process.env.CHAT_SCREENSHOT || '/tmp/desktop-chat-upgraded.png' });
  await app.close(); app = null;
  page = await launch();
  assert.equal((await page.evaluate(() => window.api.getCurrentChat())).id, first.id);
  assert.equal(await page.locator('#input').inputValue(), 'saved per-chat draft');
  assert.match(await page.locator('#messages').innerText(), /Beginning a slow reply/);
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(600, 680));
  if (await page.evaluate(() => !document.body.classList.contains('sidebar-hidden'))) await page.locator('#btn-sidebar').click();
  await page.screenshot({ path: '/tmp/desktop-chat-compact.png' });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  await page.locator('#btn-chat-delete').click(); await page.locator('#chat-dialog-save').click();
  await page.waitForFunction(() => document.getElementById('chat-title').textContent === 'New conversation');
  assert.equal((await page.evaluate(() => window.api.listChats('Amber notebook'))).length, 0);
  assert.deepEqual(errors, []);
  console.log('GUI smoke passed: settings, streaming, Markdown, clipboard, history, search, rename, export, tool denial, cancellation, restart, drafts, compact layout and delete.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (app) await app.close(); server.closeAllConnections(); server.close(); fs.rmSync(directory, { recursive: true, force: true });
});
