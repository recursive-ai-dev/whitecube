"""Native trainer's process and run management (stdlib only; no torch in GTK)."""
import fcntl
import json
import math
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE / 'pipeline'
sys.path.insert(0, str(PIPELINE))
from files import atomic_json
from model_presets import MODEL_PRESETS, DESKTOP_SIZES
from corpus_artifacts import corpus_metadata
import export_notebook

LIMITS = {'steps': (1, 10_000_000), 'max_tokens': (6000, 10_000_000_000),
          'min_document_bytes': (0, 1_000_000_000), 'max_document_bytes': (0, 1_000_000_000), 'batch_rows': (1, 65536),
          'batch_size': (1, 256), 'grad_accum_steps': (1, 256), 'cpu_threads': (1, 128),
          'max_temp': (50, 90), 'max_cpu_temp': (50, 95), 'schedule_steps': (0, 10_000_000),
          'burst_seconds': (1, 3600), 'cooldown_seconds': (1, 600),
          'warmup_steps': (0, 1_000_000), 'eval_interval': (1, 10_000_000), 'eval_iters': (1, 10000),
          'log_interval': (1, 10_000_000), 'seed': (0, 2**31 - 1),
          'early_stop_patience': (0, 1000), 'keep_snapshots': (0, 100), 'max_minutes': (0, 525_600),
          'preview_tokens': (1, 4096), 'preview_top_k': (1, 259), 'preview_seed': (0, 2**31 - 1),
          'window_width': (600, 8192), 'window_height': (500, 8192), 'run_sort': (0, 4), 'runs_pane': (240, 900)}
FLOAT_LIMITS = {'learning_rate': (1e-6, .1), 'min_lr_ratio': (0, 1), 'weight_decay': (0, 1), 'grad_clip': (0, 100),
                'early_stop_min_delta': (0, 100),
                'val_fraction': (.01, .5), 'preview_temperature': (0, 2), 'preview_top_p': (.05, 1)}
CHOICES = {'model_size': tuple(MODEL_PRESETS), 'device': ('cuda', 'cpu'), 'schedule': ('cosine', 'wsd'),
           'dtype': ('auto', 'float32', 'bfloat16', 'float16')}
# Saved with a new run and restored from its checkpoint on resume.
PLAN_FLAGS = ('learning_rate', 'min_lr_ratio', 'weight_decay', 'grad_clip', 'eval_interval', 'eval_iters', 'dtype', 'seed', 'schedule', 'val_fraction')
STOP_REASONS = {'completed': 'Completed', 'stop_requested': 'Stopped on request', 'early_stop': 'Early stop: validation stopped improving',
                'time_limit': 'Time limit reached', 'thermal': 'Temperature limit reached',
                'thermal_timeout': 'Stopped: temperatures stayed high during a thermal pause'}
NOTES_LIMIT = 2000
RECENT_LIMIT = 8
CHECKPOINT_KIND = export_notebook.CHECKPOINT_KIND
KERNEL_NAME = export_notebook.KERNEL_NAME


def bits_per_byte(loss):
    """Cross-entropy in nats per byte token, as bits per byte."""
    return loss / math.log(2) if finite_number(loss) else None


def perplexity(loss):
    return math.exp(min(loss, 700)) if finite_number(loss) else None


def checkpoint_label(kind):
    match = re.match(r'^step(\d+)$', kind or '')
    return f'Step {int(match.group(1)):,}' if match else str(kind).capitalize()


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def nonnegative_int(value, default=0):
    return int(value) if finite_number(value) and value >= 0 and value == int(value) else default


def strict_json(text):
    def invalid(value):
        raise ValueError(f'Nonfinite JSON value: {value}')
    def number(value):
        result = float(value)
        if not math.isfinite(result):
            return invalid(value)
        return result
    return json.loads(text, parse_constant=invalid, parse_float=number)


def read_json(path, max_bytes=1024 * 1024):
    try:
        path = Path(path)
        with path.open('rb') as handle:
            data = handle.read(max_bytes + 1)
        value = strict_json(data.decode('utf-8')) if len(data) <= max_bytes else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def now():
    return datetime.now(timezone.utc).isoformat()


def friendly_name(value):
    if not isinstance(value, str) or len(value.strip()) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Use a name of up to 80 characters without line breaks.')
    return value.strip()


def default_python():
    override = os.environ.get('WHITECUBE_MODEL_PYTHON')
    if override:
        return override
    for name in ('train', 'tg-serve'):
        try:
            for line in (Path.home() / '.local/bin' / name).read_text().splitlines():
                if line.startswith('exec '):
                    argv = shlex.split(line)
                    if len(argv) > 1 and Path(argv[1]).is_absolute() and Path(argv[1]).is_file():
                        return argv[1]
        except (OSError, ValueError):
            pass
    return '/usr/bin/python3'


