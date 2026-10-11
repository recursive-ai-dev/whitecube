"""Deterministic, document-grouped splits for staged uint16 token streams."""
import hashlib
import json
from contextlib import ExitStack
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
    from corpus_index import DocumentIndex
    output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as resources:
        if isinstance(records, DocumentIndex):
            index = records
        else:
            temporary = resources.enter_context(tempfile.TemporaryDirectory(prefix='.index-', dir=output.parent))
            index = resources.enter_context(DocumentIndex(Path(temporary) / 'documents.sqlite', seed, stop_requested))
            for record in records:
                check_cancelled(stop_requested)
                index.append(record)
        total = index.total
        if combined.stat().st_size != total * 2:
            raise ValueError('Token stream size does not match document records (uint16 requires two bytes per token)')
        n_val = index.select_validation(val_fraction, block_size + 2)
        _write_splits(combined, output, index, stop_requested)
    return total - n_val, n_val


def _write_splits(combined, output, index, stop_requested):
    for name in ('train.bin', 'val.bin', 'documents.jsonl'):
        if (output / name).exists() or (output / name).is_symlink():
            raise FileExistsError(f'Split output already exists: {output / name}')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.split-', dir=output) as temporary:
        stage = Path(temporary)
        with (combined.open('rb') as source, (stage / 'train.bin').open('wb') as train,
              (stage / 'val.bin').open('wb') as val,
              (stage / 'documents.jsonl').open('w', encoding='utf-8') as documents):
            positions = {'train': 0, 'val': 0}
            for offset, count, key, split, path, row, truncated, source_key, group_key in index.split_records():
                check_cancelled(stop_requested)
                source.seek(offset * 2)
                dest = val if split == 'val' else train
                documents.write(json.dumps({'split': split, 'offset': positions[split], 'tokens': count,
                    'sha256': key, 'source_sha256': source_key, 'group_sha256': group_key, 'source': path, 'row': row,
                    'truncated': bool(truncated)}, ensure_ascii=False) + '\n')
                positions[split] += count
                remaining = count * 2
                while remaining:
                    check_cancelled(stop_requested)
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise RuntimeError('Unexpected end of token stream')
                    dest.write(chunk)
                    remaining -= len(chunk)
        check_cancelled(stop_requested)
        for name in ('train.bin', 'val.bin', 'documents.jsonl'):
            os.replace(stage / name, output / name)
