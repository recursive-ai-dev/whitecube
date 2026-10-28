#!/usr/bin/python3 -I
"""Identify cached build inputs and validate the selected Electron distribution."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
BUILDER_RECIPES = ('tools/build-debian.sh', 'tools/build-inputs.py', 'tools/prepare-native-deps.sh',
                   'apps/cube-browser/scripts/bootstrap-cairo.sh',
                   'apps/cube-browser/scripts/bootstrap-jpeg.sh',
                   'apps/cube-browser/scripts/bootstrap-quickjs.sh',
                   'apps/cube-browser/scripts/bootstrap-mbedtls.sh',
                   'apps/cube-player/scripts/bootstrap.sh')
ELECTRON_RECIPES = ('apps/desktop-chat/package.json', 'apps/desktop-chat/package-lock.json')


def digest_inputs(root, names):
    digest = hashlib.sha256()
    for name in names:
        data = (root / name).read_bytes()
        digest.update(name.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
    return digest.hexdigest()


def electron_version(root=ROOT):
    manifest = json.loads((root / ELECTRON_RECIPES[0]).read_text())
    return manifest['devDependencies']['electron']


def validate_electron(distribution, expected):
    if not os.access(distribution / 'electron', os.X_OK):
        raise RuntimeError('ELECTRON_DIST must contain an executable electron')
    try:
        actual = (distribution / 'version').read_text().strip().removeprefix('v')
    except OSError as error:
        raise RuntimeError('Electron distribution must contain its version file') from error
    if actual != expected:
        raise RuntimeError(f'Electron runtime version {actual!r} does not match package.json {expected!r}')
    return actual


def ensure_electron(root, distribution, external=False, refresh=False):
    expected = electron_version(root)
    key = digest_inputs(root, ELECTRON_RECIPES)
    stamp = root / 'apps/desktop-chat/node_modules/.whitecube-inputs.sha256'
    if external:
        return validate_electron(distribution, expected)
    current = False
    if not refresh:
        try:
            current = stamp.read_text().strip() == key and validate_electron(distribution, expected) == expected
        except (OSError, RuntimeError):
            pass
    if not current:
        subprocess.run(['npm', '--prefix', str(root / 'apps/desktop-chat'), 'ci'], check=True)
        # Recent Electron npm packages expose installation as a local CLI and
        # do not fetch their binary during npm ci. Execute the pinned package's
        # installer with the selected Node runtime, without package resolution.
        installer = root / 'apps/desktop-chat/node_modules/electron/install.js'
        if not installer.is_file():
            raise RuntimeError('Pinned Electron installer is missing after npm ci')
        subprocess.run([os.environ.get('NODE', 'node'), str(installer)], check=True)
        validate_electron(distribution, expected)
        stamp.write_text(key + '\n')
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('builder-key')
    commands.add_parser('electron-key')
    validate = commands.add_parser('check-electron')
    validate.add_argument('distribution', type=Path)
    ensure = commands.add_parser('ensure-electron')
    ensure.add_argument('distribution', type=Path)
    ensure.add_argument('--external', action='store_true')
    ensure.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'builder-key':
            print(digest_inputs(args.root, BUILDER_RECIPES))
        elif args.command == 'electron-key':
            print(digest_inputs(args.root, ELECTRON_RECIPES))
        elif args.command == 'check-electron':
            print(validate_electron(args.distribution, electron_version(args.root)))
        else:
            print(ensure_electron(args.root, args.distribution, args.external, args.refresh))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'build-inputs: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
