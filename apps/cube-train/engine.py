"""Native trainer's process and run management (stdlib only; no torch in GTK)."""
import fcntl
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import uuid
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE / 'pipeline'
sys.path.insert(0, str(PIPELINE))
from files import atomic_json
from model_presets import MODEL_PRESETS, DESKTOP_SIZES


def read_json(path):
    try:
        path = Path(path)
        value = json.loads(path.read_text()) if path.stat().st_size < 1024 * 1024 else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def now():
    return datetime.now(timezone.utc).isoformat()


def friendly_name(value):
    value = str(value).strip()
    if len(value) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Use a name of up to 80 characters without line breaks.')
    return value


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
    result = subprocess.run([str(executable), '-c', code], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('This Python environment needs PyTorch and NumPy. Choose your training environment, or install python3-torch and python3-numpy for CPU training.\n' + result.stderr[-700:])
    try:
        return json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise ValueError('Python did not return usable runtime information.') from error


def process_identity(pid):
    try:
        stat = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        return {'start_ticks': stat[19], 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, ValueError, IndexError):
        return None


def job_running(job):
    return bool(job.get('pid') and process_identity(job['pid']) == job.get('identity'))


def tail_text(path, limit=48_000):
    try:
        with Path(path).open('rb') as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


class Trainer:
    def __init__(self, home=None, state=None, config=None):
        self.home = Path(home or Path.home())
        self.state = Path(state or os.environ.get('XDG_STATE_HOME', self.home / '.local/state')) / 'whitecube/trainer'
        self.config = Path(config or os.environ.get('XDG_CONFIG_HOME', self.home / '.config')) / 'whitecube/trainer.json'
        self.runs_dir = Path(os.environ.get('WHITECUBE_TRAIN_OUT', self.home / 'train-out')).expanduser()
        self.children = []
        self.pending_jobs = {}

    def preferences(self):
        return {'python': default_python(), 'dataset': str(self.home / 'train-in'),
                'model_size': '2m', 'steps': 1000, 'max_tokens': 20_000_000, 'batch_size': 4,
                'grad_accum_steps': 4, 'device': 'cuda', 'cpu_threads': 2, 'max_temp': 70,
                'max_cpu_temp': 85, 'column': '', 'schedule_steps': 0,
                'stagger_training': False, 'burst_seconds': 30, 'cooldown_seconds': 10,
                'low_priority': False, **read_json(self.config)}

    def save_preferences(self, values):
        atomic_json(self.config, {**self.preferences(), **values})

    def jobs(self):
        for child in self.children[:]:
            if child.poll() is not None:
                self.children.remove(child)
        jobs = []
        for path in (self.state / 'jobs').glob('*.json'):
            job = read_json(path)
            if not job:
                continue
            job['path'] = str(path)
            pending = self.pending_jobs.get(str(path))
            if not job.get('pid') and pending and pending['child'].poll() is None:
                job.update(pid=pending['child'].pid, identity=pending['identity'])
            job['running'] = job_running(job)
            if not job['running'] and job.get('state') in ('queued', 'starting', 'running', 'stopping'):
                job['state'] = 'interrupted'
            jobs.append(job)
        return sorted(jobs, key=lambda item: item.get('started_at', ''), reverse=True)

    def active_job(self):
        return next((job for job in self.jobs() if job['running']), None)

    def list_runs(self):
        jobs = {job.get('run'): job for job in reversed(self.jobs())}
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
            job = jobs.get(str(directory))
            prepared = all((directory / 'data' / f'{split}.bin').is_file() for split in ('train', 'val'))
            size = meta.get('model_size') or next((name for name, preset in MODEL_PRESETS.items() if preset['parameters'] == meta.get('parameters')), '1m')
            completed = max(result.get('completed_steps', 0), progress.get('completed_steps', 0))
            phase = status.get('state') or ('completed' if result and not result.get('stopped_early') else 'stopped' if result else 'prepared' if prepared else 'incomplete')
            if job:
                if job['running']:
                    phase = 'stopping' if job.get('state') == 'stopping' else status.get('state', 'starting')
                elif job.get('state') in ('failed', 'interrupted', 'stopped'):
                    phase = job['state']
            runs.append({'id': directory.name, 'directory': str(directory), 'name': meta.get('display_name') or directory.name,
                         'size': size, 'parameters': meta.get('parameters'), 'steps': meta.get('steps', 0),
                         'completed_steps': completed, 'loss': progress.get('loss'),
                         'val_loss': result.get('best_val_loss'), 'phase': phase, 'prepared': prepared,
                         'best': (directory / 'ckpt_best.pt').is_file(), 'latest': (directory / 'ckpt_latest.pt').is_file(),
                         'detail': status.get('detail', ''), 'job': job, 'meta': meta})
        return runs

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
        values = dict(values)
        values['name'] = friendly_name(values.get('name', ''))
        if values['model_size'] not in MODEL_PRESETS:
            raise ValueError('Choose a supported model size.')
        for key, low, high in [('steps', 1, 10_000_000), ('max_tokens', 6000, 10_000_000_000),
                               ('batch_size', 1, 256), ('grad_accum_steps', 1, 256), ('cpu_threads', 1, 128),
                               ('max_temp', 50, 90), ('max_cpu_temp', 50, 95), ('schedule_steps', 0, 10_000_000),
                               ('burst_seconds', 1, 3600), ('cooldown_seconds', 1, 600)]:
            values[key] = int(values[key])
            if not low <= values[key] <= high:
                raise ValueError(f'{key.replace("_", " ").capitalize()} must be between {low:,} and {high:,}.')
        if values['device'] not in ('cuda', 'cpu'):
            raise ValueError('Choose GPU or CPU.')
        python = Path(values['python']).expanduser()
        if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError('Choose a Python executable with PyTorch installed.')
        values['python'] = str(python)
        if resume_id:
            run = self.find_run(resume_id)
            if not run['prepared'] or not run['meta']:
                raise ValueError('This run has no complete prepared corpus. Start a new run.')
            if run['completed_steps'] > 0 and not run['latest']:
                raise ValueError('Continuing training needs the Latest checkpoint. Restore it from Trash or choose another run.')
            if values['steps'] <= run['completed_steps']:
                raise ValueError(f'Resume needs a total step target above {run["completed_steps"]:,}.')
        else:
            dataset = Path(values['dataset']).expanduser()
            if not dataset.exists():
                raise ValueError('Choose an existing corpus file or folder.')
            info = dataset_info(dataset)
            if not info['files']:
                raise ValueError('The corpus needs TXT, Markdown, JSONL, or Parquet files.')
            if info['empty_files']:
                raise ValueError('The corpus contains empty or unfinished files: ' + ', '.join(info['empty_files'][:3]))
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
               'started_at': now(), 'prepare_only': prepare_only, 'stop_requested': False}
        atomic_json(job_path, job)
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), 'a') as output:
            child = subprocess.Popen(['/usr/bin/python3', '-I', str(HERE / 'worker.py'), str(job_path)],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(child)
        job.update(pid=child.pid, identity=process_identity(child.pid))
        self.pending_jobs[str(job_path)] = {'child': child, 'identity': job['identity']}
        # The worker owns subsequent state writes. Never overwrite its progress.
        self.save_preferences({key: value for key, value in values.items() if key != 'name'})
        return {**job, 'path': str(job_path), 'running': True}

    def stop(self, job):
        latest = read_json(job['path'])
        if not latest.get('pid'):
            latest = {**latest, 'pid': job.get('pid'), 'identity': job.get('identity')}
        if not job_running(latest):
            raise ValueError('This training job has already stopped.')
        os.kill(latest['pid'], signal.SIGTERM)

    def chat_command(self, run_id, kind):
        run = self.find_run(run_id)
        if kind not in ('best', 'latest') or not run[kind]:
            raise ValueError('This checkpoint has not been saved yet.')
        return ['/usr/bin/desktop-chat', '--show', '--checkpoint', f'{run_id}/ckpt_{kind}.pt']

    def report(self, run_id):
        run = self.find_run(run_id)
        directory = Path(run['directory'])
        return {'application': 'Whitecube Trainer', 'exported_at': now(),
                'run': {key: value for key, value in run.items() if key not in ('job', 'meta')},
                'configuration': run['meta'], 'model_config': read_json(directory / 'local_config.json'),
                'corpus': read_json(directory / 'data/corpus.json'), 'result': read_json(directory / 'result.json'),
                'checkpoint_files': [str(path) for path in sorted(directory.glob('ckpt_*.pt'))],
                'metrics': read_metrics(directory)}


def dataset_info(source):
    source = Path(source)
    extensions = ('.txt', '.md', '.jsonl', '.parquet')
    files = [source] if source.is_file() and source.suffix.lower() in extensions else list(source.rglob('*')) if source.is_dir() else []
    supported, empty, size = [], [], 0
    for path in files:
        if path.is_file() and path.suffix.lower() in extensions:
            stat = path.stat()
            supported.append(str(path))
            size += stat.st_size
            if not stat.st_size:
                empty.append(path.name)
    return {'files': supported, 'empty_files': empty, 'bytes': size}


def read_metrics(directory):
    result = []
    for line in tail_text(Path(directory) / 'train_log.jsonl', 200_000).splitlines():
        try:
            item = json.loads(line)
            if isinstance(item.get('step'), (int, float)):
                result.append(item)
        except (ValueError, TypeError):
            continue
    return result[-1500:]


def latest_telemetry(directory):
    for line in reversed(tail_text(Path(directory) / 'gpu.jsonl', 6000).splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}
