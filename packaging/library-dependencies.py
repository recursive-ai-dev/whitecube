#!/usr/bin/python3 -I
"""Map direct ELF dependencies to their Debian owners, validating full resolution.

Dependencies of distribution libraries are managed by those libraries' packages.
Pinning their entire ldd closure unnecessarily ties a release to the builder's
distribution (for example GTK's JPEG provider differs between Debian and Ubuntu).
Every shipped ELF, including private shared libraries, contributes its own
DT_NEEDED entries; complete ldd closures still detect missing and X11 libraries.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

RESOLVED = re.compile(r'^\s*(\S+)\s+=>\s+(\S+)\s+\(', re.MULTILINE)
ABSOLUTE_RESOLVED = re.compile(r'^\s*(/\S+)\s+\(', re.MULTILINE)
NEEDED = re.compile(r'\(NEEDED\).*\[([^]]+)\]')
DIVERTED_FROM = re.compile(r'^diversion by .+ from: (.+)$', re.MULTILINE)


def package_query(path):
    result = subprocess.run(['dpkg-query', '-S', path], capture_output=True, text=True,
                            env=dict(os.environ, LC_ALL='C'))
    return result.stdout


def original_alias(path):
    # Resolve the usrmerge directory alias while retaining the library symlink.
    # Resolving the entire filename first can lose its package ownership when a
    # graphics-driver diversion redirects it into /usr/lib/mesa-diverted.
    for directory in ('lib', 'lib64', 'bin', 'sbin'):
        prefix = '/' + directory + '/'
        if path.startswith(prefix) and Path('/' + directory).resolve() == Path('/usr/' + directory).resolve():
            return '/usr' + path
    return path


def owner_for_library(path, query=package_query):
    pending = [original_alias(path), path, str(Path(path).resolve())]
    queried = set()
    while pending:
        candidate = pending.pop(0)
        if candidate in queried:
            continue
        queried.add(candidate)
        output = query(candidate)
        for line in output.splitlines():
            if line.startswith('diversion ') or ': ' not in line:
                continue
            owner, _ = line.split(': ', 1)
            owner = owner.split(':', 1)[0]
            if re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', owner):
                return owner
        # A diverted physical file may report only metadata. Query the original
        # package-owned path recorded by dpkg rather than assigning the divertor.
        pending.extend(original_alias(item) for item in DIVERTED_FROM.findall(output))
    raise RuntimeError(f'no Debian package owns required shared library: {path}')


def elf_files(stage, include_chat):
    runtime = stage / 'usr/lib/whitecube/desktop-chat-runtime'
    for path in sorted((stage / 'usr').rglob('*')):
        if path.is_symlink() or not path.is_file() or (not include_chat and path.is_relative_to(runtime)):
            continue
        with path.open('rb') as stream:
            if stream.read(4) == b'\x7fELF':
                yield path


def needed_libraries(binary):
    result = subprocess.run(['readelf', '--dynamic', '--wide', str(binary)], capture_output=True, text=True,
                            env=dict(os.environ, LC_ALL='C'))
    if result.returncode:
        raise RuntimeError(f'{binary.name}: cannot inspect ELF dependencies: {result.stderr.strip()}')
    return set(NEEDED.findall(result.stdout))


def discover(stage, include_chat=False):
    stage = stage.resolve()
    private = stage / 'usr/lib/whitecube'
    runtime = private / 'desktop-chat-runtime'
    paths = set()
    binaries = list(elf_files(stage, include_chat))
    if not binaries:
        raise RuntimeError('package contains no ELF executables or shared libraries')
    for binary in binaries:
        needed = needed_libraries(binary)
        if not needed:  # Static executables have no runtime library dependencies.
            continue
        chat = binary.is_relative_to(runtime)
        search = str(runtime) if chat else str(private)
        result = subprocess.run(['ldd', str(binary)], capture_output=True, text=True,
                                env=dict(os.environ, LD_LIBRARY_PATH=search, LC_ALL='C'))
        if result.returncode or re.search(r'=>\s+not found(?:\s|$)', result.stdout):
            raise RuntimeError(f'{binary.name}: unresolved shared library: {result.stdout.strip()} {result.stderr.strip()}')
        resolved = dict(RESOLVED.findall(result.stdout))
        # The dynamic loader can itself be a DT_NEEDED library (Chromium's
        # crashpad helper does this). ldd prints its absolute path without =>.
        for path in ABSOLUTE_RESOLVED.findall(result.stdout):
            resolved.setdefault(Path(path).name, path)
            resolved.setdefault(path, path)
        if not chat and binary.relative_to(stage).as_posix() != 'usr/bin/cube-player':
            for name in resolved:
                if name.startswith(('libX11', 'libxcb')):
                    raise RuntimeError(f'{binary.name}: X11 library reaches a native executable: {name}')
        for name in needed:
            path = resolved.get(name)
            if path is None:
                raise RuntimeError(f'{binary.name}: direct dependency did not resolve: {name}')
            if not Path(path).resolve().is_relative_to(private):
                paths.add(path)
    packages = {owner_for_library(path) for path in sorted(paths)}
    if 'whitecube-desktop' in packages:
        raise RuntimeError('unbundled native library belongs to whitecube-desktop itself')
    return sorted(packages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', type=Path)
    parser.add_argument('--chat-runtime', action='store_true', help='also discover Chromium runtime dependencies')
    args = parser.parse_args()
    try:
        for package in discover(args.stage.resolve(), args.chat_runtime):
            print(package)
    except (OSError, RuntimeError) as error:
        print(f'library-dependencies: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
