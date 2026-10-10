#!/usr/bin/env python3
"""Isolated headless protocol check; never connects to the active desktop."""
import os
import argparse
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--compositor', type=Path, default=Path(__file__).resolve().parent.parent / 'build' / 'whitecube')
parser.add_argument('--require-headless', action='store_true', help='fail instead of skipping if the sandbox denies a private socket')
options = parser.parse_args()

class Client:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.buffer = b''
        self.send(1, 1, struct.pack('I', 2))
        self.send(1, 0, struct.pack('I', 3))
        manager = None
        while True:
            obj, opcode, data = self.event()
            if obj == 2 and opcode == 0:
                name, length = struct.unpack('II', data[:8])
                interface = data[8:8+length-1].decode()
                if interface == 'ext_session_lock_manager_v1': manager = name
            if obj == 3 and opcode == 0: break
        assert manager is not None, 'session-lock global missing'
        interface = b'ext_session_lock_manager_v1\0'
        encoded = struct.pack('I', len(interface)) + interface
        encoded += b'\0' * (-len(interface) % 4)
        self.send(2, 0, struct.pack('I', manager) + encoded + struct.pack('II', 1, 4))
    def send(self, obj, opcode, payload=b''):
        self.sock.sendall(struct.pack('II', obj, ((8+len(payload)) << 16) | opcode) + payload)
    def event(self):
        while len(self.buffer) < 8:
            part = self.sock.recv(8192)
            assert part, 'compositor disconnected'
            self.buffer += part
        obj, word = struct.unpack('II', self.buffer[:8])
        size, opcode = word >> 16, word & 0xffff
        assert size >= 8
        while len(self.buffer) < size:
            part = self.sock.recv(8192)
            assert part, 'compositor disconnected'
            self.buffer += part
        data, self.buffer = self.buffer[8:size], self.buffer[size:]
        if obj == 1 and opcode == 0: raise AssertionError(f'Wayland protocol error: {data!r}')
        return obj, opcode, data
    def lock(self):
        self.send(4, 1, struct.pack('I', 5))
        while True:
            obj, opcode, data = self.event()
            if obj == 5: return opcode
    def unlock(self): self.send(5, 2)
    def close(self): self.sock.close()

with tempfile.TemporaryDirectory(prefix='whitecube-lock-check-') as temp:
    root = Path(temp)
    runtime = root/'runtime'; runtime.mkdir(mode=0o700)
    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.bind(str(runtime/'probe'))
    except PermissionError:
        if options.require_headless: raise
        print('SKIP: sandbox denies the private headless Wayland socket (rerun on the host)')
        raise SystemExit(77)
    finally:
        probe.close()
    (runtime/'probe').unlink()
    home = root/'home'; home.mkdir(mode=0o700)
    env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(root/'state'), XDG_RUNTIME_DIR=str(runtime),
               WLR_BACKENDS='headless', WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='2', WAYLAND_DISPLAY='')
    env.pop('DISPLAY', None)
    with (root/'compositor.log').open('w+') as log:
        process = subprocess.Popen([str(options.compositor.resolve())], env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic()+5
            while not (runtime/'wayland-0').exists():
                assert process.poll() is None, 'headless compositor did not start'
                assert time.monotonic() < deadline, 'headless socket timeout'
                time.sleep(.02)
            path = runtime/'wayland-0'
            first = Client(path); assert first.lock() == 0, 'first lock not acknowledged'
            second = Client(path); assert second.lock() == 1, 'concurrent locker was accepted'; second.close()
            first.close()  # The compositor must stay locked after a client crash.
            time.sleep(.05)
            replacement = Client(path); assert replacement.lock() == 0, 'replacement locker failed'
            replacement.unlock(); replacement.close()
            time.sleep(.05)
            third = Client(path); assert third.lock() == 0, 'unlock did not release previous lock'
            third.unlock(); third.close()
            # Exercise a real locker that creates and maps a surface on both outputs.
            if shutil.which('swaylock'):
                sway_env = dict(env, WAYLAND_DISPLAY='wayland-0')
                with (root/'swaylock.log').open('w+') as swaylog:
                    ready_read, ready_write = os.pipe()
                    locker = subprocess.Popen(['swaylock', '-c', '111827', '--ready-fd', str(ready_write)],
                        env=sway_env, stdout=swaylog, stderr=swaylog, pass_fds=(ready_write,))
                    os.close(ready_write)
                    try:
                        assert select.select([ready_read], [], [], 5)[0] and os.read(ready_read, 1), 'swaylock did not become ready'
                        assert locker.poll() is None, 'swaylock failed to create lock surfaces'
                        contender = Client(path); assert contender.lock() == 1, 'swaylock was not active'; contender.close()
                    finally:
                        os.close(ready_read)
                        locker.terminate(); locker.wait(timeout=5)
                time.sleep(.05)
                final = Client(path); assert final.lock() == 0, 'swaylock crash recovery failed'
                final.unlock(); final.close()
            assert process.poll() is None, 'compositor failed during lock lifecycle'
            print('session lock: two-output presentation ack, concurrent rejection, crash recovery and unlock passed' +
                ('; installed swaylock surfaces passed' if shutil.which('swaylock') else '; swaylock unavailable'))
        except BaseException:
            log.flush(); print((root/'compositor.log').read_text())
            raise
        finally:
            process.terminate(); process.wait(timeout=5)
