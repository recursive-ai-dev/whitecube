#!/usr/bin/env python3
"""Install the built browser for this user and bind Whitecube Alt+W.

Print a plan by default; --apply performs atomic writes with a timestamped backup.
--restore MANIFEST plans a rollback; add --apply to restore unchanged install files.
No packages, root operations, MIME defaults, or desktop restarts.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--apply', action='store_true')
parser.add_argument('--restore', type=Path, metavar='MANIFEST')
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
home = Path.home()
config_root = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
data_root = Path(os.environ.get('XDG_DATA_HOME', home / '.local/share'))
state_root = Path(os.environ.get('XDG_STATE_HOME', home / '.local/state'))
bin_dir = home / '.local/bin'
config = config_root / 'whitecube/config'
launcher = bin_dir / 'cube-browser'
desktop = data_root / 'applications/cube-browser.desktop'
icon = data_root / 'icons/hicolor/scalable/apps/cube-browser.svg'
targets = (launcher, config, desktop, icon)

def fingerprint(path):
    if path.is_symlink():
        return {'symlink': os.readlink(path)}
    if not path.exists():
        return {'absent': True}
    if not path.is_file():
        raise SystemExit(f'Refusing non-file: {path}')
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': path.stat().st_mode & 0o777}

def atomic(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.cube-browser-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def atomic_link(path, destination):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.cube-browser-', dir=path.parent)
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(destination, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary): os.unlink(temporary)

if args.restore:
    manifest_path = args.restore.absolute()
    records = json.loads(manifest_path.read_text())
    if (not isinstance(records, list) or len(records) != len(targets) or
            {row.get('path') for row in records} != {str(path) for path in targets}):
        raise SystemExit('Rollback manifest does not match the current HOME/XDG profile.')
    # Validate the entire operation before changing any target. A later user edit
    # must be reviewed manually, not silently replaced with the old backup.
    for row in records:
        path = Path(row['path'])
        if not row.get('installed') or fingerprint(path) != row['installed']:
            raise SystemExit(f'Rollback refused: {path} changed after installation, or the manifest predates rollback support.')
        if 'backup' in row:
            saved = Path(row['backup'])
            if saved.parent != manifest_path.parent or saved.is_symlink() or fingerprint(saved) != row.get('before'):
                raise SystemExit(f'Rollback backup is missing or changed: {saved}')
        elif row.get('existed') and 'symlink' not in row:
            raise SystemExit(f'Incomplete rollback record: {path}')
        print(f'Restore: {path}' if row['existed'] else f'Remove installed file: {path}')
    if not args.apply:
        print(f'Add --apply to restore from {manifest_path}.')
        raise SystemExit(0)
    for row in records:
        path = Path(row['path'])
        if 'symlink' in row:
            atomic_link(path, row['symlink'])
        elif 'backup' in row:
            atomic(path, Path(row['backup']).read_bytes(), row['before']['mode'])
        else:
            path.unlink()
    print(f'Restored installation files. Backup retained: {manifest_path.parent}')
    raise SystemExit(0)

build = Path(os.environ.get('CUBE_BROWSER_BUILD', root / 'build'))
for name in ('cube-browser', 'cube-renderd', 'cube-netd'):
    path = build / name
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SystemExit(f'Build missing: {path}; run ./BUILD.sh first.')
subprocess.run([str(root / 'scripts/check-links.sh'), *(str(build / n) for n in ('cube-browser', 'cube-renderd', 'cube-netd'))], check=True)
for path in (config, desktop, icon):
    if path.is_symlink():
        raise SystemExit(f'Refusing to replace a symlink: {path}')
if any(c in str(root) + str(launcher) for c in '\n\r'):
    raise SystemExit('Installation paths may not contain newlines.')
old = config.read_text() if config.exists() else ''
command = str(launcher)
# Whitecube executes its configured command via /bin/sh; quote the exact path.
import shlex
lines = [line for line in old.splitlines() if not line.strip().startswith('browser_cmd=')]
lines.append('browser_cmd=' + shlex.quote(command))
new_config = '\n'.join(lines) + '\n'
def desktop_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
entry = ('[Desktop Entry]\nType=Application\nName=Cube Browser\n'
         'Comment=Native interactive-web browser preview for Whitecube\n'
         f'Exec={desktop_quote(command)} %u\nIcon=cube-browser\n'
         'Terminal=false\nCategories=Network;WebBrowser;\n'
         'MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;\n'
         'StartupWMClass=cube-browser\n')
print(f'Launcher: {launcher} -> {root / "cube-browser.sh"}')
print(f'Application menu: {desktop}')
print(f'Alt+W: {config} -> {command}')
if not args.apply:
    print('Run scripts/install-user.py --apply to install.')
    raise SystemExit(0)
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
backup = state_root / 'cube-browser/install-backups' / stamp
backup.mkdir(parents=True, mode=0o700)
manifest = []
for index, path in enumerate(targets):
    exists = path.exists() or path.is_symlink()
    record = {'path': str(path), 'existed': exists, 'before': fingerprint(path)}
    if path.is_symlink():
        record['symlink'] = os.readlink(path)
    elif exists:
        if not path.is_file():
            raise SystemExit(f'Refusing to replace non-file: {path}')
        saved = backup / str(index)
        shutil.copy2(path, saved)
        record['backup'] = str(saved)
    manifest.append(record)
atomic(backup / 'manifest.json', json.dumps(manifest, indent=2) + '\n')
atomic_link(launcher, root / 'cube-browser.sh')
atomic(config, new_config)
atomic(desktop, entry, 0o644)
atomic(icon, (root / 'cube-browser.svg').read_text(), 0o644)
for record, path in zip(manifest, targets):
    record['installed'] = fingerprint(path)
atomic(backup / 'manifest.json', json.dumps(manifest, indent=2) + '\n')
print(f'Installed. Backup and rollback manifest: {backup}')
