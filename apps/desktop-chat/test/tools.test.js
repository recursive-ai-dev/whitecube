const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { execute, resolvePath, previewTool } = require('../src/tools');

async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'desktop-chat-tools-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

test('file reads paginate complete lines and handle UTF-8 chunk boundaries', async t => {
  const directory = await fixture(t), file = path.join(directory, 'text.txt');
  const lines = Array.from({ length: 1000 }, (_, index) => `line ${index + 1} → é 中文`);
  await fs.writeFile(file, lines.join('\n'));
  const first = await execute('read_file', { path: file, limit: 2 });
  assert.match(first, /1\tline 1 → é 中文\n2\tline 2/);
  assert.match(first, /offset: 3/);
  const end = await execute('read_file', { path: file, offset: 999 });
  assert.match(end, /999\tline 999/);
  assert.match(end, /1000\tline 1000/);
  assert.doesNotMatch(end, /�/);
});

test('oversized lines use byte continuation with forward progress', async t => {
  const directory = await fixture(t), file = path.join(directory, 'huge.txt');
  await fs.writeFile(file, 'x'.repeat(100000));
  const first = await execute('read_file', { path: file });
  assert.match(first, /byte_offset: 30720/);
  assert.ok(first.length < 32000);
  const next = await execute('read_file', { path: file, byte_offset: 30720 });
  assert.match(next, /bytes 30720-61440/);
  assert.match(next, /byte_offset: 61440/);
});

test('reads reject binary files, special files and invalid offsets', async t => {
  const directory = await fixture(t), file = path.join(directory, 'binary');
  await fs.writeFile(file, Buffer.from([1, 0, 3]));
  await assert.rejects(execute('read_file', { path: file }), /binary/);
  await assert.rejects(execute('read_file', { path: '/dev/zero' }), /regular text/);
  for (const argument of [{ offset: -1 }, { offset: 1.2 }, { limit: 801 }, { limit: 0 }, { byte_offset: -1 }]) {
    await assert.rejects(execute('read_file', { path: file, ...argument }));
  }
});

test('edits preserve literal replacement characters, mode and symlinks', async t => {
  const directory = await fixture(t), file = path.join(directory, 'text');
  await fs.writeFile(file, 'before old after', { mode: 0o640 });
  const link = path.join(directory, 'link');
  await fs.symlink(file, link);
  await execute('edit_file', { path: link, old_string: 'old', new_string: '$& $` $\'' });
  assert.equal(await fs.readFile(file, 'utf8'), 'before $& $` $\' after');
  assert.ok((await fs.lstat(link)).isSymbolicLink());
  assert.equal((await fs.stat(file)).mode & 0o777, 0o640);
  assert.deepEqual((await fs.readdir(directory)).sort(), ['link', 'text']);
});

test('invalid tool input and edits never overwrite files', async t => {
  const directory = await fixture(t), file = path.join(directory, 'text');
  await fs.writeFile(file, 'same same');
  for (const arguments_ of [{ old_string: '', new_string: '' }, { old_string: 'same', new_string: 'next' }, { old_string: 'same', new_string: {} }]) {
    await assert.rejects(execute('edit_file', { path: file, ...arguments_ }));
    assert.equal(await fs.readFile(file, 'utf8'), 'same same');
  }
  await assert.rejects(execute('constructor', {}), /Unknown tool/);
  await assert.rejects(execute('read_file', []), /object/);
  assert.throws(() => resolvePath(42), /valid file path/);
  assert.equal(resolvePath('~'), os.homedir());
});

test('move refuses to overwrite a destination and write previews show actual diff', async t => {
  const directory = await fixture(t), file = path.join(directory, 'text'), other = path.join(directory, 'other');
  await fs.writeFile(file, 'old'); await fs.writeFile(other, 'keep');
  await assert.rejects(execute('move_file', { source: file, destination: other }), /already exists/);
  assert.equal(await fs.readFile(other, 'utf8'), 'keep');
  assert.match(await previewTool('write_file', { path: file, content: 'new' }), /- old\n\+ new/);
  await execute('write_file', { path: file, content: 'new' });
  assert.equal(await fs.readFile(file, 'utf8'), 'new');
});
