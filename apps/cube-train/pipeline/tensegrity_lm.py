"""
TensegrityLM — a byte-level decoder-only language model.

Single-file, checkpointable, trainable implementation combining:
  - Rotary positional embeddings (RoPE)              [Geometry]
  - RMSNorm pre-norm transformer blocks              [Algebraic Math]
  - Grouped-Query Attention (GQA)                    [Algorithms]
  - Alternating local sliding-window / global causal attention [Space]
  - SwiGLU feed-forward                              [Social Dynamics]
  - Learnable per-layer residual gain + activation-RMS
    "homeostatic" set-point regularization           [Biology]
  - Depth-scaled stochastic layer drop (LayerDrop)   [Evolution]
  - Cosine LR schedule with warmup ("temperature annealing") [Physics]
  - Optional confidence head for adaptive sampling temperature
                                                      [Psychology]
  - Byte-level tokenizer, zero external deps         [English Language]

Honesty note: every individual mechanism above is established in the
literature (RoPE, GQA, SwiGLU, RMSNorm, LayerDrop,
LayerScale-style gains, cosine schedules). The specific combination —
particularly the homeostatic residual-gain regularization framed as an
explicit activation-RMS set-point loss, paired with depth-scaled layer
drop and alternating local/global GQA — is a coherent but UNVALIDATED
recombination. Train it, measure it, don't assume it beats a vanilla
transformer of equal size until you've run the comparison yourself.

Usage:
    python tensegrity_lm.py prepare --data_path mydata.txt --out_dir data/mycorpus
    python tensegrity_lm.py train    --data_dir data/mycorpus --out_dir runs/run1
    python tensegrity_lm.py train    --data_dir data/mycorpus --out_dir runs/run1 --init_from resume
    python tensegrity_lm.py generate --ckpt runs/run1/ckpt_best.pt --prompt "Hello" --max_new_tokens 200
    python tensegrity_lm.py eval     --ckpt runs/run1/ckpt_best.pt --data_dir data/mycorpus
"""

import argparse
import dataclasses
import glob
import json
import math
import os
import sys
import time
import inspect
import random
from dataclasses import dataclass, asdict
from typing import Optional, Tuple, List, Dict, Any

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist

try:
    _SDPA_SUPPORTS_GQA = "enable_gqa" in inspect.signature(F.scaled_dot_product_attention).parameters
except (TypeError, ValueError):
    # Some torch builds expose scaled_dot_product_attention as a builtin with no
    # introspectable signature at all (inspect.signature raises ValueError), which
    # previously crashed this entire module at import time. Fall back to the
    # pre-GQA code path instead of hard-failing on those builds.
    _SDPA_SUPPORTS_GQA = False

# =============================================================================
# Tokenizer
# =============================================================================

class ByteTokenizer:
    """
    Byte-level tokenizer. Vocabulary = 256 raw byte values + 3 special tokens:
        256 = BOS (beginning of sequence)
        257 = EOS (end of sequence / document separator)
        258 = PAD (padding, unused by default but reserved)

    Zero training required.

    Losslessness note:
      - `encode_bytes`/`decode_bytes` are lossless for ANY bytes input.
      - `encode`/`decode` round-trip losslessly for any *well-formed* Python
        str, because UTF-8 encode/decode of valid unicode is bijective.
        They are NOT lossless if `errors` is set to a lossy mode (the
        default, "replace", is lossy and matches prior behavior for
        backward compatibility) and the input contains content that
        can't be represented in strict UTF-8 (e.g. lone surrogates).
        Pass errors="strict" if you need a hard failure instead of silent
        corruption, or use encode_bytes for guaranteed lossless behavior.
    """

    VOCAB_SIZE = 259
    BOS = 256
    EOS = 257
    PAD = 258
    _SPECIAL_IDS = frozenset((BOS, EOS, PAD))

    def __len__(self) -> int:
        return self.VOCAB_SIZE

    # ------------------------------------------------------------------ #
    # str <-> ids
    # ------------------------------------------------------------------ #
    def encode(
        self,
        text: str,
        errors: str = "replace",
        add_bos: bool = False,
        add_eos: bool = False,
    ) -> List[int]:
        """
        errors: passed through to str.encode. Default "replace" preserves
            prior behavior (never raises). Use "strict" to detect
            unencodable input instead of silently corrupting it.
        add_bos/add_eos: default False, preserves prior output shape.
        """
        ids = list(text.encode("utf-8", errors=errors))
        if add_bos:
            ids.insert(0, self.BOS)
        if add_eos:
            ids.append(self.EOS)
        return ids

    def decode(
        self,
        ids: List[int],
        errors: str = "replace",
        skip_special_tokens: bool = True,
        strict: bool = False,
    ) -> str:
        """
        skip_special_tokens: default True preserves prior behavior of
            silently dropping ids outside 0-255.
        strict: if True, raise ValueError on any id outside [0, VOCAB_SIZE)
            rather than silently dropping it. Off by default to preserve
            prior (permissive) behavior.
        """
        if strict:
            bad = [i for i in ids if not (0 <= i < self.VOCAB_SIZE)]
            if bad:
                raise ValueError(f"decode: invalid token id(s) outside vocab: {bad[:5]}"
                                  f"{'...' if len(bad) > 5 else ''}")

        # Special ids are 256-258 (see VOCAB_SIZE/BOS/EOS/PAD above), so
        # 0 <= i <= 255 already excludes every special id on its own —
        # skip_special_tokens has no further effect on which bytes survive
        # (special tokens were never representable as bytes to begin with).
        byte_vals = bytes(i for i in ids if 0 <= i <= 255)

        return byte_vals.decode("utf-8", errors=errors)

    # ------------------------------------------------------------------ #
    # bytes <-> ids (the actually-lossless path)
    # ------------------------------------------------------------------ #
    def encode_bytes(
        self,
        data: bytes,
        add_bos: bool = False,
        add_eos: bool = False,
    ) -> List[int]:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"encode_bytes expects bytes/bytearray, got {type(data).__name__}")
        ids = list(data)
        if add_bos:
            ids.insert(0, self.BOS)
        if add_eos:
            ids.append(self.EOS)
        return ids

    def decode_bytes(self, ids: List[int], strict: bool = False) -> bytes:
        """
        Inverse of encode_bytes. Strips special tokens, returns raw bytes
        with no UTF-8 interpretation — the only method that guarantees a
        lossless round trip for arbitrary binary input.
        """
        if strict:
            bad = [i for i in ids if not (0 <= i < self.VOCAB_SIZE)]
            if bad:
                raise ValueError(f"decode_bytes: invalid token id(s): {bad[:5]}"
                                  f"{'...' if len(bad) > 5 else ''}")
        return bytes(i for i in ids if 0 <= i <= 255)

    # ------------------------------------------------------------------ #
    # batch convenience
    # ------------------------------------------------------------------ #
    def encode_batch(self, texts: List[str], **kwargs) -> List[List[int]]:
        return [self.encode(t, **kwargs) for t in texts]

    def decode_batch(self, batches: List[List[int]], **kwargs) -> List[str]:
        return [self.decode(ids, **kwargs) for ids in batches]

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #
    def is_special(self, token_id: int) -> bool:
        return token_id in self._SPECIAL_IDS

    def special_tokens(self) -> Tuple[int, int, int]:
        return (self.BOS, self.EOS, self.PAD)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class TensegrityConfig:
    vocab_size: int = 259
    block_size: int = 4096
    n_layer: int = 28
    n_embd: int = 2560
    n_head: int = 20
    n_kv_head: int = 4
    dropout: float = 0.0

    sliding_window: int = 1024
    global_every: int = 4
    rope_base: float = 12000.0

    ffn_mult: float = 8.0 / 3.0
    ffn_align: int = 64

    homeo_target: float = 1.0
    homeo_weight: float = 0.01
    gain_init: Optional[float] = None

    layerdrop_max: float = 0.1

    use_confidence_head: bool = True
    confidence_weight: float = 0.05

    bias: bool = False
    init_std: float = 0.02
    grad_checkpoint: bool = True
    grad_checkpoint_every: int = 1  # checkpoint every Nth block; 1 = current (all blocks) behavior

    # --- Optional upgrades, all off by default: an untouched config produces the
    # exact same model/forward behavior as before these fields existed. ---

    # Q/K RMSNorm before RoPE (LLaMA 3 / Gemma 2 / Cohere-style). Cures attention-logit
    # drift; adds 2 small RMSNorm modules (head_dim each) per attention block.
    use_qk_norm: bool = True

    # Z-loss on final logits (PaLM/Gemma 2): coef * mean(logsumexp(logits, dim=-1) ** 2),
    # added to total_loss. 0.0 disables the term entirely (not computed, not added).
    z_loss_weight: float = 0.0

    # Logit soft-capping (Gemma 2): logits = soft_cap * tanh(logits / soft_cap), applied
    # right before both the LM loss and generation. None disables it (no-op).
    logit_soft_cap: Optional[float] = None

    # Scale token embeddings by sqrt(n_embd) after lookup (Gemma 2 convention). Off by
    # default so old checkpoints' embedding scale/lm_head tying is unaffected.
    embed_scale: bool = True


    def __init__(self, *args, **kwargs):
        fields = dataclasses.fields(self)
        for f, val in zip(fields, args):
            kwargs[f.name] = val
        for f in fields:
            if f.name in kwargs:
                setattr(self, f.name, kwargs[f.name])
            elif f.default is not dataclasses.MISSING:
                setattr(self, f.name, f.default)
            elif f.default_factory is not dataclasses.MISSING:
                setattr(self, f.name, f.default_factory())
            else:
                raise TypeError(f"Missing required argument: {f.name}")
        self.__post_init__()

    def __post_init__(self):
        assert self.n_embd % self.n_head == 0, "n_embd must be divisible by n_head"
        head_dim = self.n_embd // self.n_head
        assert head_dim % 2 == 0, "head_dim (n_embd // n_head) must be even for RoPE"
        assert self.n_head % self.n_kv_head == 0, "n_head must be divisible by n_kv_head for GQA"
        assert self.global_every >= 1
        assert self.grad_checkpoint_every >= 1, "grad_checkpoint_every must be >= 1"
        assert 0.0 <= self.layerdrop_max < 1.0
        assert self.sliding_window >= 1
        if self.gain_init is None:
            self.gain_init = 1.0 / math.sqrt(2.0 * self.n_layer)


