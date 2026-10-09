#!/usr/bin/env python3
"""Install tested native desktop binaries for this account, with a rollback archive.

Default: print the install plan. --apply: save originals and replace atomically.
Never stops the compositor or starts a GUI. Logout/login activates its changes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile


def atomic(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    user_dir = Path.home()
    if os.geteuid() == 0 or os.geteuid() != os.getuid():
        parser.error('run as the desktop account without sudo')
    bindir = user_dir / '.local/bin'
    files = {}
    for name in ('whitecube', 'cube-settings', 'cube-status'):
        source = project / 'build' / name
        if not source.is_file() or not os.access(source, os.X_OK):
            parser.error('build all binaries with ./BUILD.sh before installing')
        files[bindir / name] = (source.read_bytes(), 0o755)
    for name in ('cube-privacy', 'whitecube-session'):
        files[bindir / name] = ((project / name).read_bytes(), 0o755)
    # Existing desktop launchers and already-running compositors honor this key.
    config = user_dir / '.config/whitecube/config'
    if config.is_symlink():
        parser.error('config is a symlink; resolve its install destination explicitly')
    old_config = config.read_text() if config.exists() else ''
    lines = old_config.splitlines(keepends=True)
    lines = [line for line in lines if not line.startswith('settings_cmd=')]
    next_config = ''.join(lines)
    if next_config and not next_config.endswith('\n'):
        next_config += '\n'
    # This is a shell command in the compositor configuration.
    import shlex
    next_config += 'settings_cmd=' + shlex.quote(str(bindir / 'cube-settings')) + '\n'
    files[config] = (next_config.encode(), 0o600)
    # Desktop Exec values use their own quoting, not shell quoting.
    settings_exec = str(bindir / 'cube-settings')
    for character in ('\\', '"', '`', '$'):
        settings_exec = settings_exec.replace(character, '\\' + character)
    desktop = ('[Desktop Entry]\nType=Application\nName=Whitecube Settings\n'
               'Comment=Desktop, privacy, sound, network and system preferences\n'
               f'Exec="{settings_exec}"\nIcon=preferences-system\n'
               'Terminal=false\nCategories=Settings;System;\nKeywords=Privacy;Desktop;Network;Sound;\n')
    files[user_dir / '.local/share/applications/cube-settings.desktop'] = (desktop.encode(), 0o644)
    for path in files:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            parser.error(f'refusing non-regular destination: {path}')
        if path.exists() and path.stat().st_uid != os.getuid():
            parser.error(f'destination belongs to another user: {path}')
    if not args.apply:
        print(json.dumps({'destinations': [str(p) for p in files], 'activate': 'Log out and back in; no session is stopped by this installer.'}, indent=2))
        return
    backup = Path(tempfile.mkdtemp(prefix='privacy-install-before-', dir=project / 'build'))
    manifest = {'backup': str(backup), 'files': []}
    for index, (path, (data, mode)) in enumerate(files.items()):
        prior = backup / str(index)
        if path.exists():
            shutil.copy2(path, prior)
        manifest['files'].append({'path': str(path), 'before': str(prior) if prior.exists() else None,
                                  'sha256': hashlib.sha256(data).hexdigest()})
    atomic(backup / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode(), 0o600)
    installed = []
    try:
        for path, (data, mode) in files.items():
            atomic(path, data, mode)
            installed.append(path)
    except Exception:
        for record in reversed(manifest['files']):
            path = Path(record['path'])
            if path not in installed:
                continue
            if record['before']:
                prior = Path(record['before'])
                atomic(path, prior.read_bytes(), stat.S_IMODE(prior.stat().st_mode))
            else:
                path.unlink()
        raise
    state = user_dir / '.local/state/whitecube'
    atomic(state / 'privacy-install.json', (json.dumps(manifest, indent=2) + '\n').encode(), 0o600)
    print(json.dumps({'installed': [str(p) for p in installed], 'backup': str(backup),
                      'activation': 'Log out and back in to activate compositor and bar changes.'}, indent=2))


if __name__ == '__main__':
    main()
