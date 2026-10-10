const fs = require('fs');
const fsp = fs.promises;
const path = require('path');
const os = require('os');

function resolvePath(p) {
  if (!p) throw new Error('path is required');
  let expanded = p;
  if (expanded.startsWith('~')) {
    expanded = path.join(os.homedir(), expanded.slice(1));
  }
  return path.resolve(expanded);
}

const MAX_READ_BYTES = 30 * 1024;
const MAX_READ_LINES = 800;
const MAX_DIR_ENTRIES = 300;

async function read_file({ path: rawPath, offset, limit }) {
  const target = resolvePath(rawPath);
  const stat = await fsp.stat(target);
  if (stat.isDirectory()) {
    const entries = await fsp.readdir(target, { withFileTypes: true });
    const names = entries.map((e) => (e.isDirectory() ? `${e.name}/` : e.name)).sort();
    const shown = names.slice(0, MAX_DIR_ENTRIES);
    const more =
      names.length > MAX_DIR_ENTRIES
        ? `\n… ${names.length - MAX_DIR_ENTRIES} more entries (${names.length} total)`
        : '';
    return `[directory listing for ${target}]\n${shown.join('\n')}${more}`;
  }

  const raw = await fsp.readFile(target, 'utf8');
  const allLines = raw.split('\n');
  const startIdx = Math.max(0, (offset ?? 1) - 1);
  const maxLines = limit ?? MAX_READ_LINES;

  const outLines = [];
  let bytes = 0;
  let i = startIdx;
  for (; i < allLines.length && outLines.length < maxLines; i++) {
    const numbered = `${i + 1}\t${allLines[i]}`;
    const lineBytes = Buffer.byteLength(numbered, 'utf8') + 1;
    if (bytes + lineBytes > MAX_READ_BYTES) break;
    outLines.push(numbered);
    bytes += lineBytes;
  }

  const truncated = i < allLines.length;
  const footer = truncated
    ? `\n\n[showing lines ${startIdx + 1}-${i} of ${allLines.length}; call read_file again with offset: ${i + 1} to continue]`
    : '';
  return outLines.join('\n') + footer;
}

async function write_file({ path: rawPath, content }) {
  const target = resolvePath(rawPath);
  await fsp.mkdir(path.dirname(target), { recursive: true });
  const existed = fs.existsSync(target);
  await fsp.writeFile(target, content ?? '', 'utf8');
  return `${existed ? 'Overwrote' : 'Created'} ${target} (${Buffer.byteLength(content ?? '', 'utf8')} bytes)`;
}

async function edit_file({ path: rawPath, old_string, new_string, replace_all }) {
  const target = resolvePath(rawPath);
  const original = await fsp.readFile(target, 'utf8');

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
    : original.replace(old_string, new_string ?? '');

  await fsp.writeFile(target, updated, 'utf8');
  return `Edited ${target} (${occurrences} replacement${occurrences === 1 ? '' : 's'})`;
}

async function move_file({ source, destination }) {
  const src = resolvePath(source);
  const dest = resolvePath(destination);
  await fsp.mkdir(path.dirname(dest), { recursive: true });
  try {
    await fsp.rename(src, dest);
  } catch (err) {
    if (err.code === 'EXDEV') {
      await fsp.copyFile(src, dest);
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
          limit: { type: 'integer', description: 'Max number of lines to return (default ~800, capped by a byte budget too)' }
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
  const fn = IMPLEMENTATIONS[name];
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
    const target = resolvePath(args.path);
    let existing = null;
    try {
      existing = await fsp.readFile(target, 'utf8');
    } catch (e) {
      // file doesn't exist yet — treat as a new file
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
