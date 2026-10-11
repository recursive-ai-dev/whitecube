const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('btn-send');
const stopBtn = document.getElementById('btn-stop');
const newBtn = document.getElementById('btn-new');
const hideBtn = document.getElementById('btn-hide');
const settingsBtn = document.getElementById('btn-settings');

const settingsOverlay = document.getElementById('settings-overlay');
const confirmOverlay = document.getElementById('confirm-overlay');
const renameOverlay = document.getElementById('rename-overlay');
const modelPicker = document.getElementById('model-picker');
const modelDetail = document.getElementById('model-detail');
const refreshModelsBtn = document.getElementById('btn-model-refresh');
const renameModelBtn = document.getElementById('btn-model-rename');
let modelData = null;
let changingModel = false;
let renamingId = null;

function syncModals() {
  document.getElementById('app').inert = [settingsOverlay, confirmOverlay, renameOverlay].some(el => !el.classList.contains('hidden'));
}

function updateModelControls() {
  modelPicker.disabled = sending || changingModel;
  refreshModelsBtn.disabled = sending || changingModel;
  renameModelBtn.disabled = sending || changingModel || !modelData?.items.some(item => item.id === modelPicker.value);
}

function renderModels(data) {
  modelData = data;
  modelPicker.replaceChildren();
  const api = document.createElement('option');
  api.value = 'api'; api.textContent = `API · ${data.apiModel}`; modelPicker.appendChild(api);
  const group = document.createElement('optgroup'); group.label = 'Local checkpoints · ~/train-out';
  for (const item of data.items) {
    const option = document.createElement('option'); option.value = item.id; option.textContent = item.name;
    option.title = item.checkpoint; group.appendChild(option);
  }
  modelPicker.appendChild(group);
  const selected = data.chatSource === 'local' ? data.checkpointId : 'api';
  const checkpoint = data.items.find(item => item.id === selected);
  if (selected !== 'api' && !checkpoint) {
    const missing = document.createElement('option'); missing.value = selected; missing.textContent = 'Selected checkpoint is unavailable';
    modelPicker.appendChild(missing);
  }
  modelPicker.value = selected;
  if (checkpoint) {
    const params = checkpoint.parameters ? `${(checkpoint.parameters / 1e6).toFixed(2)}M parameters · ` : '';
    const context = checkpoint.contextBytes ? `${checkpoint.contextBytes}-byte context · ` : '';
    modelDetail.textContent = `${checkpoint.run} / ${checkpoint.filename} · ${params}${context}prose continuation on CPU`;
  } else if (selected !== 'api') {
    modelDetail.textContent = 'Refresh or choose another checkpoint. Switching models starts a new chat.';
  } else {
    modelDetail.textContent = `${data.items.length} local checkpoints in ${data.directory}. Switching models starts a new chat.`;
  }
  updateModelControls();
}

async function refreshModels() {
  changingModel = true; updateModelControls();
  try { renderModels(await window.api.listModels()); }
  catch (err) { modelDetail.textContent = err.message || String(err); }
  finally { changingModel = false; updateModelControls(); }
}
refreshModelsBtn.addEventListener('click', refreshModels);
window.api.onModelsChanged(renderModels);
modelPicker.addEventListener('change', async () => {
  const id = modelPicker.value;
  changingModel = true; updateModelControls();
  try {
    renderModels(await window.api.selectModel(id));
    appendBubble('notice', `New chat with ${modelPicker.selectedOptions[0].textContent}.`);
  } catch (err) {
    if (modelData) renderModels(modelData);
    appendBubble('error', err.message || String(err));
  } finally { changingModel = false; updateModelControls(); inputEl.focus(); }
});
function closeRename() { renameOverlay.classList.add('hidden'); syncModals(); renamingId = null; renameModelBtn.focus(); }
renameModelBtn.addEventListener('click', () => {
  const item = modelData?.items.find(item => item.id === modelPicker.value);
  if (!item) return;
  renamingId = item.id;
  document.getElementById('rename-file').textContent = `${item.run} / ${item.filename}`;
  document.getElementById('rename-name').value = item.customName || item.name;
  document.getElementById('rename-error').textContent = '';
  renameOverlay.classList.remove('hidden');
  syncModals();
  document.getElementById('rename-name').focus(); document.getElementById('rename-name').select();
});
document.getElementById('rename-cancel').addEventListener('click', closeRename);
document.getElementById('rename-panel').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('rename-save'); btn.disabled = true;
  try { renderModels(await window.api.renameModel(renamingId, document.getElementById('rename-name').value)); closeRename(); }
  catch (err) { document.getElementById('rename-error').textContent = err.message || String(err); }
  finally { btn.disabled = false; }
});

