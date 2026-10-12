"""Local 1M/2M/5M/10M Tensegrity training (legacy 4M/8M supported)."""
import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
import tempfile

import numpy as np
import torch

from tensegrity_lm import TensegrityConfig, TensegrityLM, prepare_data, load_checkpoint
from tensegrity_nb_lib import run_training, training_precision
from prepare_folder import prepare_folder
from model_presets import MODEL_PRESETS
from files import atomic_json

ROOT = Path(__file__).resolve().parent


def add_model_size_flags(parser):
    group = parser.add_mutually_exclusive_group()
    for size, preset in MODEL_PRESETS.items():
        group.add_argument(f"--{size}", dest="model_size", action="store_const", const=size,
                           help=f"{preset['parameters']:,} parameters (default: 1m; resume uses saved size)")


def local_config(model_size="1m"):
    if model_size not in MODEL_PRESETS:
        raise ValueError(f"Unknown model size: {model_size}; choose {', '.join(MODEL_PRESETS)}")
    return TensegrityConfig(
        n_layer=4, n_embd=MODEL_PRESETS[model_size]["width"], n_head=4, n_kv_head=1,
        ffn_mult=2.3, ffn_align=16, block_size=256,
        sliding_window=64, global_every=2,
        use_confidence_head=False, use_qk_norm=True, embed_scale=True,
        grad_checkpoint=False, layerdrop_max=0.0,
    )


def wsd_plan(horizon, warmup_steps):
    """Warmup, hold, then decay over the final fifth of the horizon."""
    decay = max(1, round(horizon * .2))
    return max(0, horizon - warmup_steps - decay), decay


