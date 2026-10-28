const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const builder = path.resolve(__dirname, '../packaging/build-deb.js');

test('Debian builder refuses a mismatched or unsupported runtime before staging a package', { skip: process.platform !== 'linux' || spawnSync('dpkg', ['--version']).status !== 0 }, () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'whitecube-chat-package-test-'));
  try {
    const runtime = path.join(directory, 'runtime'); fs.mkdirSync(runtime);
    const output = path.join(directory, 'out');
    for (const name of ['electron', 'chrome-sandbox', 'LICENSE', 'LICENSES.chromium.html']) fs.writeFileSync(path.join(runtime, name), 'placeholder');
    fs.writeFileSync(path.join(runtime, 'version'), '0.0.0');
    let result = spawnSync(process.execPath, [builder, '--no-tests', '--output-directory', output], { env: { ...process.env, ELECTRON_DIST: runtime }, encoding: 'utf8' });
    assert.equal(result.status, 1); assert.match(result.stderr, /differs from pinned/);
    assert.equal(fs.existsSync(output), false);
    fs.writeFileSync(path.join(runtime, 'version'), require('../package.json').devDependencies.electron);
    result = spawnSync(process.execPath, [builder, '--no-tests', '--output-directory', output], { env: { ...process.env, ELECTRON_DIST: runtime }, encoding: 'utf8' });
    assert.equal(result.status, 1); assert.match(result.stderr, /Expected a Linux/);
    assert.equal(fs.existsSync(output), false);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
