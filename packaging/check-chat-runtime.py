#!/usr/bin/python3 -I
"""Verify Desktop Chat ships a usable private Electron runtime."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def check(stage):
    runtime = stage / 'usr/lib/whitecube/desktop-chat-runtime'
    problems = []
    for name in ('electron', 'version', 'LICENSE', 'LICENSES.chromium.html',
                 'resources.pak', 'icudtl.dat', 'libffmpeg.so', 'chrome-sandbox'):
        path = runtime / name
        if not path.is_file() or not path.resolve().is_relative_to(runtime.resolve()):
            problems.append(f'missing or escaping runtime file: {name}')
    if not (runtime / 'locales/en-US.pak').is_file():
        problems.append('runtime locale is missing')
    executable = runtime / 'electron'
    if not os.access(executable, os.X_OK):
        problems.append('Electron is not executable')
    sandbox = runtime / 'chrome-sandbox'
    if sandbox.exists() and sandbox.stat().st_mode & 0o7777 != 0o4755:
        problems.append('Chromium sandbox helper must have mode 4755')
    if executable.is_file():
        result = subprocess.run(['ldd', str(executable)], capture_output=True, text=True,
                                env=dict(os.environ, LD_LIBRARY_PATH=str(runtime)))
        if result.returncode or 'not found' in result.stdout:
            problems.append('unresolved Electron shared libraries')
    launcher = stage / 'usr/bin/desktop-chat'
    if not launcher.is_file() or '--ozone-platform=wayland' not in launcher.read_text():
        problems.append('Wayland chat launcher is missing')
    return problems


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', type=Path)
    args = parser.parse_args()
    problems = check(args.stage.resolve())
    if problems:
        print('check-chat-runtime: ' + '; '.join(problems), file=sys.stderr)
        sys.exit(1)
    print('check-chat-runtime: ok (private runtime, licenses, locale, launcher and libraries)')
