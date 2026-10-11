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

LIMITS = {'steps': (1, 10_000_000), 'max_tokens': (6000, 10_000_000_000),
          'batch_size': (1, 256), 'grad_accum_steps': (1, 256), 'cpu_threads': (1, 128),
          'max_temp': (50, 90), 'max_cpu_temp': (50, 95), 'schedule_steps': (0, 10_000_000),
          'burst_seconds': (1, 3600), 'cooldown_seconds': (1, 600)}


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
    code = "import json,torch,numpy,importlib.util; print(json.dumps({'torch':torch.__version__,'cuda':torch.cuda.is_available(),'gpu':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,'parquet':importlib.util.find_spec('pyarrow') is not None}))"
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
                'stagger_training': False, 'burst_seconds': 30, 'cooldown_seconds': 10,
                'low_priority': False}
        saved = read_json(self.config)
        for key, default in defaults.items():
            value = saved.get(key, default)
            if key in LIMITS:
                low, high = LIMITS[key]
                if not (finite_number(value) and value == int(value) and low <= value <= high):
                    continue
                value = int(value)
            elif type(value) is not type(default):
                continue
            if key == 'model_size' and value not in MODEL_PRESETS:
                continue
            if key == 'device' and value not in ('cuda', 'cpu'):
                continue
            defaults[key] = value
        return defaults

    def save_preferences(self, values):
        current = self.preferences()
        atomic_json(self.config, {**current, **{key: value for key, value in values.items() if key in current}})

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
                         'val_loss': result.get('best_val_loss') if finite_number(result.get('best_val_loss')) else None, 'phase': phase, 'prepared': prepared,
                         'best': (directory / 'ckpt_best.pt').is_file(), 'latest': (directory / 'ckpt_latest.pt').is_file(),
                         'detail': str(status.get('detail') or (job or {}).get('detail') or ''), 'job': job, 'meta': meta})
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

    def trash(self, run_id, kind='run'):
        run = self.find_run(run_id)
        if kind not in ('run', 'best', 'latest'):
            raise ValueError('Choose a run, Best checkpoint, or Latest checkpoint.')
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
        for key in ('stagger_training', 'low_priority'):
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
        if resume_id:
            run = self.find_run(resume_id)
            if not run['prepared'] or not run['meta']:
                raise ValueError('This run has no complete prepared corpus. Start a new run.')
            if run['completed_steps'] > 0 and not run['latest']:
                raise ValueError('Continuing training needs the Latest checkpoint. Restore it from Trash or choose another run.')
            if values['steps'] <= run['completed_steps']:
                raise ValueError(f'Resume needs a total step target above {run["completed_steps"]:,}.')
        else:
            if not isinstance(values['dataset'], str) or not values['dataset'].strip():
                raise ValueError('Choose an existing corpus file or folder.')
            dataset = Path(values['dataset']).expanduser()
            if not dataset.exists():
                raise ValueError('Choose an existing corpus file or folder.')
            info = dataset_info(dataset)
            if not info['files']:
                raise ValueError('The corpus needs TXT, Markdown, JSONL, or Parquet files.')
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
            required = values['max_tokens'] * 6 + MODEL_PRESETS[values['model_size']]['parameters'] * 40 + 64 * 1024**2
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
        command = [values['python'], '-u', str(PIPELINE / 'train_supervisor.py')]
        if resume_id:
            command += ['--resume', str(run)]
        else:
            command += [str(Path(values['dataset']).expanduser().resolve()), '--run-dir', str(run),
                        f'--{values["model_size"]}', '--name', values['name'], '--max-tokens', str(values['max_tokens'])]
            if values.get('column', '').strip():
                command += ['--column', values['column'].strip()]
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
        job = {'run': str(run), 'command': command, 'log': str(log), 'state': 'queued',
               'started_at': now(), 'prepare_only': prepare_only, 'stop_requested': False,
               'schema_version': 1}
        self.save_preferences({key: value for key, value in values.items() if key != 'name'})
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
                'metrics': read_metrics(directory)}


def dataset_info(source):
    source = Path(source).expanduser()
    extensions = ('.txt', '.md', '.jsonl', '.parquet')
    files = [source] if source.is_file() and source.suffix.lower() in extensions else sorted(source.rglob('*')) if source.is_dir() else []
    supported, empty, errors, size = [], [], [], 0
    formats = {}
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
            if not stat.st_size:
                empty.append(path.name)
        except OSError as error:
            errors.append(f'{path}: {error.strerror}')
    return {'files': supported, 'empty_files': empty, 'bytes': size, 'errors': errors, 'formats': formats}


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
