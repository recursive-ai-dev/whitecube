"""
tensegrity_nb_lib.py — extension utilities for the TensegrityLM notebook.

These functions are designed to be dropped into notebook cells. They build on
top of tensegrity_lm.py (TensegrityConfig, TensegrityLM, Block, SwiGLU,
ffn_hidden_dim, get_batch, evaluate, save_checkpoint,
load_checkpoint, capture_rng_state, restore_rng_state, get_lr, pick_dtype).
"""

import os
import json
import dataclasses
import time
import math
import numpy as np
import torch
from files import atomic_json

from tensegrity_lm import (
    TensegrityConfig, TensegrityLM, ByteTokenizer,
    ffn_hidden_dim, get_batch, evaluate, save_checkpoint, load_checkpoint,
    capture_rng_state, restore_rng_state, get_lr, get_lr_wsd, pick_dtype, _get_memmap,
    MODEL_PRESETS, get_preset_config,
)


# =============================================================================
# Named configuration presets (notebook convenience wrappers)
# =============================================================================

def base15m_config(**overrides) -> TensegrityConfig:
    """
    ~15M-parameter reference TensegrityConfig for notebook use (see
    BASE15M_CONFIG_KWARGS / MODEL_PRESETS["base15m"] in tensegrity_lm.py, and
    test_presets.py which guards its param count against drift).
    Pass field overrides as kwargs, e.g. base15m_config(dropout=0.1).
    """
    return get_preset_config("base15m", **overrides)


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

    # Per-row RNG draws stay a Python loop (each row's max_start depends on which corpus
    # was chosen for it, and np.random must be consumed in this exact row-by-row order to
    # stay reproducible / bit-identical to the pre-vectorization implementation for a
    # given seed). What actually gets vectorized is the expensive part: gathering the
    # sampled windows out of the memmap-backed arrays. Rows are grouped by source corpus
    # (at most len(data_dirs) groups, vs. batch_size individual slice+append+stack calls)
    # and each group is gathered in one fancy-indexed batch instead of a per-row loop.
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
    # startswith, not ==: "cuda:0"/"cuda:1" etc. (common on multi-GPU setups) previously
    # fell through to the no-pin-memory branch silently. Matches the same fix already
    # applied to get_batch in tensegrity_lm.py.
    if device.startswith("cuda"):
        x_t = x_t.pin_memory().to(device, non_blocking=True)
        y_t = y_t.pin_memory().to(device, non_blocking=True)
    else:
        x_t = x_t.to(device)
        y_t = y_t.to(device)
    return x_t, y_t


# =============================================================================
# Net2Net-style "wider FFN" growth (function-preserving at the moment of growth)
# =============================================================================

@torch.no_grad()
def widen_swiglu(old_ffn, new_ffn, old_hidden, new_hidden, bias):
    """
    Copy an old SwiGLU module's weights into a wider new SwiGLU module.

    - w_gate / w_up: old rows [0:old_hidden] are copied; the new_ffn's own
      (already randomly initialized) rows [old_hidden:new_hidden] are left as-is.
    - w_down: old columns [0:old_hidden] are copied; new columns
      [old_hidden:new_hidden] are zeroed.

    Because w_down's new input columns are zero, the new hidden units
    contribute exactly zero to the output regardless of what w_gate/w_up
    compute for them -> the FFN's output is IDENTICAL to the old FFN's output
    for any input, at the moment of growth. The new units only start
    influencing the output once gradients move w_down's new columns away
    from zero during continued training.
    """
    assert new_hidden >= old_hidden, "widen_swiglu only supports growing (new_hidden >= old_hidden)"

    new_ffn.w_gate.weight.data[:old_hidden, :].copy_(old_ffn.w_gate.weight.data)
    new_ffn.w_up.weight.data[:old_hidden, :].copy_(old_ffn.w_up.weight.data)

    new_ffn.w_down.weight.data[:, :old_hidden].copy_(old_ffn.w_down.weight.data)
    new_ffn.w_down.weight.data[:, old_hidden:].zero_()

    if bias:
        new_ffn.w_gate.bias.data[:old_hidden].copy_(old_ffn.w_gate.bias.data)
        new_ffn.w_up.bias.data[:old_hidden].copy_(old_ffn.w_up.bias.data)
        new_ffn.w_down.bias.data.copy_(old_ffn.w_down.bias.data)


