const fs = require('fs');
const fsp = fs.promises;
const path = require('path');
const os = require('os');

function resolvePath(p) {
  if (typeof p !== 'string' || !p.trim() || p.includes('\0') || p.length > 4096) throw new Error('A valid file path is required.');
  let expanded = p;
  if (expanded === '~' || expanded.startsWith('~/')) {
    expanded = path.join(os.homedir(), expanded.slice(2));
  }
  return path.resolve(expanded);
}

const MAX_READ_BYTES = 30 * 1024;
const MAX_READ_LINES = 800;
const MAX_DIR_ENTRIES = 300;
const MAX_EDIT_BYTES = 8 * 1024 * 1024;
const MAX_SCAN_BYTES = 32 * 1024 * 1024;

async function bytePage(handle, byteOffset, size) {
  const data = Buffer.alloc(MAX_READ_BYTES);
  const { bytesRead } = await handle.read(data, 0, data.length, byteOffset);
  const bytes = data.subarray(0, bytesRead);
  if (bytes.includes(0)) throw new Error('This appears to be a binary file. Read a text file instead.');
  const next = byteOffset + bytesRead;
  return `[bytes ${byteOffset}-${next} of ${size}]\n${bytes.toString('utf8')}` +
    (next < size ? `\n\n[call read_file with byte_offset: ${next} to continue]` : '');
}

async function read_file({ path: rawPath, offset = 1, limit = MAX_READ_LINES, byte_offset }) {
  const target = resolvePath(rawPath);
  if (!Number.isInteger(offset) || offset < 1) throw new Error('offset must be a positive line number.');
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_READ_LINES) throw new Error(`limit must be between 1 and ${MAX_READ_LINES}.`);
  if (byte_offset !== undefined && (!Number.isSafeInteger(byte_offset) || byte_offset < 0)) throw new Error('byte_offset must be a nonnegative whole number.');
  const stat = await fsp.stat(target);
  if (stat.isDirectory()) {
    const names = [];
    let more = false;
    for await (const entry of await fsp.opendir(target)) {
      if (names.length === MAX_DIR_ENTRIES) { more = true; break; }
      names.push(entry.isDirectory() ? `${entry.name}/` : entry.name);
    }
    return `[directory listing for ${target}]\n${names.sort().join('\n')}${more ? '\n… more entries (listing limited to 300)' : ''}`;
  }
  if (!stat.isFile()) throw new Error('Only regular text files and directories can be read.');
  const handle = await fsp.open(target, 'r');
  try {
    if (byte_offset !== undefined) return await bytePage(handle, byte_offset, stat.size);
    const lines = [];
    let line = 1, position = 0, lineStart = 0, pending = Buffer.alloc(0), outputBytes = 0;
    const chunk = Buffer.alloc(16 * 1024);
    while (position < stat.size) {
      if (position >= MAX_SCAN_BYTES) throw new Error(`Line offset requires scanning more than 32 MB. Use byte_offset: ${position} to continue by bytes.`);
      const { bytesRead } = await handle.read(chunk, 0, chunk.length, position);
      if (!bytesRead) break;
      const data = chunk.subarray(0, bytesRead);
      if (data.includes(0)) throw new Error('This appears to be a binary file. Read a text file instead.');
      let cursor = 0;
      while (cursor < bytesRead) {
        const newline = data.indexOf(10, cursor);
        const end = newline < 0 ? bytesRead : newline;
        if (line >= offset) {
          pending = Buffer.concat([pending, data.subarray(cursor, end)]);
          if (pending.length > MAX_READ_BYTES - 32) {
            if (!lines.length) return await bytePage(handle, lineStart, stat.size);
            return `${lines.join('\n')}\n\n[showing lines ${offset}-${line - 1}; call read_file with offset: ${line} to continue]`;
          }
        }
        if (newline < 0) break;
        if (line >= offset) {
          const numbered = `${line}\t${pending.toString('utf8').replace(/\r$/, '')}`;
          if (outputBytes + Buffer.byteLength(numbered) > MAX_READ_BYTES || lines.length >= limit) {
            return `${lines.join('\n')}\n\n[showing lines ${offset}-${line - 1}; call read_file with offset: ${line} to continue]`;
          }
          lines.push(numbered); outputBytes += Buffer.byteLength(numbered) + 1;
        }
        line++; pending = Buffer.alloc(0);
        lineStart = position + newline + 1;
        cursor = newline + 1;
      }
      position += bytesRead;
    }
    if (line >= offset && pending.length) {
      if (lines.length >= limit || outputBytes + pending.length > MAX_READ_BYTES) return `${lines.join('\n')}\n\n[call read_file with offset: ${line} to continue]`;
      lines.push(`${line}\t${pending.toString('utf8')}`);
    }
    return lines.join('\n') || (stat.size ? `[No text at line ${offset}; the file has ${line} lines.]` : '[Empty file]');
  } finally {
    await handle.close();
  }
}