let currentAssistantEl = null;
let sending = false;
let stickToBottom = true;
const activityEl = document.getElementById('activity');
const latestBtn = document.getElementById('btn-latest');
const draftKey = 'desktop-chat-draft';
try { inputEl.value = localStorage.getItem(draftKey) || ''; } catch {}
function updateComposer() {
  sendBtn.disabled = !inputEl.value.trim() || changingModel;
  document.getElementById('draft-count').textContent = inputEl.value.length.toLocaleString();
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(180, Math.max(48, inputEl.scrollHeight)) + 'px';
}
function saveDraft() { try { localStorage.setItem(draftKey, inputEl.value); } catch {} updateComposer(); }
inputEl.addEventListener('input', saveDraft);
updateComposer();
sendBtn.disabled = !inputEl.value.trim();
messagesEl.addEventListener('scroll', () => {
  stickToBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 60;
  latestBtn.classList.toggle('hidden', stickToBottom);
});
latestBtn.addEventListener('click', () => { stickToBottom = true; scrollToBottom(); });

// Streaming deltas are buffered and flushed at most once per animation frame instead of
// mutating the DOM on every token — avoids O(n^2) textContent churn and per-token layout
// thrashing from scrollToBottom() on long responses.
let pendingText = '';
let rafScheduled = false;

function scrollToBottom() {
  if (stickToBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  latestBtn.classList.toggle('hidden', stickToBottom);
}

function flushPendingText() {
  rafScheduled = false;
  if (!currentAssistantEl || !pendingText) return;
  currentAssistantEl._textNode.appendData(pendingText);
  currentAssistantEl._raw += pendingText;
  pendingText = '';
  scrollToBottom();
}

function appendBubble(role, text) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  const textNode = document.createTextNode(text);
  el.appendChild(textNode);
  el._textNode = textNode;
  el._raw = text;
  messagesEl.appendChild(el);
  if (role === 'user') {
    const copy = document.createElement('button');
    copy.className = 'copy-reply'; copy.textContent = 'Copy message';
    copy.setAttribute('aria-label', 'Copy user message');
    copy.addEventListener('click', () => window.api.copyText(el._raw));
    el.appendChild(copy);
  }
  scrollToBottom();
  return el;
}

// Streaming stays plain text (cheap); once a reply is complete it is re-rendered as Markdown.
function finishAssistant() {
  flushPendingText();
  const el = currentAssistantEl;
  currentAssistantEl = null;
  if (el && el._raw) {
    el.classList.add('rendered');
    el.innerHTML = window.renderMarkdown(el._raw);
    const copy = document.createElement('button');
    copy.className = 'copy-reply'; copy.textContent = 'Copy reply';
    copy.setAttribute('aria-label', 'Copy assistant reply');
    copy.addEventListener('click', () => {
      window.api.copyText(el._raw); copy.textContent = 'Copied';
      setTimeout(() => { copy.textContent = 'Copy reply'; }, 1200);
    });
    el.appendChild(copy);
    scrollToBottom();
  }
}

messagesEl.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.copy-btn');
  const code = btn && btn.closest('.code-block') && btn.closest('.code-block').querySelector('pre code');
  if (!code) return;
  window.api.copyText(code.textContent);
  btn.textContent = 'Copied';
  setTimeout(() => { btn.textContent = 'Copy'; }, 1200);
});

