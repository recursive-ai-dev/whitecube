#!/usr/bin/env python3
"""Cube Studio adaptation: explicitly fetch pinned x86_64 AppImage tools."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import stat
import tempfile
from urllib.request import Request, urlopen

TOOLS = {
    "appimagetool-1.9.1-x86_64.AppImage": (
        "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage",
        "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"),
    # Runtime built from fedfad51acc2860b2916a1eef32ba4a500ff31ef. The
    # checksum prevents a moving upstream 'continuous' asset changing our build.
    "runtime-x86_64": (
        "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64",
        "617aa77314d57c8b8e4fae9c3f0b35af7b30d5529cb69e98f64f92510ed98a46"),
}
MAX_TOOL_BYTES = 64 * 1024 * 1024


def validate_cached_tool(path, expected):
    """Validate through one descriptor without following links or blocking on FIFOs."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ValueError(f"Cannot safely read cached build tool: {path}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TOOL_BYTES:
            raise ValueError(f"Build tool must be a regular file of at most {MAX_TOOL_BYTES} bytes: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_TOOL_BYTES:
                    raise ValueError(f"Cached build tool exceeded the size limit: {path}")
                digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"Existing build tool failed checksum validation: {path}")
            os.fchmod(descriptor, 0o755)
    finally:
        os.close(descriptor)
    return True


def fetch(output):
    if platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("These pinned release tools support x86_64 hosts only.")
    output.mkdir(parents=True, exist_ok=True)
    for filename, (url, expected) in TOOLS.items():
        destination = output / filename
        if validate_cached_tool(destination, expected):
            print(destination)
            continue
        with tempfile.NamedTemporaryFile(prefix=".download-", dir=output, delete=False) as stream:
            staged = Path(stream.name)
            try:
                request = Request(url, headers={"User-Agent": "Chrootables-release-builder"})
                with urlopen(request, timeout=60) as response:
                    digest, size = hashlib.sha256(), 0
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_TOOL_BYTES:
                            raise ValueError("Build tool exceeded the expected size limit.")
                        digest.update(chunk)
                        stream.write(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError(f"Downloaded {filename} failed SHA256 validation; no tool was published.")
                stream.flush()
                staged.chmod(0o755)
                os.fsync(stream.fileno())
                # link() publishes atomically only if the name is still unused.
                # A competing downloader may win; accept only the same pinned tool.
                try:
                    os.link(staged, destination)
                except FileExistsError:
                    if not validate_cached_tool(destination, expected):
                        raise ValueError(f"Build tool changed during publication: {destination}")
                descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                print(destination)
            finally:
                staged.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        fetch(arguments.output.resolve())
    except (OSError, ValueError) as error:
        parser.exit(1, f"Build tool download failed: {error}\n")
