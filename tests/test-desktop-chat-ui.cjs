// Exercise the actual Electron renderer against a local fake API.
// Launch Chat with an isolated XDG_CONFIG_HOME and --remote-debugging-port=9223.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'whitecube-chat-ui-'));
  const written = path.join(temporary, 'must-not-be-written.txt');
  let mode = 'reply', modelRequests = 0;
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/models') { modelRequests++; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'ui-test-model' }] })); return; }
    assert.equal(req.url, '/v1/chat/completions');
    req.resume(); req.on('end', () => {
      res.setHeader('Content-Type', 'text/event-stream');
      const delta = mode === 'reply' ? { content: 'Hello **Whitecube**.\n```text\nworking chat\n```' } : {
        tool_calls: [{ index: 0, id: 'test-write', type: 'function', function: { name: 'write_file', arguments: JSON.stringify({ path: written, content: 'unwanted' }) } }]
      };
      res.end(`data: ${JSON.stringify({ choices: [{ delta, finish_reason: mode === 'reply' ? 'stop' : 'tool_calls' }] })}\n\ndata: [DONE]\n\n`);
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const endpoint = `http://localhost:${server.address().port}/v1`;
  const port = process.argv[2] || '9223';
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = pages.find(p => p.title === 'Desktop Chat'); assert.ok(page, 'Chat window loaded');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let sequence = 0; const pending = new Map();
  ws.onmessage = event => {
    const message = JSON.parse(event.data); const item = pending.get(message.id);
    if (!item) return; pending.delete(message.id);
    if (message.error) item.reject(new Error(message.error.message)); else item.resolve(message.result);
  };
  function command(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method} ${params.expression || ''}`)); }, 12000);
      pending.set(id, { resolve: value => { clearTimeout(timeout); resolve(value); }, reject: error => { clearTimeout(timeout); reject(error); } });
      ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async function evaluate(expression) {
    const response = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.text + ': ' + response.exceptionDetails.exception?.description);
    return response.result.value;
  }
  async function until(expression, timeoutMs = 10000) {
    const deadline = Date.now() + timeoutMs;
    while (!await evaluate(expression)) { assert.ok(Date.now() < deadline, `Timed out: ${expression}`); await new Promise(r => setTimeout(r, 30)); }
  }
  try {
    await until('typeof window.api === "object" && document.getElementById("activity") !== null');
    const configPath = await evaluate('window.api.getSettings().then(s => s.configPath)');
    assert.ok(configPath.includes('chat-ui-profile'), 'test must use an isolated profile');
    if (process.env.CHAT_TEST_FIRST_CHECKPOINT) {
      await until(`typeof modelData !== "undefined" && modelData?.checkpointId === ${JSON.stringify(process.env.CHAT_TEST_FIRST_CHECKPOINT)}`);
      assert.equal(await evaluate('modelPicker.value'), process.env.CHAT_TEST_FIRST_CHECKPOINT, 'first CLI launch selects its checkpoint');
    }
    await evaluate('window.api.cancel()');
    await until('!sending');
    await evaluate('window.api.selectModel("api")');
    await evaluate('window.api.saveSettings({endpoint:"https://example.invalid/v1", model:"ui-test-model", apiKey:""})');
    await evaluate('clearTranscript(); document.getElementById("input").value="missing-key test"; void send()');
    await until('document.querySelector(".msg.error") && !sending');
    assert.equal(await evaluate('document.getElementById("btn-new").disabled'), false);

    await evaluate('openSettings()');
    await until('document.activeElement.id === "s-endpoint"');
    await evaluate(`sEndpoint.value=${JSON.stringify(endpoint + '/chat/completions')}; sModel.value="ui-test-model"; sApiKey.value=""; document.getElementById("s-test").click()`);
    await until('document.getElementById("s-test-result").textContent === "Connected OK."');
    assert.equal(modelRequests, 1, 'Test used edited endpoint');
    await evaluate('document.getElementById("s-save").focus()');
    await command('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
    await command('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
    assert.equal(await evaluate('document.activeElement.id'), 's-endpoint', 'settings trap keyboard focus');
    await evaluate('document.getElementById("s-save").click()');
    await until('settingsOverlay.classList.contains("hidden")');
    await evaluate('document.getElementById("input").value="hello"; void send()');
    await until('!sending && Boolean(document.querySelector(".copy-reply"))');
    assert.match(await evaluate('document.querySelector(".msg.assistant.rendered").textContent'), /working chat/);
    assert.ok(await evaluate('document.querySelector(".msg.assistant.rendered strong") !== null'));
    if (process.argv[3]) {
      const screenshot = await command('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(process.argv[3], Buffer.from(screenshot.data, 'base64'));
    }

    mode = 'confirm';
    await evaluate('document.getElementById("input").value="prepare a write"; void send()');
    await until('!confirmOverlay.classList.contains("hidden")');
    assert.equal(await evaluate('document.activeElement.id'), 'confirm-deny');
    await evaluate('document.getElementById("confirm-stop").click()');
    await until('!sending && confirmOverlay.classList.contains("hidden")');
    assert.equal(fs.existsSync(written), false, 'Stop must prevent tool execution');

    if (process.argv.includes('--local')) {
      const list = await evaluate('window.api.listModels()');
      assert.ok(list.items.length >= 2, 'real local checkpoints discovered');
      const best = list.items.find(item => item.filename === 'ckpt_best.pt');
      const latest = list.items.find(item => item.run === best.run && item.filename === 'ckpt_latest.pt');
      assert.ok(best && latest, 'Best and Latest are separate choices');
      if (process.env.CHAT_TEST_LAUNCHER) {
        for (const checkpoint of [latest, best]) {
          const launched = spawnSync(process.env.CHAT_TEST_LAUNCHER, ['--show', '--checkpoint', checkpoint.id], { env: process.env, timeout: 15000 });
          assert.equal(launched.status, 0, launched.stderr?.toString());
          await until(`modelData?.checkpointId === ${JSON.stringify(checkpoint.id)} && modelPicker.value === ${JSON.stringify(checkpoint.id)}`);
        }
        console.log('PASS: first CLI checkpoint selection and repeat-launch Chat handoff update the actual model picker');
      }
      const oldSettings = await evaluate('window.api.getSettings()');
      await evaluate(`modelPicker.value=${JSON.stringify(best.id)}; modelPicker.dispatchEvent(new Event('change'))`);
      await until('!changingModel && modelData.chatSource === "local"');
      await evaluate('renameModelBtn.click()');
      await until('document.activeElement.id === "rename-name"');
      await evaluate('document.getElementById("rename-name").value="My <local> model"; document.getElementById("rename-save").click()');
      await until('renameOverlay.classList.contains("hidden") && modelPicker.selectedOptions[0].textContent === "My <local> model"');
      await command('Page.reload');
      await until('typeof modelData !== "undefined" && modelData?.chatSource === "local" && modelPicker.selectedOptions[0].textContent === "My <local> model"');
      assert.equal(await evaluate('document.querySelector("#model-picker local")'), null, 'display names render as text');
      for (const item of [best, latest]) {
        await evaluate(`modelPicker.value=${JSON.stringify(item.id)}; modelPicker.dispatchEvent(new Event('change'))`);
        await until(`!changingModel && modelData.checkpointId === ${JSON.stringify(item.id)}`);
        const disabled = await evaluate('inputEl.value="Once upon a time, a small cube"; void send(); modelPicker.disabled && renameModelBtn.disabled');
        assert.equal(disabled, true, 'checkpoint controls lock during generation');
        await until('!sending', 40000);
        assert.equal(await evaluate('document.querySelector(".msg.error")?.textContent || ""'), '', 'real checkpoint generated successfully');
        assert.ok(await evaluate('Boolean(document.querySelector(".msg.assistant.rendered"))'), 'real local reply streamed into the UI');
        if (process.argv[3]) {
          const shot = await command('Page.captureScreenshot', { format: 'png' });
          fs.writeFileSync(process.argv[3], Buffer.from(shot.data, 'base64'));
        }
      }
      await evaluate(`window.api.renameModel(${JSON.stringify(best.id)}, ${JSON.stringify(best.customName || '')})`);
      await evaluate('modelPicker.value="api"; modelPicker.dispatchEvent(new Event("change"))');
      await until('!changingModel && modelData.chatSource === "api"');
      const restored = await evaluate('window.api.getSettings()');
      assert.equal(restored.endpoint, oldSettings.endpoint); assert.equal(restored.model, oldSettings.model);
      assert.equal(restored.apiKey, oldSettings.apiKey);
      console.log('PASS: real ~/train-out discovery, exact Best/Latest selection and streaming, persistent rename, controls during generation, return to API settings');
    }

    await evaluate('inputEl.value="Saved unsent draft"; inputEl.dispatchEvent(new Event("input"))');
    await command('Page.reload');
    await until('typeof window.api === "object" && document.getElementById("input")?.value === "Saved unsent draft"');
    await evaluate('for (let i=0;i<60;i++) appendBubble("notice", "History row " + i + "\\n" + "text ".repeat(60)); messagesEl.scrollTop=0; messagesEl.dispatchEvent(new Event("scroll"))');
    await until('!stickToBottom');
    await evaluate('appendBubble("assistant", "New response while reading history")');
    assert.equal(await evaluate('messagesEl.scrollTop'), 0, 'new output must preserve history scroll');
    await evaluate('latestBtn.click()');
    assert.ok(await evaluate('messagesEl.scrollTop > 0 && stickToBottom'));
    console.log('PASS: actual Wayland Electron UI — settings focus/test/save, missing-key recovery, streaming/Markdown/copy, confirmation Stop, draft restoration and history scrolling');
  } finally {
    ws.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().then(() => process.exit(0), error => { console.error(error); process.exit(1); });
