const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('btn-send');
const stopBtn = document.getElementById('btn-stop');
const newBtn = document.getElementById('btn-new');
const hideBtn = document.getElementById('btn-hide');
const settingsBtn = document.getElementById('btn-settings');
const chatDialogOverlay = document.getElementById('chat-dialog-overlay');
const emptyState = document.getElementById('empty-state');
const chatList = document.getElementById('chat-list');
let currentChat = null;
let switchingChat = true;
let chatDialogAction = null;
let chatListRequest = 0;
let turnUsage = null;

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
  document.getElementById('app').inert = [settingsOverlay, confirmOverlay, renameOverlay, chatDialogOverlay].some(el => !el.classList.contains('hidden'));
}

function updateModelControls() {
  modelPicker.disabled = sending || changingModel || switchingChat;
  refreshModelsBtn.disabled = sending || changingModel || switchingChat;
  renameModelBtn.disabled = sending || changingModel || switchingChat || !modelData?.items.some(item => item.id === modelPicker.value);
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
    modelDetail.textContent = data.items.length ? `${data.items.length} local checkpoints available. Switching models starts a new conversation.` : 'Configure your API connection and filesystem tools in Settings.';
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
function draftKey() { return `desktop-chat-draft:${currentChat?.id || 'unsaved'}`; }
function loadDraft() {
  try {
    let draft = localStorage.getItem(draftKey());
    if (draft === null && !(currentChat?.messages?.length)) {
      draft = localStorage.getItem('desktop-chat-draft');
      if (draft !== null) { localStorage.setItem(draftKey(), draft); localStorage.removeItem('desktop-chat-draft'); }
    }
    inputEl.value = draft || '';
  } catch { inputEl.value = ''; }
  updateComposer();
}
function updateComposer() {
  sendBtn.disabled = !inputEl.value.trim() || changingModel || switchingChat || sending;
  document.getElementById('draft-count').textContent = inputEl.value.length.toLocaleString();
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(180, Math.max(48, inputEl.scrollHeight)) + 'px';
}
function saveDraft() { try { localStorage.setItem(draftKey(), inputEl.value); } catch {} updateComposer(); }
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
  updateEmptyState();
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
  updateEmptyState();
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
  document.body.classList.toggle('sending', v);
  updateChatControls();
  document.getElementById('s-save').disabled = v;
  updateModelControls();
  activityEl.textContent = v ? 'Working…' : 'Ready';
  messagesEl.setAttribute('aria-busy', String(v));
  updateComposer();
}

async function send() {
  const text = inputEl.value.trim();
  if (!text || sending || changingModel || switchingChat) return;
  turnUsage = null;
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
    !chatDialogOverlay.classList.contains('hidden') ? chatDialogOverlay :
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
  if (!modal && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') { e.preventDefault(); toggleSidebar(); }
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
    } else if (!chatDialogOverlay.classList.contains('hidden')) {
      closeChatDialog();
    } else if (!renameOverlay.classList.contains('hidden')) {
      closeRename();
    } else if (!settingsOverlay.classList.contains('hidden')) {
      closeSettings();
    } else if (!document.getElementById('find-bar').classList.contains('hidden')) {
      closeFind();
    } else if (!document.body.classList.contains('sidebar-hidden') && window.innerWidth <= 660) {
      toggleSidebar(false);
    } else {
      window.api.hidePanel();
    }
  }
});

newBtn.addEventListener('click', async () => {
  if (sending || switchingChat) return;
  saveDraft(); switchingChat = true; updateChatControls(); updateComposer();
  try { renderChat(await window.api.newChat()); await refreshChats(); }
  catch (err) { appendBubble('error', err.message || String(err)); }
  finally { switchingChat = false; updateChatControls(); updateComposer(); }
});

hideBtn.addEventListener('click', () => window.api.hidePanel());

function clearTranscript() {
  messagesEl.innerHTML = '';
  currentAssistantEl = null; pendingText = ''; stickToBottom = true;
  latestBtn.classList.add('hidden');
  updateFind();
  updateEmptyState();
  inputEl.focus();
}
window.api.onCleared(() => {
  window.api.getCurrentChat().then(chat => { if (!sending) { renderChat(chat); refreshChats(); } }).catch(showHistoryError);
});

window.api.onShown(() => {
  if (!confirmOverlay.classList.contains('hidden')) document.getElementById('confirm-deny').focus();
  else if (!chatDialogOverlay.classList.contains('hidden')) document.getElementById('chat-name').focus();
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
  } else if (event.type === 'usage') {
    turnUsage = event.usage;
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
  if (turnUsage?.total_tokens) activityEl.textContent = `Reply complete · ${Number(turnUsage.total_tokens).toLocaleString()} tokens used`;
  refreshChats();
  updateFind();
  if (settingsOverlay.classList.contains('hidden') && confirmOverlay.classList.contains('hidden')) inputEl.focus();
});

