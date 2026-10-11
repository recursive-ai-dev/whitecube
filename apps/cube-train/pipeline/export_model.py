"""Export a checkpoint's weights as safetensors with its config and a model card."""
import argparse
import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def model_card(name, run, checkpoint, saved, parameters):
    meta = read(run / 'run.json')
    result = read(run / 'result.json')
    corpus = read(run / 'data' / 'corpus.json')
    rows = [('Checkpoint', checkpoint.name), ('Parameters', f'{parameters:,}'),
            ('Completed steps', f"{saved['step'] + 1:,}"), ('Best validation loss', f"{saved['best_val_loss']:.4f}"),
            ('Context', f"{saved['config'].get('block_size', 256)} byte tokens"),
            ('Training tokens', f"{corpus['train_tokens']:,}" if isinstance(corpus.get('train_tokens'), int) else 'unknown')]
    for key, title in (('learning_rate', 'Peak learning rate'), ('schedule', 'Schedule'), ('batch_size', 'Batch size'),
                       ('grad_accum_steps', 'Gradient accumulation'), ('seed', 'Seed')):
        if key in meta:
            rows.append((title, str(meta[key])))
    if isinstance(result.get('total_training_seconds'), (int, float)):
        rows.append(('Training time', f"{result['total_training_seconds'] / 60:.1f} minutes"))
    table = '\n'.join(f'| {key} | {value} |' for key, value in rows)
    return f'''# {name}

A byte-level Tensegrity language model trained locally with Whitecube Trainer.
It continues prose; it is not an instruction-following chat model.

| Property | Value |
| --- | --- |
{table}

## Files

- `model.safetensors`: weights only (the tied output projection is stored once).
- `model.config.json`: the `TensegrityConfig` needed to rebuild the model.

Load with `tensegrity_nb_lib.load_from_safetensors('model.safetensors', 'model.config.json')`
from the Whitecube Trainer pipeline. Exported {datetime.now(timezone.utc).date().isoformat()}.
'''


def read(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='New or empty destination folder')
    parser.add_argument('--name', default='')
    args = parser.parse_args()
    checkpoint = args.checkpoint.expanduser().resolve()
    output = args.output.expanduser().absolute()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error('Choose a new or empty destination folder')
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from tensegrity_lm import TensegrityConfig, TensegrityLM, load_checkpoint
        from tensegrity_nb_lib import export_to_safetensors
        torch.set_num_threads(2)
        saved = load_checkpoint(str(checkpoint), 'cpu')
        cfg = TensegrityConfig(**saved['config'])
        model = TensegrityLM(cfg).eval()
        model.load_state_dict(saved['model_state_dict'])
        parameters = sum(p.numel() for p in model.parameters())
        output.mkdir(parents=True, exist_ok=True, mode=0o755)
        export_to_safetensors(model, cfg, str(output), name='model')
    name = args.name.strip() or checkpoint.parent.name
    card = output / 'README.md'
    staging = output / f'.README.md.{os.getpid()}'
    staging.write_text(model_card(name, checkpoint.parent, checkpoint, saved, parameters), encoding='utf-8')
    os.replace(staging, card)
    print(json.dumps({'output': str(output), 'parameters': parameters,
                      'files': sorted(path.name for path in output.iterdir())}))


if __name__ == '__main__':
    main()
