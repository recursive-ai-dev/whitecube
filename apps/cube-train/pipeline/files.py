"""Atomic metadata writes shared by the trainer and the native desktop app."""
import json
import hashlib
import os
import tempfile
from pathlib import Path


def fsync_directory(path):
    """Persist directory entry changes after atomically replacing a file."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2, allow_nan=False, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def file_sha256(path, stop_requested=None):
    """Hash large artifacts without loading them into memory."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while True:
            if stop_requested and stop_requested():
                raise InterruptedError('Corpus preparation stopped.')
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def publish_directory(stage, output):
    """Durably publish a prepared directory of regular files together."""
    stage, output = Path(stage), Path(output)
    for path in stage.iterdir():
        with path.open('rb') as source:
            os.fsync(source.fileno())
    fsync_directory(stage)
    output.mkdir()  # Fail safely if another job claimed the destination.
    try:
        os.replace(stage, output)
        fsync_directory(output.parent)
    except BaseException:
        # Only remove the empty directory that we reserved, never foreign files.
        try:
            output.rmdir()
        except OSError:
            pass
        raise
