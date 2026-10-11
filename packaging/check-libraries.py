#!/usr/bin/python3 -I
"""Verify staged native executables use shipped private libraries and no host fallback."""
import argparse
from email.parser import Parser
import os
from pathlib import Path
import re
import subprocess
import sys

NATIVE = ('usr/bin/whitecube-compositor', 'usr/bin/cube-status', 'usr/bin/cube-settings', 'usr/bin/cube-ca',
          'usr/libexec/whitecube/cube-browser/cube-browser', 'usr/libexec/whitecube/cube-browser/cube-renderd',
          'usr/libexec/whitecube/cube-browser/cube-netd')
# cube-player is not listed: libmpv (Depends: libmpv2) reaches X11 and is not on the build host.
PRIVATE = ('libwlroots-0.18.so', 'libcairo.so.2')
REQUIRED_PROVIDERS = {'libEGL.so.1': 'libegl1', 'libGLESv2.so.2': 'libgles2'}
RESOLVED = re.compile(r'^\s*(\S+)\s+=>\s+(\S+)\s+\(', re.MULTILINE)


def resolution_problems(output, private):
    problems = []
    if re.search(r'=>\s+not found(?:\s|$)', output):
        problems.append('unresolved shared library: ' + '; '.join(line.strip() for line in output.splitlines() if 'not found' in line))
    for name, path in RESOLVED.findall(output):
        resolved = Path(path).resolve()
        shipped = resolved.is_relative_to(private)
        if name in PRIVATE and not shipped:
            problems.append(f'{name} resolves from the host instead of the package: {path}')
        if not shipped and (path.startswith('/usr/lib/whitecube/') or path.startswith('/usr/local/')):
            problems.append(f'host-only library is not shipped: {path}')
        if name.startswith(('libX11', 'libxcb')):
            problems.append(f'X11 library reaches a native executable: {name}')
    return problems


def check(stage):
    private = (stage / 'usr/lib/whitecube').resolve()
    problems = []
    required_providers = set()
    for name in PRIVATE:
        library = private / name
        if not library.is_file():
            problems.append(f'private library is missing: {name}')
        elif not library.resolve().is_relative_to(private):
            problems.append(f'private library symlink escapes the package: {name}')
    env = dict(os.environ, LD_LIBRARY_PATH=str(private))
    binaries = [stage / name for name in NATIVE]
    libraries = [p for p in private.glob('*.so*') if p.is_file() and not p.is_symlink()]
    for path in binaries + libraries:
        if not path.is_file():
            problems.append(f'native executable is missing: {path.name}')
            continue
        result = subprocess.run(['ldd', str(path)], env=env, capture_output=True, text=True)
        if result.returncode:
            problems.append(f'{path.name}: ldd failed: {result.stderr.strip() or result.stdout.strip()}')
        problems.extend(f'{path.name}: {problem}' for problem in resolution_problems(result.stdout, private))
        required_providers.update(REQUIRED_PROVIDERS[name] for name, _ in RESOLVED.findall(result.stdout) if name in REQUIRED_PROVIDERS)
    control = stage / 'DEBIAN/control'
    if control.is_file():
        fields = Parser().parsestr(control.read_text())
        package = fields.get('Package', '').strip()
        dependencies = [re.match(r'\s*([a-z0-9+.-]+)', part) for part in re.split(r'[,|]', fields.get('Depends', ''))]
        if package and any(match and match.group(1) == package for match in dependencies):
            problems.append(f'package has a self dependency: {package}')
        declared = {match.group(1) for match in dependencies if match}
        for provider in sorted(required_providers - declared):
            problems.append(f'required native library provider is missing from Depends: {provider}')
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', type=Path)
    args = parser.parse_args()
    problems = check(args.stage.resolve())
    if problems:
        print('check-libraries: ' + '\n  - '.join(['failed', *problems]))
        return 1
    print('check-libraries: ok (private Cairo/wlroots shipped; native shared libraries resolved; no self dependency)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
