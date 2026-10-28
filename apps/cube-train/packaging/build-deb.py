#!/usr/bin/python3
"""Build the standalone Whitecube Trainer Debian package without root or a compositor."""
from __future__ import annotations

import argparse
import ast
import fcntl
import gzip
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

APP = Path(__file__).resolve().parent.parent
REPO = APP.parent.parent
PACKAGE = 'whitecube-trainer'
spec = importlib.util.spec_from_file_location('trainer_parquet_runtime', APP / 'packaging/parquet-runtime.py')
parquet_runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parquet_runtime)


def version():
    for statement in ast.parse((APP / 'app.py').read_text()).body:
        if isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in statement.targets):
            return ast.literal_eval(statement.value)
    raise ValueError('app.py has no VERSION assignment')


def copy_file(source, target, mode=0o644):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(mode)


def write_file(target, content, mode=0o644):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
    target.chmod(mode)


def launcher(module):
    # Resolve from the launcher so extracted packages can be verified without
    # installation, PYTHONPATH or importing a user's current working directory.
    return f'''#!/usr/bin/python3 -I
"""Standalone Whitecube Trainer launcher."""
import os
from pathlib import Path
import sys

prefix = Path(__file__).resolve().parent.parent
application = prefix / 'lib/whitecube-trainer/cube-train/{module}.py'
if not application.is_file():
    print('Whitecube Trainer files are missing; reinstall whitecube-trainer.', file=sys.stderr)
    raise SystemExit(1)
os.environ.setdefault('WHITECUBE_TRAIN_COMMAND', 'whitecube-trainer-cli')
os.execv('/usr/bin/python3', ['python3', '-I', '-B', str(application), *sys.argv[1:]])
'''


