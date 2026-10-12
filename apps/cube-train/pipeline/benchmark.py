"""Reproducible, CPU-only held-out evaluation and generation for saved runs."""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from corpus_artifacts import verify_corpus
from files import atomic_json, file_sha256, fsync_directory

REPORT_KIND = 'whitecube_checkpoint_benchmark'
DEFAULT_PROMPTS = (
    'The morning began with',
    'To solve the problem, first',
    'In the quiet garden,',
)


def resolve_checkpoint(run_dir, checkpoint):
    """Resolve a known checkpoint selector without allowing arbitrary paths."""
    if checkpoint in ('best', 'latest'):
        name = f'ckpt_{checkpoint}.pt'
    elif isinstance(checkpoint, str) and re.fullmatch(r'step[0-9]{1,12}', checkpoint):
        number = int(checkpoint[4:])
        if number < 1:
            raise ValueError('Snapshot completed steps must be positive')
        name = f'ckpt_step{number:08d}.pt'
    else:
        raise ValueError('checkpoint must be best, latest, or stepN (for example step100)')
    path = Path(run_dir) / name
    if not path.is_file():
        raise FileNotFoundError(f'Checkpoint does not exist: {path}')
    return path


def validate_output(output, run_dir):
    """Require a new report destination outside run metadata and prepared data."""
    output = Path(output).expanduser().absolute()
    if output.is_symlink():
        raise ValueError('Benchmark output cannot be a symbolic link')
    resolved = output.resolve()
    data = (Path(run_dir) / 'data').resolve()
    if (resolved.suffix.lower() != '.json' or resolved == data or data in resolved.parents
            or resolved.name in ('run.json', 'result.json', 'status.json', 'corpus.json',
                                 'meta.json', 'model.config.json')
            or resolved.name.startswith('ckpt_')):
        raise ValueError('Choose a .json report destination outside prepared data and run metadata')
    if output.exists():
        raise FileExistsError(f'Benchmark output already exists; choose a new report path: {output}')
    return output