def train(data_dir, out_dir, *, steps=1000, batch_size=4,
          grad_accum_steps=4, device="cuda", resume=False, stop_requested=None, schedule_steps=None, model_size=None,
          learning_rate=3e-4, min_lr_ratio=.1, warmup_steps=None, weight_decay=.1, grad_clip=1.0,
          eval_interval=None, eval_iters=None, log_interval=None, dtype=None,
          seed=1337, schedule='cosine', early_stop_patience=0, early_stop_min_delta=0.0, keep_snapshots=0, outcome=None):
    for name, value in {'steps': steps, 'batch_size': batch_size, 'grad_accum_steps': grad_accum_steps,
                        **{key: value for key, value in {'eval_interval': eval_interval, 'eval_iters': eval_iters,
                                                        'log_interval': log_interval}.items() if value is not None},
                        **({'schedule_steps': schedule_steps} if schedule_steps is not None else {})}.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f'{name} must be a positive integer')
    for name, value in {'seed': seed, 'early_stop_patience': early_stop_patience, 'keep_snapshots': keep_snapshots,
                        **({'warmup_steps': warmup_steps} if warmup_steps is not None else {})}.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f'{name} must be a nonnegative integer')
    for name, value, low, high in (('learning_rate', learning_rate, 1e-6, .1), ('min_lr_ratio', min_lr_ratio, 0, 1),
                                   ('weight_decay', weight_decay, 0, 1), ('grad_clip', grad_clip, 0, 100)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f'{name} must be between {low:g} and {high:g}')
    if schedule not in ('cosine', 'wsd'):
        raise ValueError('schedule must be cosine or wsd')
    if isinstance(early_stop_min_delta, bool) or not isinstance(early_stop_min_delta, (int, float)) or not math.isfinite(early_stop_min_delta) or early_stop_min_delta < 0:
        raise ValueError('early_stop_min_delta must be finite and nonnegative')
    if device not in ('cpu', 'cuda'):
        raise ValueError('device must be cpu or cuda')
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable. Select --device cpu or check the NVIDIA setup.")
    outcome = {} if outcome is None else outcome
    torch.manual_seed(seed)
    np.random.seed(seed)
    cfg = local_config(model_size or "1m")
    out_dir = Path(out_dir).resolve()
    ckpt_path = out_dir / "ckpt_latest.pt"
    saved_training = {}
    if resume:
        if not ckpt_path.is_file():
            raise FileNotFoundError(f"No checkpoint to resume: {ckpt_path}")
        checkpoint = load_checkpoint(str(ckpt_path), 'cpu')
        cfg = TensegrityConfig(**checkpoint['config'])
        saved_training = checkpoint.get('training_state') or {}
        if model_size is not None and asdict(cfg) != asdict(local_config(model_size)):
            raise ValueError(f"--{model_size} conflicts with the checkpoint model. "
                             "Resume without a size flag or start a new run.")
    elif ckpt_path.exists():
        raise FileExistsError("This run has a checkpoint. Use --resume or a new --out-dir.")
    training_precision(dtype, device, saved_training.get('precision'))
    for name, value, key in (('eval_iters', eval_iters, 'iters'), ('eval_interval', eval_interval, 'interval')):
        saved = (saved_training.get('evaluation') or {}).get(key)
        if value is not None and saved is not None and value != saved:
            raise ValueError(f'Resume {name} must match the saved evaluation plan; remove --{name.replace("_", "-")}')
    from corpus_artifacts import verify_corpus
    verify_corpus(data_dir, minimum=cfg.block_size + 1, vocab_size=cfg.vocab_size,
                  stop_requested=(lambda: stop_requested(0)) if stop_requested else None)
    model = TensegrityLM(cfg).to(device)
    count = sum(p.numel() for p in model.parameters())
    if not resume and count != MODEL_PRESETS[model_size or "1m"]["parameters"]:
        raise ValueError(f"Unexpected parameter count: {count:,}")
    print(f"Parameters: {count:,}; context: {cfg.block_size} byte tokens", flush=True)
    optimizer = model.configure_optimizer(
        weight_decay=weight_decay, learning_rate=learning_rate, betas=(0.9, 0.95),
        device_type=device, use_muon=False,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(out_dir / 'local_config.json', asdict(cfg))
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
        print(f"GPU: {torch.cuda.get_device_name(0)}", flush=True)
    horizon = schedule_steps or steps
    warmup = max(1, min(50, horizon // 10)) if warmup_steps is None else warmup_steps
    if schedule == 'wsd':
        warmup = max(1, warmup)
    stable, decay = wsd_plan(horizon, warmup) if schedule == 'wsd' else (None, None)
    result = run_training(
        model=model, optimizer=optimizer, cfg=cfg,
        out_dir=str(out_dir), device=device, data_dirs=str(Path(data_dir).resolve()),
        max_steps=steps, warmup_steps=warmup,
        learning_rate=learning_rate, min_lr=learning_rate * min_lr_ratio,
        schedule=schedule, stable_steps=stable, decay_steps=decay, grad_clip=grad_clip,
        batch_size=batch_size, grad_accum_steps=grad_accum_steps,
        log_interval=log_interval if log_interval is not None else (1 if steps <= 10 else 10),
        eval_interval=eval_interval if eval_interval is not None else (saved_training.get('evaluation') or {}).get('interval', 50),
        eval_iters=eval_iters,
        dtype=dtype,
        init_from="resume" if resume else "scratch",
        stop_requested=stop_requested, schedule_steps=schedule_steps,
        early_stop_patience=early_stop_patience, early_stop_min_delta=early_stop_min_delta,
        keep_snapshots=keep_snapshots, outcome=outcome,
    )
    if not np.isfinite(result[1]):
        raise RuntimeError("Validation loss is nonfinite")
    if device == "cuda":
        outcome['peak_vram_mib'] = round(torch.cuda.max_memory_allocated() / 2**20, 1)
        print(f"Peak allocated VRAM: {outcome['peak_vram_mib']:.1f} MiB")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    add_model_size_flags(p)
    p.add_argument("--data", type=Path, help="Local text, JSONL, Parquet file or corpus directory")
    p.add_argument("--data-dir", type=Path, help="Already prepared train.bin / val.bin directory")
    p.add_argument("--out-dir", type=Path, help="Default: ~/train-out/local<size>, e.g. ~/train-out/local2m")
    p.add_argument("--steps", type=int, default=1000, help="Total steps, including previous steps when resuming")
    p.add_argument("--schedule-steps", type=int, help="Explicit LR decay horizon; defaults to the saved plan on resume")
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--grad-accum-steps", type=int, default=4)
    p.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    p.add_argument('--dtype', choices=('auto', 'float32', 'bfloat16', 'float16'),
                   help='Compute precision (default auto; resume preserves saved precision)')
    p.add_argument('--eval-iters', type=int, help='Validation batches of eight sequences (default 20; resume reuses saved value)')
    p.add_argument('--eval-interval', type=int, help='Validate every N steps (default 50; resume reuses saved value)')
    p.add_argument('--log-interval', type=int, help='Write progress every N steps (default 1 for <=10 steps, otherwise 10)')
    p.add_argument('--early-stop-patience', type=int, default=0, help='Stop after N validations without meaningful improvement (0 disables)')
    p.add_argument('--early-stop-min-delta', type=float, default=0.0, help='Minimum absolute validation-loss improvement (default 0)')
    p.add_argument("--resume", action="store_true")
    p.add_argument("--smoke-test", action="store_true")
    args = p.parse_args()
    args.out_dir = args.out_dir or Path.home() / 'train-out' / f"local{args.model_size or '1m'}"
    if args.schedule_steps is not None and args.schedule_steps < 1:
        p.error("Schedule steps must be positive")
    if args.steps < 1 or args.batch_size < 1 or args.grad_accum_steps < 1:
        p.error("Steps, batch size and accumulation must be positive")
    if args.smoke_test:
        with tempfile.TemporaryDirectory(prefix="tensegrity-smoke-") as tmp:
            tmp = Path(tmp)
            corpus = tmp / "sample.jsonl"
            corpus.write_text("\n".join(json.dumps({"text": f"Story {i}. " + "The robot learned to balance. " * 40}) for i in range(40)))
            prepare_folder(corpus, tmp / "data", val_fraction=0.1)
            train(tmp / "data", tmp / "run", steps=3, batch_size=2,
                  grad_accum_steps=1, device=args.device, model_size=args.model_size, dtype=args.dtype,
                  eval_iters=args.eval_iters, eval_interval=args.eval_interval, log_interval=args.log_interval,
                  early_stop_patience=args.early_stop_patience, early_stop_min_delta=args.early_stop_min_delta)
            train(tmp / "data", tmp / "run", steps=4, batch_size=2,
                  grad_accum_steps=1, device=args.device, resume=True, model_size=args.model_size, dtype=args.dtype,
                  eval_iters=args.eval_iters, eval_interval=args.eval_interval, log_interval=args.log_interval,
                  early_stop_patience=args.early_stop_patience, early_stop_min_delta=args.early_stop_min_delta)
        return
    if bool(args.data) == bool(args.data_dir):
        p.error("Provide exactly one of --data or --data-dir")
    data_dir = args.data_dir or args.out_dir / 'data'
    if args.data:
        if not args.data.exists():
            p.error(f"Corpus does not exist: {args.data}")
        if not args.resume:
            if data_dir.exists():
                p.error(f"Prepared data exists at {data_dir}; use --data-dir or move it first")
            prepare_folder(args.data, data_dir, val_fraction=0.05)
    train(data_dir, args.out_dir, steps=args.steps, batch_size=args.batch_size,
          grad_accum_steps=args.grad_accum_steps, device=args.device, resume=args.resume,
          schedule_steps=args.schedule_steps, model_size=args.model_size, dtype=args.dtype,
          eval_iters=args.eval_iters, eval_interval=args.eval_interval, log_interval=args.log_interval,
          early_stop_patience=args.early_stop_patience, early_stop_min_delta=args.early_stop_min_delta)


if __name__ == "__main__":
    main()
