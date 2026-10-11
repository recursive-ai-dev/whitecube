"""Validate and reuse self-contained prepared corpora without training imports."""
from array import array
import json
from pathlib import Path
import re
import sys
import tempfile

from corpus_split import check_cancelled
from files import atomic_json, file_sha256, publish_directory

ARTIFACTS = ('train.bin', 'val.bin', 'documents.jsonl')


def corpus_metadata(directory):
    path = Path(directory) / 'corpus.json'
    with path.open('rb') as handle:
        raw = handle.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError(f'{path} exceeds the 16 MiB metadata limit')
    def invalid(value):
        raise ValueError(f'{path} contains nonfinite JSON: {value}')
    manifest = json.loads(raw, parse_constant=invalid)
    if not isinstance(manifest, dict):
        raise ValueError(f'{path} must contain a corpus metadata object')
    for name in ('train_tokens', 'val_tokens', 'total_tokens', 'document_records', 'unique_document_groups'):
        if name in manifest and (isinstance(manifest[name], bool) or not isinstance(manifest[name], int) or manifest[name] < 0):
            raise ValueError(f'{path} {name} must be a nonnegative integer')
    if (manifest.get('token_dtype', 'uint16') != 'uint16'
            or manifest.get('byte_order', sys.byteorder) != sys.byteorder
            or manifest.get('token_bytes', 2) != 2):
        raise ValueError(f'{path} uses an incompatible token format or byte order')
    artifacts = manifest.get('artifacts')
    if artifacts is not None:
        if not isinstance(artifacts, dict) or set(artifacts) - set(ARTIFACTS):
            raise ValueError(f'{path} artifacts must be an object of supported corpus files')
        for name in ('train.bin', 'val.bin'):
            record = artifacts.get(name)
            if (not isinstance(record, dict) or not isinstance(record.get('sha256'), str)
                    or not re.fullmatch('[0-9a-f]{64}', record['sha256']) or isinstance(record.get('bytes'), bool)
                    or not isinstance(record.get('bytes'), int) or record['bytes'] < 0):
                raise ValueError(f'{directory}/{name} is missing a valid corpus integrity record or token file')
    if manifest.get('document_index') is not None:
        if manifest['document_index'] != 'documents.jsonl' or not artifacts or 'documents.jsonl' not in artifacts:
            raise ValueError(f'{path} has an invalid document index integrity record')
    return manifest


def verify_corpus(directory, *, minimum=257, vocab_size=259, stop_requested=None):
    """Validate all declared artifacts and stream token checks with bounded RAM."""
    directory = Path(directory)
    manifest = corpus_metadata(directory) if (directory / 'corpus.json').is_file() else {}
    for name, expected in manifest.get('artifacts', {}).items():
        check_cancelled(stop_requested)
        path = directory / name
        if (not isinstance(expected, dict) or not path.is_file()
                or path.stat().st_size != expected.get('bytes')
                or file_sha256(path, stop_requested) != expected.get('sha256')):
            raise ValueError(f'{path} failed corpus integrity verification; restore the prepared corpus or start a new run')
    counts = {}
    for split in ('train', 'val'):
        path = directory / f'{split}.bin'
        if not path.is_file() or path.stat().st_size // 2 < minimum:
            raise ValueError(f'{path} must contain at least {minimum} tokens')
        if path.stat().st_size % 2:
            raise ValueError(f'{path} has an odd byte length; uint16 token data is truncated')
        counts[f'{split}_tokens'] = path.stat().st_size // 2
        with path.open('rb') as handle:
            while chunk := handle.read(1024 * 1024):
                check_cancelled(stop_requested)
                if len(chunk) % 2:
                    raise ValueError(f'{path} changed or has truncated uint16 token data')
                tokens = array('H')
                tokens.frombytes(chunk)
                if max(tokens) >= vocab_size:
                    raise ValueError(f'{path} contains token IDs outside the vocabulary (0-{vocab_size - 1})')
    counts['total_tokens'] = sum(counts.values())
    for name, count in counts.items():
        if name in manifest and (isinstance(manifest[name], bool) or manifest[name] != count):
            raise ValueError(f'{directory}/corpus.json {name} does not match the token files')
    return {**manifest, **counts}


def reuse_corpus(source, output, *, block_size=256, stop_requested=None, progress_callback=None):
    """Copy, verify and publish an independent corpus; never alias live run files."""
    source, output = Path(source), Path(output)
    corpus_metadata(source)  # Fail on malformed metadata before copying.
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'Prepared corpus already exists: {output}')
    names = ['corpus.json', 'train.bin', 'val.bin']
    if (source / 'documents.jsonl').is_file():
        names.append('documents.jsonl')
    snapshots = {name: (source / name).stat() for name in names}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reuse-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'corpus'
        stage.mkdir()
        for name in names:
            with (source / name).open('rb') as incoming, (stage / name).open('wb') as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    check_cancelled(stop_requested)
                    outgoing.write(chunk)
                    if progress_callback:
                        progress_callback({'phase': 'reusing', 'path': str(source / name)})
        if progress_callback:
            progress_callback({'phase': 'verifying', 'path': str(source)})
        manifest = verify_corpus(stage, minimum=block_size + 1, stop_requested=stop_requested)
        for name, previous in snapshots.items():
            current = (source / name).stat()
            if (previous.st_size, previous.st_mtime_ns, previous.st_ino) != (current.st_size, current.st_mtime_ns, current.st_ino):
                raise ValueError(f'Prepared corpus changed during reuse: {source / name}')
        manifest.update(reused_from=str(source.resolve()), reuse_verified=True,
                        artifacts={name: {'bytes': (stage / name).stat().st_size,
                                          'sha256': file_sha256(stage / name, stop_requested)}
                                   for name in names if name != 'corpus.json'})
        atomic_json(stage / 'corpus.json', manifest)
        if progress_callback:
            progress_callback({'phase': 'publishing', 'tokens': manifest['total_tokens'],
                               'documents': manifest.get('document_records', 0)})
        check_cancelled(stop_requested)
        publish_directory(stage, output)
    print(f"Reused corpus: {manifest['train_tokens']:,} training + {manifest['val_tokens']:,} validation tokens", flush=True)
    return manifest
