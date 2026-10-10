const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

function readJson(filename) {
  try { return JSON.parse(fs.readFileSync(filename, 'utf8')); } catch { return {}; }
}

// Discover files without loading torch or unpickling a checkpoint. IDs always
// come from this list; renderer input is never used as an arbitrary file path.
function discoverCheckpoints(directory = path.join(os.homedir(), 'train-out'), aliases = {}) {
  let entries;
  try { entries = fs.readdirSync(directory, { withFileTypes: true }); }
  catch (err) { if (err.code === 'ENOENT') return []; throw err; }
  const items = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const runDir = path.join(directory, entry.name);
    const run = readJson(path.join(runDir, 'run.json'));
    const result = readJson(path.join(runDir, 'result.json'));
    const cfg = readJson(path.join(runDir, 'local_config.json'));
    for (const file of fs.readdirSync(runDir, { withFileTypes: true })) {
      if (!file.isFile() || !/^ckpt_[^/\\]+\.pt$/.test(file.name)) continue;
      const checkpoint = path.join(runDir, file.name);
      let stat;
      try { stat = fs.statSync(checkpoint); } catch { continue; } // training may replace a file
      const id = `${entry.name}/${file.name}`;
      const kind = file.name === 'ckpt_best.pt' ? 'Best' : file.name === 'ckpt_latest.pt' ? 'Latest' : file.name.replace(/^ckpt_|\.pt$/g, '');
      items.push({ id, run: entry.name, filename: file.name, checkpoint,
        name: typeof aliases[id] === 'string' && aliases[id] ? aliases[id] : `${run.display_name || entry.name} · ${kind}`,
        customName: typeof aliases[id] === 'string' ? aliases[id] : '',
        kind, bytes: stat.size, mtime: stat.mtimeMs,
        parameters: run.parameters ?? null, valLoss: result.best_val_loss ?? null,
        contextBytes: cfg.block_size ?? run.block_size ?? null });
    }
  }
  return items.sort((a, b) => b.run.localeCompare(a.run) || a.filename.localeCompare(b.filename));
}

function runtime(home = os.homedir()) {
  const bundled = path.join(__dirname, '../../cube-train/pipeline');
  const moduleDir = process.env.WHITECUBE_TENSEGRITY_DIR ||
    (fs.existsSync(path.join(bundled, 'tensegrity_serve.py')) ? bundled : path.join(home, '.local/share/tensegrity'));
  if (!fs.existsSync(path.join(moduleDir, 'tensegrity_serve.py'))) {
    throw new Error(`Local model runtime is missing: ${moduleDir}/tensegrity_serve.py. Install the Tensegrity training tools first.`);
  }
  const trainerConfig = readJson(path.join(process.env.XDG_CONFIG_HOME || path.join(home, '.config'), 'whitecube/trainer.json'));
  let interpreter = process.env.WHITECUBE_MODEL_PYTHON || trainerConfig.python;
  if (!interpreter) {
    let launcher = '';
    try { launcher = fs.readFileSync(path.join(home, '.local/bin/tg-serve'), 'utf8'); } catch {}
    const match = launcher.match(/^exec\s+(?:"([^"\n]+)"|'([^'\n]+)'|([^\s]+))/m);
    interpreter = match && (match[1] || match[2] || match[3]);
  }
  interpreter = interpreter || '/usr/bin/python3';
  if (!path.isAbsolute(interpreter) || !fs.existsSync(interpreter)) {
    throw new Error('Cannot find the training Python environment. Choose it in Whitecube Trainer or set WHITECUBE_MODEL_PYTHON.');
  }
  return { interpreter, moduleDir };
}

class LocalModels {
  constructor(options = {}) {
    this.directory = options.directory || process.env.WHITECUBE_TRAIN_OUT || path.join(os.homedir(), 'train-out');
    this.child = null;
    this.endpoint = null;
    this.starting = null;
  }
  list(aliases) { return discoverCheckpoints(this.directory, aliases); }
  find(id, aliases) {
    const item = this.list(aliases).find(item => item.id === id);
    if (!item) throw new Error('Checkpoint is no longer available. Refresh the model list and choose another checkpoint.');
    return item;
  }
  async ensure(signal) {
    signal?.throwIfAborted();
    if (!this.endpoint && !this.starting) {
      this.starting = this.start().finally(() => { this.starting = null; });
    }
    const pending = this.endpoint ? Promise.resolve(this.endpoint) : this.starting;
    if (!signal) return pending;
    return new Promise((resolve, reject) => {
      const cancel = () => reject(new DOMException('Cancelled', 'AbortError'));
      signal.addEventListener('abort', cancel, { once: true });
      pending.then(resolve, reject).finally(() => signal.removeEventListener('abort', cancel));
      if (signal.aborted) cancel();
    });
  }
  start() {
    const { interpreter, moduleDir } = runtime();
    const stateDir = path.join(os.homedir(), '.local/state/whitecube');
    fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
    const logPath = path.join(stateDir, 'local-model-server.log');
    const log = fs.openSync(logPath, 'a', 0o600);
    return new Promise((resolve, reject) => {
      const child = spawn(interpreter, ['-u', path.join(__dirname, '../local-model-server.py'),
        '--module-dir', moduleDir, '--runs-dir', this.directory], { stdio: ['ignore', 'pipe', log] });
      fs.closeSync(log);
      this.child = child;
      let buffer = '', settled = false;
      const timer = setTimeout(() => {
        child.kill(); fail(new Error(`Local model server took too long to start. See ${logPath}.`));
      }, 25000);
      const fail = err => { if (!settled) { settled = true; clearTimeout(timer); reject(err); } };
      child.on('error', err => { this.child = null; fail(new Error(`Cannot start local models: ${err.message}`)); });
      child.on('exit', code => {
        if (this.child === child) { this.child = null; this.endpoint = null; }
        fail(new Error(`Local model server exited (${code}). See ${logPath}.`));
      });
      child.stdout.on('data', chunk => {
        buffer += chunk.toString();
        let end;
        while ((end = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
          if (!line.startsWith('WHITECUBE_READY ')) continue;
          let info; try { info = JSON.parse(line.slice(16)); } catch { continue; }
          if (!Number.isInteger(info.port) || info.port < 1 || info.port > 65535 || settled) continue;
          settled = true; clearTimeout(timer);
          this.endpoint = `http://127.0.0.1:${info.port}/v1`; resolve(this.endpoint);
        }
        if (buffer.length > 8192) buffer = buffer.slice(-8192);
      });
    });
  }
  stop() { this.child?.kill(); this.child = null; this.endpoint = null; }
}

module.exports = { LocalModels, discoverCheckpoints, runtime };
