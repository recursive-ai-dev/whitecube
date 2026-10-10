#!/usr/bin/env python3
"""Independent PTY owner. Browser/server exits detach; only explicit close kills."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import struct
import threading
from session_broker import MAX_MESSAGE
from server import Terminal, Problem
from workspace_data import atomic_json, process_identity

class Supervisor:
    def __init__(self, state):
        self.state = Path(state)
        self.terminals = {}
        self.names = {}
        self.lock = threading.RLock()
        from jobs import JobQueue
        self.jobs = JobQueue(self.state, self)
    def create(self, cwd, job=None, ident=None, name='Bash', **unused):
        with self.lock:
            if len(self.terminals) >= 8:
                raise Problem(409, 'Eight sessions are open. Explicitly terminate one to continue.')
            if ident and (not re.fullmatch('[a-f0-9]{32}', ident) or ident in self.terminals):
                raise Problem(400, 'Invalid session id')
            if job:
                job = Path(job)
                if job.parent != self.state/'runs' or job.is_symlink() or job.stat().st_uid != os.getuid() or job.stat().st_mode & 0o077:
                    raise Problem(400, 'Unsafe job path')
                record = json.loads(job.read_text())
                record.update(owner_pid=os.getpid(), owner_identity=process_identity(os.getpid()))
                atomic_json(job, record)
            terminal = Terminal(cwd, job=job, ident=ident)
            terminal.preview_url = unused.get('preview_url')
            self.terminals[terminal.id] = terminal
            self.names[terminal.id] = str(name)[:120]
            return {'id':terminal.id, 'name':self.names[terminal.id]}
    def close(self, ident):
        with self.lock:
            terminal = self.terminals.pop(ident, None)
            self.names.pop(ident, None)
        if terminal:
            terminal.close()
        return {'closed':True}
    def dispatch(self, request):
        op = request.get('op')
        if op == 'ping':
            return {'pid':os.getpid()}
        if op == 'create':
            return self.create(**{k:v for k,v in request.items() if k != 'op'})
        if op == 'list':
            with self.lock:
                return {'sessions':[{'id':t.id, 'name':self.names[t.id], 'exited':t.exited, **{k:v for k,v in t.output(0).items() if k.startswith('job_')}} for t in self.terminals.values()]}
        if op == 'jobs':
            return {'jobs':self.jobs.list()}
        if op == 'queue':
            return self.jobs.submit(request['workflow'], request['steps'])
        if op == 'cancel':
            return self.jobs.cancel(request['id'])
        ident = request.get('id')
        if op == 'close':
            return self.close(ident)
        with self.lock:
            terminal = self.terminals.get(ident)
        if terminal is None:
            raise Problem(404, 'Session unavailable; it may have been explicitly terminated')
        if op == 'output':
            return terminal.output(int(request.get('offset', 0)))
        if op == 'input':
            raw = base64.b64decode(request['data'], validate=True)
            if len(raw) > 65536:
                raise Problem(413, 'Input too large')
            terminal.write(raw)
            return {'ok':True}
        if op == 'resize':
            terminal.resize(request['cols'], request['rows'])
            return {'ok':True}
        raise Problem(404, 'Unknown broker operation')

class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        # File modes + peer UID, rather than a transferable HTTP credential.
        pid, uid, gid = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            return
        try:
            raw = self.rfile.readline(MAX_MESSAGE+1)
            if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
                raise Problem(413, 'Broker request too large')
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError('Broker request must be an object')
            result = self.server.supervisor.dispatch(request)
            reply = {'result':result}
        except Problem as exc:
            reply = {'status':exc.status, 'error':exc.message}
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            reply = {'status':400, 'error':str(exc)}
        self.wfile.write(json.dumps(reply).encode()+b'\n')

class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    args = parser.parse_args()
    state = args.state
    if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe broker state')
    lock_fd = os.open(state/'sessions.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = state/'sessions.sock'
        if path.is_symlink():
            raise RuntimeError('Unsafe broker socket')
        path.unlink(missing_ok=True)
        supervisor = Supervisor(state)
        try:
            with UnixServer(str(path), RequestHandler) as server:
                os.chmod(path, 0o600)
                server.supervisor = supervisor
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
                server.serve_forever(poll_interval=.2)
        finally:
            supervisor.jobs.stop()
            for ident in list(supervisor.terminals):
                supervisor.close(ident)
            path.unlink(missing_ok=True)

if __name__ == '__main__':
    main()
