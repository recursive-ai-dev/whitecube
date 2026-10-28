const { parentPort, workerData } = require('node:worker_threads');
const fs = require('node:fs');
const { History } = require('./history');

const history = new History(workerData.directory);
const cache = new Map();
const MAX_INDEX_BYTES = 64 * 1024 * 1024;
let indexBytes = 0;
function remove(id) {
  const old = cache.get(id);
  if (old) { indexBytes -= old.bytes; cache.delete(id); }
}
function list(query, options) {
  const names = fs.readdirSync(history.directory).filter(name => /^[a-f0-9-]{36}\.json$/.test(name));
  const present = new Set(names.map(name => name.slice(0, -5)));
  for (const id of cache.keys()) if (!present.has(id)) remove(id);
  const needle = query.trim().toLowerCase();
  const result = [];
  for (const id of present) {
    try {
      const stat = fs.statSync(history.filename(id), { bigint: true });
      const signature = `${stat.ino}:${stat.size}:${stat.mtimeNs}:${stat.ctimeNs}`;
      let entry = cache.get(id);
      if (!entry || entry.signature !== signature || (needle && entry.text === null)) {
        remove(id);
        const chat = history.get(id);
        const texts = chat.messages.filter(message => ['user', 'assistant'].includes(message.role));
        const text = `${chat.title}\n${texts.map(message => message.content).join('\n')}`.toLowerCase();
        entry = { signature, text, bytes: Buffer.byteLength(text), summary: {
          id, title: chat.title, updatedAt: chat.updatedAt, createdAt: chat.createdAt,
          messageCount: texts.length, preview: String(texts.at(-1)?.content || 'No messages yet').slice(0, 180), source: chat.source
        } };
        cache.set(id, entry); indexBytes += entry.bytes;
      }
      if (!needle || entry.text.includes(needle)) result.push(entry.summary);
      // Retain summaries even when large transcripts exceed the text budget.
      for (const cached of cache.values()) {
        if (indexBytes <= MAX_INDEX_BYTES) break;
        indexBytes -= cached.bytes; cached.bytes = 0; cached.text = null;
      }
    } catch { remove(id); } // Preserve damaged files and isolate them from good chats.
  }
  result.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt) || a.id.localeCompare(b.id));
  if (options === null) return result;
  const offset = options.offset ?? 0, limit = options.limit ?? 50;
  return { items: result.slice(offset, offset + limit), total: result.length, offset, hasMore: offset + limit < result.length };
}
parentPort.on('message', ({ id, query, options }) => {
  try { parentPort.postMessage({ id, value: list(query, options) }); }
  catch (error) { parentPort.postMessage({ id, error: error.message }); }
});
