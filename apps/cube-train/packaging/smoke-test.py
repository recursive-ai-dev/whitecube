#!/usr/bin/python3
"""Exercise an extracted Debian package with a real CPU training environment.

Does not install the package, change preferences, or retain fixture checkpoints.
All commands run with isolated HOME/XDG directories; logs and a JSON acceptance
report are retained in --output-dir. Requires torch and numpy in --python.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deb', required=True, type=Path)
    parser.add_argument('--python', required=True, type=Path, help='absolute training Python with PyTorch and NumPy')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'dist/acceptance')
    options = parser.parse_args()
    package = options.deb.expanduser().resolve(strict=True)
    # Preserve the venv path: resolving a symlink here would select system Python.
    python = options.python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error('--python must be an executable training interpreter')
    output = options.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'package': package.name, 'sha256': hashlib.sha256(package.read_bytes()).hexdigest(),
              'training_python': str(python), 'checks': [], 'ok': False}
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='.fixture-', dir=output) as directory:
            root = Path(directory)
            subprocess.run(['dpkg-deb', '--extract', str(package), str(root / 'package')], check=True)
            app = root / 'package/usr/lib/whitecube-trainer/cube-train'
            command = root / 'package/usr/bin/whitecube-trainer-cli'
            env = dict(os.environ, HOME=str(root / 'home'), XDG_CONFIG_HOME=str(root / 'config'),
                       XDG_STATE_HOME=str(root / 'state'), WHITECUBE_TRAIN_OUT=str(root / 'runs'),
                       WHITECUBE_MODEL_PYTHON=str(python), OMP_NUM_THREADS='2', MKL_NUM_THREADS='2',
                       DISPLAY='', WAYLAND_DISPLAY='')
            corpus = root / 'corpus'
            corpus.mkdir()
            for index in range(32):
                text = f'Document {index}: ' + (f'The cube learns a local pattern for example {index}. ' * 32)
                (corpus / f'{index:02}.txt').write_text(text, encoding='utf-8')
            def run(name, args):
                then = time.monotonic()
                completed = subprocess.run([str(part) for part in args], cwd=root, env=env,
                                           text=True, capture_output=True, timeout=180)
                (output / f'{name}.log').write_text(completed.stdout + '\n' + completed.stderr, encoding='utf-8')
                if completed.returncode:
                    raise RuntimeError(f'{name} exited {completed.returncode}: {completed.stderr[-1500:]}')
                report['checks'].append({'name': name, 'seconds': round(time.monotonic() - then, 3)})
                print(f'PASS {name}', flush=True)
                return completed.stdout
            run('version', [command, '--version'])
            # Diagnostic corpus path is supplied through an isolated draft.
            config = root / 'config/whitecube/trainer.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'dataset': str(corpus), 'python': str(python), 'device': 'cpu'}))
            readiness = json.loads(run('readiness', [command, '--doctor', '--check-runtime', '--json']))
            assert readiness['ok'] and readiness['runtime_checked'], 'Full runtime readiness failed'
            first, second = root / 'runs/first', root / 'runs/second'
            common = ['--2m', '--device', 'cpu', '--steps', '1', '--batch-size', '1', '--grad-accum-steps', '1',
                      '--eval-iters', '1', '--eval-interval', '1', '--log-interval', '1', '--dtype', 'float32',
                      '--cpu-threads', '1', '--max-cpu-temp', '95']
            run('train', [command, corpus, '--run-dir', first, *common])
            initial = json.loads((first / 'result.json').read_text())
            assert initial['completed_steps'] == 1, f'Training stopped before one step: {initial.get("stop_reason")}'
            run('resume', [command, '--resume', first, '--steps', '2', '--device', 'cpu', '--cpu-threads', '1', '--max-cpu-temp', '95'])
            resumed = json.loads((first / 'result.json').read_text())
            assert resumed['completed_steps'] == 2, f'Resume stopped early: {resumed.get("stop_reason")}'
            report['training_outcomes'] = {'initial': initial['stop_reason'], 'resumed': resumed['stop_reason']}
            run('reuse-corpus', [command, first / 'data', '--run-dir', second, *common])
            comparison = json.loads(run('comparison', [command, '--compare-runs', 'first', 'second', '--json']))
            assert comparison['comparable'] and comparison['best_run_id'] in ('first', 'second'), comparison['warnings']
            run('preview', [python, '-I', app / 'pipeline/preview.py', '--checkpoint', first / 'ckpt_best.pt',
                            '--prompt', 'The cube', '--max-new-tokens', '8', '--temperature', '0'])
            benchmark_file = root / 'benchmark.json'
            run('benchmark', [command, '--benchmark', first, '--output', benchmark_file, '--eval-iters', '1',
                              '--batch-size', '1', '--max-new-tokens', '4', '--prompt', 'The cube'])
            measured = json.loads(benchmark_file.read_text())
            assert measured['validation']['loss'] >= 0
            bundle = root / 'notebook'
            run('notebook', [command, '--export-notebook', first, '--output', bundle, '--with-corpus'])
            notebooks = list(bundle.glob('*.ipynb'))
            assert len(notebooks) == 1 and json.loads(notebooks[0].read_text())['nbformat'] == 4
            report['ok'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        report['seconds'] = round(time.monotonic() - started, 3)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