def _signature(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _unchanged(snapshots):
    for path, before in snapshots.items():
        try:
            after = _signature(path)
        except OSError as error:
            raise ValueError(f'Benchmark input changed during evaluation: {path}; retry after training stops') from error
        if after != before:
            raise ValueError(f'Benchmark input changed during evaluation: {path}; retry after training stops')


def _integer(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer between {low} and {high}')


def benchmark(run_dir, *, checkpoint='best', output=None, eval_iters=20, batch_size=4,
              seed=2026, cpu_threads=2, prompts=None, max_new_tokens=64, temperature=0.0):
    """Assess frozen artifacts; only the explicit report destination is written.

    Validation samples windows with replacement from val.bin using a private
    RandomState. Loss/perplexity include special tokens. Bits per byte measures
    NLL on raw byte targets only, excluding BOS/EOS/PAD tokens. Sampled batches
    need not cover every held-out token; counts and a batch hash make this clear.
    """
    for name, value, low, high in (
        ('eval_iters', eval_iters, 1, 10000), ('batch_size', batch_size, 1, 64),
        ('seed', seed, 0, 2**32 - 1), ('cpu_threads', cpu_threads, 1, 64),
        ('max_new_tokens', max_new_tokens, 0, 4096),
    ):
        _integer(name, value, low, high)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or temperature < 0:
        raise ValueError('temperature must be finite and nonnegative')
    prompts = list(DEFAULT_PROMPTS) if prompts is None else prompts
    if (not isinstance(prompts, (list, tuple)) or len(prompts) > 32
            or any(not isinstance(prompt, str) or len(prompt.encode('utf-8')) > 65536 for prompt in prompts)):
        raise ValueError('prompts must contain at most 32 strings, each at most 65536 UTF-8 bytes')
    run_dir = Path(run_dir).expanduser().resolve()
    checkpoint_path = resolve_checkpoint(run_dir, checkpoint)
    destination = validate_output(output, run_dir) if output is not None else None
    data = run_dir / 'data'
    inputs = [checkpoint_path, data / 'train.bin', data / 'val.bin']
    inputs.extend(path for name in ('corpus.json', 'documents.jsonl') if (path := data / name).exists())
    snapshots = {path: _signature(path) for path in inputs}
    checkpoint_hash = file_sha256(checkpoint_path)

    # Keep the CLI usable with --help even when the training environment is absent.
    import numpy as np
    import torch
    from tensegrity_lm import ByteTokenizer, TensegrityConfig, TensegrityLM, get_batch, load_checkpoint
    from tensegrity_serve import decode

    previous_threads = torch.get_num_threads()
    try:
        torch.set_num_threads(cpu_threads)
        # Isolate construction and stochastic decoding from a caller's RNG state.
        with torch.random.fork_rng(devices=[]), torch.inference_mode():
            torch.manual_seed(seed)
            saved = load_checkpoint(str(checkpoint_path), 'cpu')
            cfg = TensegrityConfig(**saved['config'])
            corpus = verify_corpus(data, minimum=cfg.block_size + 1, vocab_size=cfg.vocab_size)
            artifacts = {name: {'bytes': (data / name).stat().st_size, 'sha256': file_sha256(data / name)}
                         for name in ('train.bin', 'val.bin', 'documents.jsonl') if (data / name).is_file()}
            metadata_hash = file_sha256(data / 'corpus.json') if (data / 'corpus.json').is_file() else None
            _unchanged(snapshots)
            model = TensegrityLM(cfg).eval()
            model.load_state_dict(saved['model_state_dict'])
            parameter_count = sum(parameter.numel() for parameter in model.parameters())
            # Drop optimizer tensors before running the benchmark to limit RAM.
            completed_steps = saved['step'] + 1
            best_loss = saved['best_val_loss'] if math.isfinite(saved['best_val_loss']) else None
            checkpoint_format = saved.get('format_version')
            model_config = dict(saved['config'])
            del saved

            rng = np.random.RandomState(seed)
            batch_hash = hashlib.sha256()
            total_nll = byte_nll = 0.0
            tokens = byte_tokens = 0
            started = time.perf_counter()
            for _ in range(eval_iters):
                x, y = get_batch(str(data), 'val', batch_size, cfg.block_size, 'cpu', rng=rng)
                batch_hash.update(x.numpy().astype('<u2').tobytes())
                batch_hash.update(y.numpy().astype('<u2').tobytes())
                logits, _, _ = model(x)
                losses = torch.nn.functional.cross_entropy(
                    logits.float().reshape(-1, cfg.vocab_size), y.reshape(-1), reduction='none')
                if not torch.isfinite(losses).all():
                    raise RuntimeError('Nonfinite validation loss; checkpoint cannot be benchmarked')
                raw_bytes = y.reshape(-1) < 256
                total_nll += losses.double().sum().item()
                byte_nll += losses[raw_bytes].double().sum().item()
                tokens += y.numel()
                byte_tokens += raw_bytes.sum().item()
            validation_seconds = time.perf_counter() - started
            loss = total_nll / tokens
            perplexity = math.exp(loss) if loss < math.log(sys.float_info.max) else None
            validation = {
                'loss': loss, 'perplexity': perplexity, 'bits_per_token': loss / math.log(2),
                'bits_per_byte': byte_nll / byte_tokens / math.log(2) if byte_tokens else None,
                'tokens': tokens, 'byte_tokens': byte_tokens, 'batches': eval_iters,
                'seconds': validation_seconds, 'tokens_per_second': tokens / validation_seconds,
                'batch_sha256': batch_hash.hexdigest(),
                'sampling': 'Fixed seeded windows sampled with replacement from val.bin',
                'bits_per_byte_definition': 'Mean negative log2 probability of raw byte targets (IDs 0-255); excludes special tokens',
            }
            samples = []
            generation_tokens = 0
            generation_seconds = 0.0
            for index, prompt in enumerate(prompts):
                prompt_ids = list(prompt.encode('utf-8')[-cfg.block_size:]) or [ByteTokenizer.BOS]
                sample_seed = (seed + index) % 2**32
                torch.manual_seed(sample_seed)
                started = time.perf_counter()
                generated = list(decode(model, prompt_ids, temperature=temperature, top_k=None,
                                        top_p=None, max_new_tokens=max_new_tokens))
                seconds = time.perf_counter() - started
                samples.append({
                    'prompt': prompt, 'prompt_token_ids': prompt_ids, 'seed': sample_seed,
                    'continuation': bytes(token for token in generated if token < 256).decode('utf-8', errors='replace'),
                    'token_ids': generated, 'tokens': len(generated), 'seconds': seconds,
                    'tokens_per_second': len(generated) / seconds,
                    'stop_reason': 'eos' if generated and generated[-1] == ByteTokenizer.EOS else 'length',
                })
                generation_tokens += len(generated)
                generation_seconds += seconds
            _unchanged(snapshots)
            report = {
                'schema_version': 1, 'kind': REPORT_KIND,
                'created_at': datetime.now(timezone.utc).isoformat(), 'run_dir': str(run_dir),
                'checkpoint': {'name': checkpoint_path.name, 'path': str(checkpoint_path),
                               'sha256': checkpoint_hash, 'bytes': checkpoint_path.stat().st_size,
                               'completed_steps': completed_steps, 'best_val_loss': best_loss,
                               'format_version': checkpoint_format},
                'model': {'parameters': parameter_count, 'config': model_config},
                'corpus': {'path': str(data), 'train_tokens': corpus['train_tokens'],
                           'val_tokens': corpus['val_tokens'], 'artifacts': artifacts,
                           'integrity_verified': bool(corpus.get('artifacts')),
                           'metadata_sha256': metadata_hash, 'split_method': corpus.get('split_method')},
                'settings': {'eval_iters': eval_iters, 'batch_size': batch_size, 'seed': seed,
                             'cpu_threads': cpu_threads, 'max_new_tokens': max_new_tokens,
                             'temperature': temperature},
                'runtime': {'device': 'cpu', 'dtype': 'float32', 'python': platform.python_version(),
                            'torch': str(torch.__version__), 'numpy': np.__version__,
                            'platform': platform.platform(), 'machine': platform.machine()},
                'validation': validation,
                'generation': {'samples': samples, 'tokens': generation_tokens, 'seconds': generation_seconds,
                               'tokens_per_second': generation_tokens / generation_seconds if generation_seconds else 0.0},
            }
    finally:
        torch.set_num_threads(previous_threads)
    if destination is not None:
        validate_output(destination, run_dir)
        report['output'] = str(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.benchmark-', dir=destination.parent) as temporary:
            staged = Path(temporary) / 'report.json'
            atomic_json(staged, report)
            # An atomic hard-link publication also refuses a destination created
            # concurrently after validation; os.replace would overwrite it.
            os.link(staged, destination)
            fsync_directory(destination.parent)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--checkpoint', default='best', help='best, latest, or stepN (completed steps)')
    parser.add_argument('--output', type=Path, required=True, help='New JSON report path; existing files are never replaced')
    parser.add_argument('--eval-iters', type=int, default=20)
    parser.add_argument('--batch-size', type=int, default=4)
    parser.add_argument('--seed', type=int, default=2026)
    parser.add_argument('--cpu-threads', type=int, default=2)
    parser.add_argument('--prompt', action='append', help='Repeat to replace the three built-in continuation prompts')
    parser.add_argument('--max-new-tokens', type=int, default=64, help='Per prompt; 0 skips decoding')
    parser.add_argument('--temperature', type=float, default=0.0, help='0 uses deterministic greedy decoding')
    args = parser.parse_args(argv)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            report = benchmark(args.run_dir, checkpoint=args.checkpoint, output=args.output,
                               eval_iters=args.eval_iters, batch_size=args.batch_size, seed=args.seed,
                               cpu_threads=args.cpu_threads, prompts=args.prompt,
                               max_new_tokens=args.max_new_tokens, temperature=args.temperature)
    except (OSError, ValueError, RuntimeError, AssertionError, ImportError) as error:
        parser.exit(1, f'Benchmark failed: {error}\n')
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
