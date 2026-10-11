#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import os
import shutil

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install-user.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
with tempfile.TemporaryDirectory(prefix='cube-player-install-') as directory:
    home = Path(directory)
    launcher = home / '.local/bin/cube-player'
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\necho previous\n')
    launcher.chmod(0o751)
    old = launcher.read_bytes()
    args = ['python3', str(ROOT / 'scripts/install-user.py'), '--home', str(home)]
    subprocess.run(args, check=True, capture_output=True)
    assert launcher.read_bytes() == old
    subprocess.run(args + ['--apply'], check=True, capture_output=True)
    assert 'build/cube-player' in launcher.read_text()
    assert subprocess.check_output([str(launcher), '--version'], text=True).strip() == 'Cube Player 1.0.0'
    desktop = home / '.local/share/applications/cube-player.desktop'
    assert ' %U\n' in desktop.read_text()
    if shutil.which('desktop-file-validate'):
        subprocess.run(['desktop-file-validate', str(desktop)], check=True, capture_output=True)
    manifest = next((home / '.local/state/cube-player/install-backups').glob('*/manifest.json'))
    installed = launcher.read_bytes()
    launcher.write_text('user changed this\n')
    refused = subprocess.run(args + ['--restore', str(manifest), '--apply'], capture_output=True)
    assert refused.returncode != 0
    assert launcher.read_text() == 'user changed this\n'
    launcher.write_bytes(installed)
    subprocess.run(args + ['--restore', str(manifest), '--apply'], check=True, capture_output=True)
    assert launcher.read_bytes() == old
    assert launcher.stat().st_mode & 0o777 == 0o751
    assert not desktop.exists()
print('Launcher, installation backups, changed-file protection, and rollback passed.')
