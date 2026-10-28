"""
TensegrityLM — a byte-level decoder-only language model.

Facade module re-exporting the modular ``tensegrity`` package while preserving
Whitecube Trainer's data, checkpointing, evaluation, and CLI contracts.
"""

import argparse
import dataclasses
import json
import math
import os
import random
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F

from tensegrity.checkpoint import load_model, normalize_state_dict, unwrap_model
from tensegrity.config import TensegrityConfig, ffn_hidden_dim
from tensegrity.layers import (
    Block,
    ByteMix,
    CausalSelfAttention,
    RMSNorm,
    SwiGLU,
    activation_rms,
    apply_logit_soft_cap,
    apply_rope,
    local_causal_attention,
    native_gqa_ok,
    precompute_rope,
)
from tensegrity.model import TensegrityLM
from tensegrity.optim import (
    SingleDeviceMuonWithAuxAdam,
    adam_update,
    configure_optimizer,
    muon_update,
    zeropower_via_newtonschulz5,
)
from tensegrity.schedule import get_lr, get_lr_wsd
from tensegrity.presets import (
    BALANCED135M_CONFIG_KWARGS,
    BASE15M_CONFIG_KWARGS,
    HIGHINTEL400M_CONFIG_KWARGS,
    LADDER,
    LADDER_NAMES,
    MODEL_PRESETS,
    describe_presets,
    get_preset,
    get_preset_config,
    next_preset,
)
from tensegrity.tokenizer import ByteTokenizer


def build_attention_masks(block_size: int, sliding_window: int, device=None) -> Dict[str, torch.Tensor]:
    """
    Build boolean attention masks of shape (block_size, block_size).
    True = positions that may attend to each other (query i attends to key j).
    """
    idx = torch.arange(block_size, device=device)
    i = idx.view(-1, 1)
    j = idx.view(1, -1)
    causal = j <= i
    local = causal & (j > (i - sliding_window))
    return {"global": causal, "local": local}


# =============================================================================
# Data pipeline
# =============================================================================