def stage_tree(stage, release, epoch):
    application = stage / 'usr/lib/whitecube-trainer/cube-train'
    if not (APP / 'cli.py').is_file():
        raise ValueError('cli.py is required for the standalone command line')
    for source in sorted(APP.glob('*.py')):
        copy_file(source, application / source.name)
    for source in sorted((APP / 'pipeline').rglob('*')):
        if source.is_file() and source.suffix in {'.py', '.txt'} and '__pycache__' not in source.parts:
            copy_file(source, application / 'pipeline' / source.relative_to(APP / 'pipeline'))
    parquet_runtime.stage(application / 'parquet-runtime')
    native_dependencies = parquet_runtime.abi_dependencies(application / 'parquet-runtime')
    copy_file(APP.parent / 'cube-apps/cube_theme.py', stage / 'usr/lib/whitecube-trainer/cube-apps/cube_theme.py')
    write_file(stage / 'usr/bin/whitecube-trainer', launcher('app'), 0o755)
    write_file(stage / 'usr/bin/whitecube-trainer-cli', launcher('cli'), 0o755)
    copy_file(APP / 'packaging/whitecube-trainer.desktop', stage / 'usr/share/applications/whitecube-trainer.desktop')
    copy_file(REPO / 'data/icons/cube-train.svg', stage / 'usr/share/icons/hicolor/scalable/apps/whitecube-trainer.svg')
    copy_file(APP / 'packaging/org.whitecube.Trainer.metainfo.xml', stage / 'usr/share/metainfo/org.whitecube.Trainer.metainfo.xml')
    docs = stage / 'usr/share/doc/whitecube-trainer'
    copy_file(APP / 'packaging/copyright', docs / 'copyright')
    copy_file(REPO / 'LICENSE', docs / 'LICENSE')
    copy_file(REPO / 'NOTICE', docs / 'NOTICE')
    copy_file(APP / 'packaging/README.Debian', docs / 'README.Debian')
    (docs / 'README.md.gz').write_bytes(gzip.compress((APP / 'README.md').read_bytes(), mtime=0))
    date = time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(epoch))
    changelog = f'''whitecube-trainer ({release}) unstable; urgency=medium

  * Standalone GTK4 training workshop with diagnostics, run comparison,
    resumable training, previews, benchmarks and notebook exports.

 -- Whitecube contributors <whitecube@localhost>  {date}
'''
    (docs / 'changelog.Debian.gz').write_bytes(gzip.compress(changelog.encode(), mtime=0))
    installed_size = (sum(p.stat().st_size for p in stage.rglob('*') if p.is_file()) + 1023) // 1024
    control = f'''Package: {PACKAGE}
Version: {release}
Architecture: amd64
Section: science
Priority: optional
Maintainer: Whitecube contributors <whitecube@localhost>
Installed-Size: {installed_size}
Depends: {native_dependencies}, python3.13 | python3-pyarrow, python3-numpy, python3 (>= 3.10), python3-gi, python3-cairo, python3-gi-cairo, gir1.2-gtk-4.0 (>= 4.12), gir1.2-adw-1 (>= 1.5), libadwaita-1-0 (>= 1.5)
Recommends: python3-torch, python3-venv, fonts-dejavu-core
Suggests: python3-safetensors, python3-ipykernel, jupyter-notebook
Description: native workshop for training small local language models
 A GTK4 and libadwaita desktop application with reproducible corpus preparation,
 resumable CPU or CUDA training, live metrics, checkpoint comparison, previews,
 benchmarks and portable Jupyter notebook exports. Includes the complete local
 training pipeline and a command-line interface. Uses a configurable Python
 training environment; PyTorch is never imported into the desktop process.
 .
 Runs on existing Linux desktops without the Whitecube compositor. Settings,
 runs and runtime environments remain private to each user.
'''
    write_file(stage / 'DEBIAN/control', control)
    sums = []
    for path in sorted(stage.rglob('*')):
        if path.is_file() and 'DEBIAN' not in path.relative_to(stage).parts:
            sums.append(f'{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(stage)}\n')
    write_file(stage / 'DEBIAN/md5sums', ''.join(sums))
    for path in sorted(stage.rglob('*'), reverse=True):
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o755 if path.parent == stage / 'usr/bin' else 0o644)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    stage.chmod(0o755)
    os.utime(stage, (epoch, epoch))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=APP / 'dist')
    parser.add_argument('--version', default=version(), help='Debian package version (defaults to app VERSION)')
    parser.add_argument('--epoch', type=int, default=None, help='Reproducible build timestamp (or SOURCE_DATE_EPOCH)')
    options = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]*', options.version):
        parser.error('invalid Debian version')
    subprocess.run(['dpkg', '--validate-version', options.version], check=True)
    if options.epoch is None:
        try:
            options.epoch = int(os.environ['SOURCE_DATE_EPOCH'])
        except KeyError:
            options.epoch = int(subprocess.check_output(['git', '-C', str(REPO), 'log', '-1', '--format=%ct'], text=True).strip())
    if options.epoch < 0 or options.epoch > 0xFFFFFFFF:
        parser.error('epoch must fit an unsigned 32-bit timestamp')
    output = options.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.build.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('another package build is using this output directory')
        filename = f'{PACKAGE}_{options.version}_amd64.deb'
        with tempfile.TemporaryDirectory(prefix='.package-', dir=output) as temporary:
            temporary = Path(temporary)
            stage = temporary / 'root'
            stage.mkdir()
            stage_tree(stage, options.version, options.epoch)
            candidate = temporary / filename
            environment = dict(os.environ, SOURCE_DATE_EPOCH=str(options.epoch))
            subprocess.run(['dpkg-deb', '--root-owner-group', '--threads-max=2', '-Zxz', '-z6', '--build', str(stage), str(candidate)], check=True, env=environment)
            subprocess.run(['dpkg-deb', '--info', str(candidate)], check=True, stdout=subprocess.DEVNULL)
            destination = output / filename
            os.replace(candidate, destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        (output / f'{filename}.sha256').write_text(f'{digest}  {filename}\n')
    print(destination)
    print(f'SHA256: {digest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
