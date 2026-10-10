'use strict';
const { spawn } = require('node:child_process');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');

const jobs = new Map();
let seq = 0;

function resolveCwd(cwd) {
  if (!cwd || cwd === '~') return os.homedir();
  const c = cwd.startsWith('~/') ? path.join(os.homedir(), cwd.slice(2)) : cwd;
  try { return fs.statSync(c).isDirectory() ? c : os.homedir(); } catch { return os.homedir(); }
}

// Runs `bash -c command` in its own process group, streaming output. fd 3 reports the final
// working directory so `cd` in the terminal behaves like a real shell session.
function start({ command, cwd }, emit) {
  if (jobs.size) throw new Error('A command is already running. Stop it before starting another.');
  if (typeof command !== 'string' || !command.trim() || command.length > 65536 || command.includes('\0')) throw new Error('Command must be between 1 and 65536 characters.');
  const id = `job-${++seq}`;
  const dir = resolveCwd(cwd);
  const wrapped = `${command}\n__cc_rc=$?\nprintf '%s' "$PWD" >&3\nexit $__cc_rc`;
  const child = spawn('bash', ['-c', wrapped], {
    cwd: dir, detached: true, stdio: ['ignore', 'pipe', 'pipe', 'pipe'],
    env: { ...process.env, TERM: 'dumb', NO_COLOR: '1', PAGER: 'cat', GIT_PAGER: 'cat', SYSTEMD_PAGER: '', DEBIAN_FRONTEND: 'noninteractive' },
  });
  jobs.set(id, child);
  let finalCwd = dir;
  const t0 = Date.now();
  let outputBytes = 0, truncated = false, reportedCwd = '';
  const output = (kind, text) => {
    outputBytes += Buffer.byteLength(text);
    if (outputBytes <= 4 * 1024 * 1024) emit({ id, kind, text });
    else if (!truncated) { truncated = true; emit({ id, kind: 'err', text: '\n[Output truncated after 4 MiB; command continues. Redirect large output to a file.]\n' }); }
  };
  child.stdout.setEncoding('utf8').on('data', d => output('out', d));
  child.stderr.setEncoding('utf8').on('data', d => output('err', d));
  child.stdio[3].setEncoding('utf8').on('data', d => { if (reportedCwd.length < 8192) reportedCwd += d; });
  let done = false;
  const finish = (code, signal, error) => {
    if (done) return; done = true;
    if (reportedCwd) finalCwd = reportedCwd;
    jobs.delete(id);
    emit({ id, kind: 'exit', code, signal, error, cwd: finalCwd, ms: Date.now() - t0 });
  };
  child.on('error', (e) => finish(127, null, e.message));
  child.on('close', (code, signal) => finish(code, signal));
  return { id, cwd: dir };
}

function kill(id) {
  const c = jobs.get(id);
  if (!c) return false;
  try { process.kill(-c.pid, 'SIGTERM'); } catch { /* already gone */ }
  setTimeout(() => { if (jobs.get(id) !== c) return; try { process.kill(-c.pid, 'SIGKILL'); } catch { /* gone */ } }, 3000).unref();
  return true;
}

function killAll() { for (const id of [...jobs.keys()]) kill(id); }

module.exports = { start, kill, killAll, resolveCwd };
