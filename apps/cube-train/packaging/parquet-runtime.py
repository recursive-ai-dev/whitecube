#!/usr/bin/python3 -I
"""Prepare and verify a pinned private CPython 3.13 Parquet decoder.

The wheel is obtained from PyPI and verified before extraction. It is never
installed into host Python. GTK and training environments remain independent.
"""
import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

APP = Path(__file__).resolve().parent.parent
PIN = Path(__file__).with_suffix('.json')
RUNTIME = APP / 'parquet-runtime'
CACHE = APP / '.deps/parquet-downloads'


def configuration():
    return json.loads(PIN.read_text())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def payload_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('unsafe wheel path')
    if not path.parts or path.parts[0] not in ('pyarrow', 'pyarrow-' + configuration()['pyarrow_version'] + '.dist-info'):
        return False
    # Headers, Cython sources, upstream tests and build metadata are not used
    # by the decoder. Retain Python/native modules and all upstream licenses.
    if any(part in ('tests', 'include', 'src', '__pycache__') for part in path.parts):
        return False
    if path.suffix in ('.pxd', '.pyx', '.h', '.hpp', '.cpp', '.c', '.pyc'):
        return False
    if path.name.startswith('_pyarrow_cpp_tests') or path.name == 'RECORD':
        return False
    return True


def check(runtime, *, probe=False):
    pin = configuration()
    if json.loads((runtime / 'runtime.json').read_text()) != pin:
        raise ValueError('private Parquet runtime manifest does not match pinned inputs')
    hashes = json.loads((runtime / 'files.sha256.json').read_text())
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError('private Parquet runtime checksum manifest is empty')
    actual = {str(path.relative_to(runtime)) for path in runtime.rglob('*') if path.is_file()}
    if actual != set(hashes) | {'runtime.json', 'files.sha256.json'}:
        raise ValueError('private Parquet runtime has missing or unexpected files')
    for name, expected in hashes.items():
        member = PurePosixPath(name)
        if member.is_absolute() or '..' in member.parts:
            raise ValueError('unsafe runtime checksum path')
        path = runtime / name
        if path.is_symlink() or digest(path) != expected:
            raise ValueError('private Parquet runtime checksum mismatch: ' + name)
    distribution = runtime / ('pyarrow-' + pin['pyarrow_version'] + '.dist-info')
    metadata = Parser().parsestr((distribution / 'METADATA').read_text())
    if metadata['Name'] != 'pyarrow' or metadata['Version'] != pin['pyarrow_version'] or metadata.get_all('Requires-Dist'):
        raise ValueError('unexpected PyArrow distribution metadata/dependencies')
    for name in ('LICENSE.txt', 'NOTICE.txt'):
        if not (distribution / 'licenses' / name).is_file():
            raise ValueError('PyArrow license or notice is missing')
    if probe:
        # -S deliberately excludes system NumPy/PyArrow. Schema, string scalar,
        # dictionary and Parquet I/O must work in the private decoder alone.
        program = '''import importlib.util,json,pathlib,sys,tempfile
assert sys.version_info[:2] == (3,13)
assert importlib.util.find_spec('numpy') is None
sys.path.insert(0,sys.argv[1])
import pyarrow as pa
import pyarrow.parquet as pq
assert pa.__version__ == sys.argv[2]
with tempfile.TemporaryDirectory() as temp:
 p=pathlib.Path(temp)/'fixture.parquet'
 table=pa.table({'text':pa.array(['first document','second document',None],type=pa.string())})
 pq.write_table(table,p)
 parquet=pq.ParquetFile(p)
 assert parquet.schema_arrow.field('text').type == pa.string()
 assert [scalar.as_py() for batch in parquet.iter_batches(columns=['text']) for scalar in batch.column(0)] == ['first document','second document',None]
print(json.dumps({'pyarrow':pa.__version__,'numpy':False,'parquet':'passed'}))
'''
        subprocess.run([pin['python'], '-I', '-B', '-S', '-c', program, str(runtime.resolve()), pin['pyarrow_version']], check=True)
    return pin


