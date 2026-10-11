#!/usr/bin/env python3
"""Install or restore Cube Player's user launcher, desktop entry, and icon."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get('CUBE_PLAYER_BUILD', ROOT / 'build'))

def digest(data):
    return hashlib.sha256(data).hexdigest()

def desktop_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'

def entries(home):
    binary = BUILD / 'cube-player'
    launcher = home / '.local/bin/cube-player'
    icon = home / '.local/share/icons/hicolor/scalable/apps/cube-player.svg'
    desktop = home / '.local/share/applications/cube-player.desktop'
    wrapper = '#!/bin/sh\nexec ' + shlex.quote(str(binary)) + ' "$@"\n'
    entry = '\n'.join([
        '[Desktop Entry]', 'Type=Application', 'Version=1.0', 'Name=Cube Player',
        'GenericName=Media Player', 'Comment=Your Whitecube space for films and music',
        'Exec=' + desktop_quote(launcher) + ' %U', 'Icon=cube-player',
        'Terminal=false', 'StartupNotify=true', 'StartupWMClass=cube-player',
        'Categories=AudioVideo;Player;Audio;Video;', 'Keywords=Whitecube;mpv;film;music;media;',
        'MimeType=video/mp4;video/x-matroska;video/webm;video/quicktime;video/x-msvideo;video/mpeg;video/ogg;audio/mpeg;audio/flac;audio/x-flac;audio/ogg;audio/opus;audio/mp4;audio/x-wav;audio/wav;audio/aac;audio/x-mpegurl;application/x-mpegurl;application/vnd.apple.mpegurl;', '',
    ])
    return [(launcher, wrapper.encode(), 0o755), (icon, (ROOT / 'assets/cube-player.svg').read_bytes(), 0o644), (desktop, entry.encode(), 0o644)]

def atomic_write(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--restore', type=Path)
    args = parser.parse_args()
    home = args.home.resolve()
    owned = {str(p): (data, mode) for p, data, mode in entries(home)}
    if args.restore:
        manifest = json.loads(args.restore.read_text())
        for item in manifest['files']:
            path = Path(item['path'])
            if str(path) not in owned or path.is_symlink():
                raise SystemExit('Restore refused: unowned path or symlink: ' + str(path))
            if not path.is_file() or digest(path.read_bytes()) != item['installed_sha256']:
                raise SystemExit('Restore refused: file changed since installation: ' + str(path))
        for item in manifest['files']:
            if item['previous'] is not None:
                previous = args.restore.parent / item['previous']
                if previous.parent != args.restore.parent or previous.is_symlink() or digest(previous.read_bytes()) != item['previous_sha256']:
                    raise SystemExit('Restore refused: invalid backup')
        for item in manifest['files']:
            path = Path(item['path'])
            print('Restore', path)
            if args.apply:
                if item['previous'] is None:
                    path.unlink()
                else:
                    previous = args.restore.parent / item['previous']
                    atomic_write(path, previous.read_bytes(), item['mode'])
        if args.apply and shutil.which('update-desktop-database'):
            subprocess.run(['update-desktop-database', str(home / '.local/share/applications')], check=True)
        return
    if not (BUILD / 'cube-player').is_file():
        raise SystemExit('Build Cube Player with ./BUILD.sh first.')
    files = []
    backup = home / '.local/state/cube-player/install-backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()))
    plan = entries(home)
    for path, _, _ in plan:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SystemExit('Install refused: destination is not a regular file: ' + str(path))
    for i, (path, data, mode) in enumerate(plan):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SystemExit('Install refused: destination is not a regular file: ' + str(path))
        print('Install', path)
        record = {'path': str(path), 'installed_sha256': digest(data), 'previous': None}
        if args.apply:
            if path.exists():
                old = path.read_bytes()
                record.update(previous=f'{i}.bak', previous_sha256=digest(old), mode=path.stat().st_mode & 0o777)
                atomic_write(backup / record['previous'], old, 0o600)
        files.append(record)
    if args.apply:
        atomic_write(backup / 'manifest.json', json.dumps({'files': files}, indent=2).encode() + b'\n', 0o600)
        for path, data, mode in plan:
            atomic_write(path, data, mode)
        if shutil.which('update-desktop-database'):
            subprocess.run(['update-desktop-database', str(home / '.local/share/applications')], check=True)
        print('Restore manifest:', backup / 'manifest.json')
    else:
        print('Use --apply to install. Media defaults are not changed.')

if __name__ == '__main__':
    main()
