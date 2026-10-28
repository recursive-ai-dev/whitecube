"""Read-only desktop, training-runtime and storage readiness checks.

This module deliberately uses only the standard library and the trainer engine.
PyTorch stays in the selected interpreter, including when called from GTK.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine

VERSION = '1.1.1'
SCHEMA_VERSION = 1
MIN_FREE_BYTES = 1024**3
DESKTOP_PYTHON = '/usr/bin/python3'


def runtime_setup_hint():
    """Offer an isolated environment rather than modifying system Python."""
    environment = '~/.local/share/whitecube-train/venv'
    return (f'python3 -m venv {environment}\n'
            f'{environment}/bin/python -m pip install torch numpy\n'
            f'Select {environment}/bin/python in the training Python field. '
            'If venv is unavailable, install python3-venv first. '
            'The installed package supplies its Parquet decoder separately. Install a CUDA-compatible PyTorch build for GPU training.')


def _check(identifier, title, status, detail, remedy=None, **extra):
    check = {'id': identifier, 'title': title, 'status': status, 'detail': detail, **extra}
    if remedy:
        check['remedy'] = remedy
    return check


def _path_check(identifier, title, value, *, reserve=0):
    """Find the containing filesystem without creating application directories."""
    try:
        path = Path(value).expanduser().absolute()
        parent = path
        while not parent.exists():
            if parent == parent.parent:
                raise OSError('No existing parent directory')
            parent = parent.parent
        if not parent.is_dir():
            raise OSError(f'{parent} is a file, so the requested directory cannot be created')
        if not os.access(parent, os.W_OK | os.X_OK):
            raise OSError(f'{parent} is not writable or searchable by the current user')
        free = shutil.disk_usage(parent).free
        required = max(MIN_FREE_BYTES, reserve)
        status = 'ok' if free >= required else 'warning'
        detail = f'{path} · {free / 1024**3:.2f} GiB free on {parent}'
        if status == 'warning':
            detail += f' · suggested free space: at least {required / 1024**3:.2f} GiB'
        return _check(identifier, title, status, detail,
                      'Choose a writable location with more free space or reduce the corpus cap and model size.' if status == 'warning' else None,
                      path=str(path), filesystem_path=str(parent), free_bytes=free, suggested_bytes=required)
    except (OSError, ValueError, TypeError) as error:
        return _check(identifier, title, 'error', str(error),
                      'Choose a directory owned by your user, or fix access to its existing parent directory.')


def _desktop_check():
    code = ("import gi,json;gi.require_version('Gtk','4.0');gi.require_version('Adw','1');"
            "from gi.repository import Gtk,Adw;"
            "print(json.dumps({'gtk':f'{Gtk.get_major_version()}.{Gtk.get_minor_version()}',"
            "'adwaita':f'{Adw.get_major_version()}.{Adw.get_minor_version()}'}))")
    try:
        result = subprocess.run([DESKTOP_PYTHON, '-I', '-c', code], capture_output=True,
                                text=True, timeout=10)
        if result.returncode:
            raise ValueError(result.stderr[-800:].strip() or 'Desktop libraries could not load')
        info = json.loads(result.stdout.splitlines()[-1])
        return _check('desktop', 'Desktop libraries', 'ok',
                      f"GTK {info['gtk']} · libadwaita {info['adwaita']} · {DESKTOP_PYTHON}")
    except (OSError, ValueError, IndexError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        return _check('desktop', 'Desktop libraries', 'error', str(error),
                      'Install python3-gi, python3-gi-cairo, gir1.2-gtk-4.0 and gir1.2-adw-1 with your system package manager.')


def _corpus_check(value):
    if not value:
        return _check('corpus', 'Corpus selection', 'warning', 'No corpus is selected.',
                      'Choose a TXT, Markdown, JSONL or Parquet file, a corpus folder, or prepared data.')
    try:
        path = Path(value).expanduser()
        if not path.exists():
            raise OSError(f'{path} does not exist')
        if path.is_file():
            if path.suffix.lower() not in ('.txt', '.md', '.jsonl', '.parquet'):
                raise ValueError('Choose a TXT, Markdown, JSONL or Parquet file.')
            with path.open('rb') as handle:
                if not handle.read(1):
                    raise ValueError('The selected corpus file is empty.')
            detail = f'{path} · {path.stat().st_size:,} bytes'
        elif path.is_dir():
            # Do not walk an unbounded corpus here. Inspect on Train and prepare
            # perform format, field and split validation before training starts.
            with os.scandir(path) as entries:
                next(entries, None)
            if not os.access(path, os.R_OK | os.X_OK):
                raise OSError(f'{path} is not readable or searchable')
            detail = f'{path} · folder is accessible; use Inspect to validate its contents'
        else:
            raise ValueError('Choose a regular file or folder.')
        return _check('corpus', 'Corpus selection', 'ok', detail)
    except (OSError, ValueError, TypeError) as error:
        return _check('corpus', 'Corpus selection', 'error', str(error),
                      'Choose an existing, readable corpus. Use Inspect before starting training.')


def _required_storage(values):
    """Use the same conservative budget as engine validation, when available."""
    try:
        tokens = int(values.get('max_tokens', 0))
        parameters = engine.MODEL_PRESETS[values['model_size']]['parameters']
        if tokens < 0:
            return 0
        return tokens * 12 + parameters * 40 + 64 * 1024**2
    except (TypeError, ValueError, KeyError, OverflowError):
        return 0


def collect_diagnostics(trainer, *, check_runtime=False, values=None):
    """Return a JSON-safe readiness report without changing files or preferences.

    ``values`` overlays saved preferences, so a desktop draft is inspected before
    it is saved. The runtime optimizer probe is opt-in and has a 30 second limit;
    desktop library imports have a separate ten second limit. Corpus checks only
    establish path accessibility and deliberately do not claim split integrity.
    """
    settings = {**trainer.preferences(), **(values or {})}
    checks = [_desktop_check()]
    executable = settings.get('python', '')
    try:
        python = Path(executable).expanduser()
        valid = python.is_absolute() and python.is_file() and os.access(python, os.X_OK)
    except (OSError, ValueError, TypeError):
        valid = False
    checks.append(_check('python', 'Training Python', 'ok' if valid else 'error', str(executable),
                         None if valid else runtime_setup_hint()))
    runtime = None
    if check_runtime and valid:
        try:
            runtime = engine.probe_runtime(executable)
            if runtime.get('training_error'):
                checks.append(_check('runtime', 'CPU optimizer test', 'error', runtime['training_error'],
                                     runtime_setup_hint()))
            else:
                checks.append(_check('runtime', 'CPU optimizer test', 'ok',
                                     f"PyTorch {runtime['torch']} · NumPy {runtime.get('numpy', 'unknown')} · one AdamW step passed"))
            if settings.get('device') == 'cuda' and not runtime['cuda']:
                checks.append(_check('device', 'Selected training device', 'error',
                                     'CUDA is selected, but this runtime cannot access a CUDA device.',
                                     'Select CPU, or install a CUDA-compatible PyTorch build and verify the GPU driver.'))
            else:
                detail = f"CUDA · {runtime.get('gpu') or 'GPU available'}" if settings.get('device') == 'cuda' else 'CPU'
                checks.append(_check('device', 'Selected training device', 'ok', detail))
            if settings.get('dtype') == 'float16' and settings.get('device') != 'cuda':
                checks.append(_check('precision', 'Training precision', 'error',
                                     'Float16 requires CUDA training.', 'Select automatic, float32 or bfloat16 precision for CPU training.'))
            elif settings.get('dtype') == 'bfloat16' and settings.get('device') == 'cuda' and not runtime.get('bf16'):
                checks.append(_check('precision', 'Training precision', 'error',
                                     'The selected CUDA device does not support bfloat16.',
                                     'Select automatic, float32 or float16 precision.'))
            checks.append(_check('parquet', 'Parquet support', 'ok' if runtime.get('parquet') else 'error',
                                 'PyArrow is available through ' + str(runtime.get('parquet_provider') or 'the selected Python') + '.' if runtime.get('parquet') else
                                 runtime.get('parquet_error') or 'No usable PyArrow decoder is available.',
                                 None if runtime.get('parquet') else 'Reinstall Whitecube Trainer and its declared Parquet/Python dependencies.'))
        except (ValueError, OSError) as error:
            checks.append(_check('runtime', 'CPU optimizer test', 'error', str(error), runtime_setup_hint()))
    else:
        checks.append(_check('runtime', 'CPU optimizer test', 'warning',
                             'Not checked. Run the full readiness check to import PyTorch and perform one CPU optimizer step.' if valid
                             else 'Cannot check runtime until a valid Python executable is selected.',
                             f"Run {os.environ.get('WHITECUBE_TRAIN_COMMAND', 'whitecube-train')} --doctor --check-runtime, or use Readiness in the desktop."))
    checks.extend([
        _path_check('output', 'Training output storage', trainer.runs_dir, reserve=_required_storage(settings)),
        _path_check('state', 'Job state storage', trainer.state),
        _path_check('config', 'Preferences storage', trainer.config.parent, reserve=0),
        _corpus_check(settings.get('dataset')),
    ])
    return {'schema_version': SCHEMA_VERSION, 'version': VERSION,
            'ok': not any(check['status'] == 'error' for check in checks),
            'runtime_checked': bool(check_runtime and valid),
            'checks': checks,
            'system': {'platform': platform.system(), 'release': platform.release(),
                       'architecture': platform.machine(), 'desktop_python': DESKTOP_PYTHON,
                       'training_python': str(executable), 'device': settings.get('device', 'cpu')},
            'runtime': runtime}


def format_diagnostics(report):
    result = [f"Whitecube Trainer {report['version']} readiness", '']
    for check in report['checks']:
        result.append(f"[{check['status'].upper()}] {check['title']}: {check['detail']}")
        if check.get('remedy'):
            result.extend(f'  {line}' for line in check['remedy'].splitlines())
    result.extend(['', 'Ready: all required checks passed.' if report['ok'] else 'Action required: resolve the errors above.'])
    if not report.get('runtime_checked'):
        result.append('Training runtime has not been verified.')
    return '\n'.join(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check Whitecube Trainer readiness without modifying your system.')
    parser.add_argument('--json', action='store_true', help='print a machine-readable report')
    parser.add_argument('--check-runtime', action='store_true', help='perform one CPU optimizer step in the selected Python (30 second timeout)')
    parser.add_argument('--python', help='absolute path to the training Python')
    parser.add_argument('--output-root', type=Path, help='inspect another output directory without saving it')
    parser.add_argument('--device', choices=('cpu', 'cuda'), help='inspect a CPU or CUDA training plan')
    options = parser.parse_args(argv)
    trainer = engine.Trainer()
    if options.output_root:
        trainer.runs_dir = options.output_root.expanduser().absolute()
    values = {key: value for key, value in (('python', options.python), ('device', options.device)) if value is not None}
    report = collect_diagnostics(trainer, check_runtime=options.check_runtime, values=values)
    print(json.dumps(report, indent=2, allow_nan=False) if options.json else format_diagnostics(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
