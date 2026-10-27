const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const ID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const MAX_FILE_BYTES = 32 * 1024 * 1024;
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
    if (chat.id !== id || typeof chat.title !== 'string' || !Array.isArray(chat.messages) || !Array.isArray(chat.context)) {
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