def prepare():
    pin = configuration()
    try:
        check(RUNTIME)
        return RUNTIME
    except (OSError, ValueError):
        pass
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / pin['wheel']['filename']
    if not archive.is_file() or digest(archive) != pin['wheel']['sha256']:
        fd, temporary = tempfile.mkstemp(prefix='.pyarrow-', dir=CACHE)
        os.close(fd)
        temporary = Path(temporary)
        try:
            with urllib.request.urlopen(pin['wheel']['url'], timeout=60) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target)
            if digest(temporary) != pin['wheel']['sha256']:
                raise ValueError('PyArrow wheel SHA256 does not match the pinned PyPI artifact')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='.parquet-runtime-', dir=APP) as temporary:
        staged = Path(temporary) / 'runtime'
        staged.mkdir()
        hashes = {}
        with zipfile.ZipFile(archive) as wheel:
            for item in wheel.infolist():
                if item.is_dir() or not payload_member(item.filename):
                    continue
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError('wheel symlinks are not accepted')
                if item.filename in hashes:
                    raise ValueError('duplicate wheel payload path')
                target = staged / item.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(wheel.read(item))
                target.chmod(0o644)
                hashes[item.filename] = digest(target)
        (staged / 'runtime.json').write_text(json.dumps(pin, indent=2) + '\n')
        (staged / 'files.sha256.json').write_text(json.dumps(hashes, sort_keys=True, indent=2) + '\n')
        for folder in staged.rglob('*'):
            if folder.is_dir():
                folder.chmod(0o755)
        staged.chmod(0o755)
        check(staged)
        if RUNTIME.exists():
            shutil.rmtree(RUNTIME)
        staged.replace(RUNTIME)
    return RUNTIME


def abi_dependencies(runtime):
    """Derive standalone runtime ABI minima from the shipped wheel libraries."""
    binaries = []
    for path in sorted((runtime / 'pyarrow').glob('*')):
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            header = stream.read(64)
        if header.startswith(b'\x7fELF'):
            if len(header) < 64 or header[4:7] != b'\x02\x01\x01' or int.from_bytes(header[18:20], 'little') != 62:
                raise ValueError('private Parquet runtime contains a non-amd64 ELF')
            result = subprocess.run(['ldd', str(path)], env=dict(os.environ, LD_LIBRARY_PATH=''), capture_output=True, text=True)
            if result.returncode or 'not found' in result.stdout:
                raise ValueError('private Parquet runtime has unresolved native libraries: ' + path.name)
            binaries.append(path)
    if not binaries:
        raise ValueError('private Parquet runtime has no native libraries')
    with tempfile.TemporaryDirectory(prefix='trainer-parquet-shlibdeps-') as temporary:
        metadata = Path(temporary) / 'debian'
        metadata.mkdir()
        (metadata / 'control').write_text('Source: whitecube-trainer\nSection: science\nPriority: optional\nMaintainer: Whitecube <whitecube@localhost>\n\nPackage: whitecube-trainer\nArchitecture: amd64\nDescription: Trainer decoder\n')
        result = subprocess.run(['dpkg-shlibdeps', '-O', '--ignore-missing-info', '--warnings=0',
                                 '-l' + str((runtime / 'pyarrow').resolve()),
                                 *('-e' + str(path.resolve()) for path in binaries)],
                                cwd=temporary, capture_output=True, text=True)
    if result.returncode:
        raise ValueError('cannot derive private Parquet runtime ABI dependencies: ' + result.stderr.strip())
    values = [line.removeprefix('shlibs:Depends=') for line in result.stdout.splitlines() if line.startswith('shlibs:Depends=')]
    if len(values) != 1 or not values[0]:
        raise ValueError('private Parquet runtime produced no ABI dependencies')
    return values[0]


def stage(destination):
    source = prepare()
    if destination.exists():
        raise ValueError('private Parquet runtime destination must be absent')
    shutil.copytree(source, destination)
    check(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare')
    staging = commands.add_parser('stage')
    staging.add_argument('destination', type=Path)
    checking = commands.add_parser('check')
    checking.add_argument('runtime', type=Path, nargs='?', default=RUNTIME)
    checking.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            print(prepare())
        elif args.command == 'stage':
            print(stage(args.destination))
        else:
            check(args.runtime, probe=args.probe)
            print('private Parquet runtime: verified')
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print('parquet-runtime: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
