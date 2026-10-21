"""Export a run's checkpoint as a Jupyter notebook (stdlib only; never imports torch).

The default bundle is a self-contained folder:

    <name>.ipynb            notebook (nbformat 4.5)
    <name>.py               the same cells in `# %%` percent format (optional)
    whitecube_pipeline/     the model code the notebook imports
    checkpoint/ckpt_*.pt    a copy of the chosen checkpoint
    run/                    run.json, result.json, metrics, previews, model config
    data/                   prepared train/val corpus (optional)
    README.md

With ``bundle=False`` only the notebook (and script) are written; they point
at the run folder and the installed pipeline by absolute path.
"""
import argparse
import ast
import json
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
# Entry modules the notebook imports; their local imports are added automatically.
ENTRY_MODULES = ('tensegrity_lm', 'tensegrity_serve', 'tensegrity_nb_lib', 'train_local')
RUN_FILES = ('run.json', 'result.json', 'progress.json', 'train_log.jsonl', 'previews.jsonl', 'local_config.json')
CORPUS_FILES = ('train.bin', 'val.bin', 'corpus.json', 'documents.jsonl')
CHECKPOINT_KIND = re.compile(r'^(best|latest|step\d{8,})$')
KERNEL_NAME = 'whitecube-train'


def checkpoint_file(run_dir, kind):
    if not isinstance(kind, str) or not CHECKPOINT_KIND.match(kind):
        raise ValueError('Choose Best, Latest, or a step snapshot.')
    path = Path(run_dir) / f'ckpt_{kind}.pt'
    if not path.is_file() or path.is_symlink():
        raise ValueError('This checkpoint has not been saved yet.')
    return path


