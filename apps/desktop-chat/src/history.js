const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const ID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const MAX_FILE_BYTES = 32 * 1024 * 1024;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
function timestamp(value) {
  if (typeof value !== 'string' || value.length !== 24) return false;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) && date.toISOString() === value;
}
function toolCall(value) {
  return object(value) && typeof value.id === 'string' && value.id.length > 0 && value.id.length <= 512 &&
    value.type === 'function' && object(value.function) && typeof value.function.name === 'string' &&
    value.function.name.length > 0 && value.function.name.length <= 256 && typeof value.function.arguments === 'string';
}
function contextMessage(value) {
  if (!object(value)) return false;
  if (value.role === 'user') return typeof value.content === 'string';
  if (value.role === 'tool') return typeof value.content === 'string' && typeof value.tool_call_id === 'string' && value.tool_call_id.length > 0;
  if (value.role !== 'assistant' || !(typeof value.content === 'string' || value.content === null)) return false;
  return value.tool_calls === undefined || Array.isArray(value.tool_calls) && value.tool_calls.length > 0 && value.tool_calls.length <= 64 && value.tool_calls.every(toolCall);
}
function displayMessage(value) {
  return object(value) && ['user', 'assistant', 'tool', 'notice', 'error'].includes(value.role) && typeof value.content === 'string';
}
function validChat(chat, id) {
  return object(chat) && chat.id === id && typeof chat.title === 'string' && chat.title.trim().length > 0 &&
    chat.title.length <= 120 && !/[\x00-\x1f\x7f]/.test(chat.title) &&
    timestamp(chat.createdAt) && timestamp(chat.updatedAt) &&
    (chat.customTitle === undefined || typeof chat.customTitle === 'boolean') &&
    Array.isArray(chat.messages) && chat.messages.every(displayMessage) &&
    Array.isArray(chat.context) && chat.context.every(contextMessage) &&
    object(chat.source) && ['api', 'local'].includes(chat.source.chatSource) &&
    ['endpoint', 'model', 'checkpointId'].every(key => typeof chat.source[key] === 'string');
}
function sourceOf(settings = {}) {
  return { chatSource: settings.chatSource === 'local' ? 'local' : 'api',
    checkpointId: typeof settings.checkpointId === 'string' ? settings.checkpointId : '',
    endpoint: typeof settings.endpoint === 'string' ? settings.endpoint : '',
    model: typeof settings.model === 'string' ? settings.model : '' };
}
function atomicWrite(filename, data) {
  const temp = `${filename}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temp, JSON.stringify(data), { mode: 0o600, flag: 'wx' });
    fs.renameSync(temp, filename);
    fs.chmodSync(filename, 0o600);
  } finally { try { fs.unlinkSync(temp); } catch {} }
}
class History {
  constructor(directory) {
    this.directory = directory;
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    fs.chmodSync(directory, 0o700);
  }
  filename(id) {
    if (typeof id !== 'string' || !ID.test(id)) throw new Error('Invalid conversation ID.');
    return path.join(this.directory, `${id}.json`);
  }
  get(id) {
    const filename = this.filename(id);
    if (fs.statSync(filename).size > MAX_FILE_BYTES) throw new Error('This conversation is too large to open.');
    const chat = JSON.parse(fs.readFileSync(filename, 'utf8'));
    if (!validChat(chat, id)) {
      throw new Error('Conversation data is damaged. The original file has been preserved.');
    }
    return { ...chat, source: sourceOf(chat.source) };
  }
  list(query = '') {
    if (typeof query !== 'string' || query.length > 1000) throw new Error('Enter a search of up to 1,000 characters.');
    const needle = query.trim().toLowerCase();
    const result = [];
    for (const name of fs.readdirSync(this.directory)) {
      if (!ID.test(name.replace(/\.json$/, '')) || !name.endsWith('.json')) continue;
      try {
        const chat = this.get(name.slice(0, -5));
        const texts = chat.messages.filter(m => m.role === 'user' || m.role === 'assistant');
        if (needle && !`${chat.title}\n${texts.map(m => m.content).join('\n')}`.toLowerCase().includes(needle)) continue;
        result.push({ id: chat.id, title: chat.title, updatedAt: chat.updatedAt, createdAt: chat.createdAt,
          messageCount: texts.length, preview: String(texts.at(-1)?.content || 'No messages yet').slice(0, 180), source: chat.source });
      } catch (err) { console.warn(`Skipped unreadable conversation ${name}: ${err.message}`); }
    }
    return result.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }
  current() {
    try { return this.get(JSON.parse(fs.readFileSync(path.join(this.directory, 'active.json'), 'utf8')).id); }
    catch { const last = this.list()[0]; return last ? this.get(last.id) : null; }
  }
  activate(chat) { atomicWrite(path.join(this.directory, 'active.json'), { id: chat.id }); return chat; }
  create(settings) {
    const now = new Date().toISOString();
    const chat = { id: randomUUID(), title: 'New conversation', customTitle: false,
      createdAt: now, updatedAt: now, source: sourceOf(settings), messages: [], context: [] };
    this.save(chat); return this.activate(chat);
  }
  save(chat) {
    const serialized = JSON.stringify(chat);
    if (Buffer.byteLength(serialized) > MAX_FILE_BYTES) throw new Error('Conversation reached the 32 MB storage limit. Start a new chat.');
    atomicWrite(this.filename(chat.id), chat);
    return chat;
  }
  rename(id, title) {
    if (typeof title !== 'string' || !title.trim() || title.trim().length > 120 || /[\x00-\x1f\x7f]/.test(title)) {
      throw new Error('Use a conversation title of 1–120 characters without line breaks.');
    }
    const chat = this.get(id); chat.title = title.trim(); chat.customTitle = true;
    return this.save(chat);
  }
  delete(id) { fs.unlinkSync(this.filename(id)); }
}
function exportMarkdown(chat) {
  const lines = [`# ${chat.title}`, '', `Created: ${chat.createdAt}`, ''];
  for (const message of chat.messages) {
    if (message.role === 'user' || message.role === 'assistant') {
      lines.push(`## ${message.role === 'user' ? 'You' : 'Assistant'}`, '', String(message.content || ''), '');
    } else if (message.content) lines.push(`> ${message.role}: ${String(message.content).replace(/\n/g, '\n> ')}`, '');
  }
  return lines.join('\n');
}
module.exports = { History, sourceOf, exportMarkdown };
