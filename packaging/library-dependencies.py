#!/usr/bin/python3 -I
"""Map every unbundled native shared library to its Debian package, or fail."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

NATIVE = ('usr/bin/whitecube-compositor', 'usr/bin/cube-status', 'usr/bin/cube-settings', 'usr/bin/cube-ca',
          'usr/libexec/whitecube/cube-browser/cube-browser', 'usr/libexec/whitecube/cube-browser/cube-renderd',
          'usr/libexec/whitecube/cube-browser/cube-netd')
# cube-player is not listed: libmpv (Depends: libmpv2) reaches X11 and is not on the build host.
RESOLVED = re.compile(r'^\s*(\S+)\s+=>\s+(\S+)\s+\(', re.MULTILINE)
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


def discover(stage, include_chat=False):
    private = (stage / 'usr/lib/whitecube').resolve()
    binaries = [stage / name for name in NATIVE]
    libraries = [p for p in private.glob('*.so*') if p.is_file() and not p.is_symlink()]
    paths = set()
    runtime = private / 'desktop-chat-runtime'
    chat_paths = [runtime / 'electron', *runtime.glob('*.so')] if include_chat else []
    for binary in binaries + libraries + chat_paths:
        search = str(runtime) if binary in chat_paths else str(private)
        result = subprocess.run(['ldd', str(binary)], capture_output=True, text=True,
                                env=dict(os.environ, LD_LIBRARY_PATH=search, LC_ALL='C'))
        if result.returncode or re.search(r'=>\s+not found(?:\s|$)', result.stdout):
            raise RuntimeError(f'{binary.name}: unresolved shared library: {result.stdout.strip()} {result.stderr.strip()}')
        for _, path in RESOLVED.findall(result.stdout):
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
    except RuntimeError as error:
        print(f'library-dependencies: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