function textArgument(value, name) {
  if (typeof value !== 'string') throw new Error(`${name} must be text.`);
  if (Buffer.byteLength(value) > MAX_EDIT_BYTES) throw new Error(`${name} exceeds the 8 MB editing limit.`);
  return value;
}

async function atomicWrite(target, content) {
  let permissions = 0o600;
  try { target = await fsp.realpath(target); permissions = (await fsp.stat(target)).mode & 0o777; }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  await fsp.mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.desktop-chat-${require('crypto').randomUUID()}`;
  try {
    await fsp.writeFile(temporary, content, { encoding: 'utf8', flag: 'wx', mode: permissions });
    await fsp.chmod(temporary, permissions);
    await fsp.rename(temporary, target);
  } finally { await fsp.unlink(temporary).catch(() => {}); }
}

async function write_file({ path: rawPath, content }) {
  textArgument(content, 'content');
  const target = resolvePath(rawPath);
  const existed = fs.existsSync(target);
  await atomicWrite(target, content);
  return `${existed ? 'Overwrote' : 'Created'} ${target} (${Buffer.byteLength(content ?? '', 'utf8')} bytes)`;
}

async function edit_file({ path: rawPath, old_string, new_string, replace_all }) {
  const target = resolvePath(rawPath);
  textArgument(old_string, 'old_string'); textArgument(new_string, 'new_string');
  if (replace_all !== undefined && typeof replace_all !== 'boolean') throw new Error('replace_all must be true or false.');
  if ((await fsp.stat(target)).size > MAX_EDIT_BYTES) throw new Error('File exceeds the 8 MB editing limit.');
  const original = await fsp.readFile(target, 'utf8');
  if (original.includes('\0')) throw new Error('This appears to be a binary file. Only text files can be edited.');

  if (old_string === '') {
    throw new Error('old_string must not be empty');
  }

  const occurrences = original.split(old_string).length - 1;
  if (occurrences === 0) {
    throw new Error('old_string not found in file');
  }
  if (occurrences > 1 && !replace_all) {
    throw new Error(
      `old_string is not unique in file (${occurrences} matches). Provide more context or set replace_all: true.`
    );
  }

  const updated = replace_all
    ? original.split(old_string).join(new_string ?? '')
    : original.replace(old_string, () => new_string);

  textArgument(updated, 'Updated file');
  await atomicWrite(target, updated);
  return `Edited ${target} (${occurrences} replacement${occurrences === 1 ? '' : 's'})`;
}

async function move_file({ source, destination }) {
  const src = resolvePath(source);
  const dest = resolvePath(destination);
  try { await fsp.lstat(dest); throw new Error('Destination already exists. Choose another destination to avoid overwriting it.'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  await fsp.mkdir(path.dirname(dest), { recursive: true });
  try {
    await fsp.rename(src, dest);
  } catch (err) {
    if (err.code === 'EXDEV') {
      await fsp.copyFile(src, dest, fs.constants.COPYFILE_EXCL);
      await fsp.unlink(src);
    } else {
      throw err;
    }
  }
  return `Moved ${src} -> ${dest}`;
}

const TOOL_DEFS = [
  {
    type: 'function',
    function: {
      name: 'read_file',
      description:
        'Read a text file, or list a directory, at any absolute or ~-relative path on the system. ' +
        'Large files are paginated by line — if the result says "showing lines X-Y of Z", call again ' +
        'with offset set to continue reading.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Absolute path, or ~-relative path' },
          offset: { type: 'integer', description: '1-based line number to start reading from (default 1)' },
          limit: { type: 'integer', description: 'Max number of lines to return (default ~800, capped by a byte budget too)' },
          byte_offset: { type: 'integer', description: 'Optional 0-based byte offset for reading very long lines or large files; use the continuation offset from a previous result.' }
        },
        required: ['path']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'write_file',
      description: 'Create or overwrite a file with the given content. Creates parent directories as needed.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Absolute path, or ~-relative path' },
          content: { type: 'string', description: 'Full text content to write' }
        },
        required: ['path', 'content']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'edit_file',
      description: 'Replace an exact, unique substring in a file with new text. Fails if old_string is not found or is not unique (unless replace_all is set).',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Absolute path, or ~-relative path' },
          old_string: { type: 'string', description: 'Exact existing text to find' },
          new_string: { type: 'string', description: 'Text to replace it with' },
          replace_all: { type: 'boolean', description: 'Replace every occurrence instead of requiring uniqueness' }
        },
        required: ['path', 'old_string', 'new_string']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'move_file',
      description: 'Move or rename a file or directory from source to destination.',
      parameters: {
        type: 'object',
        properties: {
          source: { type: 'string', description: 'Absolute path, or ~-relative path' },
          destination: { type: 'string', description: 'Absolute path, or ~-relative path' }
        },
        required: ['source', 'destination']
      }
    }
  }
];

const DESTRUCTIVE_TOOLS = new Set(['write_file', 'edit_file', 'move_file']);

const IMPLEMENTATIONS = { read_file, write_file, edit_file, move_file };

async function execute(name, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('Tool arguments must be an object.');
  const fn = Object.hasOwn(IMPLEMENTATIONS, name) && IMPLEMENTATIONS[name];
  if (!fn) throw new Error(`Unknown tool: ${name}`);
  return fn(args);
}

const PREVIEW_MAX_CHARS = 4000;

function truncateForPreview(s) {
  return s.length > PREVIEW_MAX_CHARS ? `${s.slice(0, PREVIEW_MAX_CHARS)}\n… (truncated)` : s;
}

// Naive LCS-based line diff. Guarded by a cell-count cap so a huge file can't blow up
// compute/memory in the confirm dialog; falls back to a plain before/after preview instead.
function diffLines(oldStr, newStr) {
  const a = oldStr.split('\n');
  const b = newStr.split('\n');
  if (a.length * b.length > 4_000_000) {
    return `(diff omitted — file too large to compute)\n\n--- before (truncated) ---\n${truncateForPreview(oldStr)}\n\n--- after (truncated) ---\n${truncateForPreview(newStr)}`;
  }
  const m = a.length;
  const n = b.length;
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      out.push('  ' + a[i]);
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push('- ' + a[i]);
      i++;
    } else {
      out.push('+ ' + b[j]);
      j++;
    }
  }
  while (i < m) out.push('- ' + a[i++]);
  while (j < n) out.push('+ ' + b[j++]);
  return truncateForPreview(out.join('\n'));
}

async function previewTool(name, args) {
  if (name === 'write_file') {
    textArgument(args.content, 'content');
    const target = resolvePath(args.path);
    let existing = null;
    try {
      if ((await fsp.stat(target)).size > MAX_EDIT_BYTES) return `OVERWRITE: ${target}\n\nExisting file exceeds 8 MB; preview omitted.\n\n${truncateForPreview(args.content)}`;
      existing = await fsp.readFile(target, 'utf8');
    } catch (e) {
      if (e.code !== 'ENOENT') throw e;
    }
    if (existing === null) {
      return `NEW FILE: ${target}\n\n${truncateForPreview(args.content ?? '')}`;
    }
    return `OVERWRITE: ${target}\n\n${diffLines(existing, args.content ?? '')}`;
  }
  if (name === 'edit_file') {
    const target = resolvePath(args.path);
    const removed = (args.old_string || '').split('\n').map((l) => '- ' + l);
    const added = (args.new_string || '').split('\n').map((l) => '+ ' + l);
    return `EDIT: ${target}${args.replace_all ? ' (all occurrences)' : ''}\n\n${truncateForPreview([...removed, ...added].join('\n'))}`;
  }
  if (name === 'move_file') {
    return `MOVE:\n  ${resolvePath(args.source)}\n  -> ${resolvePath(args.destination)}`;
  }
  return JSON.stringify(args, null, 2);
}

module.exports = { TOOL_DEFS, DESTRUCTIVE_TOOLS, execute, resolvePath, previewTool };
