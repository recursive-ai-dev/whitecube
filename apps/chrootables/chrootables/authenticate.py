"""Authenticate in a terminal, then relay the helper's JSON to the desktop.

Whitecube adaptation: sudo reads the password from its own controlling terminal.
Only an authenticated root helper may connect to the temporary local socket.
The bridge never handles passwords and does not keep a reusable listener.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chrootables.paths import store_path

AUTHENTICATION_TIMEOUT = 180
TITLE = "Cube Chrootables — administrator access"


def peer_uid(connection):
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", credentials)[1]


def terminal_argv(command):
    """Build an argv without a shell; store and socket paths remain single arguments."""
    choices = (
        ("whitecube-terminal", ["--title", TITLE, "--"]),
        ("foot", ["--title", TITLE, "--"]),
        ("konsole", ["--separate", "--nofork", "-p", "tabtitle=" + TITLE, "-e"]),
        ("xterm", ["-T", TITLE, "-e"]),
        ("alacritty", ["--title", TITLE, "-e"]),
        ("kitty", ["--title", TITLE, "--"]),
        ("xfce4-terminal", ["--disable-server", "--title", TITLE, "-x"]),
        ("gnome-terminal", ["--wait", "--title", TITLE, "--"]),
    )
    for name, arguments in choices:
        executable = shutil.which(name)
        if executable and (name != "whitecube-terminal" or shutil.which("foot")):
            return [executable, *arguments, *command]
    raise RuntimeError("Install Foot or another supported terminal to enter your sudo password.")


def runtime_directory():
    directory = Path("/run/user") / str(os.getuid())
    try:
        info = directory.lstat()
        if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o700):
            return directory
    except OSError:
        pass
    return Path("/tmp")


def wait_for_helper(listener, terminal, *, timeout=AUTHENTICATION_TIMEOUT, input_fd=None):
    """Reject other users; stop waiting when authentication or the desktop closes."""
    deadline = time.monotonic() + timeout
    terminal_finished = False
    pending = bytearray()
    while time.monotonic() < deadline:
        watched = [listener]
        if input_fd is not None:
            watched.append(input_fd)
        readable, _, _ = select.select(watched, [], [], min(0.1, max(0, deadline - time.monotonic())))
        if listener in readable:
            connection, _ = listener.accept()
            if peer_uid(connection) == 0:
                return connection, bytes(pending)
            connection.close()
        if input_fd is not None and input_fd in readable:
            data = os.read(input_fd, 65536)
            if not data:
                raise RuntimeError("Administrator authentication was cancelled.")
            pending.extend(data)
            if len(pending) > 1048576:
                raise RuntimeError("Too many requests arrived before administrator authentication.")
        status = terminal.poll()
        if status not in (None, 0):
            raise RuntimeError("The authentication terminal closed before sudo granted access.")
        if status == 0 and not terminal_finished:
            # sudo -b closes the terminal as soon as authentication succeeds.
            # Loading manager capabilities in the detached helper may take 5s.
            deadline = min(deadline, time.monotonic() + 10)
            terminal_finished = True
    raise RuntimeError("Administrator authentication timed out. Unlock the store to try again.")


def forward(connection, input_fd, output_fd, pending=b""):
    """On desktop EOF, let the helper finish existing operations before it exits."""
    def requests():
        try:
            if pending:
                connection.sendall(pending)
            while data := os.read(input_fd, 65536):
                connection.sendall(data)
        except OSError:
            pass
        finally:
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    reader = threading.Thread(target=requests, daemon=True)
    reader.start()
    output_open = True
    while data := connection.recv(65536):
        if output_open:
            try:
                while data:
                    written = os.write(output_fd, data)
                    data = data[written:]
            except OSError:
                output_open = False
    reader.join(timeout=0.1)


def stop_terminal(terminal):
    if terminal is not None and terminal.poll() is None:
        terminal.terminate()
        try:
            terminal.wait(timeout=2)
        except subprocess.TimeoutExpired:
            terminal.kill()
            terminal.wait(timeout=2)


def bridge(store, *, input_fd=0, output_fd=1, timeout=AUTHENTICATION_TIMEOUT):
    store = store_path(store)
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("sudo is required to unlock the workspace store.")
    terminal = None
    authenticated = False
    with tempfile.TemporaryDirectory(prefix="cube-chrootables-auth-", dir=runtime_directory()) as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        address = directory / "helper.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(address))
            address.chmod(0o600)
            listener.listen(1)
            broker = Path(__file__).resolve().with_name("broker.py")
            command = [sudo, "-b", "--", "/usr/bin/python3", "-I", "-S", str(broker),
                       "--store", str(store), "--writable", "--connect", str(address)]
            environment = dict(os.environ)
            environment.pop("LD_LIBRARY_PATH", None)
            try:
                terminal = subprocess.Popen(terminal_argv(command), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            env=environment, start_new_session=True)
                connection, pending = wait_for_helper(listener, terminal, timeout=timeout, input_fd=input_fd)
                authenticated = True
                listener.close()
                # The authenticated connection is the only endpoint needed now.
                address.unlink()
                with connection:
                    forward(connection, input_fd, output_fd, pending)
            finally:
                if not authenticated:
                    stop_terminal(terminal)
                elif terminal is not None:
                    # A disconnected desktop must not kill a live filesystem task.
                    try:
                        terminal.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass


def main():
    parser = argparse.ArgumentParser(description="Unlock Cube Chrootables using sudo in a terminal")
    parser.add_argument("--store", required=True)
    args = parser.parse_args()
    try:
        bridge(args.store)
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"event": "error", "error": str(error)}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