# =============================================================================
# Named configuration presets
# =============================================================================

# "base15m": ~15M total parameters (verified by instantiating TensegrityLM and
# summing param.numel() — see test_presets.py, which asserts this stays in
# [14.5e6, 16e6]). Intended as a small, fast-to-iterate reference scale for
# local experiments and notebooks. If you edit these kwargs, re-run
# test_presets.py to confirm the size didn't drift out of range.
BASE15M_CONFIG_KWARGS: Dict[str, Any] = dict(
    n_layer=4,
    n_embd=512,
    n_head=4,
    n_kv_head=1,
    ffn_mult=6.0,
    ffn_align=64,
    block_size=512,
    sliding_window=124,
    global_every=4,
)

MODEL_PRESETS: Dict[str, Dict[str, Any]] = {
    "base15m": BASE15M_CONFIG_KWARGS,
}


def get_preset_config(name: str, **overrides) -> "TensegrityConfig":
    """
    Build a TensegrityConfig from a named preset in MODEL_PRESETS, optionally
    overriding individual fields (e.g. get_preset_config("base15m", dropout=0.1)).
    """
    if name not in MODEL_PRESETS:
        raise ValueError(f"Unknown preset {name!r}; available presets: {sorted(MODEL_PRESETS)}")
    kwargs = dict(MODEL_PRESETS[name])
    kwargs.update(overrides)
    return TensegrityConfig(**kwargs)


# =============================================================================
# Core building blocks
# =============================================================================

class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        in_dtype = x.dtype
        x_fp32 = x.float()
        rms = torch.rsqrt(x_fp32.pow(2).mean(dim=-1, keepdim=True) + self.eps)
        out = x_fp32 * rms * self.weight.float()
        return out.to(in_dtype)


