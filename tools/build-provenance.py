#!/usr/bin/python3 -I
"""Write the source and toolchain inputs used by a Whitecube package build."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('whitecube_build_inputs', ROOT / 'tools/build-inputs.py')
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


def output(command, root=ROOT):
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def collect(stage, root=ROOT):
    commit = output(['git', 'rev-parse', 'HEAD'], root)
    changes = output(['git', 'status', '--porcelain', '--untracked-files=normal'], root)
    source = {'commit': commit, 'dirty': bool(changes) if changes is not None else None}
    if source['dirty']:
        diff = output(['git', 'diff', 'HEAD', '--binary'], root) or ''
        source['changes_sha256'] = hashlib.sha256((changes + '\n' + diff).encode()).hexdigest()
    paths = (*inputs.BUILDER_RECIPES, *inputs.ELECTRON_RECIPES, 'BUILD.sh', 'meson.build')
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}
    runtime = {'electron': inputs.validate_electron(stage / 'usr/lib/whitecube/desktop-chat-runtime', inputs.electron_version(root)),
               'node': output([os.environ.get('NODE', 'node'), '--version'], root),
               'python': sys.version.split()[0],
               'compiler': output(shlex.split(os.environ.get('CC', 'cc')) + ['--version'], root),
               'meson': output(['meson', '--version'], root)}
    if runtime['compiler']:
        runtime['compiler'] = runtime['compiler'].splitlines()[0]
    packages = output(['dpkg-query', '-W', '-f=${binary:Package}=${Version}\n',
                       'libc6', 'gcc', 'libwlroots-0.18-dev', 'libcairo2-dev'], root)
    return {'format': 'org.whitecube.build-provenance', 'version': 1,
            'source': source, 'inputs_sha256': hashes, 'runtime': runtime,
            'builder_recipe_sha256': inputs.digest_inputs(root, inputs.BUILDER_RECIPES),
            'builder_packages': packages.splitlines() if packages else [],
            'source_date_epoch': os.environ.get('SOURCE_DATE_EPOCH')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', type=Path)
    args = parser.parse_args()
    destination = args.stage / 'usr/share/doc/whitecube-desktop/build-provenance.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(collect(args.stage), indent=2) + '\n')
    destination.chmod(0o644)


if __name__ == '__main__':
    main()
