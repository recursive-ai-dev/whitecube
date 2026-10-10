#!/usr/bin/env python3
"""Foreground job runner: record exit status even when the user presses Ctrl+C."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from workspace_data import atomic_json, now

def run(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe job record')
    job = json.loads(path.read_text())
    child = None
    terminating = [None]
    def caught(sig, _frame):
        # Interactive Bash already sends Ctrl+C to our entire foreground group.
        # HUP/TERM may target only the wrapper, so forward those to its child.
        if sig != signal.SIGINT:
            terminating[0] = time.monotonic()
            if child and child.poll() is None:
                child.send_signal(sig)
    previous = {sig:signal.signal(sig, caught) for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)}
    code = 127
    try:
        child = subprocess.Popen(job['argv'], cwd=job['cwd'])
        while True:
            try:
                code = child.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if terminating[0] is not None and time.monotonic()-terminating[0] > 1:
                    child.kill()
        if code < 0:
            code = 128-code
    except OSError as exc:
        print(f'Could not launch: {exc}', file=sys.stderr)
    finally:
        atomic_json(path.with_suffix('.result'), {'finished_at':now(), 'exit_code':code, 'status':'finished'})
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return code

def finish_if_missing(path, code):
    # Bash knows the foreground wrapper's status even if SIGKILL prevented its
    # finally block. This records that failure without executing the command.
    path = Path(path)
    code = int(code)
    if not 0 <= code <= 255 or path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe job completion record')
    result = path.with_suffix('.result')
    if not result.exists() and not result.is_symlink():
        atomic_json(result, {'finished_at':now(), 'exit_code':code, 'status':'finished'})

if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--finish-if-missing':
        finish_if_missing(sys.argv[2], sys.argv[3])
    else:
        sys.exit(run(sys.argv[1]))