@torch.no_grad()
def transplant_block(old_block, new_block, cfg_old, cfg_new):
    """
    Copy everything from old_block into new_block. Attention, norms, and
    residual gains are copied verbatim (their shapes never change under depth/FFN-only
    growth modes). The FFN is copied verbatim if its hidden_dim is
    unchanged, or widened via widen_swiglu if cfg_new.ffn_mult/ffn_align
    produced a larger hidden_dim. When cfg_new.n_embd > cfg_old.n_embd, embedding
    columns and projection features are widened with zero-padding.
    """
    old_D = cfg_old.n_embd
    new_D = cfg_new.n_embd

    if old_D == new_D:
        # --- Norms ---
        new_block.ln1.weight.data.copy_(old_block.ln1.weight.data)
        new_block.ln2.weight.data.copy_(old_block.ln2.weight.data)

        # --- Attention (shape-invariant under our depth/FFN growth modes) ---
        for name in ["q_proj", "k_proj", "v_proj", "o_proj"]:
            old_lin = getattr(old_block.attn, name)
            new_lin = getattr(new_block.attn, name)
            new_lin.weight.data.copy_(old_lin.weight.data)
            if cfg_old.bias:
                new_lin.bias.data.copy_(old_lin.bias.data)

        # --- Residual gains ---
        new_block.attn_gain.data.copy_(old_block.attn_gain.data)
        new_block.ffn_gain.data.copy_(old_block.ffn_gain.data)

        # --- FFN ---
        old_hidden = ffn_hidden_dim(cfg_old.n_embd, cfg_old.ffn_mult, cfg_old.ffn_align)
        new_hidden = ffn_hidden_dim(cfg_new.n_embd, cfg_new.ffn_mult, cfg_new.ffn_align)

        if old_hidden == new_hidden:
            new_block.ffn.w_gate.weight.data.copy_(old_block.ffn.w_gate.weight.data)
            new_block.ffn.w_up.weight.data.copy_(old_block.ffn.w_up.weight.data)
            new_block.ffn.w_down.weight.data.copy_(old_block.ffn.w_down.weight.data)
            if cfg_old.bias:
                new_block.ffn.w_gate.bias.data.copy_(old_block.ffn.w_gate.bias.data)
                new_block.ffn.w_up.bias.data.copy_(old_block.ffn.w_up.bias.data)
                new_block.ffn.w_down.bias.data.copy_(old_block.ffn.w_down.bias.data)
        else:
            widen_swiglu(old_block.ffn, new_block.ffn, old_hidden, new_hidden, cfg_old.bias)
    else:
        # --- Embedding width growth (n_embd) ---
        scale = (old_D / new_D) ** 0.5

        # RMSNorms: scale weight vector by sqrt(old_D / new_D) to adjust for RMS dimension
        # change. That alone is only exact when eps == 0: RMSNorm computes
        # rsqrt(mean(x^2) + eps), and zero-padding x from old_D to new_D shrinks
        # mean(x^2) by old_D/new_D while eps stays fixed, so the two terms no longer
        # scale together. Scaling eps by the same old_D/new_D factor keeps the ratio
        # eps/mean(x^2) identical to the old RMSNorm's, making the growth exact again
        # (not just approximately so) for every input, not only ones where eps is
        # negligible relative to mean(x^2).
        new_block.ln1.weight.data[:old_D].copy_(old_block.ln1.weight.data * scale)
        new_block.ln2.weight.data[:old_D].copy_(old_block.ln2.weight.data * scale)
        new_block.ln1.eps = old_block.ln1.eps * (scale ** 2)
        new_block.ln2.eps = old_block.ln2.eps * (scale ** 2)

        # Residual gains
        new_block.attn_gain.data.copy_(old_block.attn_gain.data)
        new_block.ffn_gain.data.copy_(old_block.ffn_gain.data)

        # Attention projections
        old_head_dim = cfg_old.n_embd // cfg_old.n_head
        new_head_dim = cfg_new.n_embd // cfg_new.n_head

        new_block.attn.q_proj.weight.data.zero_()
        new_block.attn.k_proj.weight.data.zero_()
        new_block.attn.v_proj.weight.data.zero_()
        new_block.attn.o_proj.weight.data.zero_()
        if cfg_old.bias:
            new_block.attn.q_proj.bias.data.zero_()
            new_block.attn.k_proj.bias.data.zero_()
            new_block.attn.v_proj.bias.data.zero_()
            new_block.attn.o_proj.bias.data.zero_()

        # grow_model() only ever calls transplant_block with a head_dim-preserving
        # (new_n_embd, new_n_head) pair -- see its assertion and the comment there.
        # RoPE's rotation frequencies are themselves a function of head_dim
        # (precompute_rope's inv_freq depends on head_dim, not just n_embd), so a
        # head_dim change would rotate every *existing* channel differently, not just
        # add new ones; there is no per-head partial-copy that stays exact under that.
        assert old_head_dim == new_head_dim, (
            "transplant_block's n_embd-growth path requires head_dim to stay fixed "
            f"(got old={old_head_dim}, new={new_head_dim}); this should be unreachable "
            "given grow_model()'s own assertion."
        )
        old_q_out = cfg_old.n_head * old_head_dim
        old_kv_out = cfg_old.n_kv_head * old_head_dim
        new_block.attn.q_proj.weight.data[:old_q_out, :old_D].copy_(old_block.attn.q_proj.weight.data)
        new_block.attn.k_proj.weight.data[:old_kv_out, :old_D].copy_(old_block.attn.k_proj.weight.data)
        new_block.attn.v_proj.weight.data[:old_kv_out, :old_D].copy_(old_block.attn.v_proj.weight.data)
        new_block.attn.o_proj.weight.data[:old_D, :old_q_out].copy_(old_block.attn.o_proj.weight.data)
        if cfg_old.bias:
            new_block.attn.q_proj.bias.data[:old_q_out].copy_(old_block.attn.q_proj.bias.data)
            new_block.attn.k_proj.bias.data[:old_kv_out].copy_(old_block.attn.k_proj.bias.data)
            new_block.attn.v_proj.bias.data[:old_kv_out].copy_(old_block.attn.v_proj.bias.data)
            new_block.attn.o_proj.bias.data[:old_D].copy_(old_block.attn.o_proj.bias.data)

        # --- FFN ---
        old_hidden = ffn_hidden_dim(cfg_old.n_embd, cfg_old.ffn_mult, cfg_old.ffn_align)
        new_hidden = ffn_hidden_dim(cfg_new.n_embd, cfg_new.ffn_mult, cfg_new.ffn_align)

        def transplant_swiglu_embd(old_swiglu, new_swiglu):
            new_swiglu.w_gate.weight.data.zero_()
            new_swiglu.w_gate.weight.data[:old_hidden, :old_D].copy_(old_swiglu.w_gate.weight.data)
            new_swiglu.w_up.weight.data.zero_()
            new_swiglu.w_up.weight.data[:old_hidden, :old_D].copy_(old_swiglu.w_up.weight.data)
            new_swiglu.w_down.weight.data.zero_()
            new_swiglu.w_down.weight.data[:old_D, :old_hidden].copy_(old_swiglu.w_down.weight.data)

            if cfg_old.bias:
                new_swiglu.w_gate.bias.data[:old_hidden].copy_(old_swiglu.w_gate.bias.data)
                new_swiglu.w_up.bias.data[:old_hidden].copy_(old_swiglu.w_up.bias.data)
                new_swiglu.w_down.bias.data[:old_D].copy_(old_swiglu.w_down.bias.data)

        transplant_swiglu_embd(old_block.ffn, new_block.ffn)


