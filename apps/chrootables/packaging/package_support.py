"""Cube Studio adaptation of upstream Chrootables rootless release utilities."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EPOCH = 1793404800  # 2026-10-31 00:00:00 UTC
APPLICATION_ID = "cube-chrootables-studio"
PACKAGE = APPLICATION_ID
PRIVATE = "usr/lib/" + PACKAGE
MANAGER_ALIAS = "cube-chrootables-manager"
EXECUTABLES = {PRIVATE + "/chrootable-manager", PRIVATE + "/" + PACKAGE}
MODULES = ("__init__.py", "app.py", "authenticate.py", "backend.py", "broker.py",
           "capture.py", "client.py", "paths.py", "style.py", "terminal.py")
ASSETS = ("demo.json", "io.github.chrootables.Desktop.svg")


def engine_version(root=ROOT):
    match = re.search(r"^APP_VERSION=([0-9][0-9A-Za-z.+~]*)$",
                      (root / "chrootable-manager").read_text(), re.MULTILINE)
    if not match:
        raise ValueError("The bundled engine has no valid APP_VERSION.")
    return match[1]


def revision(root=ROOT):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def stage_payload(stage, epoch, root=ROOT):
    """One explicit runtime allowlist shared by every Studio format."""
    application = stage / PRIVATE
    (application / "chrootables").mkdir(parents=True)
    (application / "assets").mkdir()
    for module in MODULES:
        shutil.copyfile(root / "chrootables" / module, application / "chrootables" / module)
    for asset in ASSETS:
        shutil.copyfile(root / "assets" / asset, application / "assets" / asset)
    launcher = (root / "packaging" / PACKAGE).read_text(encoding="utf-8")
    (application / PACKAGE).write_text(launcher, encoding="utf-8")
    manager = (root / "chrootable-manager").read_text(encoding="utf-8")
    (application / "chrootable-manager").write_text("#!/bin/bash\n" + manager.split("\n", 1)[1], encoding="utf-8")
    (stage / "usr/bin").mkdir(parents=True)
    for public, private in ((PACKAGE, PACKAGE), (MANAGER_ALIAS, "chrootable-manager")):
        (stage / "usr/bin" / public).symlink_to(f"../lib/{PACKAGE}/{private}")
    desktop = stage / "usr/share/applications" / (APPLICATION_ID + ".desktop")
    desktop.parent.mkdir(parents=True)
    shutil.copyfile(root / "packaging" / desktop.name, desktop)
    icon = stage / "usr/share/icons/hicolor/scalable/apps" / (APPLICATION_ID + ".svg")
    icon.parent.mkdir(parents=True)
    shutil.copyfile(root / "assets/io.github.chrootables.Desktop.svg", icon)
    docs = stage / "usr/share/doc" / PACKAGE
    docs.mkdir(parents=True)
    license_identifier(root)
    shutil.copyfile(root / "LICENSE", docs / "copyright")
    for name in ("README.md", "MANAGER.md", "UPSTREAM.md", "CHANGELOG.md"):
        compressed_copy(root / name, docs / (name + ".gz"))
    compressed_copy(root / "packaging/README.md", docs / "INSTALL.md.gz")
    manual = stage / "usr/share/man/man1" / (PACKAGE + ".1.gz")
    compressed_copy(root / "packaging" / (PACKAGE + ".1"), manual)
    (manual.parent / (MANAGER_ALIAS + ".1.gz")).symlink_to(manual.name)
    provenance = {"schema_version": 1, "application_version": upstream_version(root),
                  "engine_version": engine_version(root), "source_revision": revision(root),
                  "source_date_epoch": epoch,
                  "files_sha256": {path.relative_to(stage).as_posix(): file_digest(path)
                                   for path in sorted(stage.rglob("*"))
                                   if path.is_file() and not path.is_symlink()}}
    (docs / "build-provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    normalize_tree(stage, epoch)
    return stage


def upstream_version(root=ROOT):
    """Read metadata without importing Qt or executing application code."""
    tree = ast.parse((root / "chrootables/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+~]*", value):
                return value
    raise ValueError("chrootables/__init__.py must define a valid __version__.")


def build_metadata(revision):
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9][0-9A-Za-z.+~]*", revision):
        raise ValueError("--revision must start with a digit and contain only letters, digits, ., + or ~.")
    value = os.environ.get("SOURCE_DATE_EPOCH", str(DEFAULT_EPOCH))
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("SOURCE_DATE_EPOCH must be a nonnegative integer timestamp.")
    try:
        epoch = int(value)
        date = datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("SOURCE_DATE_EPOCH is outside the supported timestamp range.") from error
    return upstream_version(), epoch, date


def require_tool(name, configured=None, hint=""):
    """Resolve only installed or explicitly supplied tools; never install/download."""
    selected = os.fspath(configured) if configured else name
    resolved = shutil.which(selected)
    if not resolved:
        raise ValueError(f"{name} is required. {hint}".strip())
    # Preserve the final executable name: rpm2cpio is a multicall symlink on RPM6.
    return os.path.abspath(resolved)


def compressed_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output,
                           compresslevel=9, mtime=0) as compressed:
            compressed.write(source.read_bytes())


def license_identifier(root=ROOT):
    metadata = root / "packaging/license.spdx"
    if not metadata.is_file():
        return "LicenseRef-Proprietary"
    expression = metadata.read_text(encoding="utf-8").strip()
    if not expression or not re.fullmatch(r"[A-Za-z0-9.+() -]+", expression):
        raise ValueError("packaging/license.spdx must contain a single SPDX license expression.")
    if not any((root / name).is_file() for name in ("LICENSE", "LICENSE.txt", "COPYING")):
        raise ValueError("License metadata requires an upstream LICENSE, LICENSE.txt or COPYING file.")
    return expression


def normalize_tree(stage, epoch, extra_executables=()):
    executable_paths = EXECUTABLES | set(extra_executables)
    for path in [stage, *sorted(stage.rglob("*"))]:
        if not path.is_symlink():
            relative = path.relative_to(stage).as_posix()
            path.chmod(0o755 if path.is_dir() or relative in executable_paths else 0o644)
        os.utime(path, (epoch, epoch), follow_symlinks=False)


def source_archive(stage, target, epoch):
    """A normalized payload archive, independent of staging paths and the umask."""
    with target.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for path in sorted(stage.rglob("*")):
                    info = archive.gettarinfo(str(path), path.relative_to(stage).as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = epoch
                    if info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)


def run_tool(command, *, env=None, cwd=None, umask=None):
    try:
        options = {} if umask is None else {"umask": umask}
        return subprocess.run(command, env=env, cwd=cwd, text=True,
                              capture_output=True, check=True, **options)
    except subprocess.CalledProcessError as error:
        details = "\n".join(part.strip() for part in (error.stdout, error.stderr) if part)
        raise ValueError(f"{Path(command[0]).name} failed ({error.returncode}):\n{details}") from error


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

