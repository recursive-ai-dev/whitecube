"""
tensegrity_nb_lib.py — extension utilities for TensegrityLM notebooks and local pipeline.

Re-exports the modular ``tensegrity`` growth, continual learning, ladder, workflow,
evaluation, and export utilities while preserving Whitecube Trainer's ``run_training``,
``get_batch_mixed``, ``training_precision``, and safetensors helpers.
"""

import dataclasses
import json
import math
import os
import re
import shutil
import time
import numpy as np
import torch
from files import atomic_json

from tensegrity_lm import (
    ByteTokenizer,
    MODEL_PRESETS,
    TensegrityConfig,
    TensegrityLM,
    _get_memmap,
    capture_rng_state,
    evaluate,
    ffn_hidden_dim,
    get_batch,
    get_lr,
    get_lr_wsd,
    get_preset_config,
    load_checkpoint,
    normalize_state_dict,
    pick_dtype,
    restore_rng_state,
    save_checkpoint,
)
from tensegrity.continual import DistillLoss, EWCState
from tensegrity.export import (
    export_safetensors,
    load_safetensors,
    quantize_int8_dynamic,
    write_model_card,
)
from tensegrity.growth import (
    FISHER_KINDS,
    GrowthError,
    GrowthReport,
    check_function_preserved,
    check_growth_trainable,
    compute_fisher_diagonal as _tensegrity_compute_fisher_diagonal,
    derive_target,
    ewc_penalty,
    expand_reference_state,
    grow_model,
    grow_optimizer_state,
    make_ewc_loss,
    retarget,
    verify_growth_mapping,
)
from tensegrity.ladder import RungResult, climb_ladder
from tensegrity.schedule import RunPlan, extend_plan, make_plan
from tensegrity.train import EMA, TrainConfig, Trainer
from tensegrity.workflow import (
    ArmResult,
    ContinualReport,
    GrowthDecision,
    GrowthGates,
    RecoverySpec,
    compare_continual_strategies,
    grow_transaction,
    verify_roundtrips,
)


def base15m_config(**overrides) -> TensegrityConfig:
    return get_preset_config("base15m", **overrides)


def balanced135m_config(**overrides) -> TensegrityConfig:
    return get_preset_config("balanced135m", **overrides)


def highintel400m_config(**overrides) -> TensegrityConfig:
    return get_preset_config("highintel400m", **overrides)


# =============================================================================
# Multi-dataset ("replay") batching
# =============================================================================

def get_batch_mixed(data_dirs, weights, split, batch_size, block_size, device):
    """
    Like get_batch, but each row of the batch is sampled from one of several
    prepared data directories, chosen according to `weights` (normalized).
    Used to mix "old" and "new" corpora during continual training so the model
    keeps seeing examples of what it already learned (replay-based retention).
    """
    w = np.asarray(weights, dtype=np.float64)
    if not data_dirs or w.ndim != 1 or len(w) != len(data_dirs):
        raise ValueError('Provide one sampling weight per nonempty data directory list')
    if not np.isfinite(w).all() or (w < 0).any() or not np.isfinite(w.sum()) or w.sum() <= 0:
        raise ValueError('Sampling weights must be finite, nonnegative, and have a positive sum')
    if split not in ('train', 'val') or any(isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 1
                                            for value in (batch_size, block_size)):
        raise ValueError('Use train/val split and positive integer batch_size and block_size')
    w = w / w.sum()

    arrs = []
    for d in data_dirs:
        path = os.path.join(d, f"{split}.bin")
        arr = _get_memmap(path)
        if len(arr) < block_size + 1:
            raise ValueError(
                f"{path} has only {len(arr)} tokens, need at least block_size+1 ({block_size + 1})."
            )
        arrs.append(arr)

    choices = np.random.choice(len(data_dirs), size=batch_size, p=w)
    starts = np.empty(batch_size, dtype=np.int64)
    for row in range(batch_size):
        arr = arrs[choices[row]]
        max_start = len(arr) - block_size - 1
        starts[row] = np.random.randint(0, max_start + 1)

    offsets = np.arange(block_size)
    x = np.empty((batch_size, block_size), dtype=np.int64)
    y = np.empty((batch_size, block_size), dtype=np.int64)
    for d_idx in range(len(data_dirs)):
        rows = np.nonzero(choices == d_idx)[0]
        if len(rows) == 0:
            continue
        idx = starts[rows][:, None] + offsets[None, :]  # (len(rows), block_size)
        arr = arrs[d_idx]
        x[rows] = arr[idx].astype(np.int64)
        y[rows] = arr[idx + 1].astype(np.int64)

    x_t = torch.from_numpy(x)
    y_t = torch.from_numpy(y)
    if str(device).startswith("cuda"):
        x_t = x_t.pin_memory().to(device, non_blocking=True)
        y_t = y_t.pin_memory().to(device, non_blocking=True)
    else:
        x_t = x_t.to(device)
        y_t = y_t.to(device)
    return x_t, y_t


