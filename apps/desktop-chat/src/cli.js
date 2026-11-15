'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { isLocalEndpoint, validateSettings } = require('./endpoint');
const { discoverCheckpoints, runtime: localRuntime } = require('./local-models');
const { exportMarkdown, validChat } = require('./history');

const UUID_RE = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const MAX_CONFIG_BYTES = 1024 * 1024;
const MAX_CHAT_BYTES = 32 * 1024 * 1024;

function readRegularText(filename, maxBytes, tooLargeMessage) {
  const lst = fs.lstatSync(filename);
  if (!lst.isFile()) throw new Error(`Expected regular file: ${filename}`);
  if (lst.size > maxBytes) throw new Error(tooLargeMessage || `File exceeds ${maxBytes} bytes.`);
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0);
  const fd = fs.openSync(filename, flags);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile()) throw new Error(`Expected regular file: ${filename}`);
    if (st.size > maxBytes) throw new Error(tooLargeMessage || `File exceeds ${maxBytes} bytes.`);
    return fs.readFileSync(fd, 'utf8');
  } finally {
    fs.closeSync(fd);
  }
}

function appRoot() {
  return path.resolve(__dirname, '..');
}

function readVersion() {
  const root = appRoot();
  const versionFile = path.join(root, 'VERSION');
  try {
    const lst = fs.lstatSync(versionFile);
    if (lst.isFile()) return readRegularText(versionFile, 128).trim();
  } catch {}
  try {
    const pkg = JSON.parse(readRegularText(path.join(root, 'package.json'), 128 * 1024));
    if (pkg && typeof pkg.version === 'string' && pkg.version) return pkg.version;
  } catch {}
  return '0.0.0';
}

function userDataDir() {
  if (process.env.DESKTOP_CHAT_USER_DATA) return path.resolve(process.env.DESKTOP_CHAT_USER_DATA);
  const cfgHome = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config');
  return path.join(cfgHome, 'desktop-chat');
}

function trainOutDir() {
  return process.env.WHITECUBE_TRAIN_OUT || path.join(os.homedir(), 'train-out');
}

function loadRawConfig(dir) {
  const file = path.join(dir, 'config.json');
  try {
    fs.lstatSync(file);
  } catch (err) {
    if (err.code === 'ENOENT') return { present: false, path: file, data: {}, error: null };
    return { present: true, path: file, data: {}, error: err.message };
  }
  try {
    const parsed = JSON.parse(readRegularText(file, MAX_CONFIG_BYTES, 'Config file is too large.') || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { present: true, path: file, data: {}, error: 'Config root must be a JSON object.' };
    }
    return { present: true, path: file, data: parsed, error: null };
  } catch (err) {
    return { present: true, path: file, data: {}, error: err.message };
  }
}

function readConversation(conversationsDir, id) {
  if (typeof id !== 'string' || !UUID_RE.test(id)) throw new Error('Invalid conversation ID.');
  const file = path.join(conversationsDir, `${id}.json`);
  const parsed = JSON.parse(readRegularText(file, MAX_CHAT_BYTES, 'Conversation exceeds 32 MB limit.'));
  if (!validChat(parsed, id)) throw new Error('Conversation data is damaged.');
  return parsed;
}

function scanConversations(conversationsDir, query = '') {
  let lst;
  try {
    lst = fs.lstatSync(conversationsDir);
  } catch (err) {
    if (err.code === 'ENOENT') return { present: false, error: null, items: [], invalidCount: 0 };
    return { present: true, error: err.message, items: [], invalidCount: 0 };
  }
  if (!lst.isDirectory()) {
    return { present: true, error: 'Conversations path is not a real directory.', items: [], invalidCount: 0 };
  }
  if (typeof query !== 'string' || query.length > 1000) {
    throw new Error('Enter a search of up to 1,000 characters.');
  }
  const needle = query.trim().toLowerCase();
  const items = [];
  let invalidCount = 0;
  for (const name of fs.readdirSync(conversationsDir)) {
    if (!name.endsWith('.json')) continue;
    const id = name.slice(0, -5);
    if (!UUID_RE.test(id)) continue;
    try {
      const chat = readConversation(conversationsDir, id);
      const texts = chat.messages.filter(m => m.role === 'user' || m.role === 'assistant');
      if (needle && !`${chat.title}\n${texts.map(m => m.content).join('\n')}`.toLowerCase().includes(needle)) continue;
      items.push({
        id: chat.id,
        title: chat.title,
        updatedAt: chat.updatedAt,
        createdAt: chat.createdAt,
        messageCount: texts.length,
        preview: String(texts.at(-1)?.content || 'No messages yet').slice(0, 180),
        source: chat.source
      });
    } catch {
      invalidCount += 1;
    }
  }
  items.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt) || a.id.localeCompare(b.id));
  return { present: true, error: null, items, invalidCount };
}

