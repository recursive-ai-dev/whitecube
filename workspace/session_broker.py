"""Client for the independent per-user PTY supervisor (private Unix socket)."""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

MAX_MESSAGE = 2 * 1024 * 1024

class BrokerError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)

class SessionBroker:
    def __init__(self, state, start=True):
        self.state = Path(state)
        self.socket_path = self.state/'sessions.sock'
        self.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.state.is_symlink() or self.state.stat().st_uid != os.getuid() or self.state.stat().st_mode & 0o077:
            raise RuntimeError('PTY broker state must be private and owned by this user')
        if start:
            self.ensure_running()
    def ensure_running(self):
        try:
            self.request('ping')
            return
        except (OSError, BrokerError):
            pass
        # Popen detaches it from the browser server. No HTTP token is inherited.
        argv = [sys.executable, str(Path(__file__).with_name('session_daemon.py')), '--state', str(self.state)]
        with open(os.devnull, 'wb') as output:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True, close_fds=True)
        for _ in range(50):
            try:
                self.request('ping')
                return
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError('PTY broker could not start; Unix sockets may be restricted')
                time.sleep(.05)
        raise RuntimeError('PTY broker did not become ready')
    def request(self, operation, **data):
        if self.socket_path.is_symlink():
            raise RuntimeError('Unsafe PTY broker socket')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(15)
            connection.connect(str(self.socket_path))
            encoded = json.dumps({'op':operation, **data}).encode()+b'\n'
            if len(encoded) > MAX_MESSAGE:
                raise ValueError('Broker request too large')
            connection.sendall(encoded)
            with connection.makefile('rb') as stream:
                raw = stream.readline(MAX_MESSAGE+1)
            if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
                raise RuntimeError('Invalid broker response')
            response = json.loads(raw)
            if 'error' in response:
                raise BrokerError(response.get('status', 500), response['error'])
            return response['result']
    def sessions(self):
        return self.request('list')['sessions']
    def create(self, **spec):
        return self.request('create', **spec)
    def terminal(self, ident):
        return RemoteTerminal(self, ident)

class RemoteTerminal:
    def __init__(self, broker, ident):
        self.broker, self.id = broker, ident
    def output(self, offset):
        return self.broker.request('output', id=self.id, offset=offset)
    def write(self, data):
        return self.broker.request('input', id=self.id, data=base64.b64encode(data).decode())
    def resize(self, cols, rows):
        return self.broker.request('resize', id=self.id, cols=cols, rows=rows)
    def close(self):
        return self.broker.request('close', id=self.id)
