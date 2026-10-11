"""Metrics and token estimation for prompt content."""

import re
from dataclasses import dataclass


@dataclass
class TextMetrics:
    characters: int = 0
    words: int = 0
    lines: int = 0
    estimated_tokens: int = 0


def calculate_metrics(text: str) -> TextMetrics:
    """Calculate character, word, line counts and estimated tokens for a string."""
    if not text:
        return TextMetrics()

    chars = len(text)
    words = len(re.findall(r"\b\w+\b", text))
    lines = len(text.splitlines()) or (1 if text else 0)

    # Heuristic estimation for BPE tokenizers (OpenAI cl100k_base / Llama / Claude):
    # - Average English text is ~4 characters per token
    # - Code / structured text is often ~3.3 characters per token
    # - Punctuation and whitespace splits also add tokens
    code_pattern_count = len(re.findall(r"[{}\[\]()<>=+\-*/;:.,#$!&|`~]", text))
    base_token_estimate = max(1, int(chars / 3.8 + code_pattern_count * 0.15))

    return TextMetrics(
        characters=chars,
        words=words,
        lines=lines,
        estimated_tokens=base_token_estimate,
    )


def count_bpe_tokens(text: str) -> int:
    """Convenience function returning estimated BPE token count."""
    return calculate_metrics(text).estimated_tokens
