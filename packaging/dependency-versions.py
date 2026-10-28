#!/usr/bin/python3 -I
"""Derive ABI version requirements from the actual ELF files being shipped.

The ownership scanner checks that each external library has a package owner.
This additional pass uses dpkg's symbols/shlibs metadata so an artifact built
on a recent distribution cannot silently install on an incompatible old libc.
Private libraries have no Debian metadata; their dependencies are inspected too.
"""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile


def elf_files(stage):
    for path in sorted((stage / 'usr').rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            if stream.read(4) == b'\x7fELF':
                yield path


def discover(stage, excluded_packages=()):
    stage = stage.resolve()
    binaries = list(elf_files(stage))
    if not binaries:
        raise RuntimeError('package contains no ELF executables or shared libraries')
    private = stage / 'usr/lib/whitecube'
    with tempfile.TemporaryDirectory(prefix='whitecube-shlibdeps-') as tmp:
        metadata = Path(tmp) / 'debian'
        metadata.mkdir()
        (metadata / 'control').write_text(
            'Source: whitecube-desktop\nSection: x11\nPriority: optional\n'
            'Maintainer: Whitecube <whitecube@localhost>\n\n'
            'Package: whitecube-desktop\nArchitecture: any\nDescription: Whitecube desktop\n')
        result = subprocess.run([
            'dpkg-shlibdeps', '-O', '--ignore-missing-info', '--warnings=0',
            '-xwhitecube-desktop', '-S' + str(stage), '-l' + str(private),
            '-l' + str(private / 'desktop-chat-runtime'),
            '-l' + str(private / 'trainer-parquet-runtime/pyarrow'),
            *('-x' + package for package in excluded_packages),
            *('-e' + str(path) for path in binaries),
        ], cwd=tmp, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('dpkg-shlibdeps failed: ' + result.stderr.strip())
    values = [line.removeprefix('shlibs:Depends=') for line in result.stdout.splitlines()
              if line.startswith('shlibs:Depends=')]
    if len(values) != 1 or not values[0].strip():
        raise RuntimeError('dpkg-shlibdeps produced no ABI dependencies')
    return [dependency.strip() for dependency in values[0].split(',')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', type=Path)
    parser.add_argument('--exclude-package', action='append', default=[],
                        help='omit a library provider whose ABI is shipped privately')
    args = parser.parse_args()
    try:
        for dependency in discover(args.stage, args.exclude_package):
            print(dependency)
    except (OSError, RuntimeError) as error:
        print(f'dependency-versions: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