def read(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def slug(text, fallback='whitecube-model'):
    value = re.sub(r'[^A-Za-z0-9]+', '-', str(text)).strip('-').lower()[:60]
    return value or fallback


def module_closure(entries=ENTRY_MODULES, directory=PIPELINE):
    """Pipeline modules reachable from the entry modules through local imports."""
    local = {path.stem for path in directory.glob('*.py')}
    pending, found = list(entries), set()
    while pending:
        name = pending.pop()
        if name in found or name not in local:
            continue
        found.add(name)
        tree = ast.parse((directory / f'{name}.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pending.extend(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                pending.append(node.module.split('.')[0])
    return sorted(found)


def code(source):
    return {'cell_type': 'code', 'execution_count': None, 'metadata': {}, 'outputs': [],
            'source': source.strip('\n').splitlines(keepends=True)}


def markdown(source):
    return {'cell_type': 'markdown', 'metadata': {}, 'source': source.strip('\n').splitlines(keepends=True)}


def summary(run_dir, kind):
    run_dir = Path(run_dir)
    meta, result = read(run_dir / 'run.json'), read(run_dir / 'result.json')
    corpus, progress = read(run_dir / 'data' / 'corpus.json'), read(run_dir / 'progress.json')
    name = meta.get('display_name') if isinstance(meta.get('display_name'), str) and meta.get('display_name') else run_dir.name
    rows = [('Run', f'`{run_dir.name}`'), ('Checkpoint', f'`ckpt_{kind}.pt`')]
    if isinstance(meta.get('model_size'), str):
        rows.append(('Model size', meta['model_size'].upper()))
    if number(meta.get('parameters')):
        rows.append(('Parameters', f'{int(meta["parameters"]):,}'))
    steps = max((int(value) for value in (result.get('completed_steps'), progress.get('completed_steps')) if number(value)), default=None)
    if steps is not None:
        rows.append(('Completed steps (run)', f'{steps:,}' + (f' of {int(meta["steps"]):,}' if number(meta.get('steps')) else '')))
    if number(result.get('best_val_loss')):
        loss = result['best_val_loss']
        rows.append(('Best validation loss', f'{loss:.4f} nats/byte · {loss / math.log(2):.3f} bits/byte · perplexity {math.exp(min(loss, 700)):.2f}'))
    if number(corpus.get('train_tokens')):
        rows.append(('Corpus', f'{int(corpus["train_tokens"]):,} training + {int(corpus.get("val_tokens") or 0):,} validation byte tokens'))
    for key, title in (('learning_rate', 'Peak learning rate'), ('schedule', 'Schedule'), ('batch_size', 'Batch size'),
                       ('grad_accum_steps', 'Gradient accumulation'), ('seed', 'Seed'), ('device', 'Trained on')):
        if key in meta and isinstance(meta[key], (str, int, float)) and not isinstance(meta[key], bool):
            rows.append((title, str(meta[key])))
    if number(result.get('total_training_seconds')):
        rows.append(('Training time', f'{result["total_training_seconds"] / 60:.1f} minutes'))
    notes = meta.get('notes') if isinstance(meta.get('notes'), str) else ''
    return name, meta, rows, notes


def build_cells(run_dir, kind, *, bundle, export_dir, preview=None, has_corpus=True, python=None):
    """Notebook cells. Paths are relative to the bundle, or absolute when not bundling."""
    run_dir = Path(run_dir).resolve()
    name, meta, rows, notes = summary(run_dir, kind)
    preview = {'prompt': 'Once upon a time', 'tokens': 160, 'temperature': .85, 'top_k': 40, 'top_p': 1.0, 'seed': 1337, **(preview or {})}
    batch = int(meta['batch_size']) if number(meta.get('batch_size')) and meta['batch_size'] >= 1 else 4
    accumulation = int(meta['grad_accum_steps']) if number(meta.get('grad_accum_steps')) and meta['grad_accum_steps'] >= 1 else 4
    table = '\n'.join(f'| {key} | {value} |' for key, value in rows)
    if bundle:
        paths = (f"EXPORT_DIR = Path({str(export_dir)!r})  # where Whitecube Trainer wrote this bundle\n"
                 "BUNDLE_DIR = None  # set to the moved bundle folder if your kernel starts elsewhere\n"
                 "working_dir = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()\n"
                 "candidates = ([Path(BUNDLE_DIR).expanduser()] if BUNDLE_DIR is not None else\n"
                 "              [working_dir, *working_dir.parents, EXPORT_DIR])\n"
                 "HERE = next((path for path in candidates if\n"
                 "             (path / 'whitecube_pipeline' / 'tensegrity_lm.py').is_file() and\n"
                 f"             (path / 'checkpoint' / 'ckpt_{kind}.pt').is_file()), None)\n"
                 "if HERE is None:\n"
                 "    raise FileNotFoundError('Notebook bundle not found. Keep the .ipynb together with '\n"
                 "                            'whitecube_pipeline/ and checkpoint/, or set BUNDLE_DIR '\n"
                 "                            'above to the folder containing them.')\n"
                 "HERE = HERE.resolve()\n"
                 "PIPELINE = HERE / 'whitecube_pipeline'\n"
                 "RUN_DIR = HERE / 'run'\n"
                 f"CHECKPOINT = HERE / 'checkpoint' / 'ckpt_{kind}.pt'\n"
                 + (f"DATA_DIR = HERE / 'data'\n" if has_corpus else
                    f"DATA_DIR = Path({str(run_dir / 'data')!r})  # not bundled: the original run's prepared corpus\n"))
    else:
        paths = (f"PIPELINE = Path({str(PIPELINE)!r})\n"
                 f"RUN_DIR = Path({str(run_dir)!r})\n"
                 f"CHECKPOINT = RUN_DIR / 'ckpt_{kind}.pt'\n"
                 "DATA_DIR = RUN_DIR / 'data'\n"
                 "HERE = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()\n")
    environment_hint = (f' Whitecube Trainer used {python}. Select that environment in the kernel picker.'
                        if python else ' Select a Python environment with PyTorch and NumPy in the kernel picker.')
    setup = '''
## 1 · Setup

Run this notebook with a kernel that has **PyTorch and NumPy**. The Python
shown by the first cell is the notebook kernel, which can differ from the
Python used by your Jupyter application. Matplotlib is optional for curves;
safetensors is optional for the final weights export.
'''
    if python:
        kernel_command = shlex.join([str(python), '-m', 'ipykernel', 'install', '--user', '--name', KERNEL_NAME,
                                     '--display-name', 'Whitecube Trainer (PyTorch)'])
        setup += (f'\nWhitecube Trainer used `{python}`. If that environment is not listed in the\n'
                  'kernel picker, run this command in a terminal, then select **Whitecube Trainer (PyTorch)**:\n\n'
                  f'```sh\n{kernel_command}\n```\n'
                  'If it reports a missing ipykernel module, install it into that same environment first.\n')
    if bundle:
        setup += ('\nKeep the notebook with its `whitecube_pipeline/` and `checkpoint/` folders when copying it. '
                  'If the kernel starts outside a moved bundle, set `BUNDLE_DIR` in the first cell.\n')
    cells = [
        markdown(f'''
# {name}

Byte-level Tensegrity language model from **Whitecube Trainer**, exported
{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. It continues prose
in the style of its corpus; it is not an instruction-following chat model.
Context is 256 byte tokens (bytes 0–255 plus BOS/EOS/PAD).

| Property | Value |
| --- | --- |
{table}
''' + (f'\n**Run notes:** {notes}\n' if notes else '')),
        markdown(setup),
        code(f'''
import json, math, shutil, sys, time
from pathlib import Path

{paths}if not (PIPELINE / 'tensegrity_lm.py').is_file():
    raise FileNotFoundError(f'Model code not found at {{PIPELINE}}. Restore the pipeline folder or export a complete bundle.')
if not CHECKPOINT.is_file():
    raise FileNotFoundError(f'Checkpoint not found at {{CHECKPOINT}}. Restore the checkpoint or export a complete bundle.')
sys.path.insert(0, str(PIPELINE))
print('Notebook Python:', sys.executable)
try:
    import numpy as np
    import torch
except ImportError as error:
    raise ImportError('Cannot import PyTorch/NumPy in ' + sys.executable + '.' + {environment_hint!r} +
                      ' Original error: ' + str(error)) from error
from tensegrity_lm import ByteTokenizer, TensegrityConfig, TensegrityLM, evaluate, load_checkpoint
from tensegrity_serve import decode

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f'PyTorch {{torch.__version__}} on {{DEVICE}} · checkpoint {{CHECKPOINT}}')
print('Prepared corpus:', DATA_DIR if (DATA_DIR / 'train.bin').is_file() else 'not available')'''),
        markdown('## 2 · Load the checkpoint'),
        code('''
saved = load_checkpoint(str(CHECKPOINT), 'cpu')
cfg = TensegrityConfig(**saved['config'])
model = TensegrityLM(cfg)
model.load_state_dict(saved['model_state_dict'])
model = model.to(DEVICE).eval()
parameters = sum(p.numel() for p in model.parameters())
best = float(saved['best_val_loss'])
print(f"{parameters:,} parameters · {cfg.n_layer} layers · width {cfg.n_embd} · context {cfg.block_size}")
print(f"Saved after step {saved['step'] + 1:,} · best validation {best:.4f} nats/byte "
      f"= {best / math.log(2):.3f} bits/byte · perplexity {math.exp(min(best, 700)):.2f}")'''),
        markdown('## 3 · Training curves\n\nFrom `train_log.jsonl`. Loss is cross-entropy in nats per byte; divide by ln 2 for bits per byte.'),
        code('''
metrics = []
log = RUN_DIR / 'train_log.jsonl'
if log.is_file():
    for line in log.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get('step'), (int, float)):
            metrics.append(item)
train_curve = [(m['step'], m['total_loss']) for m in metrics if isinstance(m.get('total_loss'), (int, float))]
val_curve = [(m['step'], m['val_loss']) for m in metrics if isinstance(m.get('val_loss'), (int, float))]
print(f'{len(train_curve):,} training points · {len(val_curve):,} validation points')
try:
    import matplotlib.pyplot as plt
except ImportError:
    plt = None
if plt and (train_curve or val_curve):
    figure, axis = plt.subplots(figsize=(9, 3.6))
    if train_curve:
        axis.plot(*zip(*train_curve), color='#111111', linewidth=1, label='training')
    if val_curve:
        axis.plot(*zip(*val_curve), color='#c8102e', marker='s', markersize=3, linewidth=1.5, label='validation')
    axis.set_xlabel('optimizer step'); axis.set_ylabel('loss (nats/byte)'); axis.grid(alpha=.25); axis.legend()
    plt.show()
else:
    for step, loss in val_curve[-10:]:
        print(f'step {step:>8,}  val {loss:.4f}')'''),
        markdown('## 4 · Generate\n\nTemperature 0 is greedy. `top_p=None` disables nucleus sampling. Generation stops at the end-of-document token.'),
        code(f'''
def generate(prompt, max_new_tokens={int(preview['tokens'])}, temperature={float(preview['temperature'])!r}, top_k={int(preview['top_k'])},
             top_p={None if float(preview['top_p']) >= 1 else float(preview['top_p'])!r}, seed={int(preview['seed'])}):
    torch.manual_seed(seed)
    ids = list(prompt.encode('utf-8')[-cfg.block_size:]) or [ByteTokenizer.BOS]
    out = []
    with torch.no_grad():
        for token in decode(model, ids, temperature=temperature, top_k=top_k, top_p=top_p, max_new_tokens=max_new_tokens):
            if token == ByteTokenizer.EOS:
                break
            if token < 256:
                out.append(token)
    return bytes(out).decode('utf-8', errors='replace')

PROMPT = {str(preview['prompt'])!r}
print(PROMPT + generate(PROMPT))'''),
        markdown('## 5 · Next-byte probabilities\n\nWhat the model expects right after a prompt.'),
        code('''
def next_bytes(prompt, k=10):
    ids = list(prompt.encode('utf-8')[-cfg.block_size:]) or [ByteTokenizer.BOS]
    with torch.no_grad():
        logits, _, _ = model(torch.tensor([ids], device=DEVICE))
    probabilities = torch.softmax(logits[0, -1].float(), dim=-1)
    values, indices = probabilities.topk(k)
    for p, index in zip(values.tolist(), indices.tolist()):
        shown = {256: '<BOS>', 257: '<EOS>', 258: '<PAD>'}.get(index) or repr(bytes([index]).decode('latin-1'))
        print(f'{shown:>8}  {p:6.1%}  ' + '█' * round(p * 40))

next_bytes(PROMPT)'''),
        markdown('## 6 · Validation loss\n\nRe-measures the held-out split with the trainer\'s fixed evaluation sampler (20 batches of 8 windows).'),
        code('''
if (DATA_DIR / 'val.bin').is_file():
    started = time.monotonic()
    device_type = 'cuda' if DEVICE == 'cuda' else 'cpu'
    val_loss = evaluate(model, str(DATA_DIR), cfg, DEVICE, device_type, torch.float32, False, eval_iters=20)
    print(f'Validation loss {val_loss:.4f} nats/byte = {val_loss / math.log(2):.3f} bits/byte · '
          f'perplexity {math.exp(min(val_loss, 700)):.2f} · {time.monotonic() - started:.1f} s')
else:
    print('No prepared corpus here; export with the corpus or point DATA_DIR at a run\\'s data folder.')'''),
        markdown(f'''
## 7 · Continue training (optional)

Copies the checkpoint into `continued/` and resumes there with the saved
optimizer, schedule and RNG state. The original run is never modified. Set
`CONTINUE_TRAINING = True` and choose how many extra optimizer steps to run.
`SCHEDULE_STEPS` extends the learning-rate horizon; `None` keeps the saved plan.'''),
        code(f'''
CONTINUE_TRAINING = False
EXTRA_STEPS = 200
SCHEDULE_STEPS = None
WORK_DIR = HERE / 'continued'

if CONTINUE_TRAINING:
    from train_local import train
    if not (DATA_DIR / 'train.bin').is_file():
        raise FileNotFoundError(f'No prepared corpus at {{DATA_DIR}}')
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    latest = WORK_DIR / 'ckpt_latest.pt'
    if not latest.is_file():
        shutil.copy2(CHECKPOINT, latest)
    start = load_checkpoint(str(latest), 'cpu')['step'] + 1
    final_step, best_loss = train(DATA_DIR, WORK_DIR, steps=start + EXTRA_STEPS, batch_size={batch},
                                  grad_accum_steps={accumulation}, device=DEVICE, resume=True,
                                  schedule_steps=SCHEDULE_STEPS)
    print(f'Continued to step {{final_step:,}} · best validation {{best_loss:.4f}}')
    saved = load_checkpoint(str(WORK_DIR / 'ckpt_latest.pt'), 'cpu')
    model.load_state_dict(saved['model_state_dict']); model.eval()
else:
    print('Set CONTINUE_TRAINING = True to train further in', WORK_DIR)'''),
        markdown('## 8 · Export weights\n\nWrites `model.safetensors` and `model.config.json` when the optional `safetensors` package is available, then reloads them to check the round trip.'),
        code('''
try:
    import safetensors
except ImportError:
    print('Weights export skipped: install safetensors in this kernel, then rerun this cell.')
else:
    from tensegrity_nb_lib import export_to_safetensors, load_from_safetensors

    EXPORT_WEIGHTS = HERE / 'safetensors'
    try:
        model.cpu()
        weights, config = export_to_safetensors(model, cfg, str(EXPORT_WEIGHTS), name='model')
        reloaded, _ = load_from_safetensors(weights, config)
        probe = torch.tensor([list(b'Whitecube')])
        with torch.no_grad():
            same = torch.allclose(model(probe)[0], reloaded.eval()(probe)[0], atol=1e-6)
    finally:
        model.to(DEVICE)
    print('Round trip matches' if same else 'Round trip differs')'''),
    ]
    return cells


def notebook(cells, *, kernel=None, python_version=None):
    kernelspec = kernel or {'name': 'python3', 'display_name': 'Python 3', 'language': 'python'}
    return {'nbformat': 4, 'nbformat_minor': 5,
            'metadata': {'kernelspec': kernelspec,
                         'language_info': {'name': 'python', **({'version': python_version} if python_version else {})},
                         'whitecube': {'application': 'Whitecube Trainer', 'schema_version': 1}},
            'cells': [{**cell, 'id': f'wc-{index:02d}'} for index, cell in enumerate(cells)]}


def percent_script(cells, title):
    """The same cells as a `# %%` script (VS Code, Spyder, Jupytext)."""
    parts = [f'# {title} — exported from Whitecube Trainer as percent-format cells.\n']
    for cell in cells:
        text = ''.join(cell['source'])
        if cell['cell_type'] == 'markdown':
            parts.append('# %% [markdown]\n' + ''.join(f'# {line}'.rstrip() + '\n' for line in text.splitlines()))
        else:
            parts.append('# %%\n' + text + '\n')
    return '\n'.join(parts)


def bundle_size(run_dir, kind, *, bundle=True, corpus=False):
    """Bytes the export will copy, for the dialog and the free-space check."""
    total = 0
    if bundle:
        total += checkpoint_file(run_dir, kind).stat().st_size
        for name in RUN_FILES:
            path = Path(run_dir) / name
            if path.is_file():
                total += path.stat().st_size
        total += sum((PIPELINE / f'{name}.py').stat().st_size for name in module_closure())
        tensegrity_pkg = PIPELINE / 'tensegrity'
        if tensegrity_pkg.is_dir():
            total += sum(path.stat().st_size for path in sorted(tensegrity_pkg.glob('*.py')))
        if corpus:
            total += sum((Path(run_dir) / 'data' / name).stat().st_size for name in CORPUS_FILES
                         if (Path(run_dir) / 'data' / name).is_file())
    return total + 64 * 1024


def copy_file(source, destination, cancelled=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as reader, destination.open('xb') as writer:
        while chunk := reader.read(4 * 1024 * 1024):
            if cancelled and cancelled():
                raise InterruptedError('Export cancelled')
            writer.write(chunk)
        writer.flush(); os.fsync(writer.fileno())
    os.chmod(destination, 0o644)


def export(run_dir, kind, output, *, bundle=True, corpus=False, script=True, preview=None,
           python=None, kernel=None, python_version=None, cancelled=None):
    """Write the notebook export into a new or empty folder; returns the notebook path."""
    run_dir = Path(run_dir).resolve()
    source = checkpoint_file(run_dir, kind)
    output = Path(output).expanduser().absolute()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Choose a new or empty folder for the notebook export.')
    has_corpus = all((run_dir / 'data' / name).is_file() for name in ('train.bin', 'val.bin'))
    if corpus and not has_corpus:
        raise ValueError('This run has no prepared corpus to include.')
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(parent).free < bundle_size(run_dir, kind, bundle=bundle, corpus=corpus) + 16 * 1024**2:
        raise ValueError('Not enough free disk space for this export.')
    name, *_ = summary(run_dir, kind)
    stem = slug(name)
    staging = Path(tempfile.mkdtemp(prefix=f'.{output.name}-', dir=parent))
    try:
        if bundle:
            for module in module_closure():
                copy_file(PIPELINE / f'{module}.py', staging / 'whitecube_pipeline' / f'{module}.py', cancelled)
            tensegrity_pkg = PIPELINE / 'tensegrity'
            if tensegrity_pkg.is_dir():
                for pkg_file in sorted(tensegrity_pkg.glob('*.py')):
                    copy_file(pkg_file, staging / 'whitecube_pipeline' / 'tensegrity' / pkg_file.name, cancelled)
            copy_file(source, staging / 'checkpoint' / source.name, cancelled)
            for item in RUN_FILES:
                if (run_dir / item).is_file():
                    copy_file(run_dir / item, staging / 'run' / item, cancelled)
            if corpus:
                for item in CORPUS_FILES:
                    if (run_dir / 'data' / item).is_file():
                        copy_file(run_dir / 'data' / item, staging / 'data' / item, cancelled)
        cells = build_cells(run_dir, kind, bundle=bundle, export_dir=output, preview=preview,
                            has_corpus=corpus or not bundle, python=python)
        document = notebook(cells, kernel=kernel, python_version=python_version)
        (staging / f'{stem}.ipynb').write_text(json.dumps(document, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        if script:
            (staging / f'{stem}.py').write_text(percent_script(cells, name), encoding='utf-8')
        if bundle:
            (staging / 'README.md').write_text(
                f'# {name}\n\nNotebook bundle exported by Whitecube Trainer from `{run_dir}` '
                f'(checkpoint `{source.name}`).\n\nOpen `{stem}.ipynb` in Jupyter or VS Code with the '
                'training Python environment as the kernel. The kernel needs PyTorch and NumPy; '
                'matplotlib (curves) and safetensors (weights export) are optional. '
                + (f'The training Python was `{python}`. ' if python else '') +
                'See the Setup section for kernel registration instructions.\n\n'
                'Keep the notebook together with `whitecube_pipeline/` and `checkpoint/` when moving this '
                'folder. Set `BUNDLE_DIR` in its first cell if the kernel starts elsewhere. '
                + ('The prepared corpus is included.\n' if corpus else
                   'Evaluation and continued training read the original run\'s '
                   f'prepared corpus at `{run_dir / "data"}`.\n'), encoding='utf-8')
        for path in staging.rglob('*'):
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        os.chmod(staging, 0o755)
        if output.exists():
            output.rmdir()  # empty, checked above
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output / f'{stem}.ipynb'


def main():
    parser = argparse.ArgumentParser(description='Export a Whitecube Trainer checkpoint as a Jupyter notebook bundle.')
    parser.add_argument('run', type=Path, help='Run folder, for example ~/train-out/20261011-061446-879691')
    parser.add_argument('--checkpoint', default='best', help='best, latest or stepNNNNNNNN (default best)')
    parser.add_argument('--output', type=Path, required=True, help='New or empty destination folder')
    parser.add_argument('--with-corpus', action='store_true', help='Copy the prepared train/val corpus too')
    parser.add_argument('--reference', action='store_true', help='Write only the notebook; reference the run in place')
    parser.add_argument('--no-script', action='store_true', help='Skip the percent-format .py copy')
    parser.add_argument('--python', help='Training Python executable shown in setup instructions')
    parser.add_argument('--kernel-name', help='Registered Jupyter kernel to select')
    parser.add_argument('--kernel-display-name', help='Display name for the registered Jupyter kernel')
    args = parser.parse_args()
    try:
        path = export(args.run.expanduser(), args.checkpoint, args.output, bundle=not args.reference,
                      corpus=args.with_corpus, script=not args.no_script, python=args.python,
                      kernel={'name': args.kernel_name, 'display_name': args.kernel_display_name or args.kernel_name,
                              'language': 'python'} if args.kernel_name else None)
    except (ValueError, OSError) as error:
        parser.exit(1, f'{parser.prog}: {error}\n')
    print(path)


if __name__ == '__main__':
    sys.exit(main())