function cmdDoctor(args) {
  let asJson = false;
  for (const arg of args) {
    if (arg === '--json') asJson = true;
    else throw new Error(`Unknown option for doctor: ${arg}`);
  }
  const dir = userDataDir();
  const convDir = path.join(dir, 'conversations');
  const cfgState = loadRawConfig(dir);
  const convScan = scanConversations(convDir);

  const raw = cfgState.data;
  const endpoint = typeof raw.endpoint === 'string' && raw.endpoint ? raw.endpoint : 'https://api.openai.com/v1';
  const model = typeof raw.model === 'string' && raw.model ? raw.model : 'gpt-4o-mini';
  const hasApiKey = Boolean(
    (typeof raw.apiKeyEncrypted === 'string' && raw.apiKeyEncrypted) ||
    (typeof raw.apiKeyPlain === 'string' && raw.apiKeyPlain)
  );
  let validationOk = true;
  let validationError = null;
  try {
    validateSettings({
      endpoint,
      model,
      apiKey: hasApiKey ? 'configured' : '',
      temperature: raw.temperature ?? 0.7,
      maxTokens: raw.maxTokens ?? 8192
    });
  } catch (err) {
    validationOk = false;
    validationError = err.message;
  }

  let checkpointCount = 0;
  let checkpointError = null;
  try {
    const aliases = raw.modelAliases && typeof raw.modelAliases === 'object' && !Array.isArray(raw.modelAliases) ? raw.modelAliases : {};
    checkpointCount = discoverCheckpoints(trainOutDir(), aliases).length;
  } catch (err) {
    checkpointError = err.message;
  }

  let runtimeStatus;
  try {
    const rt = localRuntime();
    runtimeStatus = { ready: true, python: rt.python, moduleDir: rt.moduleDir };
  } catch (err) {
    runtimeStatus = { ready: false, error: err.message };
  }

  const ok = cfgState.error === null && convScan.error === null && checkpointError === null;
  const payload = {
    ok,
    version: readVersion(),
    node: process.version,
    userData: dir,
    config: {
      present: cfgState.present,
      path: cfgState.path,
      error: cfgState.error,
      chatSource: raw.chatSource === 'local' ? 'local' : 'api',
      endpoint,
      localEndpoint: isLocalEndpoint(endpoint),
      model,
      checkpointId: typeof raw.checkpointId === 'string' ? raw.checkpointId : '',
      toolsEnabled: raw.toolsEnabled !== false,
      confirmDestructive: raw.confirmDestructive !== false,
      hasApiKey,
      validationOk,
      validationError
    },
    conversations: {
      present: convScan.present,
      path: convDir,
      error: convScan.error,
      count: convScan.items.length,
      invalidCount: convScan.invalidCount
    },
    checkpoints: {
      directory: trainOutDir(),
      count: checkpointCount,
      error: checkpointError
    },
    localRuntime: runtimeStatus
  };

  if (asJson) {
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  } else {
    process.stdout.write(`Desktop Chat ${payload.version} (Node ${payload.node})\n`);
    process.stdout.write(`userData: ${payload.userData}\n`);
    process.stdout.write(`config: ${payload.config.present ? (payload.config.error || 'ok') : 'not configured yet'} (${payload.config.chatSource}: ${payload.config.model})\n`);
    process.stdout.write(`conversations: ${payload.conversations.count} valid, ${payload.conversations.invalidCount} unreadable\n`);
    process.stdout.write(`checkpoints: ${payload.checkpoints.count} (${payload.localRuntime.ready ? 'local server ready' : 'local Python runtime unavailable'})\n`);
  }
  return ok ? 0 : 1;
}

