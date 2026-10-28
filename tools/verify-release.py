#!/usr/bin/python3 -I
"""Verify a built release archive, isolated protocols, and its installed runtime.

Run after tools/build-debian.sh. Retains each command's log, archive manifest,
installed-window captures and a report.json, including when a check fails.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent


def validate_release_source(manifest, expected):
    source = (manifest.get('build_provenance') or {}).get('source') or {}
    if (manifest.get('version') != expected['version'] or source.get('commit') != expected['commit']
            or source.get('dirty') is not False):
        raise RuntimeError('archive provenance must match the clean release source commit and package version')


def checks(root, package, rootfs, output, previous=None):
    builder = str(root / 'tools/build-debian.sh')
    steps = [
        ('archive', [sys.executable, '-I', str(root / 'packaging/verify-deb.py'), str(package),
                     '--unpack', '--json', str(output / 'archive.json')], 300),
        ('session-lock', ['bash', builder, '--', 'bash', '-euc',
                          'source build/native-deps/environment.sh; python3 -I tests/session-lock-test.py --require-headless --compositor build/whitecube'], 300),
        ('settings', ['bash', builder, '--', 'bash', '-euc',
                      'source build/native-deps/environment.sh; python3 -I tests/settings-integration-test.py'], 300),
    ]
    if previous is not None:
        steps.append(('package-lifecycle', [sys.executable, '-I', str(root / 'packaging/smoke-lifecycle.py'),
                                           '--deb', str(package), '--previous-deb', str(previous),
                                           '--output', str(output / 'lifecycle')], 1800))
        rootfs = output / 'lifecycle/rootfs'
    steps += [
        ('installed-runtime', [sys.executable, '-I', str(root / 'packaging/smoke-installed.py'),
                               '--rootfs', str(rootfs), '--deb', str(package),
                               '--output', str(output / 'installed-runtime')], 900),
    ]
    return steps


def run_checks(steps, output, report, env=None):
    report['verification'] = 'failed'
    report['checks'] = []
    for name, command, timeout in steps:
        started = time.monotonic()
        entry = {'name': name, 'command': command, 'log': name + '.log'}
        with (output / entry['log']).open('w') as log:
            try:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        text=True, timeout=timeout)
                entry['returncode'] = result.returncode
                if name == 'archive' and result.returncode == 0 and report.get('expected_source'):
                    validate_release_source(json.loads((output / 'archive.json').read_text()), report['expected_source'])
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                entry['returncode'] = 1
                entry['error'] = str(error)
                log.write(str(error) + '\n')
        entry['elapsed_seconds'] = round(time.monotonic() - started, 2)
        report['checks'].append(entry)
        if entry['returncode']:
            report['failed_check'] = name
        elif len(report['checks']) == len(steps):
            report['verification'] = 'passed'
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(f'{name}: {"passed" if entry["returncode"] == 0 else "FAILED"} ({output / entry["log"]})', flush=True)
        if entry['returncode']:
            return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deb', type=Path, required=True)
    parser.add_argument('--previous-deb', type=Path, help='also test install/upgrade/removal in a minimal runtime root and smoke its dependencies')
    parser.add_argument('--rootfs', type=Path, default=ROOT / 'build/debian-builder/rootfs')
    parser.add_argument('--output', type=Path, required=True, help='new or empty report directory')
    parser.add_argument('--list', action='store_true', help='print commands without executing or writing files')
    args = parser.parse_args()
    package, rootfs, output = args.deb.absolute(), args.rootfs.absolute(), args.output.absolute()
    previous = args.previous_deb.absolute() if args.previous_deb else None
    steps = checks(ROOT, package, rootfs, output, previous)
    if args.list:
        print(json.dumps([{'name': name, 'command': command, 'timeout': timeout} for name, command, timeout in steps], indent=2))
        return 0
    if previous is not None and not previous.is_file():
        parser.error('--previous-deb must identify an existing release archive')
    if not package.is_file() or not (rootfs / 'usr/bin/python3').is_file():
        parser.error('--deb and --rootfs must identify a built archive and prepared Debian root')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error('--output must be absent or empty')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        parser.error('commit the release source before verification; the working tree must be clean')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with package.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    report = {'archive': str(package), 'sha256': checksum, 'format': 'org.whitecube.release-verification', 'version': 1,
              'expected_source': {'commit': commit, 'version': (ROOT / 'packaging/VERSION').read_text().strip()}}
    env = dict(os.environ, WHITECUBE_BUILDER_DIR=str(rootfs.parent))
    return run_checks(steps, output, report, env)


if __name__ == '__main__':
    sys.exit(main())
