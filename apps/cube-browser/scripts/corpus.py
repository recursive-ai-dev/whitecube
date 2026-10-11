#!/usr/bin/env python3
"""Render ten deterministic HTTP documents at two widths with an isolated profile."""
import argparse
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, default=ROOT/'build/cube-browser')
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=True)
    directory = ROOT/'tests/corpus'
    cases = json.loads((directory/'manifest.json').read_text())
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
        functools.partial(Handler, directory=directory))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix='cube-corpus-') as temporary:
            env = dict(os.environ)
            env.pop('DISPLAY', None)
            env.pop('WAYLAND_DISPLAY', None)
            env['HOME'] = temporary
            for kind in ('CONFIG', 'DATA', 'STATE', 'CACHE'):
                env[f'XDG_{kind}_HOME'] = str(Path(temporary)/kind.lower())
            def run(*args):
                return subprocess.run([str(options.browser.resolve()), *args], env=env,
                    capture_output=True, text=True, timeout=45, check=True)
            version = run('--version').stdout.strip()
            for case in cases:
                url = f'http://127.0.0.1:{server.server_port}/{case["path"]}'
                plain = run('--dump', url).stdout
                assert case['text'] in plain, f'{case["name"]}: missing visible text'
                for width in (1120, 640):
                    output = options.output/f'{case["name"]}-{width}.png'
                    started = time.monotonic()
                    run('--render', url, str(output.resolve()), f'{width}x800')
                    elapsed = round((time.monotonic()-started)*1000, 2)
                    png = output.read_bytes()
                    assert png.startswith(b'\x89PNG\r\n\x1a\n'), f'{output}: invalid image'
                    results.append(dict(document=case['name'], width=width, milliseconds=elapsed,
                        png_bytes=len(png), sha256=hashlib.sha256(png).hexdigest()))
            assert not any((Path(temporary)/'data').rglob('*')), 'headless run persisted data'
        report = dict(version=version, documents=len(cases), renders=len(results),
            median_ms=round(statistics.median(r['milliseconds'] for r in results), 2),
            results=results)
        (options.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(f'{len(cases)} documents, {len(results)} renders; median {report["median_ms"]} ms')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

if __name__ == '__main__':
    main()