# =============================================================================
# Elastic Weight Consolidation (EWC) — dual-signature compatibility wrapper
# =============================================================================

def compute_fisher_diagonal(model, cfg_or_data_dir, data_dir=None, device=None, device_type=None,
                            dtype="auto", n_batches=50, batch_size=8, chunk_size=None,
                            fisher_dtype="float32", *, n_sequences=64, seq_len=None,
                            seed=0, kind="empirical"):
    """
    Estimate the diagonal of the Fisher information matrix for every parameter.

    Supports both:
      1. Modular tensegrity v2 signature:
         ``compute_fisher_diagonal(model, data_dir, device=None, n_sequences=64, seq_len=None, dtype="auto", seed=0, kind="empirical")``
      2. Legacy notebook signature:
         ``compute_fisher_diagonal(model, cfg, data_dir, device, device_type, dtype="auto", n_batches=50, batch_size=8, chunk_size=None, fisher_dtype="float32")``
    """
    if not isinstance(cfg_or_data_dir, TensegrityConfig):
        # Modular v2 signature: second positional argument is data_dir.
        resolved_device = data_dir if (data_dir is not None and device is None) else device
        return _tensegrity_compute_fisher_diagonal(
            model, os.fspath(cfg_or_data_dir), device=resolved_device,
            n_sequences=n_sequences, seq_len=seq_len, dtype=dtype, seed=seed, kind=kind,
        )

    cfg = cfg_or_data_dir
    if device is None:
        device = str(next(model.parameters()).device)
    if device_type is None:
        device_type = "cuda" if "cuda" in str(device) else ("mps" if str(device) == "mps" else "cpu")
    assert fisher_dtype in ("float32", "float16"), (
        f"fisher_dtype must be 'float32' or 'float16', got {fisher_dtype!r}"
    )
    amp_dtype = pick_dtype(dtype, device_type)
    use_amp = amp_dtype in (torch.float16, torch.bfloat16) and device_type in ("cuda", "cpu")

    was_training = model.training
    model.eval()

    fisher = {n: torch.zeros_like(p, dtype=torch.float32) for n, p in model.named_parameters()
              if p.requires_grad}
    old_params = {n: p.detach().clone() for n, p in model.named_parameters() if p.requires_grad}

    chunk_size = chunk_size or n_batches
    batches_done = 0
    while batches_done < n_batches:
        this_chunk = min(chunk_size, n_batches - batches_done)
        for _ in range(this_chunk):
            x, y = get_batch(data_dir, "train", batch_size, cfg.block_size, device)
            model.zero_grad(set_to_none=True)
            with torch.amp.autocast(device_type=device_type if device_type != "mps" else "cpu",
                                    dtype=amp_dtype, enabled=use_amp):
                _, losses, _ = model(x, y)
                loss = losses["ce_loss"]
            loss.backward()
            for n, p in model.named_parameters():
                if p.grad is not None:
                    fisher[n] += p.grad.detach().float().pow(2)
            del x, y, losses, loss
        batches_done += this_chunk
        model.zero_grad(set_to_none=True)
        if device_type == "cuda":
            torch.cuda.empty_cache()

    for n in fisher:
        fisher[n] /= float(n_batches)

    model.zero_grad(set_to_none=True)
    if was_training:
        model.train()

    if fisher_dtype == "float16":
        fisher = {n: t.half() for n, t in fisher.items()}

    return fisher, old_params


# =============================================================================
# Training loop (notebook-friendly: single device, no DDP/compile)
# =============================================================================

SNAPSHOT_PATTERN = re.compile(r'^ckpt_step(\d{8,})\.pt$')


def save_snapshot(out_dir, completed_steps, keep):
    """Copy ckpt_latest.pt to a step-numbered snapshot and prune the oldest."""
    source = os.path.join(out_dir, 'ckpt_latest.pt')
    target = os.path.join(out_dir, f'ckpt_step{completed_steps:08d}.pt')
    staging = f'{target}.{os.getpid()}.tmp'
    try:
        shutil.copyfile(source, staging)
        os.replace(staging, target)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)
    snapshots = sorted(name for name in os.listdir(out_dir) if SNAPSHOT_PATTERN.match(name))
    for name in snapshots[:-keep]:
        os.unlink(os.path.join(out_dir, name))
    return target


