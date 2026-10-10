#!/usr/bin/env python3
"""Install per-user wc commands atomically, with backups and checked rollback."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import sys
import uuid

import wc_suite as suite


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".wc-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def completion_data(shell):
    import contextlib
    import io
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        suite.completion(argparse.Namespace(shell=shell))
    return stream.getvalue().encode()


def payloads(home):
    source = Path(__file__).resolve().parent
    library = home / ".local/lib/wc-suite"
    suite.BIN = home / ".local/bin"
    data = []
    for name in ("wc_suite.py", "install.py", "test_suite.py", "README.txt"):
        data.append((library / name, (source / name).read_bytes(), 0o644))
    for name, spec in sorted(suite.COMMANDS.items()):
        path = suite.BIN / f"wc-{name}"
        wrapper = ("#!/bin/sh\n# desc: " + spec["summary"] + "\n"
                   'command -v python3 >/dev/null 2>&1 || { echo "wc-suite requires Python 3" >&2; exit 127; }\n'
                   'wc_bin_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
                   'PATH="$wc_bin_dir:${PATH:-/usr/local/bin:/usr/bin:/bin}"\nexport PATH\n'
                   f'exec python3 "$wc_bin_dir/../lib/wc-suite/wc_suite.py" "{name}" "$@"\n')
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() and not path.is_symlink() else 0o755
        data.append((path, wrapper.encode(), mode | stat.S_IXUSR))
    bash = completion_data("bash")
    for name in suite.COMMANDS:
        data.append((home / f".local/share/bash-completion/completions/wc-{name}", bash, 0o644))
    data.append((home / ".local/share/zsh/site-functions/_wc", completion_data("zsh"), 0o644))
    fish = completion_data("fish").decode().splitlines()
    for name in suite.COMMANDS:
        lines = [line for line in fish if line.startswith(f"complete -c wc-{name} ")]
        data.append((home / f".config/fish/completions/wc-{name}.fish", ("\n".join(lines) + "\n").encode(), 0o644))
    return data


def restore(backup, home, *, check=True):
    manifest = json.loads((backup / "manifest.json").read_text())
    if manifest["home"] != str(home):
        raise ValueError("backup belongs to a different installation home")
    entries = manifest["files"]
    # Check every destination before changing any file.
    for entry in entries:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in (".local", ".config"):
            raise ValueError("unsafe path in backup manifest")
        path = home / relative
        if check and (path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != entry["installed_sha256"]):
            raise ValueError(f"file changed since installation; rollback stopped to preserve it: {path}")
        if entry["existed"] and not (backup / relative).exists() and not (backup / relative).is_symlink():
            raise ValueError(f"missing original backup: {relative}")
    for entry in reversed(entries):
        relative = Path(entry["path"])
        path = home / relative
        original = backup / relative
        if entry["existed"]:
            if original.is_symlink():
                path.unlink(missing_ok=True)
                path.symlink_to(os.readlink(original))
            else:
                write(path, original.read_bytes(), entry["mode"])
        else:
            path.unlink(missing_ok=True)


def install(home):
    data = payloads(home)  # Build everything before modifying installed files.
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    state = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state")))
    backup = suite.private_dir(state / "wc-suite/backups" / stamp)
    manifest = dict(version=suite.VERSION, home=str(home), files=[])
    for path, content, mode in data:
        relative = path.relative_to(home)
        existed = path.exists() or path.is_symlink()
        if existed:
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, target, follow_symlinks=False)
        manifest["files"].append(dict(path=str(relative), existed=existed,
                                      mode=stat.S_IMODE(path.stat().st_mode) if existed and not path.is_symlink() else mode,
                                      installed_sha256=digest(content)))
    suite.write_json(backup / "manifest.json", manifest)
    written = []
    try:
        for path, content, mode in data:
            write(path, content, mode)
            written.append(path.relative_to(home))
    except BaseException:
        # Restore only files this install already touched.
        subset = {str(p) for p in written}
        temporary_manifest = dict(manifest, files=[e for e in manifest["files"] if e["path"] in subset])
        suite.write_json(backup / "manifest.json", temporary_manifest)
        restore(backup, home, check=False)
        suite.write_json(backup / "manifest.json", manifest)
        raise
    print(f"Installed {len(suite.COMMANDS)} commands into {home / '.local/bin'}")
    print(f"Backup: {backup}")
    print(f"Rollback: python3 {home / '.local/lib/wc-suite/install.py'} --restore {backup}")
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home(), help="installation home directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--restore", type=Path, metavar="BACKUP", help="restore if installed files have not been edited")
    mode.add_argument("--dry-run", action="store_true", help="list planned destinations")
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    try:
        if args.restore:
            restore(args.restore.expanduser().resolve(), home)
            print("Restored previous installation")
        elif args.dry_run:
            for path, _, _ in payloads(home):
                print(path)
        else:
            install(home)
    except (OSError, ValueError, suite.Error) as error:
        print(f"wc-suite install: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