function appendToolBubble(html) {
  const el = document.createElement('div');
  el.className = 'msg tool';
  el.innerHTML = html;
  messagesEl.appendChild(el);
  scrollToBottom();
  return el;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function setSending(v) {
  sending = v;
  sendBtn.classList.toggle('hidden', v);
  stopBtn.classList.toggle('hidden', !v);
  newBtn.disabled = v;
  document.getElementById('s-save').disabled = v;
  updateModelControls();
  activityEl.textContent = v ? 'Working…' : 'Ready';
  messagesEl.setAttribute('aria-busy', String(v));
  sendBtn.disabled = !inputEl.value.trim();
}

async function send() {
  const text = inputEl.value.trim();
  if (!text || sending || changingModel) return;
  inputEl.value = '';
  saveDraft(); stickToBottom = true;
  const bubble = appendBubble('user', text);
  currentAssistantEl = null;
  setSending(true);
  function restoreDraft() {
    bubble.classList.add('failed-send');
    if (!inputEl.value) { inputEl.value = text; saveDraft(); }
  }
  try {
    const result = await window.api.sendMessage(text);
    if (result?.ok === false) restoreDraft();
  }
  catch (err) { restoreDraft(); finishAssistant(); appendBubble('error', err.message || String(err)); setSending(false); }
}

sendBtn.addEventListener('click', send);
stopBtn.addEventListener('click', async () => {
  activityEl.textContent = 'Stopping…';
  try { await window.api.cancel(); }
  catch (err) { appendBubble('error', err.message || String(err)); setSending(false); }
});
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowUp' && e.altKey && !sending) {
    const last = messagesEl.querySelector('.msg.user:last-of-type') || [...messagesEl.querySelectorAll('.msg.user')].at(-1);
    if (last && !inputEl.value) { e.preventDefault(); inputEl.value = last._raw; saveDraft(); }
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});

