"""Deterministic, document-grouped splits for staged uint16 token streams."""
import hashlib
import math
import numbers
import os
from pathlib import Path
import tempfile


def document_key(text, stop_requested=None):
    digest = hashlib.sha256()
    for offset in range(0, len(text), 65536):
        check_cancelled(stop_requested)
        digest.update(text[offset:offset + 65536].encode('utf-8'))
    return digest.hexdigest()


def positive_integer(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, numbers.Integral) or value < minimum:
        raise ValueError(f'{name} must be an integer of at least {minimum:,}')


def validate_split_options(val_fraction, block_size, seed):
    positive_integer(block_size, 'Block size')
    if (isinstance(val_fraction, bool) or not isinstance(val_fraction, numbers.Real)
            or not math.isfinite(val_fraction) or not 0 < val_fraction < 1):
        raise ValueError('Validation fraction must be between 0 and 1')
    if isinstance(seed, bool) or not isinstance(seed, numbers.Integral):
        raise ValueError('Split seed must be an integer')


def utf8_prefix(text, byte_limit):
    """Keep the cap on bytes while preserving complete UTF-8 characters."""
    prefix = text[:byte_limit]
    encoded = prefix.encode('utf-8')
    raw = encoded[:byte_limit]
    if len(raw) < len(encoded):
        raw = raw.decode('utf-8', errors='ignore').encode('utf-8')
    return raw, len(prefix) < len(text) or len(raw) < len(encoded)


def check_cancelled(stop_requested):
    if stop_requested and stop_requested():
        raise InterruptedError('Corpus preparation stopped.')


def split_documents(combined, output, records, *, val_fraction=0.05, block_size=256, seed=1337,
                    stop_requested=None):
    """Keep matching document hashes together; records are (offset, count, hash)."""
    validate_split_options(val_fraction, block_size, seed)
    check_cancelled(stop_requested)
    combined, output = Path(combined), Path(output)
    records = list(records)
    groups, expected_offset = {}, 0
    for offset, count, key in records:
        check_cancelled(stop_requested)
        positive_integer(count, 'Document token count')
        if isinstance(offset, bool) or not isinstance(offset, numbers.Integral) or offset != expected_offset:
            raise ValueError('Document offsets must cover the token stream consecutively without gaps or overlaps')
        if not isinstance(key, str) or not key:
            raise ValueError('Document hashes must be nonempty strings')
        expected_offset += count
        groups[key] = groups.get(key, 0) + count
    total = sum(groups.values())
    if combined.stat().st_size != total * 2:
        raise ValueError('Token stream size does not match document records (uint16 requires two bytes per token)')
    target = total * val_fraction
    minimum = block_size + 2
    if len(groups) < 2:
        raise ValueError('Document-level validation needs at least two distinct documents; '
                         'provide more JSONL/Parquet rows or separate text files.')
    ordered = sorted(groups, key=lambda key: hashlib.sha256(f'{seed}:{key}'.encode()).digest())
    # Start with a group close to the target, avoiding a giant first document
    # swallowing validation when smaller, suitable documents exist.
    ordered.sort(key=lambda key: abs(groups[key] - target))
    selected, n_val = set(), 0
    for key in ordered:
        check_cancelled(stop_requested)
        count = groups[key]
        if total - n_val - count < minimum:
            continue
        if n_val < minimum or abs(n_val + count - target) < abs(n_val - target):
            selected.add(key)
            n_val += count
    if min(n_val, total - n_val) < minimum:
        raise ValueError('Whole-document splits are too short for this context; supply more documents '
                         'or increase the token cap. No document will be split across train and validation.')
    for name in ('train.bin', 'val.bin'):
        if (output / name).exists() or (output / name).is_symlink():
            raise FileExistsError(f'Split output already exists: {output / name}')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.split-', dir=output) as temporary:
        stage = Path(temporary)
        with combined.open('rb') as source, (stage / 'train.bin').open('wb') as train, (stage / 'val.bin').open('wb') as val:
            for offset, count, key in records:
                source.seek(offset * 2)
                dest = val if key in selected else train
                remaining = count * 2
                while remaining:
                    check_cancelled(stop_requested)
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise RuntimeError('Unexpected end of token stream')
                    dest.write(chunk)
                    remaining -= len(chunk)
        check_cancelled(stop_requested)
        for name in ('train.bin', 'val.bin'):
            os.replace(stage / name, output / name)
    return total - n_val, n_val
