#!/usr/bin/python3 -I
"""Test real Debian package installation and upgrades in a disposable namespace.

Bootstrap only essential packages, declared Depends and verification tools. No
host packages, services or home directories are changed. Network is needed only
to acquire authenticated Debian packages. A successful root is retained for the
separate installed Wayland runtime smoke test.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parent.parent


def run(command, log, *, cwd=None, timeout=1200):
    with log.open('a') as stream:
        stream.write('+ ' + repr([str(item) for item in command]) + '\n')
        stream.flush()
        result = subprocess.run(list(map(str, command)), cwd=cwd, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{command[0]} failed ({result.returncode}); inspect {log}')


def development_packages(inventory):
    return [line.split('\t')[0] for line in inventory.splitlines()
            if line.split('\t')[0].split(':', 1)[0].endswith('-dev')]


def verify_package_payload(log, *, allow_modified_conffiles=False):
    # dpkg --verify describes content differences on stdout. Inspect them even
    # when dpkg exits successfully, and allow edited conffiles only on upgrade.
    command = ['dpkg', '--verify-format=rpm', '--verify', 'whitecube-desktop']
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    with log.open('a') as stream:
        stream.write('+ ' + repr(command) + '\n' + result.stdout + result.stderr)
    differences = result.stdout.splitlines()
    if allow_modified_conffiles:
        differences = [line for line in differences if not
                       (len(line.split(maxsplit=2)) == 3 and line.split(maxsplit=2)[1] == 'c')]
    if result.returncode or differences:
        raise RuntimeError('installed package payload verification failed; inspect ' + str(log))


def inside():
    """Runs only in the private writable root under fakeroot."""
    report = Path('/report')
    checks = []
    root = Path('/')
    package = Path('/repo/current.deb')
    previous = Path('/repo/previous.deb')
    verifier_spec = importlib.util.spec_from_file_location('archive_verifier', '/repo/verify-deb.py')
    verifier = importlib.util.module_from_spec(verifier_spec)
    verifier_spec.loader.exec_module(verifier)

    def command(*argv):
        run(argv, report / 'lifecycle.log')

    def record(message):
        checks.append(message)
        (report / 'lifecycle-inside.json').write_text(json.dumps({'verification': 'running', 'checks': checks}, indent=2) + '\n')

    def registered():
        result = subprocess.run(['dpkg-query', '-W', '-f=${Status}', 'whitecube-desktop'],
                                capture_output=True, text=True)
        return result.stdout.strip()

    def verify_installed(*, allow_modified_conffiles=False):
        if registered() != 'install ok installed':
            raise RuntimeError('Whitecube did not configure successfully')
        command('apt-get', 'check')
        verify_package_payload(report / 'lifecycle.log', allow_modified_conffiles=allow_modified_conffiles)
        command('/usr/bin/whitecube-session', '--check')
        command('/usr/bin/cube-status', '--selftest')
        command('/usr/bin/whitecube-doctor', '--strict', '--check', 'package', '--check', 'libraries')
        for name, files in verifier.STANDALONE_FILES.items():
            for absolute, content in files.items():
                if Path(absolute).read_bytes() != content:
                    raise RuntimeError('standalone package overwritten: ' + absolute)
                result = subprocess.run(['dpkg-query', '-S', absolute], capture_output=True, text=True, check=True)
                if result.stdout.strip() != name + ': ' + absolute:
                    raise RuntimeError('standalone package ownership changed: ' + absolute)

    def install(path):
        command('apt-get', '-y', '--no-install-recommends', '-o', 'Dpkg::Options::=--force-confold',
                '-o', 'Dpkg::Options::=--force-confdef', 'install', str(path))

    # Configure the runtime dependencies without installing Whitecube yet. All
    # archives were downloaded by mmdebstrap using signed Debian metadata.
    dependencies = [path for path in sorted(Path('/var/cache/apt/archives').glob('*.deb'))
                    if not path.name.startswith('whitecube-desktop_')]
    command('dpkg', '--force-depends', '--force-confold', '--force-confdef', '--unpack', *map(str, dependencies))
    command('dpkg', '--force-depends', '--configure', '-a')
    command('apt-get', '-y', '--no-install-recommends', '-f', 'install')
    command('apt-get', 'check')
    record('configured essential packages, declared runtime dependencies and test tools')

    for fixture in sorted(Path('/repo/fixtures').glob('*.deb')):
        command('dpkg', '--install', str(fixture))
    record('standalone Browser, Chrootables and Trainer coexistence fixtures installed')

    install(package)
    verify_installed()
    record('clean apt installation, maintainer scripts and installed binary/library checks')
    # A small private home is a sentinel for the package's never-edit-user-files contract.
    home = Path('/home/lifecycle/.config/whitecube')
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    user_config = home / 'config'
    user_config.write_text('user setting sentinel\n')
    config = Path('/etc/whitecube/config')
    config.write_text(config.read_text() + '\n# lifecycle conffile sentinel\n')
    command('apt-get', '-y', 'remove', 'whitecube-desktop')
    if Path('/usr/bin/whitecube-compositor').exists() or not config.exists():
        raise RuntimeError('remove did not remove binaries and preserve conffiles')
    command('apt-get', '-y', 'purge', 'whitecube-desktop')
    if config.exists() or user_config.read_text() != 'user setting sentinel\n':
        raise RuntimeError('purge did not remove conffiles or modified user settings')
    record('remove/purge clean registered paths and preserve account settings')

    if previous.exists():
        install(previous)
        verify_installed()
        config.write_text(config.read_text() + '\n# lifecycle upgrade sentinel\n')
        old_config = config.read_bytes()
        install(package)
        verify_installed(allow_modified_conffiles=True)
        if config.read_bytes() != old_config or user_config.read_text() != 'user setting sentinel\n':
            raise RuntimeError('upgrade failed to preserve system conffile or user settings')
        record('upgrade from previous release preserves modified conffiles and user settings')
    else:
        install(package)
        verify_installed()

    installed = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n'],
                               text=True, capture_output=True, check=True)
    (report / 'installed-packages.tsv').write_text(installed.stdout)
    if development_packages(installed.stdout):
        raise RuntimeError('runtime-only root unexpectedly contains development packages')
    record('runtime package inventory contains no development packages')
    (report / 'lifecycle-inside.json').write_text(json.dumps({'verification': 'passed', 'checks': checks}, indent=2) + '\n')
    return 0


def outside(args):
    output = args.output.absolute()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError('--output must be absent or empty')
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name in ('bwrap', 'dpkg-deb', 'dpkg-scanpackages'):
        if not shutil.which(name):
            raise RuntimeError('required host tool is missing: ' + name)
    bootstrap = args.mmdebstrap.resolve(strict=True)
    keyring = args.keyring.resolve(strict=True)
    package = args.deb.resolve(strict=True)
    if args.previous_deb:
        args.previous_deb = args.previous_deb.resolve(strict=True)
    repository = output / 'repository'
    repository.mkdir(mode=0o755)
    shutil.copy2(package, repository / 'current.deb')
    packages = subprocess.run(['dpkg-scanpackages', '--multiversion', '.'], cwd=repository,
                              text=True, capture_output=True, check=True)
    (repository / 'Packages').write_text(packages.stdout)
    root = output / 'rootfs'
    report = {'verification': 'failed', 'checks': [], 'source': {'deb': package.name},
              'scope': 'real apt/dpkg install, upgrade, remove and purge in a disposable Debian root with declared runtime dependencies',
              'limitations': ['No booted systemd, physical hardware or real Chromium setuid sandbox verification.']}
    with package.open('rb') as stream:
        report['source']['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    started = time.monotonic()
    try:
        include = 'apt,fakeroot,python3,ca-certificates,whitecube-desktop'
        run([str(bootstrap), '--mode=chrootless', '--variant=extract', '--architectures=amd64',
             '--skip=cleanup/apt', '--aptopt=APT::Install-Recommends "false"',
             '--keyring=' + str(keyring), '--include=?essential', '--include=' + include,
             'trixie', str(root), 'https://deb.debian.org/debian',
             'deb [trusted=yes] copy:' + str(repository) + ' ./'], output / 'bootstrap.log')
        report['checks'].append('authenticated Debian runtime-only bootstrap')
        (root / 'repo').mkdir(mode=0o755)
        shutil.copy2(package, root / 'repo/current.deb')
        if args.previous_deb:
            shutil.copy2(args.previous_deb, root / 'repo/previous.deb')
        shutil.copy2(PROJECT / 'packaging/verify-deb.py', root / 'repo/verify-deb.py')
        spec = importlib.util.spec_from_file_location('lifecycle_verifier', PROJECT / 'packaging/verify-deb.py')
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        (root / 'repo/fixtures').mkdir()
        verifier.standalone_fixtures(root / 'repo/fixtures')
        # No service can reach the running host. The namespace has its own
        # /run, PID tree, /dev and network, and policy-rc.d blocks service starts.
        policy = root / 'usr/sbin/policy-rc.d'
        policy.write_text('#!/bin/sh\nexit 101\n')
        policy.chmod(0o755)
        (root / 'etc/apt/sources.list').write_text('')
        for entry in (root / 'etc/apt/sources.list.d').glob('*'):
            if entry.is_file():
                entry.unlink()
        for database in ('passwd', 'group'):
            path = root / 'etc' / database
            if not path.exists():
                shutil.copy2(root / 'usr/share/base-passwd' / (database + '.master'), path)
        awk = root / 'usr/bin/awk'
        if not awk.exists():
            awk.unlink(missing_ok=True)
            awk.symlink_to('mawk')
        shell = root / 'usr/bin/sh'
        if not shell.exists():
            shell.unlink(missing_ok=True)
            shell.symlink_to('dash')
        for directory in ('report', 'run', 'dev', 'proc', 'tmp', 'home', 'root'):
            (root / directory).mkdir(exist_ok=True)
        (root / 'lifecycle.py').touch()
        run(['bwrap', '--clearenv', '--bind', str(root), '/', '--dev', '/dev', '--proc', '/proc',
             '--tmpfs', '/run', '--tmpfs', '/tmp', '--bind', str(output), '/report',
             '--ro-bind', str(Path(__file__).resolve()), '/lifecycle.py',
             '--unshare-user', '--uid', '0', '--gid', '0', '--unshare-pid', '--unshare-net',
             '--unshare-ipc', '--new-session', '--die-with-parent', '--chdir', '/',
             '--setenv', 'PATH', '/usr/sbin:/usr/bin:/sbin:/bin', '--setenv', 'HOME', '/root',
             '--setenv', 'DEBIAN_FRONTEND', 'noninteractive', '--setenv', 'FAKEROOTDONTTRYCHOWN', '1',
             '--setenv', 'LANG', 'C.UTF-8', '/usr/bin/fakeroot-sysv', '/usr/bin/python3', '-I',
             '/lifecycle.py', '--inside'], output / 'namespace.log')
        inner = json.loads((output / 'lifecycle-inside.json').read_text())
        report['checks'].extend(inner['checks'])
        report['verification'] = inner['verification']
        # GTK/D-Bus smoke runs as the normal user in another namespace.
        uid, gid = os.getuid(), os.getgid()
        for database, entry in [('passwd', f'whitecube-smoke:x:{uid}:{gid}:Smoke:/home/smoke:/bin/sh'),
                                ('group', f'whitecube-smoke:x:{gid}:')]:
            path = root / 'etc' / database
            text = path.read_text()
            if not any(line.split(':')[2:3] == [str(uid if database == 'passwd' else gid)] for line in text.splitlines()):
                path.write_text(text.rstrip('\n') + '\n' + entry + '\n')
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        report['verification'] = 'failed'
        progress = output / 'lifecycle-inside.json'
        if progress.is_file():
            try:
                for completed in json.loads(progress.read_text()).get('checks', []):
                    if completed not in report['checks']:
                        report['checks'].append(completed)
            except (OSError, ValueError, TypeError):
                pass
        report['error'] = str(error)
        print('package lifecycle failed: ' + str(error), file=sys.stderr)
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'Package lifecycle: {report["verification"]}; report: {output / "report.json"}')
    return 0 if report['verification'] == 'passed' else 1


def main():
    if sys.argv[1:] == ['--inside']:
        return inside()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deb', type=Path, required=True)
    parser.add_argument('--previous-deb', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mmdebstrap', type=Path, default=PROJECT / 'build/debian-builder/host/usr/bin/mmdebstrap')
    parser.add_argument('--keyring', type=Path, default=PROJECT / 'build/debian-builder/host/usr/share/keyrings/debian-archive-keyring.gpg')
    args = parser.parse_args()
    try:
        return outside(args)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print('package lifecycle: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