def read_file_to_bytes(filepath: str) -> bytes:
    """
    Reads a file and converts its textual contents to bytes.
    Supports .txt, .md, .parquet, and .jsonl formats.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".parquet":
        from parquet_io import pq, pick_column
        from prepare_parquet import parquet_values
        with pq.ParquetFile(filepath) as source:
            column = pick_column(source.schema_arrow)
            return '\n\n'.join(text for text in parquet_values(source, column) if text is not None).encode('utf-8')
    elif ext == ".jsonl":
        lines = []
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict):
                            val = data.get("text") or data.get("content") or data.get("body")
                            if val is None:
                                val = "\n\n".join(str(v) for v in data.values() if isinstance(v, (str, int, float)))
                            if val:
                                lines.append(str(val))
                        elif isinstance(data, str):
                            lines.append(data)
                    except Exception:
                        pass
            content = "\n\n".join(lines)
            return content.encode("utf-8", errors="replace")
        except Exception as e:
            print(f"[warning] Failed to parse jsonl {filepath}: {e}, falling back to raw read")
            with open(filepath, "rb") as fh:
                return fh.read()
    else:
        with open(filepath, "rb") as fh:
            return fh.read()


def prepare_data(data_path: str, out_dir: str, val_fraction: float = 0.1, seed: int = 1337,
                 shuffle_files: bool = False):
    """Prepare whole-document splits; matching document hashes stay together.

    Text/Markdown files are documents; JSONL and Parquet rows are documents.
    A single text document cannot supply both train and validation splits.
    """
    from prepare_folder import prepare_folder
    report = prepare_folder(data_path, out_dir, max_tokens=sys.maxsize,
                            val_fraction=val_fraction, seed=seed, shuffle_files=shuffle_files)
    meta = {"vocab_size": ByteTokenizer.VOCAB_SIZE,
            "n_train_tokens": report["train_tokens"], "n_val_tokens": report["val_tokens"],
            "source_files": report["available_files"], "val_fraction": val_fraction,
            "shuffle_files": shuffle_files, "seed": seed,
            "split_method": report["split_method"]}
    with open(os.path.join(out_dir, "meta.json"), "w") as file:
        json.dump(meta, file, indent=2)


_MEMMAP_CACHE = OrderedDict()
_MEMMAP_SIGNATURES = {}
_MEMMAP_CACHE_LIMIT = 16


def _get_memmap(path: str) -> np.memmap:
    """Cached np.memmap open. Data files are read-only and don't change during
    training, so reusing the same memmap across calls is safe and avoids a fresh
    open()+mmap() syscall on every single get_batch/get_batch_mixed call."""
    path = os.path.realpath(os.fspath(path))
    stat = os.stat(path)
    if stat.st_size == 0 or stat.st_size % np.dtype(np.uint16).itemsize:
        raise ValueError(f'{path} must be a nonempty uint16 token file (even byte length)')
    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    arr = _MEMMAP_CACHE.get(path) if _MEMMAP_SIGNATURES.get(path) == signature else None
    if arr is None:
        arr = np.memmap(path, dtype=np.uint16, mode="r")
        _MEMMAP_CACHE[path] = arr
        _MEMMAP_SIGNATURES[path] = signature
    _MEMMAP_CACHE.move_to_end(path)
    while len(_MEMMAP_CACHE) > _MEMMAP_CACHE_LIMIT:
        stale, _ = _MEMMAP_CACHE.popitem(last=False)
        _MEMMAP_SIGNATURES.pop(stale, None)
    return arr


def get_batch(data_dir: str, split: str, batch_size: int, block_size: int,
              device: str, rng=None) -> Tuple[torch.Tensor, torch.Tensor]:
    if split not in ('train', 'val'):
        raise ValueError("split must be 'train' or 'val'")
    if any(isinstance(v, bool) or not isinstance(v, (int, np.integer)) or v < 1
           for v in (batch_size, block_size)):
        raise ValueError('batch_size and block_size must be positive integers')
    path = os.path.join(data_dir, f"{split}.bin")
    arr = _get_memmap(path)
    n = len(arr)
    if n < block_size + 1:
        raise ValueError(
            f"{split}.bin has only {n} tokens, need at least block_size+1 ({block_size + 1}). "
            f"Use a larger corpus or smaller --block_size."
        )
    max_start = n - block_size - 1
    starts = (np.random if rng is None else rng).randint(0, max_start + 1, size=(batch_size,))
    offsets = np.arange(block_size)
    idx_x = starts[:, None] + offsets[None, :]  # (batch_size, block_size)
    x = arr[idx_x].astype(np.int64)
    y = arr[idx_x + 1].astype(np.int64)
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
# Checkpointing
# =============================================================================

CHECKPOINT_FORMAT_VERSION = 1


def save_checkpoint(path: str, model: nn.Module, optimizer: torch.optim.Optimizer,
                    cfg: TensegrityConfig, step: int, best_val_loss: float,
                    rng_state: Dict[str, Any], scaler: Optional[torch.amp.GradScaler] = None,
                    training_state: Optional[Dict[str, Any]] = None):
    raw_model = unwrap_model(model)

    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    ckpt = {
        "format_version": CHECKPOINT_FORMAT_VERSION,
        "model_state_dict": raw_model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict() if optimizer is not None else None,
        "scaler_state_dict": scaler.state_dict() if scaler is not None else None,
        "config": asdict(cfg),
        "step": step,
        "best_val_loss": best_val_loss,
        "rng_state": rng_state,
        "training_state": training_state,
    }
    fd, tmp_path = tempfile.mkstemp(prefix=f'.{os.path.basename(path)}-', suffix='.tmp', dir=out_dir or '.')
    try:
        with os.fdopen(fd, 'wb') as handle:
            torch.save(ckpt, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        directory_fd = os.open(out_dir or '.', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def load_checkpoint(path: str, device: str) -> Dict[str, Any]:
    ckpt = torch.load(path, map_location=device, weights_only=False)
    if not isinstance(ckpt, dict):
        raise ValueError(f'{path} is not a checkpoint mapping')
    required = ("model_state_dict", "optimizer_state_dict", "config", "step", "best_val_loss")
    missing = [k for k in required if k not in ckpt]
    if missing:
        raise ValueError(
            f"{path} is missing expected checkpoint key(s) {missing}; "
            f"this may not be a TensegrityLM checkpoint, or it predates the current format."
        )
    for name in ('config', 'model_state_dict'):
        if not isinstance(ckpt[name], dict):
            raise ValueError(f'{path} checkpoint {name} must be a mapping')
    if ckpt['optimizer_state_dict'] is not None and not isinstance(ckpt['optimizer_state_dict'], dict):
        raise ValueError(f'{path} checkpoint optimizer_state_dict must be a mapping')
    if isinstance(ckpt['step'], bool) or not isinstance(ckpt['step'], int) or ckpt['step'] < -1:
        raise ValueError(f'{path} checkpoint step must be an integer >= -1')
    loss = ckpt['best_val_loss']
    if isinstance(loss, bool) or not isinstance(loss, (int, float)) or math.isnan(loss) or loss == -math.inf:
        raise ValueError(f'{path} checkpoint best_val_loss must be a number other than NaN or negative infinity')
    ckpt_version = ckpt.get("format_version")
    if ckpt_version not in (CHECKPOINT_FORMAT_VERSION, 2):
        print(f"[warn] {path} has format_version={ckpt_version!r}, code expects "
              f"{CHECKPOINT_FORMAT_VERSION!r}. Loading will proceed, but the schema may "
              f"have changed since this checkpoint was saved -- if load_state_dict below "
              f"fails or behaves oddly, this mismatch is why.")
    return ckpt


def capture_rng_state(model: Optional[nn.Module] = None, dataset: Any = None) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "torch": torch.get_rng_state(),
        "numpy": np.random.get_state(),
        "python": random.getstate(),
    }
    if torch.cuda.is_initialized():
        state["cuda"] = torch.cuda.get_rng_state_all()
    if model is not None:
        m = unwrap_model(model)
        if hasattr(m, "layerdrop_state"):
            state["layerdrop"] = m.layerdrop_state()
    if dataset is not None and hasattr(dataset, "state_dict"):
        state["dataset"] = dataset.state_dict()
    return state


def restore_rng_state(state: Optional[Dict[str, Any]], model: Optional[nn.Module] = None,
                      dataset: Any = None):
    if state is None:
        return
    if "torch" in state:
        torch_state = state["torch"]
        if isinstance(torch_state, torch.Tensor):
            torch_state = torch_state.cpu()
        torch.set_rng_state(torch_state)
    if "numpy" in state:
        np.random.set_state(state["numpy"])
    if "python" in state:
        random.setstate(state["python"])
    if "cuda" in state and torch.cuda.is_available():
        try:
            cuda_state = state["cuda"]
            if isinstance(cuda_state, (list, tuple)):
                cuda_state = [s.cpu() if isinstance(s, torch.Tensor) else s for s in cuda_state]
            torch.cuda.set_rng_state_all(cuda_state)
        except Exception as e:
            print(f"[warn] failed to restore CUDA RNG state ({e})")
    if model is not None and "layerdrop" in state:
        m = unwrap_model(model)
        if hasattr(m, "set_layerdrop_state"):
            m.set_layerdrop_state(state["layerdrop"])
    if dataset is not None and "dataset" in state and hasattr(dataset, "load_state_dict"):
        dataset.load_state_dict(state["dataset"])


# =============================================================================
# Device / dtype helpers
# =============================================================================

def pick_device(requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def pick_dtype(requested: str, device: str) -> torch.dtype:
    if requested != "auto":
        return {"float32": torch.float32, "bfloat16": torch.bfloat16, "float16": torch.float16}[requested]
    if device == "cuda" and torch.cuda.is_bf16_supported():
        return torch.bfloat16
    if device == "cuda":
        return torch.float16
    return torch.float32


# =============================================================================
# Training & Evaluation
# =============================================================================

def build_config_from_args(args) -> TensegrityConfig:
    cfg_kwargs = {}
    preset_name = getattr(args, "preset", None)
    if preset_name is not None:
        cfg_kwargs.update(MODEL_PRESETS[preset_name])
    for f in dataclasses.fields(TensegrityConfig):
        if hasattr(args, f.name):
            val = getattr(args, f.name)
            if val is not None:
                cfg_kwargs[f.name] = val
    return TensegrityConfig(**cfg_kwargs)


def cmd_train(args):
    is_ddp = int(os.environ.get("RANK", -1)) != -1
    if is_ddp:
        from torch.nn.parallel import DistributedDataParallel as DDP
        dist.init_process_group(backend="nccl")
        ddp_rank = int(os.environ["RANK"])
        ddp_local_rank = int(os.environ["LOCAL_RANK"])
        device = f"cuda:{ddp_local_rank}"
        torch.cuda.set_device(device)
        master_process = (ddp_rank == 0)
        seed_offset = ddp_rank
    else:
        master_process = True
        seed_offset = 0
        device = pick_device(args.device)

    torch.manual_seed(args.seed + seed_offset)
    np.random.seed(args.seed + seed_offset)

    os.makedirs(args.out_dir, exist_ok=True)

    meta_path = os.path.join(args.data_dir, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        if master_process:
            print(f"[data] {meta['n_train_tokens']:,} train tokens, "
                  f"{meta['n_val_tokens']:,} val tokens, vocab={meta['vocab_size']}")

    device_type = "cuda" if "cuda" in device else ("mps" if device == "mps" else "cpu")
    dtype = pick_dtype(args.dtype, device_type)
    if master_process:
        print(f"[train] device={device} dtype={dtype}")

    ckpt_resume = None
    if args.init_from == "resume":
        latest_path = os.path.join(args.out_dir, "ckpt_latest.pt")
        if os.path.exists(latest_path):
            ckpt_resume = load_checkpoint(latest_path, device)
            if master_process:
                print(f"[resume] loaded checkpoint from {latest_path} at step {ckpt_resume['step']}")
        else:
            if master_process:
                print(f"[resume] no checkpoint found at {latest_path}, starting from scratch")

    if ckpt_resume is not None:
        cfg = TensegrityConfig.from_dict(ckpt_resume["config"])
    else:
        cfg = build_config_from_args(args)

    model = TensegrityLM(cfg)
    model.to(device)

    if ckpt_resume is not None:
        model.load_state_dict(normalize_state_dict(ckpt_resume["model_state_dict"]))

    optimizer = model.configure_optimizer(
        weight_decay=args.weight_decay, learning_rate=args.learning_rate,
        betas=(args.beta1, args.beta2), device_type=device_type,
        use_8bit=args.use_8bit_optimizer,
    )

    use_amp = dtype in (torch.float16, torch.bfloat16) and device_type in ("cuda", "cpu")
    amp_dtype = dtype if use_amp else torch.float32
    scaler = torch.amp.GradScaler(enabled=(dtype == torch.float16 and device_type == "cuda"))

    if ckpt_resume is not None:
        if ckpt_resume.get("optimizer_state_dict") is not None:
            optimizer.load_state_dict(ckpt_resume["optimizer_state_dict"])
        restore_rng_state(ckpt_resume.get("rng_state"), model=model)
        if ckpt_resume.get("scaler_state_dict") is not None:
            scaler.load_state_dict(ckpt_resume["scaler_state_dict"])

    start_step = (ckpt_resume["step"] + 1) if ckpt_resume is not None else 0
    best_val_loss = ckpt_resume["best_val_loss"] if ckpt_resume is not None else float("inf")

    raw_model = model
    if args.compile:
        try:
            model = torch.compile(model)
            if master_process:
                print("[train] torch.compile enabled")
        except Exception as e:
            if master_process:
                print(f"[train] torch.compile failed ({e}), continuing without it")

    if is_ddp:
        model = DDP(model, device_ids=[ddp_local_rank], find_unused_parameters=True)
        raw_model = model.module

    log_path = os.path.join(args.out_dir, "train_log.jsonl")
    log_file = open(log_path, "a") if master_process else None

    try:
        t0 = time.time()
        step = start_step
        running_loss = 0.0
        steps_since_log = 0

        model.train()
        optimizer.zero_grad(set_to_none=True)

        while step < args.max_steps:
            lr = get_lr(step, args.warmup_steps, args.max_steps, args.learning_rate, args.min_lr)
            for group in optimizer.param_groups:
                group["lr"] = lr

            for micro_step in range(args.grad_accum_steps):
                x, y = get_batch(args.data_dir, "train", args.batch_size, cfg.block_size, device)

                if is_ddp:
                    model.require_backward_grad_sync = (micro_step == args.grad_accum_steps - 1)

                with torch.amp.autocast(device_type=device_type if device_type != "mps" else "cpu",
                                        dtype=amp_dtype, enabled=use_amp):
                    logits, losses, _ = model(x, y)
                    loss = losses["total_loss"] / args.grad_accum_steps

                if scaler.is_enabled():
                    scaler.scale(loss).backward()
                else:
                    loss.backward()

                running_loss += losses["total_loss"].item() / args.grad_accum_steps

            steps_since_log += 1

            if args.grad_clip > 0.0:
                if scaler.is_enabled():
                    scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)

            if scaler.is_enabled():
                scaler.step(optimizer)
                scaler.update()
            else:
                optimizer.step()
            optimizer.zero_grad(set_to_none=True)

            if step % args.log_interval == 0 and master_process:
                dt = time.time() - t0
                ce = losses["ce_loss"].item()
                homeo = losses["homeo_loss"].item()
                conf = losses.get("confidence_loss")
                conf_str = f" conf={conf.item():.4f}" if conf is not None else ""
                print(f"step {step:6d} | lr {lr:.2e} | total {losses['total_loss'].item():.4f} "
                      f"(ce {ce:.4f} homeo {homeo:.4f}{conf_str}) | {dt:.1f}s")
                log_file.write(json.dumps({
                    "step": step, "lr": lr, "total_loss": losses["total_loss"].item(),
                    "ce_loss": ce, "homeo_loss": homeo,
                    "time": dt,
                }) + "\n")
                log_file.flush()
                running_loss = 0.0
                steps_since_log = 0

            if step > 0 and step % args.eval_interval == 0 and master_process:
                val_loss = evaluate(raw_model, args.data_dir, cfg, device, device_type, amp_dtype,
                                    use_amp, args.eval_iters)
                print(f"step {step:6d} | val_loss {val_loss:.4f}")
                log_file.write(json.dumps({"step": step, "val_loss": val_loss}) + "\n")
                log_file.flush()

                rng_state = capture_rng_state()
                save_checkpoint(os.path.join(args.out_dir, "ckpt_latest.pt"), raw_model, optimizer,
                                cfg, step, best_val_loss, rng_state, scaler=scaler)
                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                    save_checkpoint(os.path.join(args.out_dir, "ckpt_best.pt"), raw_model, optimizer,
                                    cfg, step, best_val_loss, rng_state, scaler=scaler)
                    print(f"step {step:6d} | new best val_loss {best_val_loss:.4f}, checkpoint saved")

            step += 1

        if master_process:
            val_loss = evaluate(raw_model, args.data_dir, cfg, device, device_type, amp_dtype,
                                use_amp, args.eval_iters)
            rng_state = capture_rng_state()
            save_checkpoint(os.path.join(args.out_dir, "ckpt_latest.pt"), raw_model, optimizer,
                            cfg, step, best_val_loss, rng_state, scaler=scaler)
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                save_checkpoint(os.path.join(args.out_dir, "ckpt_best.pt"), raw_model, optimizer,
                                cfg, step, best_val_loss, rng_state, scaler=scaler)
            print(f"[train] done. final val_loss={val_loss:.4f} best_val_loss={best_val_loss:.4f}")
    finally:
        if log_file is not None:
            log_file.close()

    if is_ddp:
        dist.destroy_process_group()


@torch.no_grad()
def evaluate(model: nn.Module, data_dir: str, cfg: TensegrityConfig, device: str,
             device_type: str = "cpu", amp_dtype: torch.dtype = torch.float32,
             use_amp: bool = False, eval_iters: int = 20) -> float:
    if eval_iters < 1:
        raise ValueError("eval_iters must be positive")
    rng = np.random.RandomState(2026)
    was_training = model.training
    model.eval()
    try:
        losses = []
        for split in ["val"]:
            for _ in range(eval_iters):
                x, y = get_batch(data_dir, split, batch_size=8, block_size=cfg.block_size, device=device, rng=rng)
                with torch.amp.autocast(device_type=device_type if device_type != "mps" else "cpu",
                                        dtype=amp_dtype, enabled=use_amp):
                    _, loss_dict, _ = model(x, y)
                value = loss_dict["ce_loss"].item()
                if not math.isfinite(value):
                    raise RuntimeError("Nonfinite validation loss")
                losses.append(value)
        return float(np.mean(losses))
    finally:
        if was_training:
            model.train()


# =============================================================================
# Generation / Eval CLI commands
# =============================================================================

def cmd_generate(args):
    device = pick_device(args.device)
    ckpt = load_checkpoint(args.ckpt, device)
    cfg = TensegrityConfig.from_dict(ckpt["config"])
    model = TensegrityLM(cfg)
    model.load_state_dict(normalize_state_dict(ckpt["model_state_dict"]))
    model.to(device)
    model.eval()

    tok = ByteTokenizer()
    if args.seed is not None:
        torch.manual_seed(args.seed)

    if args.prompt:
        ids = tok.encode(args.prompt)
    else:
        ids = []
    if args.add_bos:
        ids = [ByteTokenizer.BOS] + ids
    if not ids:
        ids = [ByteTokenizer.BOS]

    idx = torch.tensor([ids], dtype=torch.long, device=device)

    out = model.generate(
        idx, max_new_tokens=args.max_new_tokens, temperature=args.temperature,
        top_k=args.top_k, top_p=args.top_p,
        use_confidence_temp=args.use_confidence_temp,
        stop_on_eos=not args.no_stop_on_eos,
        use_cache=not args.no_kv_cache,
    )

    out_ids = out[0].tolist()
    text = tok.decode(out_ids)
    print(text)


def cmd_eval(args):
    device = pick_device(args.device)
    ckpt = load_checkpoint(args.ckpt, device)
    cfg = TensegrityConfig.from_dict(ckpt["config"])
    model = TensegrityLM(cfg)
    model.load_state_dict(normalize_state_dict(ckpt["model_state_dict"]))
    model.to(device)
    model.eval()

    device_type = "cuda" if "cuda" in device else ("mps" if device == "mps" else "cpu")
    dtype = pick_dtype(args.dtype, device_type)
    use_amp = dtype in (torch.float16, torch.bfloat16) and device_type in ("cuda", "cpu")

    val_loss = evaluate(model, args.data_dir, cfg, device, device_type, dtype, use_amp, args.eval_iters)
    print(f"val_loss = {val_loss:.4f}")
    print(f"val_perplexity = {math.exp(val_loss):.4f}")


# =============================================================================
# Argument parsing
# =============================================================================

def add_model_args(p: argparse.ArgumentParser):
    p.add_argument("--preset", type=str, default=None, choices=sorted(MODEL_PRESETS),
                   help="Apply a named model-size preset (e.g. 1m..115m, base15m) "
                        "as config defaults; any other --<field> flag passed explicitly "
                        "still overrides the preset's value for that field.")
    p.add_argument("--block_size", type=int, default=None)
    p.add_argument("--n_layer", type=int, default=None)
    p.add_argument("--n_embd", type=int, default=None)
    p.add_argument("--n_head", type=int, default=None)
    p.add_argument("--n_kv_head", type=int, default=None)
    p.add_argument("--dropout", type=float, default=None)
    p.add_argument("--sliding_window", type=int, default=None)
    p.add_argument("--global_every", type=int, default=None)
    p.add_argument("--rope_base", type=float, default=None)
    p.add_argument("--ffn_mult", type=float, default=None)
    p.add_argument("--ffn_align", type=int, default=None)
    p.add_argument("--homeo_target", type=float, default=None)
    p.add_argument("--homeo_weight", type=float, default=None)
    p.add_argument("--layerdrop_max", type=float, default=None)
    p.add_argument("--use_confidence_head", type=lambda x: x.lower() == "true", default=None)
    p.add_argument("--confidence_weight", type=float, default=None)
    p.add_argument("--bias", type=lambda x: x.lower() == "true", default=None)
    p.add_argument("--grad_checkpoint", type=lambda x: x.lower() == "true", default=None)
    p.add_argument("--grad_checkpoint_every", type=int, default=None)
    p.add_argument("--byte_mix_kernel", type=int, default=None)
    p.add_argument("--byte_mix_bilinear", type=lambda x: x.lower() == "true", default=None)
    p.add_argument("--x0_mix", type=lambda x: x.lower() == "true", default=None)
    p.add_argument("--v0_mix", type=lambda x: x.lower() == "true", default=None)
    p.add_argument("--n_loops", type=int, default=None)


def main():
    parser = argparse.ArgumentParser(description="TensegrityLM — byte-level LM")
    sub = parser.add_subparsers(dest="command", required=True)

    # prepare
    p_prep = sub.add_parser("prepare", help="Tokenize a text file/directory into train/val .bin files")
    p_prep.add_argument("--data_path", type=str, required=True)
    p_prep.add_argument("--out_dir", type=str, required=True)
    p_prep.add_argument("--val_fraction", type=float, default=0.1)
    p_prep.add_argument("--seed", type=int, default=1337)

    # train
    p_train = sub.add_parser("train", help="Train (or resume) a model")
    p_train.add_argument("--data_dir", type=str, required=True)
    p_train.add_argument("--out_dir", type=str, required=True)
    p_train.add_argument("--init_from", type=str, default="scratch", choices=["scratch", "resume"])
    p_train.add_argument("--device", type=str, default="auto")
    p_train.add_argument("--dtype", type=str, default="auto", choices=["auto", "float32", "bfloat16", "float16"])
    p_train.add_argument("--seed", type=int, default=1337)
    p_train.add_argument("--batch_size", type=int, default=12)
    p_train.add_argument("--grad_accum_steps", type=int, default=1)
    p_train.add_argument("--max_steps", type=int, default=5000)
    p_train.add_argument("--warmup_steps", type=int, default=100)
    p_train.add_argument("--learning_rate", type=float, default=3e-4)
    p_train.add_argument("--min_lr", type=float, default=3e-5)
    p_train.add_argument("--weight_decay", type=float, default=0.1)
    p_train.add_argument("--beta1", type=float, default=0.9)
    p_train.add_argument("--beta2", type=float, default=0.95)
    p_train.add_argument("--use_8bit_optimizer", action="store_true",
                         help="Use bitsandbytes' 8-bit AdamW to cut optimizer-state memory "
                              "(~4x smaller moments); silently falls back to regular AdamW "
                              "if bitsandbytes isn't installed or device is not CUDA.")
    p_train.add_argument("--grad_clip", type=float, default=1.0)
    p_train.add_argument("--log_interval", type=int, default=10)
    p_train.add_argument("--eval_interval", type=int, default=250)
    p_train.add_argument("--eval_iters", type=int, default=20)
    p_train.add_argument("--compile", action="store_true")
    add_model_args(p_train)

    # generate
    p_gen = sub.add_parser("generate", help="Generate text from a checkpoint")
    p_gen.add_argument("--ckpt", type=str, required=True)
    p_gen.add_argument("--device", type=str, default="auto")
    p_gen.add_argument("--prompt", type=str, default="")
    p_gen.add_argument("--add_bos", action="store_true")
    p_gen.add_argument("--max_new_tokens", type=int, default=200)
    p_gen.add_argument("--temperature", type=float, default=0.8)
    p_gen.add_argument("--top_k", type=int, default=None)
    p_gen.add_argument("--top_p", type=float, default=None)
    p_gen.add_argument("--use_confidence_temp", action="store_true")
    p_gen.add_argument("--no_stop_on_eos", action="store_true")
    p_gen.add_argument("--no_kv_cache", action="store_true",
                       help="Disable the KV cache and recompute attention over the full "
                            "context every step (slower; only useful for debugging/comparison).")
    p_gen.add_argument("--seed", type=int, default=None)

    # eval
    p_eval = sub.add_parser("eval", help="Evaluate a checkpoint on val data")
    p_eval.add_argument("--ckpt", type=str, required=True)
    p_eval.add_argument("--data_dir", type=str, required=True)
    p_eval.add_argument("--device", type=str, default="auto")
    p_eval.add_argument("--dtype", type=str, default="auto", choices=["auto", "float32", "bfloat16", "float16"])
    p_eval.add_argument("--eval_iters", type=int, default=50)

    args = parser.parse_args()

    if args.command == "prepare":
        prepare_data(args.data_path, args.out_dir, args.val_fraction, args.seed)
    elif args.command == "train":
        cmd_train(args)
    elif args.command == "generate":
        cmd_generate(args)
    elif args.command == "eval":
        cmd_eval(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