def probe_runtime(executable):
    executable = Path(executable).expanduser()
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError('Choose an absolute path to a Python executable with PyTorch and NumPy installed.')
    code = ("import json,torch,numpy,importlib.util; c=torch.cuda.is_available(); "
            "print(json.dumps({'torch':torch.__version__,'cuda':c,'gpu':torch.cuda.get_device_name(0) if c else None,"
            "'parquet':importlib.util.find_spec('pyarrow') is not None,'memory':list(torch.cuda.mem_get_info(0)) if c else None,"
            "'bf16':bool(c and torch.cuda.is_bf16_supported()),'numpy':numpy.__version__,'cpus':torch.get_num_threads(),"
            "'training_error':step()}))")
    # One CPU optimizer step: imports that only load during training (for
    # example sympy through torch.fx) fail here instead of in the first run.
    code = ("def step():\n try:\n  import torch\n  m=torch.nn.Linear(2,2); o=torch.optim.AdamW(m.parameters())\n"
            "  m(torch.ones(1,2)).sum().backward(); o.step(); return None\n"
            " except Exception as e:\n  return (type(e).__name__+': '+str(e))[:400]\n" + code)
    try:
        result = subprocess.run([str(executable), '-I', '-c', code], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as error:
        raise ValueError('Python runtime check timed out after 30 seconds. Check the selected environment.') from error
    except OSError as error:
        raise ValueError(f'Unable to start the Python training environment: {error}') from error
    if result.returncode:
        raise ValueError('This Python environment needs PyTorch and NumPy. Choose your training environment, or install python3-torch and python3-numpy for CPU training.\n' + result.stderr[-700:])
    try:
        info = strict_json(result.stdout.splitlines()[-1])
        if (not isinstance(info, dict) or not isinstance(info.get('torch'), str)
                or not isinstance(info.get('cuda'), bool) or not isinstance(info.get('parquet'), bool)
                or not (info.get('gpu') is None or isinstance(info['gpu'], str))):
            raise ValueError('Unexpected runtime response')
        memory = info.get('memory')
        if not (isinstance(memory, list) and len(memory) == 2 and all(nonnegative_int(value, -1) >= 0 for value in memory)):
            info['memory'] = None
        info['bf16'] = info.get('bf16') is True
        if not isinstance(info.get('training_error'), str):
            info['training_error'] = None
        return info
    except (ValueError, IndexError, TypeError) as error:
        raise ValueError('Python did not return usable runtime information.') from error


def process_identity(pid):
    try:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        stat = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        return {'start_ticks': stat[19], 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, ValueError, IndexError):
        return None


def job_running(job):
    identity = job.get('identity')
    return bool(isinstance(identity, dict) and identity.get('start_ticks') and identity.get('boot_id')
                and process_identity(job.get('pid')) == identity)


def tail_text(path, limit=48_000):
    try:
        with Path(path).open('rb') as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode('utf-8', errors='replace')
    except (OSError, ValueError, TypeError):
        return ''


class Trainer:
    def __init__(self, home=None, state=None, config=None):
        self.home = Path(home or Path.home())
        self.state = Path(state or os.environ.get('XDG_STATE_HOME', self.home / '.local/state')) / 'whitecube/trainer'
        self.config = Path(config or os.environ.get('XDG_CONFIG_HOME', self.home / '.config')) / 'whitecube/trainer.json'
        self.runs_dir = Path(os.environ.get('WHITECUBE_TRAIN_OUT', self.home / 'train-out')).expanduser().resolve()
        self.children = []
        self.pending_jobs = {}

    def preferences(self):
        defaults = {'python': default_python(), 'dataset': str(self.home / 'train-in'), 'draft_name': '',
                'model_size': '2m', 'steps': 1000, 'max_tokens': 20_000_000, 'batch_size': 4,
                'grad_accum_steps': 4, 'device': 'cuda', 'cpu_threads': 2, 'max_temp': 70,
                'max_cpu_temp': 85, 'column': '', 'schedule_steps': 0,
                'deduplicate': False, 'shuffle_files': False, 'min_document_bytes': 0,
                'max_document_bytes': 0, 'batch_rows': 32,
                'stagger_training': False, 'burst_seconds': 30, 'cooldown_seconds': 10,
                'low_priority': False, 'learning_rate': 3e-4, 'min_lr_ratio': .1, 'warmup_steps': 0,
                'weight_decay': .1, 'grad_clip': 1.0, 'eval_interval': 50, 'seed': 1337, 'schedule': 'cosine',
                'val_fraction': .05, 'early_stop_patience': 0, 'keep_snapshots': 0, 'max_minutes': 0,
                'dtype': 'auto', 'eval_iters': 20, 'log_interval': 10, 'early_stop_min_delta': 0.0,
                'thermal_pause': False, 'preview_tokens': 120, 'preview_temperature': .85, 'preview_top_k': 40,
                'preview_top_p': 1.0, 'preview_seed': 1337, 'window_width': 1060, 'window_height': 780, 'run_sort': 0,
                'recent_datasets': [], 'runs_pane': 360}
        saved = read_json(self.config)
        for key, default in defaults.items():
            value = saved.get(key, default)
            if key in FLOAT_LIMITS:
                low, high = FLOAT_LIMITS[key]
                if not (finite_number(value) and low <= value <= high):
                    continue
                value = float(value)
            elif key in LIMITS:
                low, high = LIMITS[key]
                if not (finite_number(value) and value == int(value) and low <= value <= high):
                    continue
                value = int(value)
            elif type(value) is not type(default):
                continue
            if key in CHOICES and value not in CHOICES[key]:
                continue
            if key == 'recent_datasets' and not valid_recent(value):
                continue
            defaults[key] = value
        return defaults

    @staticmethod
    def defaults():
        """Factory training settings, without the user's paths or draft name."""
        values = Trainer(home='/nonexistent', config='/nonexistent').preferences()
        return {key: value for key, value in values.items()
                if key not in ('python', 'dataset', 'draft_name', 'window_width', 'window_height', 'run_sort',
                               'recent_datasets', 'runs_pane')}

    def save_preferences(self, values):
        if not isinstance(values, dict):
            raise ValueError('Trainer preferences must be an object')
        current = self.preferences()
        for key, value in values.items():
            if key not in current:
                continue
            if key in FLOAT_LIMITS:
                low, high = FLOAT_LIMITS[key]
                if not finite_number(value) or not low <= value <= high:
                    raise ValueError(f'{key} must be a number from {low:g} to {high:g}')
                value = float(value)
            elif key in LIMITS:
                low, high = LIMITS[key]
                if not finite_number(value) or not low <= value <= high or value != int(value):
                    raise ValueError(f'{key} must be an integer from {low} to {high}')
                value = int(value)
            elif type(value) is not type(current[key]):
                raise ValueError(f'Invalid {key} preference')
            if isinstance(value, str) and (len(value) > 4096 or '\0' in value):
                raise ValueError(f'{key} is too long or contains a null character')
            if key == 'model_size' and value not in MODEL_PRESETS:
                raise ValueError('Choose a supported model size')
            if key == 'device' and value not in ('cuda', 'cpu'):
                raise ValueError('Choose CPU or CUDA')
            if key in CHOICES and value not in CHOICES[key]:
                raise ValueError(f'Invalid {key} preference')
            if key == 'draft_name':
                value = friendly_name(value)
            if key == 'recent_datasets' and not valid_recent(value):
                raise ValueError('Recent corpora must be up to 8 paths')
            current[key] = value
        atomic_json(self.config, current)

    def jobs(self):
        for child in self.children[:]:
            if child.poll() is not None:
                self.children.remove(child)
        self.pending_jobs = {path: entry for path, entry in self.pending_jobs.items()
                             if entry['child'].poll() is None}
        jobs = []
        for path in (self.state / 'jobs').glob('*.json'):
            job = read_json(path)
            if not job or any(not isinstance(job.get(key), str) or not job[key].strip()
                              or '\0' in job[key] or not Path(job[key]).is_absolute()
                              for key in ('run', 'log')):
                continue
            try:
                job['run'] = str(Path(job['run']).resolve())
            except (OSError, ValueError, RuntimeError):
                continue
            job['path'] = str(path)
            pending = self.pending_jobs.get(str(path))
            if not job.get('pid') and pending and pending['child'].poll() is None:
                job.update(pid=pending['child'].pid, identity=pending['identity'])
            job['running'] = job_running(job)
            if not job['running'] and job.get('state') in ('queued', 'starting', 'running', 'stopping'):
                job['state'] = 'interrupted'
            jobs.append(job)
        return sorted(jobs, key=lambda item: str(item.get('started_at', '')), reverse=True)

    def active_job(self):
        return next((job for job in self.jobs() if job['running']), None)

    def list_runs(self):
        jobs = {job['run']: job for job in reversed(self.jobs())}
        runs = []
        if not self.runs_dir.is_dir():
            return runs
        for directory in sorted(self.runs_dir.iterdir(), reverse=True):
            if not directory.is_dir() or directory.is_symlink():
                continue
            meta = read_json(directory / 'run.json')
            result = read_json(directory / 'result.json')
            progress = read_json(directory / 'progress.json')
            status = read_json(directory / 'status.json')
            job = jobs.get(str(directory.resolve()))
            prepared = all((directory / 'data' / f'{split}.bin').is_file() for split in ('train', 'val'))
            size = meta.get('model_size')
            if not isinstance(size, str) or size not in MODEL_PRESETS:
                size = next((name for name, preset in MODEL_PRESETS.items() if preset['parameters'] == meta.get('parameters')), '1m')
            completed = max(nonnegative_int(result.get('completed_steps')), nonnegative_int(progress.get('completed_steps')))
            phase = status.get('state') or ('completed' if result and not result.get('stopped_early') else 'stopped' if result else 'prepared' if prepared else 'incomplete')
            if job:
                if job['running']:
                    phase = 'stopping' if job.get('state') == 'stopping' else status.get('state', 'starting')
                elif job.get('state') in ('failed', 'interrupted', 'stopped'):
                    phase = job['state']
            if phase in ('starting', 'preparing', 'training', 'cooling', 'stopping') and not (job and job['running']):
                # CLI jobs have no desktop job record: respect their shared lock.
                if not self.pipeline_busy():
                    phase = 'interrupted'
            if not isinstance(phase, str):
                phase = 'incomplete'
            name = meta.get('display_name')
            runs.append({'id': directory.name, 'directory': str(directory), 'name': name if isinstance(name, str) and name else directory.name,
                         'size': size, 'parameters': nonnegative_int(meta.get('parameters')) or None, 'steps': nonnegative_int(meta.get('steps')),
                         'completed_steps': completed, 'loss': progress.get('loss') if finite_number(progress.get('loss')) else None,
                         'dtype': next((value for value in (progress.get('dtype'), result.get('dtype'))
                                        if value in ('float32', 'bfloat16', 'float16')), None),
                         'val_loss': result.get('best_val_loss') if finite_number(result.get('best_val_loss')) else None, 'phase': phase, 'prepared': prepared,
                         'best': (directory / 'ckpt_best.pt').is_file(), 'latest': (directory / 'ckpt_latest.pt').is_file(),
                         'detail': str(status.get('detail') or (job or {}).get('detail') or ''), 'job': job, 'meta': meta,
                         'pinned': meta.get('pinned') is True, 'notes': notes if isinstance(notes := meta.get('notes'), str) else '',
                         'created': created_time(directory, meta), 'stop_reason': result.get('stop_reason') if result.get('stop_reason') in STOP_REASONS else None,
                         'training_seconds': result.get('total_training_seconds') if finite_number(result.get('total_training_seconds')) else None,
                         'finished_at': result.get('finished_at') if isinstance(result.get('finished_at'), str) else None,
                         'peak_vram_mib': result.get('peak_vram_mib') if finite_number(result.get('peak_vram_mib')) else None,
                         'snapshots': len(snapshot_files(directory))})
        return runs

    def pipeline_busy(self):
        path = self.home / '.local/state/tensegrity/train.lock'
        try:
            with path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            pass
        return False

    def find_run(self, run_id):
        run = next((run for run in self.list_runs() if run['id'] == run_id), None)
        if not run:
            raise ValueError('This run is unavailable. Refresh the run list.')
        return run

    def rename_run(self, run_id, name):
        run = self.find_run(run_id)
        if run['job'] and run['job']['running']:
            raise ValueError('Stop this run before renaming it.')
        if not (Path(run['directory']) / 'run.json').is_file():
            raise ValueError('A run can be named after corpus preparation finishes.')
        atomic_json(Path(run['directory']) / 'run.json', {**run['meta'], 'display_name': friendly_name(name)})

    def update_run(self, run_id, *, pinned=None, notes=None):
        """Pin a run or save its notes. Safe while training: only run.json changes."""
        run = self.find_run(run_id)
        path = Path(run['directory']) / 'run.json'
        if not path.is_file():
            raise ValueError('Pins and notes are available after corpus preparation finishes.')
        meta = read_json(path)
        if pinned is not None:
            if not isinstance(pinned, bool):
                raise ValueError('Pinned must be true or false.')
            meta['pinned'] = pinned
        if notes is not None:
            if not isinstance(notes, str) or len(notes) > NOTES_LIMIT or '\0' in notes:
                raise ValueError(f'Notes can be up to {NOTES_LIMIT:,} characters.')
            meta['notes'] = notes.strip()
        atomic_json(path, meta)

    def disk_usage(self, run_id):
        return directory_size(self.find_run(run_id)['directory'])

    def throughput(self, size, device):
        """Median tokens/second of earlier runs with this model size and device."""
        rates = sorted(rate for run in self.list_runs()
                       if run['size'] == size and run['meta'].get('device') == device
                       for rate in [read_json(Path(run['directory']) / 'progress.json').get('tokens_per_second')]
                       if finite_number(rate) and rate > 0)
        return rates[len(rates) // 2] if rates else None

    def save_preview(self, run_id, record):
        run = self.find_run(run_id)
        if not isinstance(record, dict) or not isinstance(record.get('text'), str):
            raise ValueError('Generate a preview before saving it.')
        entry = {'saved_at': now(), **{key: value for key, value in record.items()
                                       if key in PREVIEW_FIELDS and (isinstance(value, str) and len(value) <= 20_000 or finite_number(value))}}
        path = Path(run['directory']) / 'previews.jsonl'
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
        return entry

    def previews(self, run_id, limit=20):
        result = []
        for line in tail_text(Path(self.find_run(run_id)['directory']) / 'previews.jsonl', 400_000).splitlines():
            try:
                item = strict_json(line)
                if isinstance(item, dict) and isinstance(item.get('text'), str):
                    result.append(item)
            except (ValueError, TypeError, RecursionError):
                continue
        return result[-limit:]

    def checkpoint_path(self, run_id, kind):
        """Best, Latest or a step snapshot (stepNNNNNNNN) of a run."""
        return export_notebook.checkpoint_file(self.find_run(run_id)['directory'], kind)

    def checkpoints(self, run_id):
        """Saved checkpoints with their step (exact for snapshots), size and time."""
        run = self.find_run(run_id)
        directory = Path(run['directory'])
        metrics = read_metrics(directory)
        evaluated = [(item['step'], item['val_loss']) for item in metrics if finite_number(item.get('val_loss'))]
        result = []
        for kind in ('best', 'latest', *(name[5:-3] for name in reversed(snapshot_files(directory)))):
            path = directory / f'ckpt_{kind}.pt'
            try:
                stat = path.stat()
            except OSError:
                continue
            if kind == 'best':
                step = min(evaluated, key=lambda item: item[1])[0] if evaluated else None
                loss = run['val_loss']
            elif kind == 'latest':
                step, loss = run['completed_steps'] or None, None
            else:
                step = int(kind[4:])
                loss = next((value for at, value in reversed(evaluated) if at <= step), None)
            result.append({'kind': kind, 'label': checkpoint_label(kind), 'path': str(path), 'bytes': stat.st_size,
                           'modified': stat.st_mtime, 'step': step, 'val_loss': loss})
        return result

    def preview_command(self, run_id, kind, prompt, python, *, tokens=120, temperature=.85, top_k=40, top_p=1.0, seed=1337):
        run = self.find_run(run_id)
        checkpoint = self.checkpoint_path(run_id, kind)
        values = {'preview_tokens': tokens, 'preview_temperature': temperature, 'preview_top_k': top_k,
                  'preview_top_p': top_p, 'preview_seed': seed}
        for key, value in values.items():
            low, high = LIMITS.get(key) or FLOAT_LIMITS[key]
            if not finite_number(value) or not low <= value <= high or key in LIMITS and value != int(value):
                raise ValueError(f'{key.replace("preview_", "Preview ").replace("_", " ")} must be between {low:g} and {high:g}.')
        return [str(Path(python).expanduser()), '-u', str(PIPELINE / 'preview.py'),
                '--checkpoint', str(checkpoint), '--prompt', prompt,
                '--max-new-tokens', str(int(tokens)), '--temperature', repr(float(temperature)),
                '--top-k', str(int(top_k)), '--top-p', repr(float(top_p)), '--seed', str(int(seed))]

    def benchmark_command(self, run_id, kind, python, *, eval_iters=20, batch_size=4,
                          seed=2026, tokens=64, prompts=()):
        run = self.find_run(run_id)
        self.checkpoint_path(run_id, kind)
        if not run['prepared']:
            raise ValueError('Benchmarking needs this run’s prepared corpus.')
        if self.pipeline_busy():
            raise ValueError('Wait for training to finish before benchmarking checkpoints.')
        executable = Path(python).expanduser()
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError('Choose an executable Python training environment on Train.')
        for key, value in {'eval_iters': eval_iters, 'batch_size': batch_size,
                           'seed': seed, 'preview_tokens': tokens}.items():
            low, high = LIMITS[key]
            if key == 'batch_size':
                high = 64
            if not finite_number(value) or value != int(value) or not low <= value <= high:
                raise ValueError(f'Invalid benchmark {key.replace("_", " ")}.')
        if not isinstance(prompts, (tuple, list)) or len(prompts) > 8 or any(
                not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > 4096 or '\0' in prompt
                for prompt in prompts):
            raise ValueError('Use up to eight prompts, each at most 4,096 UTF-8 bytes.')
        destination = Path(run['directory']) / 'benchmarks' / f'{time.time_ns()}-{kind}-{uuid.uuid4().hex[:8]}.json'
        command = [str(executable), '-I', str(PIPELINE / 'benchmark.py'), '--run-dir', run['directory'],
                   '--checkpoint', kind, '--output', str(destination), '--eval-iters', str(int(eval_iters)),
                   '--batch-size', str(int(batch_size)), '--seed', str(int(seed)), '--max-new-tokens', str(int(tokens)),
                   '--cpu-threads', '2', '--temperature', '0']
        for prompt in prompts:
            command += ['--prompt', prompt]
        return command

    def benchmarks(self, run_id, limit=20):
        directory = Path(self.find_run(run_id)['directory']) / 'benchmarks'
        reports = []
        for path in sorted(directory.glob('*.json'), reverse=True)[:limit]:
            report = read_json(path, max_bytes=2 * 1024 * 1024)
            if (report.get('kind') == 'whitecube_checkpoint_benchmark'
                    and isinstance(report.get('checkpoint'), dict) and isinstance(report.get('validation'), dict)
                    and finite_number(report['validation'].get('loss'))
                    and all(isinstance(report.get(key, {}), dict) for key in ('settings', 'generation', 'corpus', 'model'))
                    and isinstance(report.get('generation', {}).get('samples', []), list)
                    and all(isinstance(sample, dict) for sample in report.get('generation', {}).get('samples', []))):
                reports.append(report)
        return reports

    def export_command(self, run_id, kind, destination, python):
        run = self.find_run(run_id)
        checkpoint = self.checkpoint_path(run_id, kind)
        destination = Path(destination).expanduser()
        if not destination.is_absolute() or destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise ValueError('Choose a new or empty folder for the exported model.')
        return [str(Path(python).expanduser()), '-u', str(PIPELINE / 'export_model.py'),
                '--checkpoint', str(checkpoint),
                '--output', str(destination), '--name', run['name']]

    def resume_command(self, run_id, steps):
        run = self.find_run(run_id)
        return shlex.join(['whitecube-train', '--resume', run['directory'], '--steps', str(int(steps))])

    def trash(self, run_id, kind='run'):
        run = self.find_run(run_id)
        if kind != 'run' and not (isinstance(kind, str) and CHECKPOINT_KIND.match(kind)):
            raise ValueError('Choose a run, Best checkpoint, Latest checkpoint or a step snapshot.')
        if run['job'] and run['job']['running']:
            raise ValueError('Stop this run before moving it to Trash.')
        target = Path(run['directory']) if kind == 'run' else Path(run['directory']) / f'ckpt_{kind}.pt'
        if not target.exists() or target.is_symlink():
            raise ValueError('This checkpoint is unavailable.')
        lockdir = self.home / '.local/state/tensegrity'
        lockdir.mkdir(parents=True, exist_ok=True)
        with (lockdir / 'train.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('Stop the active training process before removing models.') from error
            trash_root = Path(os.environ.get('XDG_DATA_HOME', self.home / '.local/share')) / 'Trash'
            files, info = trash_root / 'files', trash_root / 'info'
            files.mkdir(parents=True, exist_ok=True, mode=0o700)
            info.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = f'{target.name}.{uuid.uuid4().hex[:10]}'
            destination = files / name
            metadata = info / f'{name}.trashinfo'
            descriptor = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as handle:
                handle.write('[Trash Info]\nPath=' + quote(str(target.resolve()), safe='/') + '\nDeletionDate=' + datetime.now().strftime('%Y-%m-%dT%H:%M:%S') + '\n')
            try:
                target.rename(destination)
            except OSError:
                metadata.unlink(missing_ok=True)
                raise ValueError('Unable to move to your Trash. Use your file manager if the run is on another filesystem.')
            return destination

    def cleanup_candidates(self):
        """Failed or interrupted runs that never saved data or weights."""
        return [run for run in self.list_runs()
                if run['phase'] in ('failed', 'interrupted', 'incomplete') and not run['prepared']
                and not run['best'] and not run['latest'] and not run['snapshots'] and not run['pinned']
                and not (run['job'] and run['job'].get('running'))]

    def clean_up_runs(self, run_ids):
        moved, errors = [], []
        for run_id in run_ids:
            try:
                self.trash(run_id)
                moved.append(run_id)
            except (ValueError, OSError) as error:
                errors.append(f'{run_id}: {error}')
        return moved, errors

    def notebook_export(self, run_id, kind, destination, *, bundle=True, corpus=False, script=True, preview=None, cancelled=None):
        run = self.find_run(run_id)
        if run['job'] and run['job'].get('running') and kind == 'latest':
            raise ValueError('Latest changes while this run trains. Export Best or a snapshot, or stop the run first.')
        python = self.preferences()['python']
        kernel = installed_kernel(python)
        return export_notebook.export(run['directory'], kind, destination, bundle=bundle, corpus=corpus, script=script,
                                      preview=preview, python=python, cancelled=cancelled,
                                      kernel={'name': kernel['name'], 'display_name': kernel['display_name'], 'language': 'python'} if kernel else None)

    def kernel_command(self, python):
        python = Path(python).expanduser()
        if not python.is_absolute() or not python.is_file():
            raise ValueError('Choose the Python training environment on the Train page first.')
        return [str(python), '-m', 'ipykernel', 'install', '--user', '--name', KERNEL_NAME,
                '--display-name', 'Whitecube Trainer (PyTorch)']

    def validate(self, values, resume_id=None):
        values = {**self.preferences(), **values}
        values['name'] = friendly_name(values.get('name', ''))
        if not isinstance(values['model_size'], str) or values['model_size'] not in MODEL_PRESETS:
            raise ValueError('Choose a supported model size.')
        for key, (low, high) in LIMITS.items():
            value = values[key]
            try:
                integer = int(value)
                if isinstance(value, bool) or (not isinstance(value, str) and value != integer):
                    raise ValueError('Not a whole number')
            except (ValueError, TypeError, OverflowError):
                raise ValueError(f'{key.replace("_", " ").capitalize()} must be a whole number.') from None
            if not low <= integer <= high:
                raise ValueError(f'{key.replace("_", " ").capitalize()} must be between {low:,} and {high:,}.')
            values[key] = integer
        for key, (low, high) in FLOAT_LIMITS.items():
            value = values[key]
            if not finite_number(value) or not low <= value <= high:
                raise ValueError(f'{key.replace("_", " ").capitalize()} must be between {low:g} and {high:g}.')
            values[key] = float(value)
        if values['schedule'] not in CHOICES['schedule']:
            raise ValueError('Choose a cosine or warmup-stable-decay schedule.')
        if values['dtype'] not in CHOICES['dtype']:
            raise ValueError('Choose automatic, float32, bfloat16 or float16 precision.')
        if not resume_id and values['device'] == 'cpu' and values['dtype'] == 'float16':
            raise ValueError('Float16 training requires CUDA. Choose float32, bfloat16 or automatic for CPU.')
        for key in ('stagger_training', 'low_priority', 'thermal_pause', 'deduplicate', 'shuffle_files'):
            if not isinstance(values[key], bool):
                raise ValueError(f'{key.replace("_", " ").capitalize()} must be enabled or disabled.')
        if values['device'] not in ('cuda', 'cpu'):
            raise ValueError('Choose GPU or CPU.')
        if not isinstance(values['python'], str) or not values['python'].strip():
            raise ValueError('Choose a Python executable with PyTorch installed.')
        python = Path(values['python']).expanduser()
        if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError('Choose a Python executable with PyTorch installed.')
        values['python'] = str(python)
        column = values.get('column', '')
        if not isinstance(column, str) or (column.strip() and any(not part or any(ord(c) < 32 for c in part) for part in column.strip().split('.'))):
            raise ValueError('Use a text field name or a dotted path such as document.html.')
        values['column'] = column.strip()
        if values['max_document_bytes'] and values['min_document_bytes'] > values['max_document_bytes']:
            raise ValueError('Minimum document bytes cannot exceed the maximum.')
        if resume_id:
            run = self.find_run(resume_id)
            if not run['prepared'] or not run['meta']:
                raise ValueError('This run has no complete prepared corpus. Start a new run.')
            if run['completed_steps'] > 0 and not run['latest']:
                raise ValueError('Continuing training needs the Latest checkpoint. Restore it from Trash or choose another run.')
            if values['device'] == 'cpu' and run.get('dtype') == 'float16':
                raise ValueError('This checkpoint saved float16 precision and needs CUDA to resume. Select CUDA on Train.')
            if values['steps'] <= run['completed_steps']:
                raise ValueError(f'Resume needs a total step target above {run["completed_steps"]:,}.')
        else:
            if not isinstance(values['dataset'], str) or not values['dataset'].strip():
                raise ValueError('Choose an existing corpus file or folder.')
            dataset = Path(values['dataset']).expanduser()
            if not dataset.exists():
                raise ValueError('Choose an existing corpus file or folder.')
            info = dataset_info(dataset)
            if not info['files'] and not info.get('prepared'):
                raise ValueError('The corpus needs TXT, Markdown, JSONL, Parquet files, or a prepared corpus folder.')
            if info['empty_files']:
                raise ValueError('The corpus contains empty or unfinished files: ' + ', '.join(info['empty_files'][:3]))
            if info['errors']:
                raise ValueError('Unable to read corpus files: ' + '; '.join(info['errors'][:3]))
            values['dataset'] = str(dataset.resolve())
            target = self.runs_dir
            while not target.exists():
                target = target.parent
            # uint16 corpus + model/Adam state + temporary preparation. This is
            # a conservative disk budget, separate from the GPU memory estimate.
            # Reserve room for token staging, disk-backed metadata and provenance.
            # This estimate varies with document lengths; staging failures roll back.
            corpus_budget = info['bytes'] if info.get('prepared') else values['max_tokens'] * 12
            required = corpus_budget + MODEL_PRESETS[values['model_size']]['parameters'] * 40 + 64 * 1024**2
            if shutil.disk_usage(target).free < required:
                raise ValueError(f'Not enough free disk space for this plan (budget {required / 1024**3:.2f} GiB). Reduce the corpus cap or choose a smaller model.')
        return values

    def start(self, values, *, resume_id=None, prepare_only=False):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Serialize launch through worker registration, including other app windows.
        fd = os.open(self.state / 'launch.lock', os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as launch_lock:
            try:
                fcntl.flock(launch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('Another training job is being started. Wait for it to register.') from error
            return self._start(values, resume_id=resume_id, prepare_only=prepare_only)

    def _start(self, values, *, resume_id=None, prepare_only=False):
        if self.active_job():
            raise ValueError('A training job is already running. Stop it or let it finish.')
        lockdir = self.home / '.local/state/tensegrity'
        lockdir.mkdir(parents=True, exist_ok=True)
        with (lockdir / 'train.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('Another train process is running. Stop that process before starting a desktop run.') from error
        values = self.validate(values, resume_id)
        run = Path(self.find_run(resume_id)['directory']) if resume_id else self.runs_dir / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        job_id = uuid.uuid4().hex
        job_path = self.state / 'jobs' / f'{job_id}.json'
        log = self.state / f'{job_id}.log'
        command = self.command(values, run, resume_id=resume_id, prepare_only=prepare_only)
        job = {'run': str(run), 'command': command, 'log': str(log), 'state': 'queued',
               'started_at': now(), 'prepare_only': prepare_only, 'stop_requested': False,
               'schema_version': 1}
        recent = values['recent_datasets']
        if not resume_id:
            recent = [values['dataset'], *(item for item in recent if item != values['dataset'])][:RECENT_LIMIT]
        self.save_preferences({**{key: value for key, value in values.items() if key != 'name'}, 'recent_datasets': recent})
        atomic_json(job_path, job)
        try:
            with os.fdopen(os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), 'a') as output:
                child = subprocess.Popen(['/usr/bin/python3', '-I', str(HERE / 'worker.py'), str(job_path)],
                                         stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            atomic_json(job_path, {**job, 'state': 'failed', 'detail': str(error), 'finished_at': now(), 'exit_code': 1})
            raise ValueError(f'Unable to start the training worker: {error}') from error
        self.children.append(child)
        job.update(pid=child.pid, identity=process_identity(child.pid))
        self.pending_jobs[str(job_path)] = {'child': child, 'identity': job['identity']}
        # The worker owns subsequent state writes. Never overwrite its progress.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            registered = read_json(job_path)
            if registered.get('pid') == child.pid and registered.get('identity'):
                return {**registered, 'path': str(job_path), 'running': job_running(registered)}
            if child.poll() is not None:
                break
            time.sleep(.02)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
        detail = tail_text(log, 2000) or 'The training worker did not register within five seconds.'
        atomic_json(job_path, {**job, 'state': 'failed', 'detail': detail, 'finished_at': now(), 'exit_code': child.returncode or 1})
        raise ValueError(detail)

    def command(self, values, run, *, resume_id=None, prepare_only=False):
        """Supervisor arguments for validated values."""
        command = [values['python'], '-u', str(PIPELINE / 'train_supervisor.py')]
        if resume_id:
            command += ['--resume', str(run)]
        else:
            command += [str(Path(values['dataset']).expanduser().resolve()), '--run-dir', str(run),
                        f'--{values["model_size"]}', '--name', values['name'], '--max-tokens', str(values['max_tokens'])]
            if values.get('column', '').strip():
                command += ['--column', values['column'].strip()]
            for key in ('deduplicate', 'shuffle_files'):
                if values.get(key):
                    command += ['--' + key.replace('_', '-')]
            for key in ('min_document_bytes', 'max_document_bytes', 'batch_rows'):
                command += ['--' + key.replace('_', '-'), str(values[key])]
        for key in ('steps', 'batch_size', 'grad_accum_steps', 'device', 'cpu_threads', 'max_temp', 'max_cpu_temp'):
            command += ['--' + key.replace('_', '-'), str(values[key])]
        if prepare_only:
            if resume_id:
                raise ValueError('Choose a new run for corpus preparation.')
            command += ['--prepare-only']
        if values.get('schedule_steps'):
            command += ['--schedule-steps', str(values['schedule_steps'])]
        if values.get('stagger_training') and not prepare_only:
            command += ['--burst-seconds', str(values['burst_seconds']), '--cooldown-seconds', str(values['cooldown_seconds'])]
        if values.get('low_priority'):
            command += ['--low-priority']
        if not resume_id:
            for key in PLAN_FLAGS:
                command += ['--' + key.replace('_', '-'), repr(values[key]) if isinstance(values[key], float) else str(values[key])]
            if values['warmup_steps']:
                command += ['--warmup-steps', str(values['warmup_steps'])]
        if not prepare_only:
            for key in ('early_stop_patience', 'early_stop_min_delta', 'log_interval', 'keep_snapshots', 'max_minutes'):
                command += ['--' + key.replace('_', '-'), str(values[key])]
            command += ['--thermal-pause' if values['thermal_pause'] else '--no-thermal-pause']
        return command

    def stop(self, job):
        latest = read_json(job['path'])
        if not latest.get('pid'):
            latest = {**latest, 'pid': job.get('pid'), 'identity': job.get('identity')}
        if not job_running(latest):
            raise ValueError('This training job has already stopped.')
        try:
            os.kill(latest['pid'], signal.SIGTERM)
        except ProcessLookupError as error:
            raise ValueError('This training job has already stopped.') from error

    def chat_command(self, run_id, kind):
        run = self.find_run(run_id)
        if kind not in ('best', 'latest') or not run[kind]:
            raise ValueError('This checkpoint has not been saved yet.')
        return ['/usr/bin/desktop-chat', '--show', '--checkpoint', f'{run_id}/ckpt_{kind}.pt']

    def report(self, run_id):
        run = self.find_run(run_id)
        directory = Path(run['directory'])
        corpus_path = directory / 'data/corpus.json'
        corpus = read_json(corpus_path, max_bytes=16 * 1024 * 1024)
        warnings = []
        if corpus_path.exists() and not corpus:
            warnings.append('Corpus metadata could not be read: it is invalid, unreadable, or exceeds the 16 MiB export limit.')
        return {'application': 'Whitecube Trainer', 'schema_version': 1, 'exported_at': now(),
                'run': {key: value for key, value in run.items() if key not in ('job', 'meta')},
                'configuration': run['meta'], 'model_config': read_json(directory / 'local_config.json'),
                'corpus': corpus, 'report_warnings': warnings, 'result': read_json(directory / 'result.json'),
                'status': read_json(directory / 'status.json'), 'failure': read_json(directory / 'failure.json'),
                'progress': read_json(directory / 'progress.json'), 'telemetry': latest_telemetry(directory),
                'checkpoint_files': [str(path) for path in sorted(directory.glob('ckpt_*.pt'))],
                'notes': run['notes'], 'previews': self.previews(run_id), 'benchmarks': self.benchmarks(run_id),
                'disk_bytes': directory_size(directory), 'metrics': read_metrics(directory)}

    def summary(self):
        """Plain-text run table for the command line."""
        lines = []
        for run in self.list_runs():
            loss = f'{run["val_loss"]:.4f}' if run['val_loss'] is not None else '—'
            marks = ('*' if run['pinned'] else ' ') + ('B' if run['best'] else '-') + ('L' if run['latest'] else '-')
            lines.append(f'{marks} {run["id"]:<24} {run["size"].upper():>4} {run["phase"]:<12} '
                         f'{run["completed_steps"]:>9,}/{run["steps"]:<9,} val {loss:<8} {run["name"]}')
        return '\n'.join(lines) or f'No runs in {self.runs_dir}'

    def status_text(self):
        job = self.active_job()
        if not job:
            return 'No desktop training job is running.' + (' A command-line training process holds the training lock.' if self.pipeline_busy() else '')
        run = next((item for item in self.list_runs() if item['directory'] == job['run']), None)
        progress = read_json(Path(job['run']) / 'progress.json')
        text = f'{run["name"] if run else Path(job["run"]).name}: {run["phase"] if run else job.get("state")}'
        if run:
            text += f' · {run["completed_steps"]:,} / {run["steps"]:,} steps'
        if finite_number(progress.get('loss')):
            text += f' · loss {progress["loss"]:.4f}'
        if finite_number(progress.get('eta_seconds')):
            text += f' · about {int(progress["eta_seconds"]) // 60} min remaining'
        return text + f'\nRun folder: {job["run"]}\nLog: {job["log"]}'


def valid_recent(value):
    return (isinstance(value, list) and len(value) <= RECENT_LIMIT
            and all(isinstance(item, str) and item and len(item) <= 4096 and '\0' not in item for item in value))


def repair_hint(error, python):
    """A pip command for a missing or damaged module reported by the runtime check."""
    match = re.search(r"No module named '([A-Za-z0-9_]+)", error or '')
    if not match:
        return ''
    return f'{shlex.quote(str(python))} -m pip install --force-reinstall {match.group(1)}'


def jupyter_data_dir():
    if os.environ.get('JUPYTER_DATA_DIR'):
        return Path(os.environ['JUPYTER_DATA_DIR'])
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'jupyter'


def installed_kernel(python=None):
    """Find a Python kernelspec for the selected environment, including existing names.

    Do not resolve executable symlinks: separate virtualenvs often share the same
    underlying Python binary but have different installed training packages.
    """
    selected = os.path.abspath(os.path.expanduser(str(python))) if python else None
    roots = [Path(part) for part in os.environ.get('JUPYTER_PATH', '').split(os.pathsep) if part]
    roots.append(jupyter_data_dir())
    if selected:
        roots.append(Path(selected).parent.parent / 'share/jupyter')
    roots.extend((Path('/usr/local/share/jupyter'), Path('/usr/share/jupyter')))
    seen = set()
    candidates = []
    for root in roots:
        try:
            paths = sorted((root / 'kernels').glob('*/kernel.json'))
        except OSError:
            continue
        for path in paths:
            name = path.parent.name
            if name in seen:
                continue
            seen.add(name)
            spec = read_json(path)
            argv = spec.get('argv')
            if not (isinstance(spec.get('display_name'), str) and isinstance(argv, list)
                    and argv and isinstance(argv[0], str)):
                continue
            if selected:
                if not Path(argv[0]).is_absolute() or os.path.abspath(argv[0]) != selected:
                    continue
                if not os.path.isfile(selected):
                    continue
            elif name != KERNEL_NAME:
                continue
            candidates.append({**spec, 'name': name})
    return next((spec for spec in candidates if spec['name'] == KERNEL_NAME), candidates[0] if candidates else None)


def jupyter_command(python=None):
    """argv prefix that opens a notebook: JupyterLab, classic Notebook, then VS Code."""
    folders = [Path(python).expanduser().parent] if python else []
    for name, prefix in (('jupyter-lab', []), ('jupyter-notebook', []), ('code', [])):
        for folder in folders:
            if (folder / name).is_file() and os.access(folder / name, os.X_OK):
                return [str(folder / name), *prefix]
        found = shutil.which(name)
        if found:
            return [found, *prefix]
    return None


def set_notebook_kernel(path, display_name, name=KERNEL_NAME):
    """Point an exported notebook at the registered training kernel."""
    path = Path(path)
    document = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or document.get('nbformat') != 4:
        raise ValueError('This is not a version 4 notebook.')
    document.setdefault('metadata', {})['kernelspec'] = {'name': name, 'display_name': display_name, 'language': 'python'}
    atomic_json(path, document)


FIELD_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
TEXT_FIELDS = ('text', 'content', 'story', 'body', 'document.html')
result = {}
for name in sys.argv[2:]:
    fields, auto, error = [], None, None
    try:
        if name.lower().endswith('.parquet'):
            import pyarrow.parquet as pq
            from prepare_parquet import field_type, pick_column
            import pyarrow as pa
            schema = pq.ParquetFile(name).schema_arrow
            def walk(prefix, typ):
                if pa.types.is_dictionary(typ): typ = typ.value_type
                if pa.types.is_string(typ) or pa.types.is_large_string(typ): fields.append(prefix)
                elif pa.types.is_struct(typ):
                    for index in range(typ.num_fields): walk(prefix + '.' + typ.field(index).name, typ.field(index).type)
            for field in schema: walk(field.name, field.type)
            try: auto = pick_column(schema)
            except ValueError: auto = None
        else:
            with open(name, encoding='utf-8', errors='replace') as handle:
                for number, line in enumerate(handle):
                    if number >= 50: break
                    try: row = json.loads(line)
                    except ValueError: continue
                    if not isinstance(row, dict): continue
                    for key, value in row.items():
                        if isinstance(value, str) and key not in fields: fields.append(key)
                        if isinstance(value, dict):
                            for inner, item in value.items():
                                if isinstance(item, str) and key + '.' + inner not in fields: fields.append(key + '.' + inner)
            auto = next((field for field in TEXT_FIELDS if field in fields), None)
    except Exception as exception:
        error = (type(exception).__name__ + ': ' + str(exception))[:300]
    result[name] = {'fields': fields[:64], 'auto': auto, 'error': error}
print(json.dumps(result))
'''


def probe_fields(files, python):
    """Text fields of JSONL/Parquet files and the field automatic selection would read."""
    files = [str(path) for path in files if str(path).lower().endswith(('.jsonl', '.parquet'))][:200]
    if not files:
        return {}
    try:
        result = subprocess.run([str(Path(python).expanduser()), '-I', '-c', FIELD_PROBE, str(PIPELINE), *files],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f'Unable to read corpus fields: {error}') from error
    if result.returncode:
        raise ValueError('Unable to read corpus fields: ' + (result.stderr.strip().splitlines() or ['unknown error'])[-1])
    try:
        value = strict_json(result.stdout.splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise ValueError('The field probe returned no usable result.') from error
    return value if isinstance(value, dict) else {}


def field_summary(probe, override=''):
    """(message, fields, failing files) for the Train page; flags files the run would reject."""
    if not probe:
        return '', [], []
    counts, missing, broken = {}, [], []
    for name, item in probe.items():
        if not isinstance(item, dict):
            continue
        if item.get('error'):
            broken.append(Path(name).name)
        for field in item.get('fields') or []:
            counts[field] = counts.get(field, 0) + 1
        chosen = override or item.get('auto')
        if not item.get('error') and (not chosen or chosen not in (item.get('fields') or [])):
            missing.append(Path(name).name)
    used = {}
    for item in probe.values():
        if isinstance(item, dict) and not override and item.get('auto'):
            used[item['auto']] = used.get(item['auto'], 0) + 1
    parts = []
    if override:
        parts.append(f'Text field “{override}” · found in {counts.get(override, 0)} of {len(probe)} files')
    elif used:
        parts.append('Automatic text field: ' + ', '.join(f'{field} ({count} file{"s" if count != 1 else ""})' for field, count in sorted(used.items())))
    if missing:
        parts.append(f'{len(missing)} file{"s" if len(missing) != 1 else ""} would fail ({", ".join(missing[:3])}{"…" if len(missing) > 3 else ""})'
                     + ('; choose a field they all have, or clear the override' if override else '; choose a text field'))
    if broken:
        parts.append(f'unreadable: {", ".join(broken[:3])}')
    return ' · '.join(parts) + '.', sorted(counts, key=lambda field: (-counts[field], field)), missing


PREVIEW_FIELDS = ('text', 'prompt', 'checkpoint', 'tokens', 'temperature', 'top_k', 'top_p', 'seed', 'seconds')
SNAPSHOT = re.compile(r'^ckpt_step(\d{8,})\.pt$')


def snapshot_files(directory):
    try:
        return sorted(path.name for path in Path(directory).iterdir() if SNAPSHOT.match(path.name))
    except OSError:
        return []


def created_time(directory, meta):
    value = meta.get('created_at')
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    match = re.match(r'^(\d{8}-\d{6})', directory.name)
    if match:
        try:
            return datetime.strptime(match.group(1), '%Y%m%d-%H%M%S').timestamp()
        except ValueError:
            pass
    try:
        return directory.stat().st_mtime
    except OSError:
        return 0


def directory_size(directory):
    total = 0
    stack = [Path(directory)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def dataset_info(source):
    source = Path(source).expanduser()
    if source.is_dir() and (source / 'corpus.json').is_file():
        errors, files, size, manifest = [], [], 0, {}
        try:
            manifest = corpus_metadata(source)
            for name in ('train.bin', 'val.bin', 'documents.jsonl', 'corpus.json'):
                path = source / name
                if name == 'documents.jsonl' and not path.exists() and manifest.get('document_index') is None:
                    continue
                with path.open('rb') as handle:
                    handle.read(1)
                files.append(str(path))
                size += path.stat().st_size
        except (OSError, ValueError) as error:
            errors.append(str(error))
        return {'files': files, 'empty_files': [], 'bytes': size, 'errors': errors, 'formats': {},
                'largest': None, 'prepared': True, 'corpus': manifest}
    extensions = ('.txt', '.md', '.jsonl', '.parquet')
    files = [source] if source.is_file() and source.suffix.lower() in extensions else sorted(source.rglob('*')) if source.is_dir() else []
    supported, empty, errors, size = [], [], [], 0
    formats = {}
    largest = None
    for path in files:
        if path.suffix.lower() not in extensions:
            continue
        try:
            if not path.is_file():
                continue
            stat = path.stat()
            with path.open('rb') as handle:
                handle.read(1)
            supported.append(str(path))
            size += stat.st_size
            formats[path.suffix.lower()] = formats.get(path.suffix.lower(), 0) + 1
            if largest is None or stat.st_size > largest[1]:
                largest = (path.name, stat.st_size)
            if not stat.st_size:
                empty.append(path.name)
        except OSError as error:
            errors.append(f'{path}: {error.strerror}')
    return {'files': supported, 'empty_files': empty, 'bytes': size, 'errors': errors, 'formats': formats,
            'largest': largest}


def read_metrics(directory):
    result = []
    for line in tail_text(Path(directory) / 'train_log.jsonl', 200_000).splitlines():
        try:
            item = strict_json(line)
            if isinstance(item, dict) and finite_number(item.get('step')) and item['step'] >= 0:
                result.append({key: value for key, value in item.items()
                               if key not in ('total_loss', 'val_loss', 'ce_loss', 'homeo_loss', 'lr', 'tok_s', 'grad_norm')
                               or finite_number(value)})
        except (ValueError, TypeError, RecursionError):
            continue
    return result[-1500:]


def latest_telemetry(directory):
    for line in reversed(tail_text(Path(directory) / 'gpu.jsonl', 6000).splitlines()):
        try:
            item = strict_json(line)
            if isinstance(item, dict):
                return {key: value for key, value in item.items()
                        if key not in ('temperature_c', 'cpu_temperature_c', 'memory_used_mib', 'memory_total_mib', 'utilization_pct')
                        or value is None or finite_number(value)}
        except (ValueError, TypeError, RecursionError):
            continue
    return {}