def precompute_rope(head_dim: int, max_seq_len: int, base: float = 10000.0,
                     device=None, dtype=torch.float32) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Precompute cos/sin tables for RoPE. Returns tensors of shape (max_seq_len, head_dim // 2).
    """
    assert head_dim % 2 == 0
    inv_freq = 1.0 / (base ** (torch.arange(0, head_dim, 2, device=device, dtype=torch.float32) / head_dim))
    t = torch.arange(max_seq_len, device=device, dtype=torch.float32)
    freqs = torch.outer(t, inv_freq)  # (max_seq_len, head_dim // 2)
    cos = freqs.cos().to(dtype)
    sin = freqs.sin().to(dtype)
    return cos, sin


def apply_rope(x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor) -> torch.Tensor:
    B, H, T, D = x.shape
    half = D // 2
    assert T <= cos.shape[0], (
        f"sequence length {T} exceeds precomputed RoPE table length {cos.shape[0]}; "
        f"call precompute_rope with a larger max_seq_len"
    )
    in_dtype = x.dtype
    x_fp32 = x.float()
    x1 = x_fp32[..., :half]
    x2 = x_fp32[..., half:]
    cos_ = cos[:T].float().view(1, 1, T, half)
    sin_ = sin[:T].float().view(1, 1, T, half)
    out1 = x1 * cos_ - x2 * sin_
    out2 = x2 * cos_ + x1 * sin_
    return torch.cat([out1, out2], dim=-1).to(in_dtype)


def activation_rms(x: torch.Tensor) -> torch.Tensor:
    """Mean RMS of activations across the feature dimension, averaged over batch/tokens."""
    return x.float().pow(2).mean(dim=-1).sqrt().mean()


def apply_logit_soft_cap(logits: torch.Tensor, soft_cap: float) -> torch.Tensor:
    """Gemma-2-style logit soft-capping: soft_cap * tanh(logits / soft_cap). Bounds
    logits to (-soft_cap, soft_cap) with a smooth (not hard-clipped) falloff."""
    in_dtype = logits.dtype
    capped = soft_cap * torch.tanh(logits.float() / soft_cap)
    return capped.to(in_dtype)


# =============================================================================
# Attention (Grouped-Query, with alternating local/global masks)
# =============================================================================

class CausalSelfAttention(nn.Module):
    def __init__(self, cfg: TensegrityConfig, is_global: bool):
        super().__init__()
        assert cfg.n_embd % cfg.n_head == 0, (
            f"n_embd ({cfg.n_embd}) must be divisible by n_head ({cfg.n_head}); "
            f"otherwise head_dim truncates silently and o_proj/q_proj shapes desync."
        )
        assert cfg.n_head % cfg.n_kv_head == 0, (
            f"n_head ({cfg.n_head}) must be divisible by n_kv_head ({cfg.n_kv_head}) for GQA grouping."
        )
        self.n_head = cfg.n_head
        self.n_kv_head = cfg.n_kv_head
        self.head_dim = cfg.n_embd // cfg.n_head
        self.n_rep = cfg.n_head // cfg.n_kv_head
        self.is_global = is_global
        self.sliding_window = cfg.sliding_window
        self.dropout_p = cfg.dropout
        self.q_proj = nn.Linear(cfg.n_embd, cfg.n_head * self.head_dim, bias=cfg.bias)
        self.k_proj = nn.Linear(cfg.n_embd, cfg.n_kv_head * self.head_dim, bias=cfg.bias)
        self.v_proj = nn.Linear(cfg.n_embd, cfg.n_kv_head * self.head_dim, bias=cfg.bias)
        self.o_proj = nn.Linear(cfg.n_head * self.head_dim, cfg.n_embd, bias=cfg.bias)
        self.resid_dropout = nn.Dropout(cfg.dropout)
        self._chunk_plan = None
        self._chunk_plan_T = None
        self._chunk_plan_device = None

        # Q/K RMSNorm (LLaMA 3 / Gemma 2 / Cohere): per-head normalization applied
        # right after projection, before RoPE. Off by default -- modules aren't even
        # created, so a checkpoint saved with use_qk_norm=False has no q_norm/k_norm
        # keys, matching a model built with the same (default) config exactly.
        if cfg.use_qk_norm:
            self.q_norm = RMSNorm(self.head_dim)
            self.k_norm = RMSNorm(self.head_dim)
        else:
            self.q_norm = None
            self.k_norm = None

    def forward(self, x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor) -> torch.Tensor:
        # No mask parameter here: global layers get an exact causal mask via
        # is_causal=True, and local layers get one via _local_attention's chunking --
        # neither ever materializes a (T, T) score matrix. (forward_incremental, the
        # separate KV-cached path used by generate(), does take an explicit mask --
        # it needs one for prefill, where there's no is_causal-style shortcut once a
        # cache is involved.)
        B, T, C = x.shape
        q = self.q_proj(x).view(B, T, self.n_head, self.head_dim).transpose(1, 2)       # (B, nH, T, D)
        k = self.k_proj(x).view(B, T, self.n_kv_head, self.head_dim).transpose(1, 2)    # (B, nKV, T, D)
        v = self.v_proj(x).view(B, T, self.n_kv_head, self.head_dim).transpose(1, 2)    # (B, nKV, T, D)
        if self.q_norm is not None:
            q = self.q_norm(q)
            k = self.k_norm(k)
        q = apply_rope(q, cos, sin)
        k = apply_rope(k, cos, sin)

        dropout_p = self.dropout_p if self.training else 0.0
        if self.n_rep > 1 and _SDPA_SUPPORTS_GQA:
            gqa_kwargs = {"enable_gqa": True}
        else:
            gqa_kwargs = {}
            if self.n_rep > 1:
                # Fallback for torch < 2.5.
                k = k.repeat_interleave(self.n_rep, dim=1)
                v = v.repeat_interleave(self.n_rep, dim=1)

        if self.is_global:
            # No custom mask needed: is_causal=True is exactly the "global" mask
            # and lets SDPA pick its fused Flash/memory-efficient kernel instead
            # of materializing a (B, nH, T, T) score matrix.
            y = F.scaled_dot_product_attention(q, k, v, dropout_p=dropout_p, is_causal=True, **gqa_kwargs)
        else:
            y = self._local_attention(q, k, v, dropout_p, gqa_kwargs)

        y = y.transpose(1, 2).contiguous().view(B, T, self.n_head * self.head_dim)
        y = self.o_proj(y)
        y = self.resid_dropout(y)
        return y

    def _local_attention(self, q: torch.Tensor, k: torch.Tensor, v: torch.Tensor,
                          dropout_p: float, gqa_kwargs: dict) -> torch.Tensor:
        """
        Sliding-window causal attention, computed one window-sized chunk of
        queries at a time against only the keys/values that chunk could
        legally attend to. Equivalent to masking a full (T, T) causal matrix
        with `j > i - sliding_window` (the old behavior), but never
        materializes that (T, T) tensor -- each chunk's score matrix is at
        most (sliding_window, 2 * sliding_window - 1).
        """
        W = self.sliding_window
        T = q.shape[2]
        plan = self._get_local_chunk_plan(T, W, q.device)
        outs = []
        for q_start, q_end, k_start, mask in plan:
            qc = q[:, :, q_start:q_end, :]
            kc = k[:, :, k_start:q_end, :]
            vc = v[:, :, k_start:q_end, :]
            if mask is None:
                outs.append(F.scaled_dot_product_attention(
                    qc, kc, vc, dropout_p=dropout_p, is_causal=True, **gqa_kwargs
                ))
            else:
                outs.append(F.scaled_dot_product_attention(
                    qc, kc, vc, attn_mask=mask, dropout_p=dropout_p, **gqa_kwargs
                ))
        return torch.cat(outs, dim=2)

    def _get_local_chunk_plan(self, T: int, W: int, device: torch.device):
        """
        Cached list of (q_start, q_end, k_start, mask) chunks covering [0, T)
        in windows of size W. k_start == q_start for the first chunk (its
        window covers the whole chunk, so mask=None + is_causal=True is
        exact); later chunks need trailing keys from the previous chunk too,
        so they get an explicit small mask. Cache is keyed on (T, device) and
        is safe to reuse across grad-checkpoint recomputation since it's a
        pure function of those two values.
        """
        if (self._chunk_plan is not None and self._chunk_plan_T == T
                and self._chunk_plan_device == device):
            return self._chunk_plan

        plan = []
        n_chunks = (T + W - 1) // W
        for c in range(n_chunks):
            q_start, q_end = c * W, min((c + 1) * W, T)
            k_start = max(0, q_start - W + 1)
            if k_start == q_start:
                plan.append((q_start, q_end, k_start, None))
                continue
            qlen = q_end - q_start
            offset = q_start - k_start
            i = torch.arange(qlen, device=device).view(-1, 1)
            j = torch.arange(offset + qlen, device=device).view(1, -1)
            mask = (j <= i + offset) & (j > i - W + offset)
            plan.append((q_start, q_end, k_start, mask))

        self._chunk_plan = plan
        self._chunk_plan_T = T
        self._chunk_plan_device = device
        return plan

    def forward_incremental(
        self, x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor,
        past_kv: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        attn_mask: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        KV-cached counterpart to forward(), used only by TensegrityLM.generate().
        Does not touch or share code with forward() -- training always goes
        through forward() unchanged.

        Two supported call patterns:
          - Prefill: attn_mask given (the usual boolean local/global mask,
            sliced to the chunk being processed), past_kv=None. x holds a
            whole new chunk (e.g. the prompt). Attention-wise this is
            identical to forward(); it additionally returns the (k, v) cache
            needed to seed subsequent decode steps.
          - Decode: attn_mask=None, past_kv from a previous call, x is a
            single new token (T == 1). No mask is applied because every key
            retained in the cache is causally at or before the query position
            by construction, so there is nothing to mask out.

        For local (sliding-window) layers, k/v -- both what's attended to and
        what's returned as the new cache -- are trimmed to the trailing
        `sliding_window` entries, since no future query can ever reach further
        back than that regardless of how long generation continues.
        """
        B, T, C = x.shape
        q = self.q_proj(x).view(B, T, self.n_head, self.head_dim).transpose(1, 2)
        k = self.k_proj(x).view(B, T, self.n_kv_head, self.head_dim).transpose(1, 2)
        v = self.v_proj(x).view(B, T, self.n_kv_head, self.head_dim).transpose(1, 2)
        if self.q_norm is not None:
            q = self.q_norm(q)
            k = self.k_norm(k)
        q = apply_rope(q, cos, sin)
        k = apply_rope(k, cos, sin)

        if attn_mask is not None:
            # Prefill: attn_mask already encodes the local/global windowing across
            # all T query positions, so k/v must stay at their full T length here
            # -- trimming before this call would desync them from the mask's key
            # dimension (each row of the mask expects to see the *whole* chunk).
            assert past_kv is None, "prefill (attn_mask given) must not also receive a past_kv cache"
        else:
            assert T == 1, "no-mask incremental attention only supports single-token decode steps"
            if past_kv is not None:
                past_k, past_v = past_kv
                k = torch.cat([past_k, k], dim=2)
                v = torch.cat([past_v, v], dim=2)
            # No mask enforces the window here, so it must be applied directly:
            # trim to the trailing `sliding_window` keys/values before attending.
            if not self.is_global and k.shape[2] > self.sliding_window:
                k = k[:, :, -self.sliding_window:, :]
                v = v[:, :, -self.sliding_window:, :]

        if self.n_rep > 1:
            if _SDPA_SUPPORTS_GQA:
                y = F.scaled_dot_product_attention(
                    q, k, v, attn_mask=attn_mask, dropout_p=0.0, is_causal=False, enable_gqa=True,
                )
            else:
                y = F.scaled_dot_product_attention(
                    q, k.repeat_interleave(self.n_rep, dim=1), v.repeat_interleave(self.n_rep, dim=1),
                    attn_mask=attn_mask, dropout_p=0.0, is_causal=False,
                )
        else:
            y = F.scaled_dot_product_attention(q, k, v, attn_mask=attn_mask, dropout_p=0.0, is_causal=False)

        y = y.transpose(1, 2).contiguous().view(B, T, self.n_head * self.head_dim)
        y = self.o_proj(y)

        # Cache handed back to the caller: local layers only ever need the
        # trailing `sliding_window` keys/values regardless of how long this
        # chunk was (a long prefill chunk gets trimmed down here; a decode
        # step was already trimmed above, so this is a no-op for it).
        if not self.is_global and k.shape[2] > self.sliding_window:
            new_cache = (k[:, :, -self.sliding_window:, :], v[:, :, -self.sliding_window:, :])
        else:
            new_cache = (k, v)

        return y, new_cache


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
# Feed-forward: SwiGLU
# =============================================================================

def ffn_hidden_dim(n_embd: int, mult: float, align: int) -> int:
    assert n_embd > 0, f"n_embd must be positive, got {n_embd}"
    assert mult > 0, f"mult must be positive, got {mult}"
    assert align >= 1, f"align must be a positive integer, got {align}"
    hidden = int(n_embd * mult)
    hidden = align * ((hidden + align - 1) // align)
    return max(hidden, align)


class SwiGLU(nn.Module):
    def __init__(self, n_embd: int, hidden_dim: int, bias: bool, dropout: float):
        super().__init__()
        self.w_gate = nn.Linear(n_embd, hidden_dim, bias=bias)
        self.w_up = nn.Linear(n_embd, hidden_dim, bias=bias)
        self.w_down = nn.Linear(hidden_dim, n_embd, bias=bias)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.dropout(self.w_down(F.silu(self.w_gate(x)) * self.w_up(x)))




# =============================================================================
# Transformer block
# =============================================================================

class Block(nn.Module):
    def __init__(self, cfg: TensegrityConfig, layer_idx: int):
        super().__init__()
        assert cfg.global_every > 0, f"global_every must be positive, got {cfg.global_every}"
        assert cfg.n_layer >= 1, f"n_layer must be >= 1, got {cfg.n_layer}"
        assert 0.0 <= cfg.layerdrop_max <= 1.0, (
            f"layerdrop_max must be in [0, 1], got {cfg.layerdrop_max}"
        )

        self.layer_idx = layer_idx
        is_global = ((layer_idx + 1) % cfg.global_every == 0)

        self.ln1 = RMSNorm(cfg.n_embd)
        self.attn = CausalSelfAttention(cfg, is_global=is_global)
        self.ln2 = RMSNorm(cfg.n_embd)

        hidden_dim = ffn_hidden_dim(cfg.n_embd, cfg.ffn_mult, cfg.ffn_align)
        self.ffn = SwiGLU(cfg.n_embd, hidden_dim, cfg.bias, cfg.dropout)

        # Homeostatic residual gains (learnable per-layer, per-sublayer scalars)
        self.attn_gain = nn.Parameter(torch.tensor(float(cfg.gain_init)))
        self.ffn_gain = nn.Parameter(torch.tensor(float(cfg.gain_init)))
        self.homeo_target = cfg.homeo_target

        # Depth-scaled stochastic depth probability
        n_layer = cfg.n_layer
        if n_layer > 1:
            self.drop_p = cfg.layerdrop_max * (layer_idx / (n_layer - 1))
        else:
            self.drop_p = 0.0

    def forward(self, x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor,
                ) -> Tuple[torch.Tensor, torch.Tensor, bool]:
        # Stochastic depth: during training, randomly skip the entire block (batch-level)
        if self.training and self.drop_p > 0.0:
            skip = torch.rand((), device=x.device)
            # Synchronize the drop decision across DDP ranks. Without this, ranks
            # can independently choose different skip/no-skip paths, which desyncs
            # the set of parameters that receive gradients this step and can hang
            # or silently corrupt DDP's gradient all-reduce. No-op outside DDP.
            if dist.is_available() and dist.is_initialized():
                dist.broadcast(skip, src=0)
            # This .item() sync was already happening here regardless (it's how the
            # skip/no-skip decision itself gets made), so surfacing it as the
            # return value below adds no new synchronization point.
            if skip.item() < self.drop_p:
                zero = x.new_zeros((), dtype=torch.float32)  # match dtype of the non-dropped path below
                return x, zero, True

        attn_out = self.attn(self.ln1(x), cos, sin)
        homeo_attn = (activation_rms(attn_out) - self.homeo_target).pow(2)
        x = x + self.attn_gain * attn_out

        ffn_out = self.ffn(self.ln2(x))
        homeo_ffn = (activation_rms(ffn_out) - self.homeo_target).pow(2)
        x = x + self.ffn_gain * ffn_out

        homeo = homeo_attn + homeo_ffn
        return x, homeo, False

    def forward_incremental(
        self, x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor,
        past_kv: Optional[Tuple[torch.Tensor, torch.Tensor]],
        attn_mask: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        KV-cached counterpart to forward(), used only by TensegrityLM.generate().
        No LayerDrop (generate() always runs in eval(), where drop_p never
        triggers anyway -- see the `self.training and ...` guard above) and no
        homeostatic-loss bookkeeping (inference reports no losses).
        """
        attn_out, new_kv = self.attn.forward_incremental(self.ln1(x), cos, sin, past_kv, attn_mask)
        x = x + self.attn_gain * attn_out

        ffn_out = self.ffn(self.ln2(x))
        x = x + self.ffn_gain * ffn_out

        return x, new_kv


# =============================================================================
# Muon optimizer (single-device variant)
# =============================================================================
#
# Vendored from https://github.com/KellerJordan/Muon (muon.py, SingleDeviceMuonWithAuxAdam),
# the reference implementation behind the modded-nanogpt speedrun results, adapted to drop
# the torch.distributed all_gather logic that variant needs for multi-GPU training (this repo
# targets a single GPU). zeropower_via_newtonschulz5's coefficients and muon_update's momentum/
# scaling formulas are reproduced verbatim -- these are exactly the numbers the speedrun results
# are evidence for, not values to casually retune. Off by default (TensegrityLM.configure_optimizer
# use_muon=False): produces the identical AdamW-only optimizer as before this was added.
#
# Muon should only be used for hidden weight layers (attention/FFN projection matrices).
# The token embedding (tied to lm_head), the confidence head, and every 1D parameter (RMSNorm
# weights, the homeostatic attn_gain/ffn_gain scalars) still go through AdamW -- see
# TensegrityLM.configure_optimizer's partitioning logic below for how that split is drawn for
# this specific model.

def zeropower_via_newtonschulz5(G: torch.Tensor, steps: int = 5) -> torch.Tensor:
    """
    Newton-Schulz iteration computing an approximate orthogonalization of G (the nearest
    matrix with all singular values ~1, not an exact SVD-based projection -- see upstream
    docstring: this produces US'V^T with S'_ii ~ Uniform(0.5, 1.5), which is empirically
    just as good for this use case as exact orthogonalization and much cheaper).
    """
    assert G.ndim >= 2
    a, b, c = (3.4445, -4.7750, 2.0315)
    X = G.bfloat16()
    if G.size(-2) > G.size(-1):
        X = X.mT
    X = X / (X.norm(dim=(-2, -1), keepdim=True) + 1e-7)
    for _ in range(steps):
        A = X @ X.mT
        B = b * A + c * A @ A
        X = a * X + B @ X
    if G.size(-2) > G.size(-1):
        X = X.mT
    return X


def muon_update(grad: torch.Tensor, momentum: torch.Tensor, beta: float = 0.95,
                 ns_steps: int = 5, nesterov: bool = True) -> torch.Tensor:
    momentum.lerp_(grad, 1 - beta)
    update = grad.lerp_(momentum, beta) if nesterov else momentum
    if update.ndim == 4:  # conv filters, not used by this model but kept for fidelity to upstream
        update = update.view(len(update), -1)
    update = zeropower_via_newtonschulz5(update, steps=ns_steps)
    update = update * max(1.0, update.size(-2) / update.size(-1)) ** 0.5
    return update


def adam_update(grad: torch.Tensor, buf1: torch.Tensor, buf2: torch.Tensor, step: int,
                 betas: Tuple[float, float], eps: float) -> torch.Tensor:
    buf1.lerp_(grad, 1 - betas[0])
    buf2.lerp_(grad.square(), 1 - betas[1])
    buf1c = buf1 / (1 - betas[0] ** step)
    buf2c = buf2 / (1 - betas[1] ** step)
    return buf1c / (buf2c.sqrt() + eps)


class SingleDeviceMuonWithAuxAdam(torch.optim.Optimizer):
    """
    Single-GPU combined optimizer: runs Muon on param groups with use_muon=True, AdamW
    (reimplemented inline, matching upstream) on groups with use_muon=False. One optimizer
    object so the training loop only has one .step()/.zero_grad() to call, same as a plain
    AdamW would need.
    """
    def __init__(self, param_groups: List[Dict[str, Any]]):
        for group in param_groups:
            assert "use_muon" in group
            if group["use_muon"]:
                group["lr"] = group.get("lr", 0.02)
                group["momentum"] = group.get("momentum", 0.95)
                group["weight_decay"] = group.get("weight_decay", 0)
                assert set(group.keys()) == {"params", "lr", "momentum", "weight_decay", "use_muon"}
            else:
                group["lr"] = group.get("lr", 3e-4)
                group["betas"] = group.get("betas", (0.9, 0.95))
                group["eps"] = group.get("eps", 1e-10)
                group["weight_decay"] = group.get("weight_decay", 0)
                assert set(group.keys()) == {"params", "lr", "betas", "eps", "weight_decay", "use_muon"}
        super().__init__(param_groups, dict())

    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            if group["use_muon"]:
                for p in group["params"]:
                    if p.grad is None:
                        continue
                    state = self.state[p]
                    if len(state) == 0:
                        state["momentum_buffer"] = torch.zeros_like(p)
                    update = muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])
                    p.mul_(1 - group["lr"] * group["weight_decay"])
                    p.add_(update.reshape(p.shape), alpha=-group["lr"])
            else:
                for p in group["params"]:
                    if p.grad is None:
                        continue
                    state = self.state[p]
                    if len(state) == 0:
                        state["exp_avg"] = torch.zeros_like(p)
                        state["exp_avg_sq"] = torch.zeros_like(p)
                        state["step"] = 0
                    state["step"] += 1
                    update = adam_update(p.grad, state["exp_avg"], state["exp_avg_sq"],
                                          state["step"], group["betas"], group["eps"])
                    p.mul_(1 - group["lr"] * group["weight_decay"])
                    p.add_(update, alpha=-group["lr"])

        return loss


# =============================================================================
# Full model
# =============================================================================

class TensegrityLM(nn.Module):
    def __init__(self, cfg: TensegrityConfig):
        super().__init__()
        assert cfg.n_layer >= 1, f"n_layer must be >= 1, got {cfg.n_layer}"

        self.cfg = cfg
        self.grad_checkpoint = cfg.grad_checkpoint
        self.grad_checkpoint_every = cfg.grad_checkpoint_every

        self.token_emb = nn.Embedding(cfg.vocab_size, cfg.n_embd)
        self.drop = nn.Dropout(cfg.dropout)
        self.blocks = nn.ModuleList([Block(cfg, i) for i in range(cfg.n_layer)])
        self.ln_f = RMSNorm(cfg.n_embd)
        self.lm_head = nn.Linear(cfg.n_embd, cfg.vocab_size, bias=False)

        # Weight tying
        self.token_emb.weight = self.lm_head.weight

        # Optional confidence head: predicts P(argmax prediction == target)
        if cfg.use_confidence_head:
            self.confidence_head = nn.Linear(cfg.n_embd, 1, bias=True)
        else:
            self.confidence_head = None

        head_dim = cfg.n_embd // cfg.n_head
        cos, sin = precompute_rope(head_dim, cfg.block_size, base=cfg.rope_base)
        self.register_buffer("rope_cos", cos, persistent=False)
        self.register_buffer("rope_sin", sin, persistent=False)

        # Attention masks are built lazily (see _get_masks) sized to the actual T of each
        # forward call rather than eagerly materialized at the full (block_size, block_size)
        # here -- for large block_size that dense boolean pair is O(block_size^2) memory
        # that's wasteful since T is almost always block_size at training time anyway (so the
        # lazily-built mask ends up the same size) and can be much smaller at eval/generation
        # time. A single-entry cache (keyed on T) avoids rebuilding every step in the common
        # case where T doesn't change between calls.
        self._mask_cache_T: Optional[int] = None
        self._mask_cache: Optional[Dict[str, torch.Tensor]] = None

        self.apply(self._init_weights)

        n_params = sum(p.numel() for p in self.parameters())
        print(f"[TensegrityLM] layers={cfg.n_layer} "
              f"n_embd={cfg.n_embd} n_head={cfg.n_head} n_kv_head={cfg.n_kv_head} "
              f"vocab={cfg.vocab_size} block_size={cfg.block_size}")
        print(f"[TensegrityLM] total parameters: {n_params:,} ({n_params/1e6:.2f}M)")

    def _init_weights(self, module: nn.Module):
        if isinstance(module, nn.Linear):
            nn.init.normal_(module.weight, mean=0.0, std=self.cfg.init_std)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, mean=0.0, std=self.cfg.init_std)

    def _get_masks(self, T: int, device: torch.device) -> Dict[str, torch.Tensor]:
        """
        Return the (T, T) global/local boolean masks, rebuilding only when T (or device)
        differs from the last call. build_attention_masks(T, ...) is exactly equal to
        build_attention_masks(block_size, ...)[:T, :T] (the causal/window relation only
        depends on the query/key indices themselves, not on block_size), so this is a
        drop-in replacement for the old eagerly-materialized block_size-sized buffers.
        """
        cached = self._mask_cache
        if self._mask_cache_T != T or cached is None or cached["global"].device != device:
            cached = build_attention_masks(T, self.cfg.sliding_window, device=device)
            self._mask_cache = cached
            self._mask_cache_T = T
        return cached

    def forward(self, idx: torch.Tensor, targets: Optional[torch.Tensor] = None
                 ) -> Tuple[torch.Tensor, Optional[Dict[str, torch.Tensor]], Optional[torch.Tensor]]:
        """
        idx:     (B, T) integer token ids, T <= block_size
        targets: (B, T) integer token ids (next-token targets), or None for inference

        Returns:
            logits: (B, T, vocab_size)
            losses: dict of scalar tensors (only if targets is not None), else None
            confidence: (B, T) sigmoid confidence scores (only if targets is None and head enabled), else None
        """
        B, T = idx.shape
        assert T <= self.cfg.block_size, f"sequence length {T} exceeds block_size {self.cfg.block_size}"

        x = self.token_emb(idx)  # (B, T, C)
        if self.cfg.embed_scale:
            x = x * (self.cfg.n_embd ** 0.5)
        x = self.drop(x)

        cos = self.rope_cos[:T]
        sin = self.rope_sin[:T]

        # Explicit fp32: Block returns fp32 homeo, so keep the running total fp32 from
        # the start rather than relying on implicit type promotion on the first add.
        total_homeo = x.new_zeros((), dtype=torch.float32)
        n_active = 0

        use_checkpoint = self.grad_checkpoint and self.training and x.requires_grad

        for i, block in enumerate(self.blocks):
            # grad_checkpoint_every == 1 (default) checkpoints every block, identical to prior
            # behavior. Larger values checkpoint only every Nth block (by index), trading some
            # of the memory savings back for fewer recomputation passes during backward.
            if use_checkpoint and i % self.grad_checkpoint_every == 0:
                x, homeo, was_skipped = torch.utils.checkpoint.checkpoint(
                    block, x, cos, sin, use_reentrant=False
                )
            else:
                x, homeo, was_skipped = block(x, cos, sin)
            total_homeo = total_homeo + homeo
            if not was_skipped:
                n_active += 1

        x = self.ln_f(x)
        logits = self.lm_head(x)
        if self.cfg.logit_soft_cap is not None:
            logits = apply_logit_soft_cap(logits, self.cfg.logit_soft_cap)

        if targets is not None:
            ce_loss = F.cross_entropy(
                logits.view(-1, logits.size(-1)), targets.view(-1),
                ignore_index=-1,
            )

            denom = max(1, n_active * 2)
            avg_homeo = total_homeo / denom

            losses = {
                "ce_loss": ce_loss,
                "homeo_loss": avg_homeo,
            }

            total_loss = ce_loss + self.cfg.homeo_weight * avg_homeo

            if self.cfg.z_loss_weight > 0.0:
                # PaLM / Gemma 2 z-loss: penalizes logsumexp(logits) drifting away from 0,
                # which keeps the softmax normalizer well-scaled and prevents logit blowup.
                z_loss = self.cfg.z_loss_weight * torch.logsumexp(logits.float(), dim=-1).pow(2).mean()
                losses["z_loss"] = z_loss
                total_loss = total_loss + z_loss

            if self.confidence_head is not None:
                conf_logits = self.confidence_head(x).squeeze(-1)  # (B, T)
                with torch.no_grad():
                    preds = logits.argmax(dim=-1)  # (B, T)
                    correct = (preds == targets).float()
                    valid = (targets != -1).float()
                conf_loss_raw = F.binary_cross_entropy_with_logits(
                    conf_logits, correct, reduction="none"
                )
                denom_c = valid.sum().clamp_min(1.0)
                conf_loss = (conf_loss_raw * valid).sum() / denom_c
                losses["confidence_loss"] = conf_loss
                total_loss = total_loss + self.cfg.confidence_weight * conf_loss

            losses["total_loss"] = total_loss
            return logits, losses, None

        else:
            confidence = None
            if self.confidence_head is not None:
                conf_logits = self.confidence_head(x).squeeze(-1)  # (B, T)
                confidence = torch.sigmoid(conf_logits)
            return logits, None, confidence

    def forward_incremental(
        self, idx: torch.Tensor,
        past_kv_list: Optional[List[Optional[Tuple[torch.Tensor, torch.Tensor]]]] = None,
        pos_start: int = 0,
    ) -> Tuple[torch.Tensor, List[Tuple[torch.Tensor, torch.Tensor]], Optional[torch.Tensor]]:
        """
        KV-cached counterpart to forward(), used only by generate() for
        autoregressive sampling. Entirely separate code path from forward():
        grad_checkpoint, homeostatic loss, and LayerDrop are a
        training-time concern that forward() still handles exactly as before;
        none of that applies here since generate() always runs the model in
        eval() under @torch.no_grad().

        idx: (B, T) ids for the NEW tokens being fed this call -- the whole
             prompt on the first (prefill) call, a single token (T == 1) on
             every subsequent (decode) call.
        past_kv_list: None on the prefill call; otherwise the list this method
             returned on the previous call (one (k, v) cache per block).
        pos_start: absolute position of idx[:, 0] in the full generated
             sequence. Needed because, unlike forward() (which always starts
             at position 0), idx here is typically a suffix of the sequence,
             so the RoPE table must be sliced at an offset.

        Returns (logits, new_past_kv_list, confidence): logits/confidence
        cover only the T new positions, mirroring forward()'s shapes.
        """
        B, T = idx.shape
        is_prefill = past_kv_list is None
        if is_prefill:
            past_kv_list = [None] * len(self.blocks)

        x = self.token_emb(idx)
        if self.cfg.embed_scale:
            x = x * (self.cfg.n_embd ** 0.5)
        x = self.drop(x)

        cos = self.rope_cos[pos_start:pos_start + T]
        sin = self.rope_sin[pos_start:pos_start + T]

        if is_prefill:
            masks = self._get_masks(T, x.device)
            mask_global = masks["global"]
            mask_local = masks["local"]
        else:
            mask_global = None
            mask_local = None

        new_past_kv_list = []
        for block, past_kv in zip(self.blocks, past_kv_list):
            attn_mask = mask_global if block.attn.is_global else mask_local
            x, new_kv = block.forward_incremental(x, cos, sin, past_kv, attn_mask)
            new_past_kv_list.append(new_kv)

        x = self.ln_f(x)
        logits = self.lm_head(x)
        if self.cfg.logit_soft_cap is not None:
            logits = apply_logit_soft_cap(logits, self.cfg.logit_soft_cap)

        confidence = None
        if self.confidence_head is not None:
            conf_logits = self.confidence_head(x).squeeze(-1)  # (B, T)
            confidence = torch.sigmoid(conf_logits)

        return logits, new_past_kv_list, confidence

    # -------------------------------------------------------------------
    # Generation
    # -------------------------------------------------------------------
    @torch.no_grad()
    def generate(self, idx: torch.Tensor, max_new_tokens: int,
                  temperature: float = 1.0, top_k: Optional[int] = None,
                  top_p: Optional[float] = None,
                  use_confidence_temp: bool = False,
                  conf_temp_min: float = 0.5, conf_temp_max: float = 1.5,
                  stop_on_eos: bool = True, use_cache: bool = True) -> torch.Tensor:
        """
        idx: (B, T) starting context.
        Returns (B, T + max_new_tokens) — may be shorter if EOS is hit and stop_on_eos
        is True, in which case generation stops once every sequence in the batch has
        produced EOS (finished sequences are padded with EOS in the interim, not
        re-sampled).

        use_cache: if True (default), reuse a per-layer KV cache
            (forward_incremental) across decode steps instead of recomputing
            attention over the whole context on every new token. This is a
            pure performance optimization -- it produces bit-identical token
            sequences to use_cache=False for the same seed/inputs (see
            test_kv_cache.py) -- not an approximation, so it's safe to leave
            on by default. If the running sequence ever exceeds cfg.block_size,
            the cache is transparently rebuilt from the truncated trailing
            window, matching the sliding-window behavior of the non-cached path.
        """
        assert temperature >= 0.0, f"temperature must be >= 0, got {temperature}"
        assert top_k is None or top_k > 0, f"top_k must be None or positive, got {top_k}"

        # generate() must not permanently change the model's mode: if this is called
        # mid-training (e.g. periodic sampling), leaving the model in eval() afterward
        # silently disables dropout/stochastic depth for the rest of training with no
        # error anywhere.
        was_training = self.training
        self.eval()
        try:
            B = idx.size(0)
            finished = torch.zeros(B, dtype=torch.bool, device=idx.device) if stop_on_eos else None
            past_kv_list = None

            for _ in range(max_new_tokens):
                T = idx.size(1)
                if use_cache:
                    if past_kv_list is None or T > self.cfg.block_size:
                        # Prefill: either the very first step, or the running sequence
                        # just outgrew block_size and the cache must be rebuilt from the
                        # truncated trailing window (same sliding-window fallback the
                        # non-cached path below uses on every single step).
                        idx_cond = idx if T <= self.cfg.block_size else idx[:, -self.cfg.block_size:]
                        logits, past_kv_list, confidence = self.forward_incremental(idx_cond, None, 0)
                    else:
                        # Steady-state decode: only the just-sampled token is new; its
                        # absolute position is T - 1 (idx already includes it).
                        logits, past_kv_list, confidence = self.forward_incremental(idx[:, -1:], past_kv_list, T - 1)
                    logits = logits[:, -1, :]  # (B, V)
                else:
                    idx_cond = idx if T <= self.cfg.block_size else idx[:, -self.cfg.block_size:]
                    logits, _, confidence = self.forward(idx_cond, targets=None)
                    logits = logits[:, -1, :]  # (B, V)

                greedy = False
                if temperature == 0.0:
                    # Greedy decoding. An explicit temperature=0 request must win over
                    # confidence-temp scaling below — otherwise a caller with
                    # use_confidence_temp=True and an active confidence head could never
                    # get deterministic output, even when explicitly asking for it.
                    # (Also: temperature=0 previously divided by zero -> NaN logits ->
                    # multinomial errors out; this is a common expected convention instead.)
                    greedy = True
                elif use_confidence_temp and confidence is not None:
                    conf = confidence[:, -1]  # (B,)
                    # Per-sequence temperature scaling: low confidence -> higher temperature
                    scale = conf_temp_max - conf * (conf_temp_max - conf_temp_min)  # (B,)
                    logits = logits / scale.unsqueeze(-1).clamp_min(1e-4)
                else:
                    logits = logits / temperature

                if greedy:
                    next_tok = logits.argmax(dim=-1, keepdim=True)
                else:
                    if top_k is not None:
                        v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                        logits[logits < v[:, [-1]]] = -float("inf")

                    if top_p is not None:
                        sorted_logits, sorted_idx = torch.sort(logits, descending=True, dim=-1)
                        probs = F.softmax(sorted_logits, dim=-1)
                        cumprobs = torch.cumsum(probs, dim=-1)
                        cutoff = cumprobs > top_p
                        cutoff[:, 1:] = cutoff[:, :-1].clone()
                        cutoff[:, 0] = False
                        sorted_logits = sorted_logits.masked_fill(cutoff, -float("inf"))
                        logits = torch.full_like(logits, -float("inf")).scatter(1, sorted_idx, sorted_logits)

                    probs = F.softmax(logits, dim=-1)
                    next_tok = torch.multinomial(probs, num_samples=1)  # (B, 1)

                if stop_on_eos:
                    # Freeze already-finished sequences instead of continuing to sample
                    # them, so a fast-finishing sequence doesn't keep drifting while
                    # waiting on slower ones in the same batch.
                    next_tok = torch.where(
                        finished.unsqueeze(-1),
                        torch.full_like(next_tok, ByteTokenizer.EOS),
                        next_tok,
                    )

                idx = torch.cat([idx, next_tok], dim=1)

                if stop_on_eos:
                    finished = finished | (next_tok.squeeze(-1) == ByteTokenizer.EOS)
                    if finished.all():
                        break

            return idx
        finally:
            self.train(was_training)

    # -------------------------------------------------------------------
    # Optimizer configuration
    # -------------------------------------------------------------------
    def configure_optimizer(self, weight_decay: float, learning_rate: float,
                             betas: Tuple[float, float], device_type: str,
                             use_8bit: bool = False,
                             use_muon: bool = False,
                             muon_lr: float = 0.02,
                             muon_momentum: float = 0.95) -> torch.optim.Optimizer:
        """
        use_muon: off by default (identical AdamW-only optimizer as before this flag
            existed). When True, returns a SingleDeviceMuonWithAuxAdam instead: every
            ndim>=2 parameter belonging to self.blocks (attention q/k/v/o_proj, SwiGLU
            weights, and Q/K norm
            weights are ndim==1 so they don't qualify) goes to Muon; everything else --
            the tied token_emb/lm_head weight, the confidence head, and every 1D parameter
            (RMSNorm weights, the homeostatic attn_gain/ffn_gain scalars) -- goes to AdamW
            in the same optimizer object. This split follows Muon's own usage guidance
            (see the class docstring above): it's meant for hidden weight layers only, not
            embeddings/output heads/scalars.
        """
        if use_muon:
            return self._configure_muon_optimizer(weight_decay, learning_rate, betas,
                                                    muon_lr, muon_momentum)

        decay_params = []
        no_decay_params = []
        seen = set()
        for name, p in self.named_parameters():
            if not p.requires_grad:
                continue
            if id(p) in seen:
                continue
            seen.add(id(p))
            if p.dim() >= 2:
                decay_params.append(p)
            else:
                no_decay_params.append(p)

        groups = [
            {"params": decay_params, "weight_decay": weight_decay},
            {"params": no_decay_params, "weight_decay": 0.0},
        ]

        # Low-memory 8-bit AdamW path: same defensive try/import-or-fall-back pattern as
        # the fused-AdamW detection below. AdamW keeps 2 fp32 moment tensors per param
        # (exp_avg, exp_avg_sq) -- 8 bytes/param of pure optimizer state on top of the
        # model weights themselves; bitsandbytes' AdamW8bit quantizes those moments to
        # int8 (~2 bytes/param), a ~4x reduction in optimizer-state memory. Only
        # attempted on CUDA (bitsandbytes' 8-bit kernels are CUDA-only); any ImportError
        # or non-CUDA device silently falls back to the existing torch.optim.AdamW path.
        if use_8bit and device_type == "cuda" and torch.cuda.is_available():
            try:
                import bitsandbytes as bnb
                optimizer = bnb.optim.AdamW8bit(groups, lr=learning_rate, betas=betas)
                return optimizer
            except ImportError:
                pass

        # Robust fused-AdamW feature detection: __code__.co_varnames breaks on any
        # PyTorch build where __init__ is wrapped by a decorator that doesn't preserve
        # __code__ faithfully. inspect.signature + try/except mirrors the SDPA
        # enable_gqa feature-detection used in CausalSelfAttention. Also confirm CUDA
        # is actually available rather than trusting the device_type string alone —
        # otherwise fused=True can be handed to an optimizer with no CUDA tensors and
        # fail confusingly at the first .step() instead of here.
        use_fused = False
        if device_type == "cuda" and torch.cuda.is_available():
            try:
                use_fused = "fused" in inspect.signature(torch.optim.AdamW).parameters
            except (TypeError, ValueError):
                use_fused = False

        extra = {"fused": True} if use_fused else {}
        optimizer = torch.optim.AdamW(groups, lr=learning_rate, betas=betas, **extra)
        return optimizer

    def _configure_muon_optimizer(self, weight_decay: float, learning_rate: float,
                                   betas: Tuple[float, float],
                                   muon_lr: float, muon_momentum: float) -> torch.optim.Optimizer:
        seen = set()
        muon_params = []
        for name, p in self.blocks.named_parameters():
            if not p.requires_grad or id(p) in seen:
                continue
            if p.dim() >= 2:
                muon_params.append(p)
                seen.add(id(p))

        # Everything not already claimed by the Muon pass above: the tied token_emb/lm_head
        # weight, the confidence head, and every 1D param (norms, homeostatic gains) --
        # deliberately walking self.named_parameters() (the whole model) a second time
        # rather than special-casing by name, so nothing new added to the model in the
        # future silently falls through uncovered by either group.
        adamw_decay_params = []
        adamw_no_decay_params = []
        for name, p in self.named_parameters():
            if not p.requires_grad or id(p) in seen:
                continue
            seen.add(id(p))
            if p.dim() >= 2:
                adamw_decay_params.append(p)
            else:
                adamw_no_decay_params.append(p)

        param_groups = [
            dict(params=muon_params, lr=muon_lr, momentum=muon_momentum,
                 weight_decay=weight_decay, use_muon=True),
            dict(params=adamw_decay_params, lr=learning_rate, betas=betas,
                 eps=1e-10, weight_decay=weight_decay, use_muon=False),
            dict(params=adamw_no_decay_params, lr=learning_rate, betas=betas,
                 eps=1e-10, weight_decay=0.0, use_muon=False),
        ]
        return SingleDeviceMuonWithAuxAdam(param_groups)


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
        try:
            import pandas as pd
            df = pd.read_parquet(filepath)
            str_cols = [c for c in df.columns if pd.api.types.is_string_dtype(df[c]) or df[c].dtype == object]
            lines = []
            for col in str_cols:
                lines.extend(df[col].dropna().astype(str).tolist())
            content = "\n\n".join(lines)
            return content.encode("utf-8", errors="replace")
        except Exception as e:
            print(f"[warning] Failed to parse parquet {filepath}: {e}, falling back to raw read")
            with open(filepath, "rb") as fh:
                return fh.read()
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
                                val = "\n".join(str(v) for v in data.values() if isinstance(v, (str, int, float)))
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



_MEMMAP_CACHE: Dict[str, np.memmap] = {}


def _get_memmap(path: str) -> np.memmap:
    """Cached np.memmap open. Data files are read-only and don't change during
    training, so reusing the same memmap across calls is safe and avoids a fresh
    open()+mmap() syscall on every single get_batch/get_batch_mixed call."""
    arr = _MEMMAP_CACHE.get(path)
    if arr is None:
        arr = np.memmap(path, dtype=np.uint16, mode="r")
        _MEMMAP_CACHE[path] = arr
    return arr


def get_batch(data_dir: str, split: str, batch_size: int, block_size: int,
               device: str, rng=None) -> Tuple[torch.Tensor, torch.Tensor]:
    path = os.path.join(data_dir, f"{split}.bin")
    arr = _get_memmap(path)
    n = len(arr)
    if n <= block_size + 1:
        raise ValueError(
            f"{split}.bin has only {n} tokens, need > block_size+1 ({block_size + 1}). "
            f"Use a larger corpus or smaller --block_size."
        )
    max_start = n - block_size - 1
    starts = (np.random if rng is None else rng).randint(0, max_start + 1, size=(batch_size,))
    # Vectorized gather: build a (batch_size, block_size) index matrix via broadcasting
    # instead of a Python for-loop of per-row memmap slices + np.stack. Same starts, same
    # underlying array -> bit-identical x/y to the old loop, just without per-row Python
    # overhead (measurable at this model's step-time scale -- see test_get_batch.py).
    offsets = np.arange(block_size)
    idx_x = starts[:, None] + offsets[None, :]  # (batch_size, block_size)
    x = arr[idx_x].astype(np.int64)
    y = arr[idx_x + 1].astype(np.int64)
    x_t = torch.from_numpy(x)
    y_t = torch.from_numpy(y)
    # startswith, not ==: DDP/multi-GPU runs commonly pass "cuda:0", "cuda:1", etc.,
    # which previously fell through to the no-pin-memory branch silently.
    if device.startswith("cuda"):
        x_t = x_t.pin_memory().to(device, non_blocking=True)
        y_t = y_t.pin_memory().to(device, non_blocking=True)
    else:
        x_t = x_t.to(device)
        y_t = y_t.to(device)
    return x_t, y_t


# =============================================================================
# Checkpointing
# =============================================================================

CHECKPOINT_FORMAT_VERSION = 1  # bump when the checkpoint schema changes; load_checkpoint warns on mismatch


def save_checkpoint(path: str, model: nn.Module, optimizer: torch.optim.Optimizer,
                     cfg: TensegrityConfig, step: int, best_val_loss: float,
                     rng_state: Dict[str, Any], scaler: Optional[torch.amp.GradScaler] = None,
                     training_state: Optional[Dict[str, Any]] = None):
    """
    scaler: pass the training GradScaler (if fp16 mixed precision is in use) so its
        loss-scale/growth-tracker state survives resume. Default None preserves prior
        checkpoint schema exactly for callers that don't pass it. If scaler is passed
        but disabled (e.g. bf16/fp32 training), its state_dict is still saved (it's a
        cheap no-op dict) for schema consistency across checkpoints from the same run.
    """
    raw_model = model.module if hasattr(model, "module") else model

    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    ckpt = {
        "format_version": CHECKPOINT_FORMAT_VERSION,
        "model_state_dict": raw_model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
        "scaler_state_dict": scaler.state_dict() if scaler is not None else None,
        "config": asdict(cfg),
        "step": step,
        "best_val_loss": best_val_loss,
        "rng_state": rng_state,
        "training_state": training_state,
    }
    tmp_path = path + ".tmp"
    torch.save(ckpt, tmp_path)
    # Flush to disk before the atomic rename, so a crash/power-loss immediately after
    # os.replace can't leave a checkpoint that *looks* complete (the rename succeeded)
    # but whose contents were still sitting in OS write-back cache.
    with open(tmp_path, "r+b") as f:
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def load_checkpoint(path: str, device: str) -> Dict[str, Any]:
    ckpt = torch.load(path, map_location=device, weights_only=False)
    required = ("model_state_dict", "optimizer_state_dict", "config", "step", "best_val_loss")
    missing = [k for k in required if k not in ckpt]
    if missing:
        raise ValueError(
            f"{path} is missing expected checkpoint key(s) {missing}; "
            f"this may not be a TensegrityLM checkpoint, or it predates the current format."
        )
    ckpt_version = ckpt.get("format_version")
    if ckpt_version != CHECKPOINT_FORMAT_VERSION:
        print(f"[warn] {path} has format_version={ckpt_version!r}, code expects "
              f"{CHECKPOINT_FORMAT_VERSION!r}. Loading will proceed, but the schema may "
              f"have changed since this checkpoint was saved -- if load_state_dict below "
              f"fails or behaves oddly, this mismatch is why.")
    return ckpt


def capture_rng_state() -> Dict[str, Any]:
    state = {
        "torch": torch.get_rng_state(),
        "numpy": np.random.get_state(),
    }
    if torch.cuda.is_available():
        state["cuda"] = torch.cuda.get_rng_state_all()
    return state


def restore_rng_state(state: Dict[str, Any]):
    if state is None:
        return
    if "torch" in state:
        torch_state = state["torch"]
        if isinstance(torch_state, torch.Tensor):
            torch_state = torch_state.cpu()
        torch.set_rng_state(torch_state)
    if "numpy" in state:
        np.random.set_state(state["numpy"])
    if "cuda" in state and torch.cuda.is_available():
        try:
            cuda_state = state["cuda"]
            if isinstance(cuda_state, (list, tuple)):
                cuda_state = [s.cpu() if isinstance(s, torch.Tensor) else s for s in cuda_state]
            torch.cuda.set_rng_state_all(cuda_state)
        except Exception as e:
            print(f"[warn] failed to restore CUDA RNG state ({e})")


# =============================================================================
# Learning rate schedule (cosine with warmup — "temperature annealing")
# =============================================================================

def get_lr(step: int, warmup_steps: int, max_steps: int, max_lr: float, min_lr: float) -> float:
    if step < warmup_steps:
        return max_lr * (step + 1) / warmup_steps
    if step >= max_steps:
        return min_lr
    decay_ratio = (step - warmup_steps) / max(1, (max_steps - warmup_steps))
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))
    return min_lr + coeff * (max_lr - min_lr)


def get_lr_wsd(step: int, warmup_steps: int, stable_steps: int, decay_steps: int,
               max_lr: float, min_lr: float) -> float:
    """
    Warmup-Stable-Decay (MiniCPM; also used by Llama 3.1, DeepSeek-V2): linear warmup to
    max_lr, hold flat at max_lr for stable_steps, then linearly decay to min_lr over
    decay_steps. Unlike get_lr's cosine schedule, decay only has to be as long as
    decay_steps -- you don't have to commit to a total token budget upfront, since "when do
    I start decaying" is a decision you can make later by just choosing when this function's
    stable phase ends, not something baked into the schedule shape from step 0.

    step is measured from 0; the three phases are
        [0, warmup_steps), [warmup_steps, warmup_steps + stable_steps),
        [warmup_steps + stable_steps, warmup_steps + stable_steps + decay_steps),
    and any step beyond that returns min_lr, same convention as get_lr.
    """
    assert warmup_steps >= 1, "warmup_steps must be >= 1"
    assert decay_steps >= 1, "decay_steps must be >= 1"
    decay_start = warmup_steps + stable_steps
    decay_end = decay_start + decay_steps

    if step < warmup_steps:
        return max_lr * (step + 1) / warmup_steps
    if step < decay_start:
        return max_lr
    if step >= decay_end:
        return min_lr
    decay_ratio = (step - decay_start) / decay_steps
    return max_lr - decay_ratio * (max_lr - min_lr)


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
# Training
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
        ddp_world_size = int(os.environ["WORLD_SIZE"])
        device = f"cuda:{ddp_local_rank}"
        torch.cuda.set_device(device)
        master_process = (ddp_rank == 0)
        seed_offset = ddp_rank
    else:
        ddp_world_size = 1
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
        cfg = TensegrityConfig(**ckpt_resume["config"])
    else:
        cfg = build_config_from_args(args)

    model = TensegrityLM(cfg)
    model.to(device)

    if ckpt_resume is not None:
        model.load_state_dict(ckpt_resume["model_state_dict"])

    optimizer = model.configure_optimizer(
        weight_decay=args.weight_decay, learning_rate=args.learning_rate,
        betas=(args.beta1, args.beta2), device_type=device_type,
        use_8bit=args.use_8bit_optimizer,
    )

    # BUGFIX: scaler must exist before the resume block below can call
    # scaler.load_state_dict(...) on it. It previously was constructed ~30 lines further
    # down, after the resume block already referenced it — an UnboundLocalError waiting
    # to happen the moment scaler_state_dict was actually populated in a checkpoint (see
    # the save_checkpoint calls further down, which previously never passed scaler=scaler
    # at all, so this path was silently never exercised).
    use_amp = dtype in (torch.float16, torch.bfloat16) and device_type in ("cuda", "cpu")
    amp_dtype = dtype if use_amp else torch.float32
    scaler = torch.amp.GradScaler(enabled=(dtype == torch.float16 and device_type == "cuda"))

    if ckpt_resume is not None:
        optimizer.load_state_dict(ckpt_resume["optimizer_state_dict"])
        restore_rng_state(ckpt_resume.get("rng_state"))
        if ckpt_resume.get("scaler_state_dict") is not None:
            scaler.load_state_dict(ckpt_resume["scaler_state_dict"])

    # Checkpoints are saved with the step number that was just completed (see the
    # save_checkpoint calls below, which happen before `step += 1`), so resuming
    # must continue from the next step, not repeat the one already trained.
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
        # NOTE: find_unused_parameters=True is required because LayerDrop may skip
        # entire layers, leaving their params without gradients. This adds overhead.
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
                avg_loss = running_loss / max(1, steps_since_log)
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
             device_type: str, amp_dtype: torch.dtype, use_amp: bool, eval_iters: int) -> float:
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
    cfg = TensegrityConfig(**ckpt["config"])
    model = TensegrityLM(cfg)
    model.load_state_dict(ckpt["model_state_dict"])
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
    cfg = TensegrityConfig(**ckpt["config"])
    model = TensegrityLM(cfg)
    model.load_state_dict(ckpt["model_state_dict"])
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
                   help="Apply a named model-size preset (e.g. base15m, ~15M params) "
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


def main():
    parser = argparse.ArgumentParser(description="TensegrityLM — byte-level LM, single-file implementation")
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