def training_precision(requested, device_type, saved=None):
    """Resolve AMP precision, preserving a checkpoint's actual compute dtype."""
    choices = ('auto', 'float32', 'bfloat16', 'float16')
    if requested is not None and requested not in choices:
        raise ValueError('dtype must be auto, float32, bfloat16 or float16')
    if saved is not None:
        if not isinstance(saved, dict) or saved.get('requested') not in choices or saved.get('resolved') not in choices[1:]:
            raise ValueError('Invalid saved precision plan')
        if requested is not None and requested not in (saved['requested'], saved['resolved']):
            raise ValueError('Resume dtype must match the saved precision plan; remove --dtype')
        requested, resolved = saved['requested'], saved['resolved']
    else:
        requested = requested or 'auto'
        resolved = requested
    if device_type == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable. Select --device cpu or check the NVIDIA setup.')
    if device_type == 'cpu' and resolved == 'float16':
        raise ValueError('float16 training requires CUDA; use float32 or bfloat16 on CPU')
    if device_type == 'cuda' and resolved == 'bfloat16' and not torch.cuda.is_bf16_supported():
        raise ValueError('bfloat16 is unavailable on this CUDA device; use auto or float16 for a new run')
    if device_type == 'mps' and resolved not in ('auto', 'float32'):
        raise ValueError('MPS training supports auto or float32 precision')
    return requested, pick_dtype(resolved, device_type)