_GROWTH_LOCKED_FIELDS = [
    "vocab_size", "block_size", "n_embd", "n_head", "n_kv_head",
    "sliding_window", "global_every",
    "bias", "dropout", "init_std", "homeo_target", "homeo_weight",
    "use_confidence_head", "confidence_weight",
]


@torch.no_grad()
def grow_model(old_model, old_cfg, new_n_layer=None, ffn_growth_mult=None,
               new_n_embd=None, new_n_head=None, new_n_kv_head=None, device=None):
    """
    Create a strictly more capable model that, at the moment of growth,
    computes EXACTLY the same function as old_model (verified by the equality
    check in check_function_preserved).

    Three independent growth axes are supported, and any combination may be used:

      - new_n_layer:    append (new_n_layer - old_cfg.n_layer) new transformer
                        blocks at the end of the stack. New blocks have their
                        residual gains initialized to exactly 0, so they are
                        a no-op at init: x = x + 0*attn_out = x.
      - ffn_growth_mult: widen every block's FFN hidden dimension by this
                        factor (Net2Net "wider net"). New hidden units
                        contribute exactly 0 to the residual stream at init
                        because w_down's new input columns are zeroed.
      - new_n_embd / new_n_head: widen the residual stream dimension (n_embd).
                        Embedding columns, RMSNorm weights, and attention projections
                        are widened. By default, head_dim is kept fixed and n_head
                        (and n_kv_head proportionally for GQA) grows with n_embd,
                        preserving RoPE position frequencies and attention distribution.

    vocab_size, block_size, sliding_window, global_every, bias, and
    homeostasis/confidence hyperparameters are NOT changed by this function.
    """
    if device is None:
        device = next(old_model.parameters()).device

    new_cfg_kwargs = dataclasses.asdict(old_cfg)

    if new_n_layer is not None:
        assert new_n_layer >= old_cfg.n_layer, "new_n_layer must be >= old_cfg.n_layer"
        new_cfg_kwargs["n_layer"] = new_n_layer
        if new_n_layer != old_cfg.n_layer:
            # let TensegrityConfig.__post_init__ recompute gain_init for the new depth;
            # this only affects the *new* blocks (old blocks' gains are copied below).
            new_cfg_kwargs["gain_init"] = None

    if ffn_growth_mult is not None:
        assert ffn_growth_mult >= 1.0, "ffn_growth_mult must be >= 1.0"
        new_cfg_kwargs["ffn_mult"] = old_cfg.ffn_mult * ffn_growth_mult

    if new_n_embd is not None or new_n_head is not None:
        old_head_dim = old_cfg.n_embd // old_cfg.n_head
        if new_n_embd is not None:
            assert new_n_embd >= old_cfg.n_embd, "new_n_embd must be >= old_cfg.n_embd"
            new_cfg_kwargs["n_embd"] = new_n_embd
        else:
            assert new_n_head >= old_cfg.n_head, "new_n_head must be >= old_cfg.n_head"
            new_n_embd = new_n_head * old_head_dim
            new_cfg_kwargs["n_embd"] = new_n_embd

        # head_dim must stay fixed: precompute_rope's inv_freq is a function of head_dim
        # itself, so changing it would change the RoPE rotation angle applied to every
        # *existing* channel, not just add new ones -- there is no way to keep old
        # channels' attention scores exact under a head_dim change. Earlier revisions of
        # this function had a "custom head_dim" fallback for new_n_embd values that
        # don't divide old_head_dim evenly; it looked like it worked (fresh random-init
        # models pass loose atol checks) but was never actually exact -- verified to
        # diverge by ~0.08 max logit difference on a realistically-trained model, well
        # outside any reasonable tolerance. Only the fixed-head_dim convention is exact.
        if new_n_embd % old_head_dim != 0:
            raise ValueError(
                f"new_n_embd ({new_n_embd}) is not a multiple of the existing head_dim "
                f"({old_head_dim} = old n_embd {old_cfg.n_embd} // old n_head {old_cfg.n_head}). "
                f"grow_model only supports function-preserving growth that keeps head_dim "
                f"fixed (adding more heads, not widening each head) -- pick a new_n_embd "
                f"that's a multiple of {old_head_dim}, e.g. "
                f"{old_head_dim * (new_n_embd // old_head_dim + 1)}."
            )
        implied_n_head = new_n_embd // old_head_dim
        if new_n_head is None:
            new_n_head = implied_n_head
        elif new_n_head != implied_n_head:
            raise ValueError(
                f"new_n_head={new_n_head} would change head_dim from {old_head_dim} to "
                f"{new_n_embd // new_n_head} (not function-preserving under RoPE -- see "
                f"above). For new_n_embd={new_n_embd}, the only head_dim-preserving choice "
                f"is new_n_head={implied_n_head}; omit new_n_head to have it inferred."
            )
        assert new_n_head >= old_cfg.n_head, "new_n_head must be >= old_cfg.n_head"
        new_cfg_kwargs["n_head"] = new_n_head

        if new_n_kv_head is None:
            gqa_ratio = old_cfg.n_head // old_cfg.n_kv_head
            new_n_kv_head = max(1, new_n_head // gqa_ratio)
        new_cfg_kwargs["n_kv_head"] = new_n_kv_head

    new_cfg = TensegrityConfig(**new_cfg_kwargs)

    # Sanity: confirm none of the "locked" fields drifted.
    locked_fields = list(_GROWTH_LOCKED_FIELDS)
    if new_n_embd is not None or new_n_head is not None:
        for f in ["n_embd", "n_head", "n_kv_head"]:
            if f in locked_fields:
                locked_fields.remove(f)

    for field_name in locked_fields:
        old_val = getattr(old_cfg, field_name)
        new_val = getattr(new_cfg, field_name)
        assert old_val == new_val, (
            f"grow_model must not change '{field_name}' "
            f"(old={old_val!r}, new={new_val!r}); this would break function preservation."
        )

    old_hidden = ffn_hidden_dim(old_cfg.n_embd, old_cfg.ffn_mult, old_cfg.ffn_align)
    new_hidden = ffn_hidden_dim(new_cfg.n_embd, new_cfg.ffn_mult, new_cfg.ffn_align)
    if new_hidden < old_hidden:
        raise ValueError(
            f"Resulting ffn hidden_dim ({new_hidden}) is smaller than the current one "
            f"({old_hidden}). Increase ffn_growth_mult or leave it as None."
        )

    new_model = TensegrityLM(new_cfg)
    new_model.to(device)
    old_model.to(device)

    # --- Global / shared modules ---
    if old_cfg.n_embd != new_cfg.n_embd:
        scale = (old_cfg.n_embd / new_cfg.n_embd) ** 0.5
        new_model.token_emb.weight.data.zero_()
        new_model.token_emb.weight.data[:, :old_cfg.n_embd].copy_(old_model.token_emb.weight.data)
        new_model.ln_f.weight.data[:old_cfg.n_embd].copy_(old_model.ln_f.weight.data * scale)
        # See transplant_block's matching comment: eps must scale by old_D/new_D too,
        # not just the weight vector, for RMSNorm growth to be exact rather than approximate.
        new_model.ln_f.eps = old_model.ln_f.eps * (scale ** 2)
        if old_cfg.use_confidence_head and new_cfg.use_confidence_head:
            new_model.confidence_head.weight.data.zero_()
            new_model.confidence_head.weight.data[:, :old_cfg.n_embd].copy_(old_model.confidence_head.weight.data)
            new_model.confidence_head.bias.data.copy_(old_model.confidence_head.bias.data)
    else:
        new_model.token_emb.weight.data.copy_(old_model.token_emb.weight.data)  # also lm_head (tied)
        new_model.ln_f.weight.data.copy_(old_model.ln_f.weight.data)
        if old_cfg.use_confidence_head and new_cfg.use_confidence_head:
            new_model.confidence_head.weight.data.copy_(old_model.confidence_head.weight.data)
            new_model.confidence_head.bias.data.copy_(old_model.confidence_head.bias.data)

    # --- Existing blocks: copy / widen ---
    for i in range(old_cfg.n_layer):
        transplant_block(old_model.blocks[i], new_model.blocks[i], old_cfg, new_cfg)

    # --- New blocks (depth growth only): zero residual gains -> exact no-op ---
    for i in range(old_cfg.n_layer, new_cfg.n_layer):
        new_model.blocks[i].attn_gain.data.zero_()
        new_model.blocks[i].ffn_gain.data.zero_()

    return new_model, new_cfg


@torch.no_grad()
def check_function_preserved(old_model, new_model, cfg, device, n_tokens=48, batch=2, atol=1e-5):
    """
    Feed identical random byte sequences through old_model and new_model
    (both in eval mode, fp32, no autocast) and confirm the logits match.
    This is the empirical proof that grow_model() retained everything the
    old model knew.
    """
    if old_model.cfg.n_embd != new_model.cfg.n_embd and atol == 1e-5:
        atol = 1e-3
    was_training_old = old_model.training
    was_training_new = new_model.training
    old_model.eval()
    new_model.eval()
    # TF32 silently truncates matmul mantissas on Ampere+ GPUs and is NOT part of
    # autocast -- left enabled (its default), it can push "exact" fp32 growth checks
    # over typical atol thresholds purely from matmul rounding, unrelated to any real
    # function-preservation bug. Disable it for the duration of this check only.
    prev_matmul_tf32 = torch.backends.cuda.matmul.allow_tf32
    prev_cudnn_tf32 = torch.backends.cudnn.allow_tf32
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    try:
        torch.manual_seed(0)
        x = torch.randint(0, cfg.vocab_size, (batch, min(n_tokens, cfg.block_size)), device=device)
        old_logits, _, _ = old_model(x, targets=None)
        new_logits, _, _ = new_model(x, targets=None)
        max_diff = (old_logits - new_logits).abs().max().item()
        ok = max_diff < atol
        print(f"[grow_model] max |old_logits - new_logits| = {max_diff:.3e} "
              f"({'PASS' if ok else 'FAIL'}, atol={atol:.1e})")
        if not ok:
            raise AssertionError(
                "grow_model() did not preserve the model's function. "
                "Do not continue training on this model until this is fixed."
            )
        return max_diff
    finally:
        torch.backends.cuda.matmul.allow_tf32 = prev_matmul_tf32
        torch.backends.cudnn.allow_tf32 = prev_cudnn_tf32
        if was_training_old:
            old_model.train()
        if was_training_new:
            new_model.train()


# =============================================================================
# Elastic Weight Consolidation (EWC) — "retain information"
# =============================================================================

def compute_fisher_diagonal(model, cfg, data_dir, device, device_type, dtype="auto",
                             n_batches=50, batch_size=8, chunk_size=None,
                             fisher_dtype="float32"):
    """
    Estimate the diagonal of the Fisher information matrix for every
    parameter, using the cross-entropy loss on `n_batches` random batches
    drawn from data_dir's train split. Larger values mean "this weight
    matters more for this dataset; don't let it move much."

    chunk_size: if given, process n_batches in groups of this size, explicitly
        freeing intermediate CUDA tensors (del + torch.cuda.empty_cache()) between
        chunks. Purely a peak-memory knob for constrained hardware -- the running
        fisher accumulator itself always stays resident throughout (that's the whole
        point of it being a running sum), so this only bounds the *transient* memory
        used by each batch's forward/backward, not the final result's footprint.
        Default None preserves prior behavior exactly (one un-chunked loop).
    fisher_dtype: "float32" (default, unchanged) or "float16" -- dtype of the
        *returned* fisher dict. Accumulation during the loop always happens in fp32
        regardless of this setting (summing many fp16 squared-gradient terms would
        degrade precision compounding over n_batches); only the final returned tensors
        are cast, roughly halving the dict's memory footprint for continual-learning
        workflows that keep it resident across a whole training run. Halving here is
        the accuracy tradeoff being documented, not a bug -- fp16 can't represent the
        full dynamic range of squared gradients as precisely as fp32.

    Returns a dict {param_name: fisher_tensor} (same shapes as the model's
    current parameters) and a matching snapshot {param_name: value_tensor}
    of the parameters at the time of computation.
    """
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


@torch.no_grad()
def expand_reference_state(old_fisher, old_params, new_model):
    """
    Re-map an (old_fisher, old_params) pair computed on the pre-growth model
    onto the post-growth model's parameter shapes:

      - Parameters whose shape is unchanged (almost everything: attention,
        norms, gains, embeddings, old FFN columns) are carried over directly.
      - Widened FFN tensors are zero-padded: the old region keeps its real
        Fisher value (and its old value, so EWC pulls it back there), while
        the newly-added region gets Fisher = 0 and old_value = its current
        (freshly initialized / post-grow) value, so EWC imposes NO penalty
        on the new capacity — it is free to specialize on new data.
      - Parameters that did not exist before growth (new transformer blocks)
        are absent from the returned dicts entirely -> also unconstrained.

    NOTE: For multi-stage growth (grow -> train -> grow again), you should
    recompute Fisher on the intermediate model after each training phase and
    pass that fresh Fisher to the next expand_reference_state call. Using the
    original pre-growth Fisher on a twice-grown model will under-constrain the
    first growth's new capacity.
    """
    if old_fisher is None or old_params is None:
        return {}, {}

    new_fisher = {}
    new_old_params = {}
    for name, p in new_model.named_parameters():
        if name not in old_fisher:
            continue
        of = old_fisher[name]
        op = old_params[name]
        if of.shape == p.shape:
            new_fisher[name] = of.to(p.device)
            new_old_params[name] = op.to(p.device)
        else:
            nf = torch.zeros_like(p, dtype=torch.float32)
            nop = p.detach().clone()
            slices = tuple(slice(0, s) for s in of.shape)
            nf[slices] = of.to(p.device)
            nop[slices] = op.to(p.device)
            new_fisher[name] = nf
            new_old_params[name] = nop
    return new_fisher, new_old_params


def ewc_penalty(model, fisher, old_params):
    """sum_i fisher_i * (theta_i - theta_old_i)^2, over parameters present in `fisher`."""
    total = None
    for name, p in model.named_parameters():
        f = fisher.get(name)
        if f is None:
            continue
        term = (f * (p - old_params[name]).pow(2)).sum()
        total = term if total is None else total + term
    if total is None:
        return torch.zeros((), device=next(model.parameters()).device)
    return total


# =============================================================================
# Training loop (notebook-friendly: single device, no DDP/compile)
# =============================================================================

def run_training(model, optimizer, cfg, out_dir, device,
                  data_dirs, data_weights=None,
                  max_steps=1000, warmup_steps=50,
                  learning_rate=3e-4, min_lr=3e-5,
                  schedule="cosine", stable_steps=None, decay_steps=None,
                  grad_clip=1.0, batch_size=12, grad_accum_steps=1,
                  log_interval=10, eval_interval=100, eval_iters=20,
                  dtype="auto", start_step=0, best_val_loss=float("inf"),
                  ewc_fisher=None, ewc_old_params=None, ewc_lambda=0.0,
                  init_from="scratch", stop_requested=None, schedule_steps=None):
    """
    Train `model` for steps [start_step, max_steps). Checkpoints
    (ckpt_latest.pt / ckpt_best.pt) and a train_log.jsonl are written to
    out_dir. Validation is always measured against data_dirs[0].

    data_dirs: str (single prepared corpus) or list of prepared corpus dirs.
    data_weights: sampling weights over data_dirs (only used if len > 1);
                   normalized internally. Use this to "replay" an old corpus
                   while training on a new one.
    ewc_*: outputs of compute_fisher_diagonal / expand_reference_state, plus
           a weight ewc_lambda. Set ewc_lambda=0.0 (default) to disable. Not
           affected by init_from="resume" -- EWC anchors are not part of the
           checkpoint schema and must still be supplied fresh by the caller
           on every call (e.g. re-run compute_fisher_diagonal /
           expand_reference_state before resuming a continual-training run).
    init_from: "scratch" (default; preserves prior behavior exactly, no
        checkpoint is read) or "resume". "resume" looks for
        {out_dir}/ckpt_latest.pt; if found, loads the checkpoint's model /
        optimizer / GradScaler / RNG state IN PLACE into the `model` and
        `optimizer` objects you passed in, and overrides start_step /
        best_val_loss with the checkpoint's values. If no checkpoint exists
        at out_dir yet, falls back to the scratch behavior using whatever
        start_step/best_val_loss you passed (with a printed notice).

        IMPORTANT: unlike tensegrity_lm.py's cmd_train, this function does
        not construct the model itself -- it can only load weights into the
        model/optimizer you already built. If the checkpoint at out_dir was
        saved under a different TensegrityConfig than `cfg`, resuming would
        load mismatched weights into a differently-shaped model, sometimes
        with a clear shape-mismatch error and sometimes (if parameter counts
        happen to coincide) with no error but silently wrong weights. This
        is checked explicitly below and raises before any loading happens.

    schedule: "cosine" (default; preserves prior behavior exactly) or "wsd" (warmup-
        stable-decay, see get_lr_wsd in tensegrity_lm.py). "wsd" requires stable_steps
        and decay_steps; warmup_steps + stable_steps + decay_steps need not equal
        max_steps (the schedule just holds at min_lr past decay_end, same convention
        as get_lr past max_steps).

        Either schedule is applied as a per-group MULTIPLIER, not an absolute value:
        each optimizer.param_groups[i]'s "lr" at construction time (its "base lr") is
        captured once before training starts, and every step's schedule value scales
        that base proportionally. For a plain AdamW optimizer (every group built with
        the same lr=learning_rate) this is mathematically identical to the old
        "overwrite every group to the same absolute lr" behavior. It's what makes a
        Muon optimizer (whose groups intentionally start at different base LRs --
        e.g. 0.02 for the Muon group vs. learning_rate for its AdamW groups, see
        TensegrityLM.configure_optimizer(use_muon=True)) work correctly here: without
        this, every group would get stomped to the same absolute value every step,
        silently breaking Muon's intended LR scale.

    schedule_steps: decay horizon, independent of the stopping step. New checkpoints
        preserve the complete schedule plan. On resume, omission keeps that plan;
        an explicit value replaces its horizon. Legacy checkpoints default to a
        horizon of completed steps and print a notice. Validation uses fixed
        batches; best loss resets when the evaluation configuration changes.

    Returns (final_step, best_val_loss).
    """
    if init_from not in ('scratch', 'resume'):
        raise ValueError("init_from must be 'scratch' or 'resume'")
    if schedule not in ('cosine', 'wsd'):
        raise ValueError("schedule must be 'cosine' or 'wsd'")
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
    device = str(device)

    os.makedirs(out_dir, exist_ok=True)
    device_type = "cuda" if "cuda" in device else ("mps" if device == "mps" else "cpu")
    amp_dtype = pick_dtype(dtype, device_type)
    use_amp = amp_dtype in (torch.float16, torch.bfloat16) and device_type in ("cuda", "cpu")
    # Constructed before any resume-load below (same fix as cmd_train in
    # tensegrity_lm.py): scaler must exist before scaler.load_state_dict can
    # be called on it.
    scaler = torch.amp.GradScaler(enabled=(amp_dtype == torch.float16 and device_type == "cuda"))

    # Keep the constructor's base rates: checkpoint rates are already scheduled.
    base_lrs = [g["lr"] for g in optimizer.param_groups]
    requested_schedule_steps = schedule_steps
    schedule_steps = max_steps if schedule_steps is None else schedule_steps
    if schedule_steps < 1:
        raise ValueError("schedule_steps must be positive")
    if init_from == "resume":
        latest_path = os.path.join(out_dir, "ckpt_latest.pt")
        if os.path.exists(latest_path):
            ckpt_resume = load_checkpoint(latest_path, device)

            cur_cfg_dict = dataclasses.asdict(cfg)
            ckpt_cfg_dict = ckpt_resume["config"]
            assert cur_cfg_dict == ckpt_cfg_dict, (
                f"init_from='resume' found a checkpoint at {latest_path} whose saved "
                f"config does not match the `cfg` passed to run_training. Resuming "
                f"would load mismatched weights into a differently-shaped/configured "
                f"model. Reconstruct `model` with TensegrityConfig(**checkpoint['config']) "
                f"before calling run_training, or point `out_dir` at a fresh directory "
                f"for a from-scratch run."
            )

            model.load_state_dict(ckpt_resume["model_state_dict"])
            optimizer.load_state_dict(ckpt_resume["optimizer_state_dict"])
            restore_rng_state(ckpt_resume.get("rng_state"))
            saved_scaler = ckpt_resume.get('scaler_state_dict')
            if scaler.is_enabled() and saved_scaler:
                scaler.load_state_dict(saved_scaler)

            # Checkpoints are saved with the step number that was just completed
            # (see the save_checkpoint calls below, before `step += 1`), so resuming
            # must continue from the next step, not repeat the one already trained.
            start_step = ckpt_resume["step"] + 1
            best_val_loss = ckpt_resume["best_val_loss"]
            saved_training = ckpt_resume.get("training_state")
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
                if saved_training.get("evaluation") != {"version": 1, "seed": 2026, "iters": eval_iters}:
                    best_val_loss = float("inf")
            else:
                # Old checkpoints contain no schedule horizon. Avoid an implicit LR restart.
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
        "evaluation": {"version": 1, "seed": 2026, "iters": eval_iters},
        'batching': {'batch_size': batch_size, 'grad_accum_steps': grad_accum_steps},
    }
    log_path = os.path.join(out_dir, "train_log.jsonl")
    log_file = open(log_path, "a")

    # Each group's own starting lr (set by configure_optimizer -- e.g. 0.02 for a Muon
    # group, learning_rate for AdamW groups) is the multiplier's base. For a plain
    # AdamW optimizer every group starts at the same lr=learning_rate, so this reduces
    # exactly to the old single-value behavior; see the schedule= docstring above.
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
            # Reject corrupt gradients before optimizer state or weights are changed.
            gradient_norm = torch.nn.utils.clip_grad_norm_(
                model.parameters(), grad_clip if grad_clip > 0 else float('inf'),
                error_if_nonfinite=not scaler.is_enabled())
            if scaler.is_enabled() and not torch.isfinite(gradient_norm).item():
                # GradScaler can recover fp16 overflow by lowering its scale. A
                # skipped update must not advance the schedule or completed steps.
                # A norm reduction can overflow even when every individual
                # gradient is finite. GradScaler's found-inf flag then stays
                # clear, so calling scaler.step() would still update weights.
                # Lower the scale explicitly and never step on an invalid norm.
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
                # optimizer.param_groups[0]'s actual current lr (not the raw 0..1 multiplier):
                # for a plain-AdamW optimizer this is the single lr every group shares, same
                # number the old log always showed. For Muon it's the Muon group's lr (its
                # own base_lrs[0] * lr_mult) -- lr_mult itself is also logged since it's the
                # one number that's directly comparable across every group regardless of base.
                lr0 = optimizer.param_groups[0]["lr"]
                print(f"step {step:6d} | lr {lr0:.2e} (mult {lr_mult:.3f}) | total {accum_total:.4f} "
                      f"(ce {ce:.4f} homeo {homeo:.4f}{conf_str}) | {dt:.1f}s")
                last_progress = {
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
                # BUGFIX: was `os.path.join(args.out_dir, ...)` and `raw_model` — neither
                # `args` nor `raw_model` exist in this function's scope (that's cmd_train's
                # vocabulary in tensegrity_lm.py, not run_training's). Caused a guaranteed
                # NameError the first time eval_interval was reached. Also now passes
                # scaler=scaler consistently at all four save points below, not just this one,
                # so GradScaler state (fp16 loss-scale) actually survives every checkpoint.
                improved = val_loss < best_val_loss
                best_val_loss = min(best_val_loss, val_loss)
                save_checkpoint(os.path.join(out_dir, "ckpt_latest.pt"), model, optimizer, cfg, step, best_val_loss, rng_state, scaler=scaler, training_state=training_state)
                if improved:
                    save_checkpoint(os.path.join(out_dir, "ckpt_best.pt"), model, optimizer, cfg, step, best_val_loss, rng_state, scaler=scaler, training_state=training_state)
                    print(f"step {step:6d} | new best val_loss {best_val_loss:.4f} -> ckpt_best.pt")

            step += 1
            if stop_requested is not None and stop_requested(step):
                print(f"[train] stopping after {step} completed steps; saving checkpoint")
                break

        if last_evaluated_step != step:
            val_loss = evaluate(model, data_dirs[0], cfg, device, device_type, amp_dtype, use_amp, eval_iters)
        if not math.isfinite(val_loss):
            raise RuntimeError('Nonfinite validation loss; checkpoint was not replaced')
        rng_state = capture_rng_state()
        improved = val_loss < best_val_loss
        best_val_loss = min(best_val_loss, val_loss)
        # Persist the last completed zero-based step, matching periodic saves.
        save_checkpoint(os.path.join(out_dir, "ckpt_latest.pt"), model, optimizer, cfg, step - 1, best_val_loss, rng_state, scaler=scaler, training_state=training_state)
        if improved:
            save_checkpoint(os.path.join(out_dir, "ckpt_best.pt"), model, optimizer, cfg, step - 1, best_val_loss, rng_state, scaler=scaler, training_state=training_state)
        print(f"[train] done. step={step} final_val_loss[{os.path.basename(data_dirs[0])}]={val_loss:.4f} "
              f"best_val_loss={best_val_loss:.4f}")
        elapsed = time.monotonic() - t0
        log_file.write(json.dumps({'step': step - 1, 'completed_steps': step, 'val_loss': val_loss,
                                   'best_val_loss': best_val_loss, 'event': 'finished'}) + '\n')
        log_file.flush()
        atomic_json(os.path.join(out_dir, 'progress.json'), {
            **last_progress,
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

    token_emb.weight and lm_head.weight are tied (the same tensor); since
    safetensors cannot store two tensors that share memory, lm_head.weight
    is cloned into an independent copy before saving. Both keys are written
    (with identical values), so the file loads cleanly with either a tied or
    untied model.
    """
    from safetensors.torch import save_file

    os.makedirs(out_dir, exist_ok=True)
    state_dict = model.state_dict()

    tensors = {}
    for k, v in state_dict.items():
        t = v.detach().cpu().contiguous()
        tensors[k] = t

    # Break the tie: clone lm_head.weight so it doesn't alias token_emb.weight.
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
        cfg = TensegrityConfig(**json.load(f))

    model = TensegrityLM(cfg)
    state_dict = load_file(weights_path)
    model.load_state_dict(state_dict)
    # Re-establish weight tying: safetensors stores lm_head and token_emb as
    # independent tensors, so load_state_dict breaks the tie. Restore it here.
    model.lm_head.weight = model.token_emb.weight
    model.to(device)
    return model, cfg


# =============================================================================
# Confidence-head calibration
# =============================================================================

@torch.no_grad()
def evaluate_confidence_calibration(model, cfg, data_dir, device, device_type, dtype="auto",
                                     n_batches=20, batch_size=8, n_bins=10):
    """
    Measurement/tooling only -- does not change confidence_weight or the confidence
    loss formula (see TensegrityLM.forward()'s confidence_head branch).

    The confidence head predicts P(argmax prediction == target). This buckets that
    predicted probability into n_bins bins over [0, 1] using data_dir's val split, and
    for each bin computes the *empirical* accuracy (fraction of tokens in that bin whose
    argmax prediction was actually correct). A well-calibrated head has empirical
    accuracy close to the bin's mean predicted confidence in every bin -- e.g. tokens
    the head scored ~0.8 confident should be right ~80% of the time. Also reports
    Expected Calibration Error (ECE): the count-weighted average |accuracy - confidence|
    gap across bins, a single scalar summary (0 = perfectly calibrated).

    Returns {"bins": [{"bin_lo", "bin_hi", "count", "accuracy", "mean_confidence"}, ...],
             "ece": float, "n_tokens": int}.
    """
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

            # Right-closed bins except the last, which also absorbs conf == 1.0 exactly.
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
