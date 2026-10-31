"""Validate shared workspace-store paths before crossing process boundaries."""
from __future__ import annotations

import os
from pathlib import Path


def store_path(value):
    """Return a canonical absolute store, refusing root aliases and unsafe text.

    A missing store is allowed: reading it must not create it. The final path
    must not be a symlink, consistent with the manager's mutation checks.
    """
    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise ValueError("Store must be an absolute directory other than /.") from error
    if (not isinstance(raw, str) or not raw
            or any(ord(character) < 32 or ord(character) == 127 for character in raw)):
        raise ValueError("Store paths must not contain control characters.")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("Store must be an absolute directory other than /.")
    try:
        if candidate.is_symlink():
            raise ValueError("The workspace store must not be a symlink.")
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("The workspace store path could not be resolved.") from error
    if resolved == Path("/"):
        raise ValueError("Store must be an absolute directory other than /.")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("The workspace store must be a directory.")
    return resolved
