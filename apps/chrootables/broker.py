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
       {"id": N, "build": {"workdir": DIR, "kind": "chroot"|"vm"}}
                                          run the fixed Cube Strap build helper
  "passive": true on any request keeps it from counting as activity (background
  refreshes must not hold the helper open forever).
  out: {"ready": true, "uid": 0}          once, after start
       {"id": N, "line": TEXT}            each stderr line as it arrives
       {"id": N, "status": S, "stdout": TEXT, "stderr": TEXT}   when done

The helper executes the manager path it was started with, or the bundled Cube
Strap helper with a fixed argument schema (no shell). It exits when the window closes its stdin (after
letting running operations finish, so nothing is left half-written) or after
`--idle` seconds without a request, which locks the window again.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time

MAX_LINE = 1 << 20
MAX_ARGS = 16
MAX_REQUEST_DEPTH = 64


def load_request(line):
    request = json.loads(line)
    pending = [(request, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth >= MAX_REQUEST_DEPTH:
                raise ValueError('Request nesting is too deep.')
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    return request


def strap_build_command(params):
    """The sole additional command: build in a private directory for sudo's user."""
    if (not isinstance(params, dict) or set(params) != {'workdir', 'kind'}
            or params.get('kind') not in ('chroot', 'vm')):
        raise ValueError('A build requires only its workdir and chroot/vm kind.')
    directory = params['workdir']
    if (not isinstance(directory, str) or not 0 < len(directory) <= 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in directory)):
        raise ValueError('Invalid Cube Strap build directory.')
    workdir = Path(directory)
    if not workdir.is_absolute() or not re.fullmatch(r'build-[A-Za-z0-9_]{1,32}', workdir.name):
        raise ValueError('Invalid Cube Strap build directory.')
    uid = os.environ.get('SUDO_UID', str(os.getuid()))
    gid = os.environ.get('SUDO_GID', str(os.getgid()))
    if not all(re.fullmatch(r'[0-9]{1,10}', value) and int(value) <= 4294967294 for value in (uid, gid)):
        raise ValueError('The invoking user identity is invalid.')
    for path, file_type in ((workdir, stat.S_ISDIR), (workdir / 'cube.conf', stat.S_ISREG)):
        info = path.lstat()
        if (not file_type(info.st_mode) or info.st_uid != int(uid)
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('Build files must be private and owned by the invoking user.')
        if path.name == 'cube.conf' and not 0 < info.st_size <= 1048576:
            raise ValueError('The build configuration is empty or too large.')
    if workdir.resolve(strict=True) != workdir:
        raise ValueError('The build directory must use its real absolute path.')
    application = Path(__file__).resolve().parent.parent / 'cube-strap'
    helper = application / 'cube-strap-build'
    if not helper.is_file():
        helper = Path('/usr/libexec/whitecube/cube-strap-build')
    debstrap = application / 'deb-strap.sh'
    if any(path.is_symlink() or not path.is_file() for path in (helper, debstrap)):
        raise ValueError('The bundled Cube Strap helper or bootstrap script is unavailable.')
    return [str(helper), '--debstrap', str(debstrap), '--config', str(workdir / 'cube.conf'),
            '--workdir', str(workdir), '--kind', params['kind'], '--owner', uid + ':' + gid]


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
    starting: set[int] = set()
    cancelled: set[int] = set()
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

    def run(request_id: int, command: list[str], merge: bool, passive: bool):
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT if merge else subprocess.PIPE, text=True,
                                       errors='replace', env=env, start_new_session=True, bufsize=1)
        except OSError as error:
            with jobs_lock:
                starting.discard(request_id)
                cancelled.discard(request_id)
            send({'id': request_id, 'status': 127, 'stdout': '', 'stderr': f'Error: {error}\n'})
            return
        with jobs_lock:
            jobs[request_id] = process
            starting.discard(request_id)
            stop = request_id in cancelled
        if stop:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
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
            cancelled.discard(request_id)
        if not passive:
            last_request[0] = time.monotonic()
        send({'id': request_id, 'status': process.returncode, 'stdout': ''.join(parts), 'stderr': ''.join(errors)})

    def watchdog():
        while True:
            time.sleep(min(5, max(1, idle // 2)) if idle else 5)
            with jobs_lock:
                running = bool(jobs or starting)
            if idle and not running and time.monotonic() - last_request[0] > idle:
                send({'idle': True})
                os._exit(0)

    threading.Thread(target=watchdog, daemon=True).start()
    send({'ready': True, 'uid': os.geteuid(), 'idle': idle})

    for raw in sys.stdin:
        if len(raw) > MAX_LINE:
            continue
        try:
            request = load_request(raw)
        except (ValueError, RecursionError):
            continue            # never echo unparsable input: it could be a stray password
        if not isinstance(request, dict) or not isinstance(request.get('id'), int):
            continue
        if not request.get('passive'):
            last_request[0] = time.monotonic()
        request_id = request['id']
        if request.get('ping'):
            send({'id': request_id, 'status': 0, 'stdout': 'pong', 'stderr': ''})
        elif 'cancel' in request:
            identifier = request.get('cancel')
            if type(identifier) is not int:
                send({'id': request_id, 'status': 64, 'stdout': '', 'stderr': 'Error: malformed cancellation\n'})
                continue
            with jobs_lock:
                process = jobs.get(identifier)
                pending = identifier in starting
                if process or pending:
                    cancelled.add(identifier)
            if process:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
            send({'id': request_id, 'status': 0 if process or pending else 1, 'stdout': '', 'stderr': ''})
        else:
            if 'build' in request:
                try:
                    command = strap_build_command(request['build'])
                except (OSError, ValueError) as error:
                    send({'id': request_id, 'status': 64, 'stdout': '', 'stderr': f'Error: {error}\n'})
                    continue
            else:
                args = request.get('run')
                if (not isinstance(args, list) or not args or len(args) > MAX_ARGS
                        or not all(isinstance(a, str) and '\0' not in a and len(a) <= 4096 for a in args) or not args[0].startswith('--')):
                    send({'id': request_id, 'status': 64, 'stdout': '', 'stderr': 'Error: malformed request\n'})
                    continue
                command = [str(manager), *args]
            with jobs_lock:
                starting.add(request_id)
            thread = threading.Thread(target=run, args=(request_id, command, bool(request.get('merge')), bool(request.get('passive'))), daemon=True)
            threads.append(thread)
            thread.start()
    # The window went away: finish what is running, then exit.
    for thread in threads:
        thread.join()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
