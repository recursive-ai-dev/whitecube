#!/usr/bin/python3 -I
"""Verify the built Debian archive independently of its build staging directory.

Checks root ownership, modes, paths, control metadata, configuration registration,
and every payload checksum without executing package scripts or installing it.
Optional JSON output records the release's architecture, dependencies and SHA256.
"""
import argparse
from contextlib import contextmanager
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

PACKAGE = 'whitecube-desktop'
SANDBOX = 'usr/lib/whitecube/desktop-chat-runtime/chrome-sandbox'


def payload_digest(stream, name, problems):
    """Hash the file while validating any ELF header against our target ABI."""
    header = stream.read(64)
    if header.startswith(b'\x7fELF'):
        # ELFCLASS64, ELFDATA2LSB, EV_CURRENT and EM_X86_64. Architecture in
        # control alone cannot detect an accidentally bundled ARM runtime.
        if (len(header) < 64 or header[4:7] != b'\x02\x01\x01'
                or int.from_bytes(header[18:20], 'little') != 62):
            problems.append(f'ELF file is not amd64: {name}')
    digest = hashlib.md5(header)
    return hashlib.file_digest(stream, lambda: digest).hexdigest()


@contextmanager
def archive_members(package, flag):
    with subprocess.Popen(['dpkg-deb', flag, str(package)], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
                yield archive
        finally:
            # Close early if validation raised, allowing dpkg-deb to receive SIGPIPE.
            process.stdout.close()
        error = process.stderr.read().decode(errors='replace').strip()
        if process.wait():
            raise RuntimeError(error or 'dpkg-deb could not read the archive')


def payload_path(name):
    if name in ('.', './'):
        return ''
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise RuntimeError(f'unsafe archive path: {name}')
    return str(path)


def resolved_payload_path(name, payload):
    """Follow links using archive entries, never files on the build machine."""
    pending, resolved, links = list(PurePosixPath(name).parts), [], 0
    while pending:
        component = pending.pop(0)
        if component in ('/', '.'):
            continue
        if component == '..':
            if not resolved:
                raise RuntimeError(f'symlink escapes installed root: {name}')
            resolved.pop()
            continue
        resolved.append(component)
        member = payload.get('/'.join(resolved))
        if member is not None and member.issym():
            links += 1
            if links > 40:
                raise RuntimeError(f'cyclic symlink: {name}')
            resolved.pop()
            target = PurePosixPath(member.linkname)
            if target.is_absolute():
                resolved = []
            pending = list(target.parts) + pending
    destination = '/'.join(resolved)
    if destination and destination not in payload:
        raise RuntimeError(f'dangling symlink: {name} -> {destination}')
    return destination


def check(package):
    problems = []
    with archive_members(package, '--ctrl-tarfile') as control_archive:
        control_files = {}
        seen_control = set()
        for member in control_archive:
            name = payload_path(member.name)
            if name in seen_control:
                problems.append(f'duplicate control path: {name}')
            seen_control.add(name)
            if member.uid != 0 or member.gid != 0:
                problems.append(f'control file is not root-owned: {name}')
            if name and (not member.isfile() or '/' in name):
                problems.append(f'unexpected control entry: {name}')
            if member.isfile():
                control_files[name] = control_archive.extractfile(member).read()
                expected_mode = 0o755 if name in ('postinst', 'postrm', 'preinst', 'prerm') else 0o644
                if member.mode != expected_mode:
                    problems.append(f'control file has incorrect mode: {name}')
    if not {'control', 'conffiles', 'md5sums', 'postinst', 'postrm'} <= control_files.keys():
        problems.append('required control metadata or maintainer scripts are missing')
    fields = Parser().parsestr(control_files.get('control', b'').decode())
    for field in set(fields.keys()):
        if len(fields.get_all(field)) != 1:
            problems.append(f'duplicate control field: {field}')
    for field in ('Package', 'Version', 'Architecture', 'Maintainer', 'Depends', 'Description', 'Installed-Size'):
        if not fields.get(field, '').strip():
            problems.append(f'control field is missing: {field}')
    if fields.get('Package') != PACKAGE:
        problems.append(f'incorrect package name: {fields.get("Package")}')
    if fields.get('Architecture') != 'amd64':
        problems.append(f'incorrect package architecture: {fields.get("Architecture")}')
    dependencies = fields.get('Depends', '')
    if re.search(r'(?:^|[,|])\s*whitecube-desktop(?:\s|$|[,|])', dependencies):
        problems.append('package depends on itself')
    if not re.search(r'(?:^|[,|])\s*libc6\s*\(>=\s*[^)]+\)', dependencies):
        problems.append('libc6 ABI minimum is missing from Depends')

    expected_hashes = {}
    for line in control_files.get('md5sums', b'').decode().splitlines():
        match = re.fullmatch(r'([a-f0-9]{32})  (.+)', line)
        if not match:
            problems.append(f'invalid md5sums entry: {line}')
            continue
        name = payload_path(match[2])
        if name in expected_hashes:
            problems.append(f'duplicate md5sums entry: {name}')
        expected_hashes[name] = match[1]
    payload, checksums = {}, {}
    with archive_members(package, '--fsys-tarfile') as archive:
        for member in archive:
            name = payload_path(member.name)
            if not name:
                continue
            if name in payload:
                problems.append(f'duplicate payload path: {name}')
            payload[name] = member
            if not name.startswith(('usr/', 'etc/')) and name not in ('usr', 'etc'):
                problems.append(f'unexpected payload location: {name}')
            if member.uid != 0 or member.gid != 0:
                problems.append(f'payload is not root-owned: {name}')
            if not (member.isfile() or member.isdir() or member.issym()):
                problems.append(f'unexpected payload file type: {name}')
            if member.mode & 0o022 and not member.issym():
                problems.append(f'payload is writable by group or others: {name}')
            if member.mode & 0o6000 and name != SANDBOX:
                problems.append(f'unexpected setuid/setgid file: {name}')
            if member.isfile():
                checksums[name] = payload_digest(archive.extractfile(member), name, problems)
    for name, member in payload.items():
        # A later entry must not overwrite a previously declared symlink's
        # target, or traverse a regular file as a directory when unpacked.
        for parent in PurePosixPath(name).parents:
            ancestor = payload.get(str(parent))
            if ancestor is not None and not ancestor.isdir():
                problems.append(f'payload parent is not a directory: {name} through {parent}')
                break
        if member.issym():
            try:
                resolved_payload_path(name, payload)
            except RuntimeError as error:
                problems.append(str(error))
    for name, checksum in checksums.items():
        if expected_hashes.get(name) != checksum:
            problems.append(f'missing or incorrect payload checksum: {name}')
    for name in expected_hashes.keys() - checksums.keys():
        problems.append(f'md5sums names an absent file: {name}')
    conffile_lines = control_files.get('conffiles', b'').decode().splitlines()
    conffiles = set(conffile_lines)
    if len(conffiles) != len(conffile_lines):
        problems.append('duplicate conffiles entry')
    for name in conffiles:
        if not name.startswith('/etc/') or name.lstrip('/') not in checksums:
            problems.append(f'conffile is absent or outside /etc: {name}')
    # Every shipped /etc file is user-editable and must survive package upgrades.
    for name in checksums:
        if name.startswith('etc/') and '/' + name not in conffiles:
            problems.append(f'configuration file is not registered as a conffile: {name}')
    sandbox = payload.get(SANDBOX)
    if sandbox is None or not sandbox.isfile() or sandbox.mode != 0o4755:
        problems.append('Chromium sandbox helper must be a root-owned 4755 regular file')
    with package.open('rb') as stream:
        sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    summary = {
        'package': fields.get('Package'), 'version': fields.get('Version'),
        'architecture': fields.get('Architecture'),
        'filename': f'{fields.get("Package")}_{fields.get("Version")}_{fields.get("Architecture")}.deb',
        'size_bytes': package.stat().st_size,
        'sha256': sha256,
        'payload_files': len(checksums), 'dependencies': dependencies,
        'conffiles': sorted(conffiles), 'verification': 'passed' if not problems else 'failed',
    }
    return problems, summary


def smoke_unpack(package):
    """Use dpkg's real unpack/database path in a disposable root, with no scripts.

    This is an unpack test. Configuring and launching the Wayland session still
    require an installed system. Refuse archives with a preinst, since dpkg
    would execute that script even for --unpack.
    """
    with archive_members(package, '--ctrl-tarfile') as archive:
        for member in archive:
            if payload_path(member.name) == 'preinst':
                raise RuntimeError('rootless unpack smoke test requires a package without preinst')
    with tempfile.TemporaryDirectory(prefix='whitecube-unpack-') as tmp:
        root = Path(tmp) / 'root'
        command = [
            'dpkg', '--root=' + str(root), '--log=' + str(Path(tmp) / 'dpkg.log'),
            '--force-not-root', '--no-triggers', '--unpack', str(package.resolve()),
        ]
        # dpkg 1.22 still attempts chown(0) under --force-not-root. That uid is
        # deliberately unmapped in the builder's ordinary-user namespace.
        if os.geteuid() != 0 and not os.environ.get('FAKEROOTKEY'):
            fakeroot = shutil.which('fakeroot')
            if fakeroot is None:
                raise RuntimeError('fakeroot is required for the rootless dpkg unpack smoke test')
            command.insert(0, fakeroot)
        result = subprocess.run(command, text=True, capture_output=True,
                                env=dict(os.environ, FAKEROOTDONTTRYCHOWN='1'))
        if result.returncode:
            raise RuntimeError('dpkg unpack smoke test failed: ' + result.stderr.strip())
        info = root / 'var/lib/dpkg/info'
        fields = Parser().parsestr((root / 'var/lib/dpkg/status').read_text())
        if fields.get('Package') != PACKAGE or fields.get('Status') != 'install ok unpacked':
            raise RuntimeError('dpkg did not register the unpacked package')
        config = set((info / (PACKAGE + '.conffiles')).read_text().splitlines())
        for line in (info / (PACKAGE + '.md5sums')).read_text().splitlines():
            checksum, name = line.split('  ', 1)
            installed = root / name
            if '/' + name in config:
                installed = installed.with_name(installed.name + '.dpkg-new')
            if not installed.is_file():
                raise RuntimeError(f'dpkg did not unpack {name}')
            with installed.open('rb') as stream:
                if hashlib.file_digest(stream, 'md5').hexdigest() != checksum:
                    raise RuntimeError(f'dpkg unpacked inconsistent data for {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--json', type=Path, help='write a machine-readable release manifest')
    parser.add_argument('--unpack', action='store_true', help='also unpack with dpkg into a disposable package root')
    args = parser.parse_args()
    try:
        problems, summary = check(args.package)
    except (OSError, RuntimeError, tarfile.TarError, ValueError) as error:
        print(f'verify-deb: {error}', file=sys.stderr)
        return 1
    if problems:
        print('verify-deb: failed\n  - ' + '\n  - '.join(problems), file=sys.stderr)
        return 1
    if args.unpack:
        try:
            smoke_unpack(args.package)
        except (OSError, RuntimeError, tarfile.TarError, ValueError) as error:
            print(f'verify-deb: {error}', file=sys.stderr)
            return 1
        summary['unpack_smoke'] = 'passed'
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2) + '\n')
    print(f'verify-deb: passed ({summary["payload_files"]} payload files; ownership, modes, conffiles and checksums)')
    if args.unpack:
        print('verify-deb: dpkg unpack and installed package database/checksums passed in a disposable root')
    return 0


if __name__ == '__main__':
    sys.exit(main())
