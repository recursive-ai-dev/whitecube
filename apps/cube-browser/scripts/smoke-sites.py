#!/usr/bin/env python3
"""Supplementary live-network exports, always using a temporary private profile."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
options = parser.parse_args()
options.output.mkdir(parents=True, exist_ok=True)
browser = ROOT/'build/cube-browser'
results = []
with tempfile.TemporaryDirectory(prefix='cube-live-smoke-') as temporary:
    env = dict(os.environ, HOME=temporary)
    for key in ('CONFIG', 'DATA', 'STATE', 'CACHE'):
        env[f'XDG_{key}_HOME'] = str(Path(temporary)/key.lower())
    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET'):
        env.pop(key, None)
    version = subprocess.check_output([str(browser), '--version'], env=env, text=True).strip()
    for name, url in (('example', 'https://example.com'), ('debian', 'https://www.debian.org')):
        for mode in ('dump', 'render'):
            command = [str(browser), '--'+mode, url]
            if mode == 'render': command.append(str((options.output/(name+'.png')).resolve()))
            started = time.monotonic()
            try:
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=50)
                if mode == 'dump': (options.output/(name+'.txt')).write_text(result.stdout)
                row = dict(site=name, url=url, mode=mode, returncode=result.returncode,
                           seconds=round(time.monotonic()-started, 3), diagnostics=result.stderr.strip())
            except subprocess.TimeoutExpired:
                row = dict(site=name, url=url, mode=mode, returncode=None, seconds=50, diagnostics='Command timed out')
            results.append(row)
            print(f'{name} {mode}: status {row["returncode"]}, {row["seconds"]}s', flush=True)
    assert not any((Path(temporary)/'data').rglob('*')), 'headless export persisted data'
(options.output/'report.json').write_text(json.dumps(dict(version=version, results=results), indent=2)+'\n')
# These sites are supplementary observations, not the deterministic release gate.
