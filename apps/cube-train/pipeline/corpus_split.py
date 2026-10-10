"""Deterministic, document-grouped splits for staged uint16 token streams."""
import hashlib
from pathlib import Path


def document_key(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def split_documents(combined, output, records, *, val_fraction=0.05, block_size=256, seed=1337):
    """Keep matching document hashes together; records are (offset, count, hash)."""
    groups = {}
    for offset, count, key in records:
        groups[key] = groups.get(key, 0) + count
    total = sum(groups.values())
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
        count = groups[key]
        if total - n_val - count < minimum:
            continue
        if n_val < minimum or abs(n_val + count - target) < abs(n_val - target):
            selected.add(key)
            n_val += count
    if min(n_val, total - n_val) < minimum:
        raise ValueError('Whole-document splits are too short for this context; supply more documents '
                         'or increase the token cap. No document will be split across train and validation.')
    output = Path(output)
    with open(combined, 'rb') as source, (output / 'train.bin').open('wb') as train, (output / 'val.bin').open('wb') as val:
        for offset, count, key in records:
            source.seek(offset * 2)
            dest = val if key in selected else train
            remaining = count * 2
            while remaining:
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise RuntimeError('Unexpected end of token stream')
                dest.write(chunk)
                remaining -= len(chunk)
    return total - n_val, n_val
