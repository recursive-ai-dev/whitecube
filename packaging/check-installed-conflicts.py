#!/usr/bin/python3 -I
"""Check a .deb or staged tree against a host's installed dpkg file ownership.

This is read-only and does not install, remove or configure packages. Directory
entries and the package's own earlier version are excluded. Parent directory
aliases such as /bin -> /usr/bin are resolved in the selected host filesystem;
the final filename is preserved because replacing a symlink replaces that link.
Replaces relationships are not evaluated, so their overlaps require review too.
"""
import argparse
from email.parser import Parser
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile

ACTIVE_STATES = {'installed', 'unpacked', 'half-installed', 'half-configured',
                 'triggers-awaited', 'triggers-pending'}


def archive_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise RuntimeError(f'unsafe package path: {name}')
    return '/' + str(path) if str(path) != '.' else '/'


def inventory(package):
    if package.is_dir():
        fields = Parser().parsestr((package / 'DEBIAN/control').read_text())
        paths = {archive_path(path.relative_to(package).as_posix()) for path in package.rglob('*')
                 if not path.is_relative_to(package / 'DEBIAN') and (path.is_symlink() or path.is_file())}
    else:
        control = subprocess.run(['dpkg-deb', '--field', str(package)], capture_output=True, text=True, check=True)
        fields = Parser().parsestr(control.stdout)
        paths = set()
        with subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(package)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as process:
            try:
                with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
                    for member in archive:
                        path = archive_path(member.name)
                        if not member.isdir():
                            paths.add(path)
            finally:
                process.stdout.close()
            error = process.stderr.read().decode(errors='replace').strip()
            if process.wait():
                raise RuntimeError(error or 'dpkg-deb could not read package payload')
    name = fields.get('Package', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', name):
        raise RuntimeError('package control has no valid Package field')
    return fields, paths


def canonical_path(path, root, cache):
    """Resolve only parent links, interpreting absolute links within host root."""
    source = PurePosixPath(path)
    parent = str(source.parent)
    if parent not in cache:
        pending, resolved, links = list(source.parent.parts), [], 0
        while pending:
            part = pending.pop(0)
            if part in ('/', '.'):
                continue
            if part == '..':
                if resolved:
                    resolved.pop()
                continue
            candidate = root.joinpath(*resolved, part)
            if candidate.is_symlink():
                links += 1
                if links > 40:
                    raise RuntimeError(f'cyclic host directory alias: {path}')
                target = PurePosixPath(os.readlink(candidate))
                if target.is_absolute():
                    resolved = []
                pending = list(target.parts) + pending
            else:
                resolved.append(part)
        cache[parent] = '/' + '/'.join(resolved)
    return cache[parent].rstrip('/') + '/' + source.name


def installed_packages(database):
    packages = {}
    text = (database / 'status').read_text(errors='surrogateescape')
    for paragraph in re.split(r'\n[ \t]*\n', text):
        fields = Parser().parsestr(paragraph)
        status = fields.get('Status', '').split()
        name, architecture = fields.get('Package'), fields.get('Architecture')
        if not name or not status or status[-1] not in ACTIVE_STATES:
            continue
        entry = {'package': name, 'version': fields.get('Version', '')}
        packages[name] = entry
        if architecture:
            packages[name + ':' + architecture] = entry
    return packages


def check(package, host_root=Path('/')):
    root = host_root.resolve()
    fields, paths = inventory(package)
    name = fields['Package']
    database = root / 'var/lib/dpkg'
    packages = installed_packages(database)
    info = database / 'info'
    if not info.is_dir():
        raise RuntimeError(f'installed dpkg file lists are missing: {info}')
    cache, candidate_paths, owners = {}, {}, {}
    for path in paths:
        candidate_paths.setdefault(canonical_path(path, root, cache), set()).add(path)
    for listing in sorted(info.glob('*.list')):
        identity = listing.name[:-5]
        owner = packages.get(identity)
        if owner is None or owner['package'] == name:
            continue
        for path in listing.read_text(errors='surrogateescape').splitlines():
            if not path.startswith('/'):
                continue
            canonical = canonical_path(path, root, cache)
            if canonical in candidate_paths:
                owners.setdefault(canonical, {})[identity] = dict(owner, package=identity)
    conflicts = []
    for canonical in sorted(owners):
        for path in sorted(candidate_paths[canonical]):
            conflicts.append({'path': path, 'canonical_path': canonical,
                              'owners': [owners[canonical][owner] for owner in sorted(owners[canonical])]})
    return {'package': name, 'version': fields.get('Version', ''), 'host_root': str(root),
            'files_checked': len(paths), 'installed_packages': len({entry['package'] for entry in packages.values()}),
            'conflicts': conflicts, 'replaces': fields.get('Replaces', ''), 'replaces_evaluated': False,
            'result': 'conflicts' if conflicts else 'passed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path, help='.deb archive or staged tree containing DEBIAN/control')
    parser.add_argument('--host-root', type=Path, default=Path('/'), help='installed host filesystem root (default: /)')
    parser.add_argument('--json', action='store_true', help='print a machine-readable result')
    args = parser.parse_args()
    try:
        report = check(args.package, args.host_root)
    except (OSError, RuntimeError, tarfile.TarError, ValueError, subprocess.CalledProcessError) as error:
        print(f'check-installed-conflicts: {error}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    elif report['conflicts']:
        print(f'check-installed-conflicts: {len(report["conflicts"])} conflict(s) for {report["package"]}:')
        for entry in report['conflicts']:
            owners = ', '.join(owner['package'] + ' (' + owner['version'] + ')' for owner in entry['owners'])
            print(f'  {entry["path"]}: owned by {owners}')
    else:
        print(f'check-installed-conflicts: passed ({report["files_checked"]} payload files; '
              f'{report["installed_packages"]} installed packages)')
    if report['replaces'] and not args.json:
        print('check-installed-conflicts: Replaces relationships were not evaluated; overlaps require review.')
    return 1 if report['conflicts'] else 0


if __name__ == '__main__':
    sys.exit(main())