document.addEventListener('keydown', (e) => {
  const modal = !confirmOverlay.classList.contains('hidden') ? confirmOverlay :
    !renameOverlay.classList.contains('hidden') ? renameOverlay :
    !settingsOverlay.classList.contains('hidden') ? settingsOverlay : null;
  if (modal && e.key === 'Tab') {
    const controls = [...modal.querySelectorAll('button, input, select, textarea, [tabindex="0"]')].filter(el => !el.disabled && el.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (!first) { e.preventDefault(); return; }
    if (e.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
      e.preventDefault(); first.focus();
    }
  }
  if (!modal && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); openFind(); }
  if (!modal && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'l') { e.preventDefault(); inputEl.focus(); }
  if (!modal && (e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'c' && !window.getSelection()?.toString()) { e.preventDefault(); copyChat(); }
  if (!modal && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
    e.preventDefault(); if (!sending) newBtn.click();
  }
  if (!modal && (e.ctrlKey || e.metaKey) && e.key === ',') { e.preventDefault(); openSettings(); }
  if (e.key === 'Escape') {
    if (!confirmOverlay.classList.contains('hidden')) {
      // require explicit choice
    } else if (!renameOverlay.classList.contains('hidden')) {
      closeRename();
    } else if (!settingsOverlay.classList.contains('hidden')) {
      closeSettings();
    } else if (!document.getElementById('find-bar').classList.contains('hidden')) {
      closeFind();
    } else {
      window.api.hidePanel();
    }
  }
});

newBtn.addEventListener('click', async () => {
  if (sending) return;
  try { await window.api.newChat(); clearTranscript(); }
  catch (err) { appendBubble('error', err.message || String(err)); }
});

hideBtn.addEventListener('click', () => window.api.hidePanel());

function clearTranscript() {
  messagesEl.innerHTML = '';
  currentAssistantEl = null; pendingText = ''; stickToBottom = true;
  latestBtn.classList.add('hidden');
  updateFind();
  inputEl.focus();
}
window.api.onCleared(clearTranscript);

window.api.onShown(() => {
  if (!confirmOverlay.classList.contains('hidden')) document.getElementById('confirm-deny').focus();
  else if (!renameOverlay.classList.contains('hidden')) document.getElementById('rename-name').focus();
  else if (!settingsOverlay.classList.contains('hidden')) sEndpoint.focus();
  else inputEl.focus();
});

window.api.onEvent((event) => {
  if (event.type === 'text-delta') {
    if (!currentAssistantEl) currentAssistantEl = appendBubble('assistant', '');
    pendingText += event.text;
    if (!rafScheduled) {
      rafScheduled = true;
      requestAnimationFrame(flushPendingText);
    }
  } else if (event.type === 'tool-call') {
    finishAssistant();
    appendToolBubble(
      `<span class="tool-name">▶ ${escapeHtml(event.name)}</span>\n${escapeHtml(JSON.stringify(event.args, null, 2))}`
    );
  } else if (event.type === 'tool-result') {
    appendToolBubble(
      `<span class="tool-name">✓ ${escapeHtml(event.name)}</span>\n${escapeHtml(event.result)}`
    );
  } else if (event.type === 'tool-error') {
    appendToolBubble(
      `<span class="tool-name tool-error">✗ ${escapeHtml(event.name)}</span>\n<span class="tool-error">${escapeHtml(event.error)}</span>`
    );
  } else if (event.type === 'tool-declined') {
    appendToolBubble(`<span class="tool-name tool-error">✗ ${escapeHtml(event.name)} declined</span>`);
  } else if (event.type === 'notice') {
    finishAssistant();
    appendBubble('notice', event.text);
  } else if (event.type === 'error') {
    finishAssistant();
    appendBubble('error', event.message);
  }
});

window.api.onDone(() => {
  finishAssistant();
  setSending(false);
  updateFind();
  if (settingsOverlay.classList.contains('hidden') && confirmOverlay.classList.contains('hidden')) inputEl.focus();
});

// ---- confirmation dialog ----

let activeConfirm = null;
const confirmTitle = document.getElementById('confirm-title');
const confirmBody = document.getElementById('confirm-body');

window.api.onConfirmRequest(({ requestId, name, args, preview }) => {
  activeConfirm = requestId;
  confirmTitle.textContent = `Allow ${name}?`;
  confirmBody.textContent = preview || JSON.stringify(args, null, 2);
  confirmOverlay.classList.remove('hidden');
  syncModals();
  document.getElementById('confirm-deny').focus();
});
window.api.onConfirmCancelled(() => {
  activeConfirm = null; confirmOverlay.classList.add('hidden');
  syncModals();
});
document.getElementById('confirm-stop').addEventListener('click', () => stopBtn.click());

document.getElementById('confirm-allow').addEventListener('click', () => {
  if (activeConfirm) window.api.respondConfirm(activeConfirm, true);
  activeConfirm = null;
  confirmOverlay.classList.add('hidden');
  syncModals();
  stopBtn.focus();
});

document.getElementById('confirm-deny').addEventListener('click', () => {
  if (activeConfirm) window.api.respondConfirm(activeConfirm, false);
  activeConfirm = null;
  confirmOverlay.classList.add('hidden');
  syncModals();
  stopBtn.focus();
});

// ---- settings ----

const sEndpoint = document.getElementById('s-endpoint');
const sModel = document.getElementById('s-model');
const sApiKey = document.getElementById('s-apikey');
const sConfirm = document.getElementById('s-confirm');
const sHotkey = document.getElementById('s-hotkey');
const sKeyStorage = document.getElementById('s-keystorage');

async function openSettings() {
  let s;
  try { s = await window.api.getSettings(); }
  catch (err) { appendBubble('error', err.message || String(err)); return; }
  sEndpoint.value = s.endpoint || '';
  sModel.value = s.model || '';
  sApiKey.value = s.apiKey || '';
  sConfirm.checked = !!s.confirmDestructive;
  sHotkey.value = s.hotkey || '';
  sKeyStorage.textContent =
    s.keyStorage === 'encrypted'
      ? 'API key is encrypted at rest via the OS keyring.'
      : 'No OS keyring available — API key is stored in plaintext in config.json.';
  document.getElementById('s-path').textContent = s.configPath ? `Config: ${s.configPath}` : '';
  document.getElementById('s-test-result').textContent = '';
  settingsOverlay.classList.remove('hidden');
  syncModals();
  sEndpoint.focus();
}

function closeSettings() { settingsOverlay.classList.add('hidden'); syncModals(); sApiKey.value = ''; inputEl.focus(); }
function editedSettings() {
  return { endpoint: sEndpoint.value.trim(), model: sModel.value.trim(), apiKey: sApiKey.value,
    confirmDestructive: sConfirm.checked, hotkey: sHotkey.value.trim() || 'CommandOrControl+Shift+T' };
}

window.api.onSettingsOpen(openSettings);

settingsBtn.addEventListener('click', openSettings);
document.getElementById('s-cancel').addEventListener('click', closeSettings);

document.getElementById('s-save').addEventListener('click', async () => {
  const resultEl = document.getElementById('s-test-result');
  const button = document.getElementById('s-save'); button.disabled = true;
  try {
    await window.api.saveSettings(editedSettings());
    await refreshModels();
    closeSettings();
  } catch (err) {
    resultEl.textContent = err.message || String(err);
  } finally { button.disabled = sending; }
});

document.getElementById('s-test').addEventListener('click', async () => {
  const resultEl = document.getElementById('s-test-result');
  resultEl.textContent = 'Testing…';
  const button = document.getElementById('s-test'); button.disabled = true;
  try {
    const r = await window.api.testSettings(editedSettings());
    resultEl.textContent = r.warning || 'Connected OK.';
  } catch (err) {
    resultEl.textContent = err.message || String(err);
  } finally { button.disabled = false; }
});

document.getElementById('s-clear').addEventListener('click', async () => {
  const resultEl = document.getElementById('s-test-result');
  try {
    await window.api.clearKey(); sApiKey.value = ''; resultEl.textContent = 'Key cleared (saved).';
  } catch (err) { resultEl.textContent = err.message || String(err); }
});

// first run: if no endpoint/model configured, nudge into settings
window.api.getSettings().then((s) => {
  if (s.needsKey && !s.apiKey) {
    appendBubble('assistant', 'Choose a local checkpoint in Talk to, or set your API endpoint, model, and key in Settings (⚙). Local checkpoints need no API key.');
  }
}).catch(err => appendBubble('error', err.message || String(err)));

inputEl.focus();
refreshModels();

let foundMessages = [], foundIndex = -1;
function updateFind() {
  const needle = document.getElementById('find-text').value.trim().normalize('NFKC').toLocaleLowerCase();
  for (const el of messagesEl.children) el.classList.remove('find-match', 'find-current');
  foundMessages = needle ? [...messagesEl.children].filter(el => (el._raw || el.textContent).normalize('NFKC').toLocaleLowerCase().includes(needle)) : [];
  for (const el of foundMessages) el.classList.add('find-match');
  foundIndex = Math.min(foundIndex, foundMessages.length - 1);
  document.getElementById('find-count').textContent = `${foundMessages.length} matching messages`;
  for (const id of ['find-prev', 'find-next']) document.getElementById(id).disabled = !foundMessages.length;
}
function navigateFind(delta) {
  if (!foundMessages.length) return;
  for (const el of foundMessages) el.classList.remove('find-current');
  foundIndex = (foundIndex + delta + foundMessages.length) % foundMessages.length;
  const el = foundMessages[foundIndex]; el.classList.add('find-current');
  stickToBottom = false; el.scrollIntoView({block:'center'});
  document.getElementById('find-count').textContent = `${foundIndex + 1} of ${foundMessages.length} matching messages`;
}
function openFind() {
  document.getElementById('find-bar').classList.remove('hidden');
  updateFind(); document.getElementById('find-text').focus();
}
function closeFind() {
  document.getElementById('find-bar').classList.add('hidden');
  document.getElementById('find-text').value = ''; updateFind(); inputEl.focus();
}
function copyChat() {
  flushPendingText();
  const text = [...messagesEl.querySelectorAll('.msg.user, .msg.assistant')].map(el => `## ${el.classList.contains('user') ? 'You' : 'Assistant'}\n\n${el._raw}`).join('\n\n');
  if (text) { window.api.copyText(text); activityEl.textContent = 'Conversation copied as Markdown'; }
}
document.getElementById('btn-find').addEventListener('click', openFind);
document.getElementById('find-close').addEventListener('click', closeFind);
document.getElementById('find-text').addEventListener('input', () => { foundIndex = -1; updateFind(); });
document.getElementById('find-text').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); navigateFind(e.shiftKey ? -1 : 1); } });
document.getElementById('find-next').addEventListener('click', () => navigateFind(1));
document.getElementById('find-prev').addEventListener('click', () => navigateFind(-1));
document.getElementById('btn-copy-chat').addEventListener('click', copyChat);