// ---- confirmation dialog ----

let activeConfirm = null;
const confirmTitle = document.getElementById('confirm-title');
const confirmBody = document.getElementById('confirm-body');

window.api.onConfirmRequest(({ requestId, name, args, preview }) => {
  activeConfirm = requestId;
  const labels = { write_file: 'write this file', edit_file: 'edit this file', move_file: 'move this file' };
  confirmTitle.textContent = `Allow ${labels[name] || name}?`;
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
  document.getElementById('s-temperature').value = s.temperature ?? 0.7;
  document.getElementById('s-max-tokens').value = s.maxTokens ?? 8192;
  document.getElementById('s-tools').checked = s.toolsEnabled !== false;
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
    confirmDestructive: sConfirm.checked, hotkey: sHotkey.value.trim() || 'CommandOrControl+Shift+T',
    toolsEnabled: document.getElementById('s-tools').checked, temperature: Number(document.getElementById('s-temperature').value),
    maxTokens: Number(document.getElementById('s-max-tokens').value) };
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


// Saved conversations are owned by the main process. The renderer keeps only
// drafts and presentation state; tools, credentials and file writes stay in IPC.
function updateEmptyState() {
  emptyState.classList.toggle('hidden', messagesEl.children.length > 0);
}
function updateChatControls() {
  const blocked = sending || switchingChat;
  document.getElementById('btn-sidebar-new').disabled = blocked;
  newBtn.disabled = blocked;
  document.getElementById('btn-chat-rename').disabled = blocked || !currentChat;
  document.getElementById('btn-chat-delete').disabled = blocked || !currentChat;
  document.getElementById('btn-export').disabled = blocked || !currentChat;
  for (const button of chatList.querySelectorAll('button')) button.disabled = blocked;
  updateModelControls();
  updateComposer();
}
function showHistoryError(err) { activityEl.textContent = err.message || String(err); }
function chatDate(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  return date.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
}
async function refreshChats() {
  const request = ++chatListRequest;
  try {
    const list = await window.api.listChats(document.getElementById('chat-search').value.trim());
    if (request !== chatListRequest) return;
    chatList.replaceChildren();
    if (!list.length) {
      const hint = document.createElement('p'); hint.className = 'chat-empty';
      hint.textContent = document.getElementById('chat-search').value.trim() ? 'No conversations match your search.' : 'Your conversations will appear here.';
      chatList.appendChild(hint);
    }
    for (const chat of list) {
      const button = document.createElement('button'); button.className = 'chat-item';
      const active = chat.id === currentChat?.id;
      button.classList.toggle('active', active); button.setAttribute('aria-current', active ? 'true' : 'false');
      button.title = chat.title || 'New conversation';
      for (const [cls, text] of [['chat-item-title', chat.title || 'New conversation'], ['chat-item-preview', chat.preview || 'Start with a message'], ['chat-item-meta', `${chatDate(chat.updatedAt)} · ${chat.messageCount || 0} messages`]]) {
        const el = document.createElement('span'); el.className = cls; el.textContent = text; button.appendChild(el);
      }
      button.addEventListener('click', () => selectSavedChat(chat.id));
      chatList.appendChild(button);
    }
    updateChatControls();
    // Renames and automatic titles can arrive after a streamed reply.
    const active = list.find(chat => chat.id === currentChat?.id);
    if (active) { currentChat.title = active.title; document.getElementById('chat-title').textContent = active.title; }
  } catch (err) { showHistoryError(err); }
}
function renderChat(chat) {
  if (!chat) return;
  currentChat = chat;
  clearTranscript();
  document.getElementById('chat-title').textContent = chat.title || 'New conversation';
  for (const message of chat.messages || []) {
    if (message.role === 'system' || (!message.content && message.role !== 'tool')) continue;
    const role = ['user', 'assistant', 'tool', 'notice', 'error'].includes(message.role) ? message.role : 'notice';
    const content = typeof message.content === 'string' ? message.content : JSON.stringify(message.content ?? '');
    const bubble = appendBubble(role, content);
    if (role === 'assistant') { currentAssistantEl = bubble; finishAssistant(); }
  }
  loadDraft(); updateEmptyState(); updateFind(); updateChatControls();
  activityEl.textContent = 'Ready';
  if (window.innerWidth <= 660) toggleSidebar(false);
  inputEl.focus();
}
async function selectSavedChat(id) {
  if (sending || switchingChat || id === currentChat?.id) return;
  saveDraft(); switchingChat = true; updateChatControls(); updateComposer();
  try { renderChat(await window.api.selectChat(id)); await Promise.all([refreshChats(), refreshModels()]); }
  catch (err) { showHistoryError(err); }
  finally { switchingChat = false; updateChatControls(); updateComposer(); updateModelControls(); }
}
function toggleSidebar(show) {
  const visible = typeof show === 'boolean' ? show : document.body.classList.contains('sidebar-hidden');
  document.body.classList.toggle('sidebar-hidden', !visible);
  document.getElementById('btn-sidebar').setAttribute('aria-expanded', String(visible));
  if (visible) document.getElementById('chat-search').focus();
}
function closeChatDialog() {
  chatDialogOverlay.classList.add('hidden'); syncModals(); inputEl.focus();
}
function openChatDialog(action) {
  if (!currentChat || sending || switchingChat) return;
  chatDialogAction = action;
  const removing = action === 'delete';
  document.getElementById('chat-dialog-title').textContent = removing ? 'Delete conversation?' : 'Rename conversation';
  document.getElementById('chat-dialog-description').textContent = removing ? `“${currentChat.title}” will be permanently removed from this device.` : 'Give this conversation a name you can find later.';
  document.getElementById('chat-name-label').classList.toggle('hidden', removing);
  const name = document.getElementById('chat-name'); name.classList.toggle('hidden', removing); name.value = currentChat.title || '';
  const save = document.getElementById('chat-dialog-save'); save.textContent = removing ? 'Delete conversation' : 'Save name'; save.classList.toggle('danger', removing);
  document.getElementById('chat-dialog-error').textContent = '';
  chatDialogOverlay.classList.remove('hidden'); syncModals();
  if (removing) document.getElementById('chat-dialog-cancel').focus(); else { name.focus(); name.select(); }
}
const compactLayout = window.matchMedia('(max-width: 660px)');
compactLayout.addEventListener('change', event => { if (event.matches) toggleSidebar(false); });
document.getElementById('btn-sidebar').addEventListener('click', () => toggleSidebar());
document.getElementById('btn-sidebar-new').addEventListener('click', () => newBtn.click());
document.getElementById('chat-search').addEventListener('input', refreshChats);
document.getElementById('btn-chat-rename').addEventListener('click', () => openChatDialog('rename'));
document.getElementById('btn-chat-delete').addEventListener('click', () => openChatDialog('delete'));
document.getElementById('chat-dialog-cancel').addEventListener('click', closeChatDialog);
document.getElementById('chat-dialog').addEventListener('submit', async e => {
  e.preventDefault(); if (!currentChat) return;
  const button = document.getElementById('chat-dialog-save'); button.disabled = true;
  try {
    if (chatDialogAction === 'delete') {
      const oldId = currentChat.id;
      const result = await window.api.deleteChat(currentChat.id);
      try { localStorage.removeItem(`desktop-chat-draft:${oldId}`); } catch {}
      renderChat(result.activeChat || await window.api.getCurrentChat());
      await refreshModels();
    } else {
      const title = document.getElementById('chat-name').value.trim();
      if (!title) throw new Error('Enter a conversation name.');
      const chat = await window.api.renameChat(currentChat.id, title);
      currentChat.title = chat.title; document.getElementById('chat-title').textContent = chat.title;
    }
    closeChatDialog(); await refreshChats();
  } catch (err) { document.getElementById('chat-dialog-error').textContent = err.message || String(err); }
  finally { button.disabled = false; }
});
document.getElementById('btn-export').addEventListener('click', async () => {
  if (!currentChat || sending) return;
  try {
    const result = await window.api.exportChat(currentChat.id, 'markdown');
    if (!result.cancelled) activityEl.textContent = `Saved to ${result.path}`;
  } catch (err) { showHistoryError(err); }
});
document.getElementById('btn-setup').addEventListener('click', openSettings);
for (const button of document.querySelectorAll('.starter')) button.addEventListener('click', () => {
  inputEl.value = button.dataset.prompt; saveDraft(); inputEl.focus();
});
window.api.onChatsChanged(() => refreshChats());
async function initializeWorkspace() {
  if (window.innerWidth <= 660) toggleSidebar(false);
  updateChatControls(); updateComposer(); updateModelControls();
  try {
    renderChat(await window.api.getCurrentChat());
    await Promise.all([refreshChats(), refreshModels()]);
    const settings = await window.api.getSettings();
    if (settings.needsKey && !settings.apiKey && !messagesEl.children.length) activityEl.textContent = 'Choose a local model or configure your API connection in Settings';
  } catch (err) { showHistoryError(err); }
  finally { switchingChat = false; updateChatControls(); updateComposer(); updateModelControls(); }
}
initializeWorkspace();