def run_training(model, optimizer, cfg, out_dir, device,
                 data_dirs, data_weights=None,
                 max_steps=1000, warmup_steps=50,
                 learning_rate=3e-4, min_lr=3e-5,
                 schedule="cosine", stable_steps=None, decay_steps=None,
                 grad_clip=1.0, batch_size=12, grad_accum_steps=1,
                 log_interval=10, eval_interval=None, eval_iters=None,
                 dtype=None, start_step=0, best_val_loss=float("inf"),
                 ewc_fisher=None, ewc_old_params=None, ewc_lambda=0.0,
                 ewc_state=None, distill_loss=None,
                 init_from="scratch", stop_requested=None, schedule_steps=None,
                 early_stop_patience=0, early_stop_min_delta=0.0, keep_snapshots=0, outcome=None):
    """
    Train `model` for steps [start_step, max_steps). Checkpoints
    (ckpt_latest.pt / ckpt_best.pt) and a train_log.jsonl are written to
    out_dir. Validation is always measured against data_dirs[0].
    """
    if init_from not in ('scratch', 'resume'):
        raise ValueError("init_from must be 'scratch' or 'resume'")
    if schedule not in ('cosine', 'wsd'):
        raise ValueError("schedule must be 'cosine' or 'wsd'")
    if isinstance(early_stop_min_delta, bool) or not isinstance(early_stop_min_delta, (int, float)) or not math.isfinite(early_stop_min_delta) or early_stop_min_delta < 0:
        raise ValueError('early_stop_min_delta must be finite and nonnegative')
    device = str(device)
    device_type = 'cuda' if 'cuda' in device else ('mps' if device == 'mps' else 'cpu')
    latest_path = os.path.join(out_dir, 'ckpt_latest.pt')
    ckpt_resume = load_checkpoint(latest_path, device) if init_from == 'resume' and os.path.exists(latest_path) else None
    saved_training = (ckpt_resume.get('training_state') or {}) if ckpt_resume else {}
    saved_evaluation = saved_training.get('evaluation') or {}
    settings = {'eval_interval': (eval_interval, 'interval', 100), 'eval_iters': (eval_iters, 'iters', 20)}
    resolved_evaluation = {}
    for name, (requested, key, default) in settings.items():
        if requested is not None and (isinstance(requested, bool) or not isinstance(requested, (int, np.integer)) or requested < 1):
            raise ValueError(f'{name} must be a positive integer')
        saved_value = saved_evaluation.get(key)
        if saved_value is not None and requested is not None and requested != saved_value:
            raise ValueError(f'Resume {name} must match the saved evaluation plan; remove --{name.replace("_", "-")}')
        resolved_evaluation[name] = saved_value if saved_value is not None else (default if requested is None else requested)
    eval_interval, eval_iters = resolved_evaluation['eval_interval'], resolved_evaluation['eval_iters']
    dtype, amp_dtype = training_precision(dtype, device_type, saved_training.get('precision'))
    for name, value in {'early_stop_patience': early_stop_patience, 'keep_snapshots': keep_snapshots}.items():
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 0:
            raise ValueError(f'{name} must be a nonnegative integer')
    if outcome is not None and not isinstance(outcome, dict):
        raise ValueError('outcome must be a dict or None')
    outcome = {} if outcome is None else outcome
    integer_settings = {'max_steps': max_steps, 'warmup_steps': warmup_steps,
                        'batch_size': batch_size, 'grad_accum_steps': grad_accum_steps,
                        'log_interval': log_interval, 'eval_interval': eval_interval, 'eval_iters': eval_iters}
    if schedule_steps is not None:
        integer_settings['schedule_steps'] = schedule_steps
    for name, value in integer_settings.items():
        minimum = 0 if name == 'warmup_steps' else 1
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < minimum:
            raise ValueError(f'{name} must be an integer >= {minimum}')
    if isinstance(start_step, bool) or not isinstance(start_step, (int, np.integer)) or start_step < 0:
        raise ValueError('start_step must be a nonnegative integer')
    for name, value in {'learning_rate': learning_rate, 'min_lr': min_lr,
                        'grad_clip': grad_clip, 'ewc_lambda': ewc_lambda}.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f'{name} must be finite and nonnegative')
    if learning_rate <= 0 or min_lr > learning_rate:
        raise ValueError('learning_rate must be positive and min_lr must not exceed it')
    if ewc_state is not None:
        ewc_fisher = ewc_state.fisher
        ewc_old_params = ewc_state.params
    if ewc_lambda > 0 and (ewc_fisher is None or ewc_old_params is None):
        raise ValueError('Positive ewc_lambda requires fisher and old parameter anchors')
    if schedule == "wsd":
        if warmup_steps < 1 or not isinstance(stable_steps, int) or stable_steps < 0 or not isinstance(decay_steps, int) or decay_steps < 1:
            raise ValueError("schedule='wsd' requires positive warmup/decay and nonnegative stable_steps")
    if isinstance(data_dirs, (str, os.PathLike)):
        data_dirs = [os.fspath(data_dirs)]
    else:
        data_dirs = [os.fspath(path) for path in data_dirs]
    if not data_dirs:
        raise ValueError('At least one prepared data directory is required')
    if data_weights is None:
        data_weights = [1.0] * len(data_dirs)
    weights = np.asarray(data_weights, dtype=np.float64)
    if weights.ndim != 1 or len(weights) != len(data_dirs) or not np.isfinite(weights).all() or (weights < 0).any() or not np.isfinite(weights.sum()) or weights.sum() <= 0:
        raise ValueError('Provide finite nonnegative data_weights with a positive sum, one per data directory')
    os.makedirs(out_dir, exist_ok=True)
    use_amp = amp_dtype in (torch.float16, torch.bfloat16) and device_type in ("cuda", "cpu")
    scaler = torch.amp.GradScaler(enabled=(amp_dtype == torch.float16 and device_type == "cuda"))

    base_lrs = [g["lr"] for g in optimizer.param_groups]
    requested_schedule_steps = schedule_steps
    schedule_steps = max_steps if schedule_steps is None else schedule_steps
    if schedule_steps < 1:
        raise ValueError("schedule_steps must be positive")
    if init_from == "resume":
        if ckpt_resume is not None:
            cur_cfg_dict = dataclasses.asdict(cfg)
            ckpt_cfg_dict = dataclasses.asdict(TensegrityConfig.from_dict(ckpt_resume["config"], warn_unknown=False))
            assert cur_cfg_dict == ckpt_cfg_dict, (
                f"init_from='resume' found a checkpoint at {latest_path} whose saved "
                f"config does not match the `cfg` passed to run_training. Resuming "
                f"would load mismatched weights into a differently-shaped/configured "
                f"model. Reconstruct `model` with TensegrityConfig(**checkpoint['config']) "
                f"before calling run_training, or point `out_dir` at a fresh directory "
                f"for a from-scratch run."
            )

            model.load_state_dict(normalize_state_dict(ckpt_resume["model_state_dict"]))
            if ckpt_resume.get("optimizer_state_dict") is not None:
                optimizer.load_state_dict(ckpt_resume["optimizer_state_dict"])
            restore_rng_state(ckpt_resume.get("rng_state"), model=model)
            saved_scaler = ckpt_resume.get('scaler_state_dict')
            if scaler.is_enabled() and saved_scaler:
                scaler.load_state_dict(saved_scaler)

            start_step = ckpt_resume["step"] + 1
            best_val_loss = ckpt_resume["best_val_loss"]
            if saved_training:
                batching = {'batch_size': batch_size, 'grad_accum_steps': grad_accum_steps}
                if saved_training.get('batching', batching) != batching:
                    raise ValueError('Resume batch_size and grad_accum_steps must match the saved training plan')
                plan = saved_training["schedule"]
                schedule = plan["kind"]
                warmup_steps = plan["warmup_steps"]
                stable_steps, decay_steps = plan["stable_steps"], plan["decay_steps"]
                learning_rate, min_lr = plan["learning_rate"], plan["min_lr"]
                base_lrs = plan["base_lrs"]
                schedule_steps = plan["schedule_steps"] if requested_schedule_steps is None else requested_schedule_steps
                if saved_evaluation.get('version') != 1 or saved_evaluation.get('seed') != 2026:
                    best_val_loss = float("inf")
            else:
                schedule_steps = start_step if requested_schedule_steps is None else requested_schedule_steps
                warmup_steps = max(1, min(50, start_step // 10))
                best_val_loss = float("inf")
                print("[resume] legacy checkpoint has no schedule plan; using completed steps as "
                      "the horizon. Set --schedule-steps to explicitly choose a new horizon. "
                      "Validation best is reset for repeatable evaluation.")
            if requested_schedule_steps is not None:
                print(f"[resume] explicit schedule horizon: {schedule_steps}")
            print(f"[resume] loaded checkpoint from {latest_path} at step={start_step}, "
                  f"best_val_loss={best_val_loss:.4f}")
        else:
            print(f"[resume] no checkpoint found at {latest_path}; starting from scratch "
                  f"(start_step={start_step}, best_val_loss={best_val_loss})")

    if max_steps < start_step:
        raise ValueError(f'max_steps ({max_steps}) cannot be below completed steps ({start_step})')
    if len(base_lrs) != len(optimizer.param_groups) or any(not math.isfinite(lr) or lr < 0 for lr in base_lrs):
        raise ValueError('Saved learning rates must match optimizer groups and be finite and nonnegative')

    training_state = {
        "schedule": {"kind": schedule, "warmup_steps": warmup_steps,
                     "schedule_steps": schedule_steps, "stable_steps": stable_steps,
                     "decay_steps": decay_steps, "learning_rate": learning_rate,
                     "min_lr": min_lr, "base_lrs": base_lrs},
        "evaluation": {"version": 1, "seed": 2026, "iters": eval_iters, 'interval': eval_interval, 'batch_size': 8},
        'precision': {'requested': dtype, 'resolved': str(amp_dtype).removeprefix('torch.')},
        'batching': {'batch_size': batch_size, 'grad_accum_steps': grad_accum_steps},
    }
    saved_stopping = saved_training.get('early_stopping') or {}
    evaluations, stale_evaluations = 0, 0
    early_stop_reference = best_val_loss
    if saved_stopping and saved_stopping.get('min_delta') == early_stop_min_delta and math.isfinite(best_val_loss):
        evaluations = saved_stopping.get('evaluations', 0)
        stale_evaluations = saved_stopping.get('evaluations_without_improvement', 0)
        early_stop_reference = saved_stopping.get('reference_loss', best_val_loss)
        for value in (evaluations, stale_evaluations):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError('Invalid saved early stopping counters')
        if not isinstance(early_stop_reference, (int, float)) or not math.isfinite(early_stop_reference):
            raise ValueError('Invalid saved early stopping reference loss')
    stopping_state = {'min_delta': early_stop_min_delta, 'reference_loss': early_stop_reference,
                      'evaluations': evaluations, 'evaluations_without_improvement': stale_evaluations}
    training_state['early_stopping'] = stopping_state
    precision_details = {'dtype': training_state['precision']['resolved'], 'eval_iters': eval_iters,
                         'validation_tokens_per_evaluation': eval_iters * 8 * cfg.block_size}

    def record_evaluation(loss):
        nonlocal evaluations, stale_evaluations, early_stop_reference
        evaluations += 1
        if loss < early_stop_reference - early_stop_min_delta:
            early_stop_reference, stale_evaluations = loss, 0
        else:
            stale_evaluations += 1
        stopping_state.update(reference_loss=early_stop_reference, evaluations=evaluations,
                              evaluations_without_improvement=stale_evaluations)
        outcome.update(evaluations=evaluations, evaluations_without_improvement=stale_evaluations)

    print(f"Precision: {precision_details['dtype']}; validation: {eval_iters} batches / "
          f"{precision_details['validation_tokens_per_evaluation']:,} tokens every {eval_interval} steps", flush=True)
    log_path = os.path.join(out_dir, "train_log.jsonl")
    log_file = open(log_path, "a")

    floor_ratio = (min_lr / learning_rate) if learning_rate != 0 else 0.0

    try:
        model.train()
        optimizer.zero_grad(set_to_none=True)
        step = start_step
        t0 = time.monotonic()
        last_losses = None
        last_progress = {}
        last_evaluated_step = None
        overflow_retries = 0
        tokens_per_step = batch_size * grad_accum_steps * cfg.block_size
        outcome.update(stop_reason='completed', evaluations=evaluations,
                       evaluations_without_improvement=stale_evaluations, **precision_details)

        while step < max_steps:
            if schedule == "wsd":
                lr_mult = get_lr_wsd(step, warmup_steps, stable_steps, decay_steps,
                                     max_lr=1.0, min_lr=floor_ratio)
            else:
                lr_mult = get_lr(step, warmup_steps, schedule_steps, max_lr=1.0, min_lr=floor_ratio)
            for g, base in zip(optimizer.param_groups, base_lrs):
                g["lr"] = base * lr_mult

            accum_total = 0.0
            for _ in range(grad_accum_steps):
                if len(data_dirs) == 1:
                    x, y = get_batch(data_dirs[0], "train", batch_size, cfg.block_size, device)
                else:
                    x, y = get_batch_mixed(data_dirs, data_weights, "train", batch_size, cfg.block_size, device)

                with torch.amp.autocast(device_type=device_type if device_type != "mps" else "cpu",
                                        dtype=amp_dtype, enabled=use_amp):
                    logits, losses, _ = model(x, y)
                    total = losses["total_loss"]
                    if ewc_lambda > 0.0 and ewc_fisher is not None:
                        total = total + ewc_lambda * ewc_penalty(model, ewc_fisher, ewc_old_params)
                    if distill_loss is not None:
                        total = total + distill_loss(logits, x, y)
                    scaled = total / grad_accum_steps

                if not torch.isfinite(total).item():
                    raise RuntimeError(f"Nonfinite training loss at step {step}")

                if scaler.is_enabled():
                    scaler.scale(scaled).backward()
                else:
                    scaled.backward()

                accum_total += total.item() / grad_accum_steps
                last_losses = losses

            if scaler.is_enabled():
                scaler.unscale_(optimizer)
            gradient_norm = torch.nn.utils.clip_grad_norm_(
                model.parameters(), grad_clip if grad_clip > 0 else float('inf'),
                error_if_nonfinite=not scaler.is_enabled())
            if scaler.is_enabled() and not torch.isfinite(gradient_norm).item():
                scaler.update(new_scale=scaler.get_scale() * scaler.get_backoff_factor())
                optimizer.zero_grad(set_to_none=True)
                overflow_retries += 1
                if overflow_retries >= 16:
                    raise RuntimeError(f'Persistent nonfinite gradients at step {step}; checkpoint was not replaced')
                print(f'[train] fp16 overflow at step {step}; retrying with scale {scaler.get_scale():g}', flush=True)
                if stop_requested is not None and stop_requested(step):
                    break
                continue
            overflow_retries = 0

            if scaler.is_enabled():
                scaler.step(optimizer)
                scaler.update()
            else:
                optimizer.step()
            optimizer.zero_grad(set_to_none=True)

            if step % log_interval == 0:
                dt = time.monotonic() - t0
                ce = last_losses["ce_loss"].item()
                homeo = last_losses["homeo_loss"].item()
                conf = last_losses.get("confidence_loss")
                conf_str = f" conf={conf.item():.4f}" if conf is not None else ""
                lr0 = optimizer.param_groups[0]["lr"]
                print(f"step {step:6d} | lr {lr0:.2e} (mult {lr_mult:.3f}) | total {accum_total:.4f} "
                      f"(ce {ce:.4f} homeo {homeo:.4f}{conf_str}) | {dt:.1f}s")
                last_progress = {
                    **precision_details,
                    'completed_steps': step + 1, 'planned_steps': max_steps,
                    'loss': accum_total, 'learning_rate': lr0, 'elapsed_seconds': dt,
                    'tokens_processed': (step + 1) * tokens_per_step,
                    'tokens_per_second': (step + 1 - start_step) * tokens_per_step / max(dt, 1e-9),
                    'gradient_norm': float(gradient_norm),
                    'eta_seconds': max_steps and max(0, max_steps - step - 1) * dt / (step + 1 - start_step),
                }
                log_file.write(json.dumps({
                    "step": step, "lr": lr0, "lr_mult": lr_mult, "total_loss": accum_total,
                    "ce_loss": ce, "homeo_loss": homeo, "time": dt,
                    'gradient_norm': last_progress['gradient_norm'],
                    'tokens_per_second': last_progress['tokens_per_second'],
                }) + "\n")
                log_file.flush()

                atomic_json(os.path.join(out_dir, 'progress.json'), last_progress)

            if (step + 1) % eval_interval == 0:
                val_loss = evaluate(model, data_dirs[0], cfg, device, device_type, amp_dtype, use_amp, eval_iters)
                if not math.isfinite(val_loss):
                    raise RuntimeError('Nonfinite validation loss; checkpoint was not replaced')
                last_evaluated_step = step + 1
                print(f"step {step:6d} | val_loss[{os.path.basename(data_dirs[0])}] {val_loss:.4f}")
                log_file.write(json.dumps({"step": step, "val_loss": val_loss, "val_dir": data_dirs[0]}) + "\n")
                log_file.flush()

                rng_state = capture_rng_state()
                improved = val_loss < best_val_loss
                best_val_loss = min(best_val_loss, val_loss)
                record_evaluation(val_loss)
                save_checkpoint(os.path.join(out_dir, "ckpt_latest.pt"), model, optimizer, cfg, step, best_val_loss, rng_state, scaler=scaler, training_state=training_state)
                if improved:
                    save_checkpoint(os.path.join(out_dir, "ckpt_best.pt"), model, optimizer, cfg, step, best_val_loss, rng_state, scaler=scaler, training_state=training_state)
                    print(f"step {step:6d} | new best val_loss {best_val_loss:.4f} -> ckpt_best.pt")
                if keep_snapshots:
                    save_snapshot(out_dir, step + 1, keep_snapshots)
                if early_stop_patience and stale_evaluations >= early_stop_patience and step + 1 < max_steps:
                    step += 1
                    outcome['stop_reason'] = 'early_stop'
                    print(f"[train] early stop: validation loss did not improve in {stale_evaluations} "
                          f"evaluations; best {best_val_loss:.4f}")
                    break

            step += 1
            if stop_requested is not None and stop_requested(step):
                outcome['stop_reason'] = 'stop_requested'
                print(f"[train] stopping after {step} completed steps; saving checkpoint")
                break

        if outcome['stop_reason'] == 'stop_requested' and step >= max_steps:
            outcome['stop_reason'] = 'completed'
        outcome['final_loss'] = accum_total if step > start_step else last_progress.get('loss')
        if last_evaluated_step != step:
            val_loss = evaluate(model, data_dirs[0], cfg, device, device_type, amp_dtype, use_amp, eval_iters)
        if not math.isfinite(val_loss):
            raise RuntimeError('Nonfinite validation loss; checkpoint was not replaced')
        if last_evaluated_step != step and (step > start_step or evaluations == 0):
            record_evaluation(val_loss)
        rng_state = capture_rng_state()
        improved = val_loss < best_val_loss
        best_val_loss = min(best_val_loss, val_loss)
        save_checkpoint(os.path.join(out_dir, "ckpt_latest.pt"), model, optimizer, cfg, step - 1, best_val_loss, rng_state, scaler=scaler, training_state=training_state)
        if improved:
            save_checkpoint(os.path.join(out_dir, "ckpt_best.pt"), model, optimizer, cfg, step - 1, best_val_loss, rng_state, scaler=scaler, training_state=training_state)
        if keep_snapshots and last_evaluated_step != step:
            save_snapshot(out_dir, step, keep_snapshots)
        outcome['final_val_loss'] = val_loss
        print(f"[train] done. step={step} final_val_loss[{os.path.basename(data_dirs[0])}]={val_loss:.4f} "
              f"best_val_loss={best_val_loss:.4f}")
        elapsed = time.monotonic() - t0
        log_file.write(json.dumps({'step': step - 1, 'completed_steps': step, 'val_loss': val_loss,
                                   'best_val_loss': best_val_loss, 'event': 'finished'}) + '\n')
        log_file.flush()
        atomic_json(os.path.join(out_dir, 'progress.json'), {
            **last_progress,
            **precision_details,
            'completed_steps': step, 'planned_steps': max_steps, 'best_val_loss': best_val_loss,
            'elapsed_seconds': elapsed, 'tokens_processed': step * tokens_per_step,
            'tokens_per_second': (step - start_step) * tokens_per_step / max(elapsed, 1e-9),
            'loss': accum_total if step > start_step else last_progress.get('loss'),
            'learning_rate': optimizer.param_groups[0]['lr'],
            'eta_seconds': 0 if step >= max_steps else last_progress.get('eta_seconds'),
        })
        return step, best_val_loss
    finally:
        log_file.close()


# =============================================================================
# safetensors export / import
# =============================================================================

def export_to_safetensors(model, cfg, out_dir, name="model"):
    """
    Write {out_dir}/{name}.safetensors (weights only) and
    {out_dir}/{name}.config.json (TensegrityConfig, needed to reconstruct
    the model before loading the weights back in).
    """
    from safetensors.torch import save_file

    os.makedirs(out_dir, exist_ok=True)
    state_dict = model.state_dict()

    tensors = {}
    for k, v in state_dict.items():
        t = v.detach().cpu().contiguous()
        tensors[k] = t

    if "lm_head.weight" in tensors and "token_emb.weight" in tensors:
        if tensors["lm_head.weight"].data_ptr() == tensors["token_emb.weight"].data_ptr():
            tensors["lm_head.weight"] = tensors["lm_head.weight"].clone()

    weights_path = os.path.join(out_dir, f"{name}.safetensors")
    save_file(tensors, weights_path, metadata={"format": "pt"})

    config_path = os.path.join(out_dir, f"{name}.config.json")
    with open(config_path, "w") as f:
        json.dump(dataclasses.asdict(cfg), f, indent=2)

    print(f"[export] wrote {weights_path}")
    print(f"[export] wrote {config_path}")
    return weights_path, config_path


def load_from_safetensors(weights_path, config_path, device="cpu"):
    """
    Reconstruct a TensegrityLM from a .safetensors weights file and its
    companion config.json (as written by export_to_safetensors).
    """
    from safetensors.torch import load_file

    with open(config_path) as f:
        cfg = TensegrityConfig.from_dict(json.load(f), warn_unknown=False)

    model = TensegrityLM(cfg)
    state_dict = load_file(weights_path)
    model.load_state_dict(normalize_state_dict(state_dict), strict=False)
    model.lm_head.weight = model.token_emb.weight
    model.to(device)
    return model, cfg


# =============================================================================
# Confidence-head calibration
# =============================================================================

@torch.no_grad()
def evaluate_confidence_calibration(model, cfg, data_dir, device, device_type="cpu", dtype="auto",
                                    n_batches=20, batch_size=8, n_bins=10):
    assert model.confidence_head is not None, (
        "model has no confidence head (cfg.use_confidence_head=False); nothing to calibrate."
    )
    amp_dtype = pick_dtype(dtype, device_type)
    use_amp = amp_dtype in (torch.float16, torch.bfloat16) and device_type in ("cuda", "cpu")

    was_training = model.training
    model.eval()

    bin_edges = torch.linspace(0.0, 1.0, n_bins + 1)
    bin_correct = torch.zeros(n_bins)
    bin_conf_sum = torch.zeros(n_bins)
    bin_count = torch.zeros(n_bins)

    try:
        for _ in range(n_batches):
            x, y = get_batch(data_dir, "val", batch_size, cfg.block_size, device)
            with torch.amp.autocast(device_type=device_type if device_type != "mps" else "cpu",
                                    dtype=amp_dtype, enabled=use_amp):
                logits, _, confidence = model(x, targets=None)
            preds = logits.argmax(dim=-1)
            correct = (preds == y).float().reshape(-1).cpu()
            conf = confidence.float().reshape(-1).clamp(0.0, 1.0).cpu()

            bin_idx = torch.bucketize(conf, bin_edges[1:-1], right=False)
            for b in range(n_bins):
                mask = bin_idx == b
                n = mask.sum()
                if n == 0:
                    continue
                bin_count[b] += n
                bin_correct[b] += correct[mask].sum()
                bin_conf_sum[b] += conf[mask].sum()
    finally:
        if was_training:
            model.train()

    total = bin_count.sum().item()
    rows = []
    ece = 0.0
    for b in range(n_bins):
        n = bin_count[b].item()
        if n == 0:
            acc, mean_conf = float("nan"), float("nan")
        else:
            acc = (bin_correct[b] / bin_count[b]).item()
            mean_conf = (bin_conf_sum[b] / bin_count[b]).item()
            ece += (n / total) * abs(acc - mean_conf)
        rows.append({
            "bin_lo": bin_edges[b].item(), "bin_hi": bin_edges[b + 1].item(),
            "count": int(n), "accuracy": acc, "mean_confidence": mean_conf,
        })

    print(f"{'bin':>14s} {'count':>8s} {'accuracy':>10s} {'mean_conf':>10s}")
    for row in rows:
        acc_str = f"{row['accuracy']:.4f}" if row["count"] > 0 else "n/a"
        conf_str = f"{row['mean_confidence']:.4f}" if row["count"] > 0 else "n/a"
        print(f"[{row['bin_lo']:.2f},{row['bin_hi']:.2f}) {row['count']:8d} "
              f"{acc_str:>10s} {conf_str:>10s}")
    print(f"Expected Calibration Error (ECE): {ece:.4f}  (n_tokens={int(total)})")

    return {"bins": rows, "ece": ece, "n_tokens": int(total)}