function cmdModels(args) {
  let asJson = false;
  for (const arg of args) {
    if (arg === '--json') asJson = true;
    else throw new Error(`Unknown option for models: ${arg}`);
  }
  const cfgState = loadRawConfig(userDataDir());
  const aliases = cfgState.data.modelAliases && typeof cfgState.data.modelAliases === 'object' && !Array.isArray(cfgState.data.modelAliases)
    ? cfgState.data.modelAliases
    : {};
  const models = discoverCheckpoints(trainOutDir(), aliases);
  if (asJson) {
    process.stdout.write(`${JSON.stringify(models, null, 2)}\n`);
  } else if (!models.length) {
    process.stdout.write(`No local checkpoints found in ${trainOutDir()}.\n`);
  } else {
    for (const item of models) {
      process.stdout.write(`${item.id}\t${item.name}\t${item.bytes} bytes\n`);
    }
  }
  return 0;
}

function cmdChats(args) {
  let asJson = false;
  let query = '';
  let limit = null;
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === '--json') asJson = true;
    else if (arg === '--query') {
      if (i + 1 >= args.length) throw new Error('--query requires a value.');
      query = args[++i];
    } else if (arg === '--limit') {
      if (i + 1 >= args.length) throw new Error('--limit requires a positive integer.');
      const parsed = Number(args[++i]);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > 10000) throw new Error('--limit must be an integer between 1 and 10000.');
      limit = parsed;
    } else {
      throw new Error(`Unknown option for chats: ${arg}`);
    }
  }
  const convDir = path.join(userDataDir(), 'conversations');
  const scan = scanConversations(convDir, query);
  if (scan.error) throw new Error(scan.error);
  const items = limit !== null ? scan.items.slice(0, limit) : scan.items;
  if (asJson) {
    process.stdout.write(`${JSON.stringify(items, null, 2)}\n`);
  } else if (!items.length) {
    process.stdout.write('No matching conversations found.\n');
  } else {
    for (const item of items) {
      process.stdout.write(`${item.id}\t${item.updatedAt}\t${item.messageCount} msgs\t${item.title}\n`);
    }
  }
  return 0;
}

function cmdExport(args) {
  let format = 'markdown';
  let id = '';
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === '--format') {
      if (i + 1 >= args.length) throw new Error('--format requires markdown or json.');
      format = args[++i];
      if (format !== 'markdown' && format !== 'json') throw new Error('--format must be markdown or json.');
    } else if (arg.startsWith('-')) {
      throw new Error(`Unknown option for export: ${arg}`);
    } else if (!id) {
      id = arg;
    } else {
      throw new Error(`Unexpected extra argument for export: ${arg}`);
    }
  }
  if (!id) throw new Error('export requires a conversation ID.');
  const convDir = path.join(userDataDir(), 'conversations');
  const chat = readConversation(convDir, id);
  if (format === 'json') {
    process.stdout.write(`${JSON.stringify(chat, null, 2)}\n`);
  } else {
    process.stdout.write(exportMarkdown(chat));
    if (!exportMarkdown(chat).endsWith('\n')) process.stdout.write('\n');
  }
  return 0;
}

function usage() {
  process.stdout.write(`Usage: desktop-chat --cli <command> [options]
Headless diagnostics and conversation utilities for Desktop Chat.

Commands:
  doctor [--json]                 Inspect config, conversations, and local model runtime
  models [--json]                 List discovered local checkpoints
  chats [--query TEXT] [--limit N] [--json]
                                  List or search saved conversations
  export <ID> [--format markdown|json]
                                  Export a conversation transcript
  --version                       Print version
  --help                          Show this help
`);
}

function main(argv) {
  const cmd = argv[0];
  if (!cmd || cmd === '-h' || cmd === '--help' || cmd === 'help') {
    usage();
    return 0;
  }
  if (cmd === '-V' || cmd === '--version' || cmd === 'version') {
    process.stdout.write(`Desktop Chat ${readVersion()}\n`);
    return 0;
  }
  const rest = argv.slice(1);
  if (cmd === 'doctor') return cmdDoctor(rest);
  if (cmd === 'models') return cmdModels(rest);
  if (cmd === 'chats') return cmdChats(rest);
  if (cmd === 'export') return cmdExport(rest);
  throw new Error(`Unknown CLI command: ${cmd}`);
}

try {
  process.exitCode = main(process.argv.slice(2));
} catch (err) {
  process.stderr.write(`desktop-chat --cli: ${err.message}\n`);
  process.exitCode = 1;
}
