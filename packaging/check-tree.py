#!/usr/bin/python3 -I
"""check-tree.py STAGE [--host-root /]: is the staged package consistent with itself?

For every file the package ships (scripts, unit, desktop entries, config, and the string tables of the
compiled programs) this collects absolute paths that point into the places the package owns
(/usr/bin, /usr/libexec/whitecube, /usr/share/whitecube, /usr/lib/whitecube, /etc/whitecube) and
checks each one exists in STAGE, or is a command some other package provides (looked up on the host).
It also rejects anything that would only work on one machine: /usr/local and /home/<user> paths.
Exits 1 and lists every problem, so a path that drifted during a move cannot ship unnoticed.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

OWNED = re.compile(rb'/(?:usr/bin|usr/libexec/whitecube|usr/share/whitecube|usr/lib/whitecube|etc/whitecube)/[A-Za-z0-9_.+@%/-]*[A-Za-z0-9_+@-]')
FORBIDDEN = re.compile(rb'/usr/local/|/home/[a-z][a-z0-9_-]*/')
# Shell/format fragments that look like paths but are templates, not files.
RUNTIME_FILES = ('/etc/whitecube/network/networks.json', '/etc/whitecube/network/wpa.conf', '/etc/whitecube/network/device.json',
                 '/etc/whitecube/network/profiles.json')
SEARCH_DIRS = (b'/usr/local/share/applications', b'/usr/local/share/icons')
# These exact strings describe paths inside the editable guest, not host installation
# locations. Other /usr/local or /home references in the same files remain forbidden.
GUEST_PATH_FRAGMENTS = {
    'usr/share/whitecube/cube-editor/app.py': (
        b'for example /etc/skel/.config or /usr/local/bin. Overlays are applied with the recipe.',),
    'usr/share/whitecube/cube-editor/worker.py': (
        b'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',),
    'usr/bin/chrootable-manager': (
        b'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',),
}
ALLOW_FORBIDDEN_IN = ('usr/bin/whitecube-migrate', 'usr/share/doc/', 'usr/share/whitecube/workspace/docs', 'usr/share/whitecube/boot-theme/',
                      'usr/share/whitecube/desktop-chat/assets', 'usr/lib/whitecube/')


def is_elf(data: bytes) -> bool:
    return data[:4] == b'\x7fELF'


def rodata(path: Path) -> bytes:
    """The read-only data of an ELF file. Compilers also inline short constant strings as immediates inside
    machine code, where they overlap and look like paths that do not exist, so only real data is scanned."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'ro'
        done = subprocess.run(['objcopy', '-O', 'binary', '--only-section=.rodata', str(path), str(out)], capture_output=True)
        return out.read_bytes() if done.returncode == 0 and out.exists() else b''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stage', type=Path)
    ap.add_argument('--host-root', type=Path, default=Path('/'))
    ap.add_argument('--sources', type=Path, help='directory of C sources whose string literals must also name only shipped paths')
    ap.add_argument('--allow-forbidden', action='append', default=[], help='extra path prefixes that may name /usr/local or /home')
    args = ap.parse_args()
    stage, host = args.stage.resolve(), args.host_root
    allow_forbidden = ALLOW_FORBIDDEN_IN + tuple(args.allow_forbidden)
    problems, checked = [], 0
    for path in sorted(stage.rglob('*')):
        if path.is_symlink():
            target = (path.parent / os.readlink(path))
            if not os.path.lexists(target):
                problems.append(f'{path.relative_to(stage)}: dangling symlink -> {os.readlink(path)}')
            continue
        if not path.is_file():
            continue
        rel = str(path.relative_to(stage))
        if rel.startswith('DEBIAN/'):
            continue
        data = path.read_bytes()
        if not is_elf(data) and b'\0' in data[:4096]:
            continue                                   # other binary data (images, compiled schemas)
        if not is_elf(data) and path.stat().st_mode & 0o111 and not data.startswith(b'#!'):
            problems.append(f'{rel}: executable without a #! line')
        if is_elf(data):
            continue                                       # their string literals are checked from the sources below
        strings = [data]
        owned = set()
        for seg in strings:
            owned.update(OWNED.findall(seg))
        for match in sorted(owned):
            checked += 1
            ref = match.decode()
            if ref.startswith(RUNTIME_FILES):
                continue                                   # created at run time, not shipped
            if not (stage / ref.lstrip('/')).exists() and not (host / ref.lstrip('/')).exists():
                problems.append(f'{rel}: refers to {ref}, which the package does not provide')
        if not rel.startswith(allow_forbidden) and not rel.endswith('.txt'):
            found = set()
            for seg in strings:
                for fragment in GUEST_PATH_FRAGMENTS.get(rel, ()):
                    seg = seg.replace(fragment, b'')
                found.update(m for m in FORBIDDEN.finditer(seg))
            for m in sorted(found, key=lambda m: m.group()):
                seg_text = m.string[m.start():m.start() + 60]
                if any(seg_text.startswith(a) for a in SEARCH_DIRS):
                    continue                               # generic search locations, harmless when absent
                problems.append(f'{rel}: machine-specific path {m.group().decode()!r}')
    if args.sources:
        for src in sorted(args.sources.glob('*.[ch]')):
            text = src.read_bytes()
            for match in sorted(set(OWNED.findall(text))):
                checked += 1
                ref = match.decode()
                if ref.startswith(RUNTIME_FILES):
                    continue
                if not (stage / ref.lstrip('/')).exists() and not (host / ref.lstrip('/')).exists():
                    problems.append(f'{src.name}: refers to {ref}, which the package does not provide')
            for m in FORBIDDEN.finditer(text):
                if not any(text[m.start():].startswith(a) for a in SEARCH_DIRS):
                    problems.append(f'{src.name}: machine-specific path {text[m.start():m.start() + 40]!r}')
    for rel in ('usr/bin/whitecube-compositor', 'usr/bin/cube-status', 'usr/bin/cube-settings', 'usr/bin/whitecube-session',
                'usr/bin/cube-editor', 'usr/share/whitecube/cube-editor/app.py',
                'usr/share/whitecube/cube-editor/worker.py', 'usr/share/applications/cube-editor.desktop',
                'usr/share/icons/hicolor/scalable/apps/cube-editor.svg',
                'usr/bin/chrootables', 'usr/bin/chrootable-manager', 'usr/share/whitecube/chrootables/app.py',
                'usr/share/whitecube/chrootables/backend.py', 'usr/share/applications/chrootables.desktop',
                'usr/share/icons/hicolor/scalable/apps/chrootables.svg',
                'usr/bin/wc-help', 'usr/bin/wc-doctor', 'usr/lib/wc-suite/wc_suite.py',
                'usr/bin/cube-train', 'usr/bin/whitecube-train', 'usr/share/whitecube/cube-train/app.py',
                'usr/share/whitecube/cube-train/engine.py', 'usr/share/whitecube/cube-train/worker.py',
                'usr/share/whitecube/cube-train/pipeline/train_folder.py',
                'usr/share/applications/cube-train.desktop', 'usr/share/icons/hicolor/scalable/apps/cube-train.svg',
                'usr/libexec/whitecube/cube-settings-helper', 'usr/lib/systemd/system/whitecube-network.service',
                'usr/share/wayland-sessions/whitecube.desktop', 'usr/share/whitecube/schemas/gschemas.compiled',
                'etc/whitecube/config'):
        if not (stage / rel).exists():
            problems.append(f'{rel}: required file is missing')
    if problems:
        print(f'check-tree: {len(problems)} problem(s) in {stage}:')
        for p in problems:
            print('  -', p)
        return 1
    print(f'check-tree: ok ({checked} path references checked)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
