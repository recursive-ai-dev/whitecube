'use strict';
// Drives the running app over the Chrome DevTools protocol. Usage: node e2e-driver.cjs PORT OUTDIR
const fs = require('node:fs');
const path = require('node:path');
const { buildSync } = require('esbuild');
const [port, outDir] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const exceptions = [];
const check = (name, ok, extra = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${extra ? ` (${extra})` : ''}`); };

(async () => {
  let targets;
  for (let i = 0; i < 60; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (targets.some((t) => t.type === 'page')) break; } catch { /* not up yet */ }
    await sleep(500);
  }
  const page = targets?.find((t) => t.type === 'page');
  if (!page) { console.log('FAIL no page target'); process.exit(1); }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener('open', r));
  let id = 0; const pending = new Map();
  ws.addEventListener('message', (m) => { const d = JSON.parse(m.data); if (d.method === 'Runtime.exceptionThrown') exceptions.push(d.params.exceptionDetails.text); if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); } });
  const send = (method, params = {}) => new Promise((r) => { const n = ++id; pending.set(n, r); ws.send(JSON.stringify({ id: n, method, params })); });
  const ev = async (expr) => { const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true }); if (r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails)); return r.result.result.value; };
  const text = () => ev('document.body.innerText');
  const waitFor = async (pred, ms = 8000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { const t = await text(); if (pred(t)) return t; await sleep(200); } return text(); };
  const shot = async (name) => { const r = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(outDir, `${name}.png`), Buffer.from(r.result.data, 'base64')); };
  const nav = (label) => ev(`[...document.querySelectorAll('aside button')].find(b => b.innerText.includes(${JSON.stringify(label)})).click()`);

  await send('Page.enable');
  await send('Runtime.enable');
  check('bridge exposed', await ev('typeof window.lab.run') === 'function');
  check('node not exposed to page', await ev('typeof require === "undefined" && typeof process === "undefined"'));
  // CDP evaluation bypasses CSP, so probe it the way page content would: an injected inline script.
  check('csp blocks inline script', await ev('(() => { const el = document.createElement("script"); el.textContent = "window.__inl = 1"; document.head.appendChild(el); return window.__inl === undefined; })()'));

  // Reload and sample quickly: the chain is fast, so catch it mid-flight.
  await send('Page.reload');
  let boot = '';
  for (let i = 0; i < 100 && !/boot chain/i.test(boot); i++) { await sleep(50); try { boot = await text(); } catch { /* page reloading */ } }
  check('boot chain shows all stages', /Wayland gate/.test(boot) && /Wallpaper \(picsum\)/.test(boot) && /Updates/.test(boot));
  await shot('boot');
  for (let i = 0; i < 80 && await ev('!!document.querySelector("[role=status]")'); i++) await sleep(500);
  check('boot chain completes', await ev('!document.querySelector("[role=status]")'));
  check('wallpaper canvas present', await ev('!!document.querySelector("canvas[data-zoomed]")'));
  let t = await waitFor((x) => /CPU/.test(x) && /Memory/.test(x));
  check('overview shows live meters', /CPU/i.test(t) && /Memory/i.test(t) && /Storage/i.test(t));
  await sleep(2500); await shot('overview');
  check('header reports wayland', /wayland/.test(await text()));
  check('icon font loads locally', await ev('document.fonts.load(\'20px "Material Symbols Outlined Variable"\').then(f => f.length > 0)'));
  check('layout styles are present', await ev('getComputedStyle(document.querySelector("aside")).width === "256px"'));
  if (process.env.CC_SMOKE_ONLY === '1') {
    const info = await ev('window.lab.appInfo()');
    const documentInfo = await ev('({ title: document.title, header: document.querySelector("header").innerText })');
    fs.writeFileSync(path.join(outDir, 'runtime-info.json'), JSON.stringify({ ...info, ...documentInfo }, null, 2) + '\n');
    check('installed runtime is packaged', info.packaged === true, `v${info.version}; Electron ${info.electron}`);
    check('no uncaught renderer exceptions', exceptions.length === 0, exceptions.join('; '));
    console.log(`${results.filter(Boolean).length}/${results.length} smoke checks passed`);
    process.exit(results.every(Boolean) ? 0 : 1);
  }

  // Terminal: real command, real cwd tracking
  await ev('document.querySelector("button[aria-label=\\\"Expand terminal\\\"]")?.click()');
  await ev('document.getElementById("cc-term-input").focus()');
  const type = async (s) => { await send('Input.insertText', { text: s }); await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r' }); await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 }); };
  await type('echo hello-from-bash && cd /usr');
  t = await waitFor((x) => /\[done\]/.test(x));
  check('command output is real', /hello-from-bash/.test(t));
  check('exit reported', /\[done\] exit 0/.test(t));
  await ev('document.getElementById("cc-term-input").focus()');
  await type('pwd');
  t = await waitFor((x) => (x.match(/\[done\]/g) || []).length >= 2);
  check('cd persists between commands', /\n\/usr\n/.test(t) || /:\/usr\$ pwd/.test(t), 'prompt shows /usr');
  await ev('document.getElementById("cc-term-input").focus()');
  await type('sh -c "exit 7"');
  t = await waitFor((x) => /\[failed\] exit 7/.test(x));
  check('failure exit code reported', /\[failed\] exit 7/.test(t));
  await ev('document.getElementById("cc-term-input").focus()');
  await type('sleep 30');
  await sleep(500);
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'c', code: 'KeyC', modifiers: 2, windowsVirtualKeyCode: 67 });
  t = await waitFor((x) => /stopped \(SIGTERM\)/.test(x));
  check('ctrl+c stops running command', /stopped \(SIGTERM\)/.test(t));
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }))'); await sleep(100);
  await send('Input.insertText', { text: 'Run Disk Usage' }); await sleep(100);
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 }); await sleep(400);
  check('palette execution uses the current working directory', await ev('window.lab.activity().then(r => r.records[0].cwd === "/usr")'));
  await shot('terminal');
  await ev('[...document.querySelectorAll("button")].find(b => b.getAttribute("aria-label") === "Collapse terminal").click()');

  for (const [label, expect] of [['Processes', /Command/], ['Storage', /Device map/], ['Installed Packages', /installed size reported by dpkg/], ['Diagnostics', /System diagnostics/], ['Command Activity', /Review & repeat/], ['Settings & Backups', /Export backup/], ['Network', /Connectivity probe/i], ['Services', /Services|\.service|Loading|system/i], ['Journal', /Errors|live/], ['Updates', /Up to date|updates available|Checking|apt/i], ['Display Session', /Wayland sockets/], ['Kernel Settings', /Search sysctl|kernel|net\./i], ['All Commands', /System Update/]]) {
    await nav(label); await sleep(1800);
    const x = await text();
    check(`view: ${label}`, expect.test(x));
    await shot(label.toLowerCase().replace(/\W+/g, '-'));
  }
  await nav('Display Session'); await sleep(800);
  const d = await text();
  check('display panel: wayland session', /Session type\s*wayland/.test(d.replace(/\n/g, ' ')) || /wayland/.test(d));
  check('display panel: DISPLAY hidden', /hidden from this app/.test(d));

  check('storage bridge returns real filesystems', await ev('window.lab.storage().then(r => r.filesystems.length > 0 && r.devices.length > 0)'));
  check('package inventory returns installed packages', await ev('window.lab.packages().then(r => r.packages.some(p => p.name === "bash"))'));
  check('diagnostics returns service and package checks', await ev('window.lab.diagnostics().then(r => r.checks.some(c => c.id === "services") && r.checks.some(c => c.id === "packages"))'));
  check('activity records success and failure', await ev('window.lab.activity().then(r => r.records.some(x => x.status === "success") && r.records.some(x => x.code === 7))'));
  check('invalid export names fail before opening a dialog', await ev('window.lab.saveFile("../../bad", "data").then(r => !!r.error)'));
  check('invalid service names are rejected', await ev('window.lab.serviceAction("user", "--help", "start").then(r => r.ok === false)'));
  check('kernel settings are readable with a desktop PATH', await ev('window.lab.sysctl.list().then(r => r.some(e => e.key === "vm.swappiness"))'));
  await nav('Command Activity'); await sleep(400);
  await ev('[...document.querySelectorAll("main button")].find(b => b.textContent === "Review & repeat").click()');
  check('activity replay requires confirmation', await ev('!!document.querySelector("[role=dialog]")'));
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))');
  await sleep(100);
  check('escape dismisses confirmation', await ev('!document.querySelector("[role=dialog]")'));
  await nav('Installed Packages'); await sleep(300);
  await ev('document.querySelector("input[aria-label^=Search]").focus()');
  await send('Input.insertText', { text: 'this-package-does-not-exist-cc-test' });
  t = await waitFor(x => /No packages match/.test(x));
  check('package search filters the inventory', /No packages match/.test(t));
  // Reload verifies history persistence through the main process and renderer restart.
  await ev('localStorage.setItem("cc:boot", "0")');
  await send('Page.reload'); await sleep(800);
  check('startup preference survives reload', await ev('!document.querySelector("[aria-label=" + JSON.stringify("Starting Whitecube Command Center") + "]")'));
  check('activity survives renderer reload', await ev('window.lab.activity().then(r => r.records.length >= 4)'));

  const sig = await ev('(async () => { const r = await window.lab.signal(1, "SIGTERM"); return r.ok === false; })()');
  check('cannot signal pid 1', sig);
  const probe = await ev('window.lab.probe(["127.0.0.1"]).then(r => r[0].loss)');
  check('probe reaches localhost', probe === 0, String(probe));
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }))');
  await sleep(300);
  check('ctrl+k opens palette', /Go to Network/.test(await text()));
  await shot('palette');
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))');
  await nav('Overview'); await sleep(300);
  check('overview has trends, attention and pinned commands', /Resource trends/.test(await text()) && /Attention center/.test(await text()) && /Pinned commands/.test(await text()));
  const sampleCount = await ev('Number(document.querySelector("svg[aria-label*=history]").getAttribute("aria-label").match(/(\\d+) samples/)[1])');
  await nav('Storage'); await sleep(300); await nav('Overview'); await sleep(300);
  check('resource history survives view navigation', await ev('Number(document.querySelector("svg[aria-label*=history]").getAttribute("aria-label").match(/(\\d+) samples/)[1])') >= sampleCount);
  await ev('[...document.querySelectorAll("[aria-label=\\\"Trend metric\\\"] button")].find(b => b.textContent === "Memory").click()');
  check('memory trend can be selected', await ev('document.querySelector("svg[aria-label*=history]").getAttribute("aria-label").startsWith("Memory")'));
  await ev('[...document.querySelectorAll("[aria-label=\\\"Trend time window\\\"] button")].find(b => b.textContent === "10m").click()');
  check('ten-minute history can be selected', await ev('document.querySelector("svg[aria-label*=history]").getAttribute("aria-label").includes("10 minutes")'));
  await nav('All Commands'); await sleep(200);
  check('command cards show the actual shell command', await ev('!!document.querySelector("article code")'));
  await ev('document.querySelector("button[aria-label=\\\"Unpin Disk Usage\\\"]").click()'); await sleep(100);
  check('command can be unpinned', await ev('!!document.querySelector("button[aria-label=\\\"Pin Disk Usage\\\"]")'));
  await ev('document.querySelector("button[aria-label=\\\"Pin Disk Usage\\\"]").click()');
  await ev('document.querySelector("button[aria-label=\\\"Edit Disk Usage\\\"]").focus(); document.activeElement.click()'); await sleep(100);
  check('command editor focuses its named input', await ev('document.activeElement.getAttribute("aria-label") === "Command name"'));
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', modifiers: 8, windowsVirtualKeyCode: 9 });
  check('shift-tab is trapped inside the command editor', await ev('document.querySelector("[role=dialog]").contains(document.activeElement)'));
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))'); await sleep(100);
  check('closing the editor restores its trigger focus', await ev('document.activeElement.getAttribute("aria-label") === "Edit Disk Usage"'));
  await nav('Shortcut Groups'); await sleep(100);
  await ev('[...document.querySelectorAll("main button")].find(b => b.innerText.includes("New Group")).click()');
  await ev('document.querySelector("input[aria-label=\\\"Trigger shortcut\\\"]").focus()'); await send('Input.insertText', { text: 'Ctrl+Shift+G' }); await sleep(100);
  check('duplicate group shortcuts are explained and cannot be saved', await ev('document.body.innerText.includes("already used") && [...document.querySelectorAll("button")].find(b => b.textContent === "Save Group").disabled'));
  await nav('Settings & Backups'); await sleep(100);
  await ev('(() => { const el = document.querySelector("select[aria-label=\\\"Monitoring refresh interval\\\"]"); el.value = "5000"; el.dispatchEvent(new Event("change", { bubbles: true })); })()');
  check('monitoring frequency is stored', await ev('localStorage.getItem("cc:refresh-ms") === "5000"'));
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "G", ctrlKey: true, shiftKey: true, bubbles: true }))'); await sleep(100);
  check('Core Actions uses a shortcut the desktop does not intercept', await ev('document.querySelector("[role=dialog]")?.getAttribute("aria-label") === "Core Actions"'));
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))');
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }))'); await sleep(100);
  await send('Input.insertText', { text: 'Open Core Actions' }); await sleep(100);
  check('shortcut groups are discoverable in the palette', /Open Core Actions/.test(await text()));
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 }); await sleep(100);
  check('palette opens shortcut groups', await ev('document.querySelector("[role=dialog]")?.getAttribute("aria-label") === "Core Actions"'));
  await ev('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))');

  await nav('Overview');
  await send('Emulation.setDeviceMetricsOverride', { width: 1360, height: 960, deviceScaleFactor: 1, mobile: false }); await sleep(200); await shot('overview-improved');
  await send('Emulation.setDeviceMetricsOverride', { width: 960, height: 720, deviceScaleFactor: 1, mobile: false }); await sleep(200); await shot('overview-compact');
  check('compact layout fits the window width', await ev('document.documentElement.scrollWidth <= 960'));
  await send('Emulation.clearDeviceMetricsOverride');
  const fixture = buildSync({ entryPoints: [path.join(__dirname, '../tests/polling-fixture.tsx')], bundle: true, platform: 'browser', format: 'iife', write: false }).outputFiles[0].text;
  await ev(fixture);
  for (const [name, ok] of await ev('window.__runPollingChecks()')) check(name, ok);
  check('no uncaught renderer exceptions', exceptions.length === 0, exceptions.join('; '));

  const bad = results.filter((r) => !r).length;
  console.log(bad ? `${bad} FAILED` : `ALL ${results.length} CHECKS PASSED`);
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.log('FAIL driver error', e.message); process.exit(1); });
