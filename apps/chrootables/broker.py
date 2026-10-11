"""Chrootables root helper: started once through sudo, runs chrootable-manager on request.

sudo on this desktop may keep no timestamp at all (timestamp_timeout=0), so
validating a password once and then calling `sudo -n` for every command cannot
work. Instead the window starts this helper with `sudo -S` (the password goes to
sudo once and is never kept) and sends it requests over its stdin.

Protocol: one JSON object per line.
  in:  {"id": N, "run": [ARG, ...]}       run the manager with ARGs
                                          ("merge": true streams stdout too)
       {"id": N, "cancel": TARGET_ID}     SIGTERM the running request TARGET_ID
       {"id": N, "ping": true}            liveness check
  "passive": true on any request keeps it from counting as activity (background
  refreshes must not hold the helper open forever).
  out: {"ready": true, "uid": 0}          once, after start
       {"id": N, "line": TEXT}            each stderr line as it arrives
       {"id": N, "status": S, "stdout": TEXT, "stderr": TEXT}   when done

The helper only ever executes the manager path it was started with, with an
argument list (no shell). It exits when the window closes its stdin (after
letting running operations finish, so nothing is left half-written) or after
`--idle` seconds without a request, which locks the window again.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

MAX_LINE = 1 << 20
MAX_ARGS = 16


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('usage: broker.py MANAGER [--store DIR] [--idle SECONDS]', file=sys.stderr)
        return 64
    manager = Path(argv[1])
    store, idle = None, 900
    options = argv[2:]
    while options:
        option, value, options = options[0], (options[1] if len(options) > 1 else ''), options[2:]
        if option == '--store' and value.startswith('/'):
            store = value
        elif option == '--idle' and value.isdigit():
            idle = int(value)
        else:
            print(f'broker.py: bad option {option}', file=sys.stderr)
            return 64
    if not manager.is_absolute() or not manager.is_file():
        print(f'broker.py: {manager} is not a file', file=sys.stderr)
        return 66

    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'SUDO_USER', 'SUDO_UID', 'SUDO_GID', 'TERM')}
    env['LC_ALL'] = 'C.UTF-8'
    if store:
        env['CHROOTABLE_HOME'] = store

    out_lock = threading.Lock()
    jobs: dict[int, subprocess.Popen] = {}
    jobs_lock = threading.Lock()
    threads: list[threading.Thread] = []
    last_request = [time.monotonic()]

    def send(message: dict):
        data = json.dumps(message) + '\n'
        with out_lock:
            try:
                sys.stdout.write(data)
                sys.stdout.flush()
            except (BrokenPipeError, ValueError):
                pass

    def run(request_id: int, args: list[str], merge: bool, passive: bool):
        try:
            process = subprocess.Popen([str(manager), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT if merge else subprocess.PIPE, text=True,
                                       errors='replace', env=env, start_new_session=True, bufsize=1)
        except OSError as error:
            send({'id': request_id, 'status': 127, 'stdout': '', 'stderr': f'Error: {error}\n'})
            return
        with jobs_lock:
            jobs[request_id] = process
        parts: list[str] = []
        errors: list[str] = []
        if merge:
            reader, stream, collected = None, process.stdout, parts
        else:
            reader = threading.Thread(target=lambda: parts.append(process.stdout.read()), daemon=True)
            reader.start()
            stream, collected = process.stderr, errors
        for line in stream:
            collected.append(line)
            send({'id': request_id, 'line': line.rstrip('\n')})
        process.wait()
        if reader:
            reader.join()
        with jobs_lock:
            jobs.pop(request_id, None)
        if not passive:
            last_request[0] = time.monotonic()
        send({'id': request_id, 'status': process.returncode, 'stdout': ''.join(parts), 'stderr': ''.join(errors)})

    def watchdog():
        while True:
            time.sleep(min(5, max(1, idle // 2)) if idle else 5)
            with jobs_lock:
                running = bool(jobs)
            if idle and not running and time.monotonic() - last_request[0] > idle:
                send({'idle': True})
                os._exit(0)

    threading.Thread(target=watchdog, daemon=True).start()
    send({'ready': True, 'uid': os.geteuid(), 'idle': idle})

    for raw in sys.stdin:
        if len(raw) > MAX_LINE:
            continue
        try:
            request = json.loads(raw)
        except ValueError:
            continue            # never echo unparsable input: it could be a stray password
        if not isinstance(request, dict) or not isinstance(request.get('id'), int):
            continue
        if not request.get('passive'):
            last_request[0] = time.monotonic()
        request_id = request['id']
        if request.get('ping'):
            send({'id': request_id, 'status': 0, 'stdout': 'pong', 'stderr': ''})
        elif 'cancel' in request:
            with jobs_lock:
                process = jobs.get(request.get('cancel'))
            if process:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
            send({'id': request_id, 'status': 0 if process else 1, 'stdout': '', 'stderr': ''})
        else:
            args = request.get('run')
            if (not isinstance(args, list) or not args or len(args) > MAX_ARGS
                    or not all(isinstance(a, str) and '\0' not in a for a in args) or not args[0].startswith('--')):
                send({'id': request_id, 'status': 64, 'stdout': '', 'stderr': 'Error: malformed request\n'})
                continue
            thread = threading.Thread(target=run, args=(request_id, args, bool(request.get('merge')), bool(request.get('passive'))), daemon=True)
            threads.append(thread)
            thread.start()
    # The window went away: finish what is running, then exit.
    for thread in threads:
        thread.join()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
