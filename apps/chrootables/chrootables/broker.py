"""Workspace helper with stdio and a private terminal-authentication connection.

Whitecube adaptation: --connect accepts only the sudo user's private Unix socket.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import sys
import threading

# Works when invoked as an absolute script by sudo with a clean environment.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chrootables.backend import Store, MUTATIONS, OperationCancelled

MAX_PENDING_REQUESTS = 16
MAX_REQUEST_DEPTH = 64


def load_request(line):
    request = json.loads(line)
    pending = [(request, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth >= MAX_REQUEST_DEPTH:
                raise ValueError("Request nesting is too deep.")
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    return request


def connect_to_desktop(path, user):
    """Trust only a private endpoint owned by the user who invoked sudo."""
    address = Path(path)
    if not address.is_absolute():
        raise ValueError("The authentication socket must have an absolute path.")
    directory = address.parent.lstat()
    endpoint = address.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != user
            or stat.S_IMODE(directory.st_mode) != 0o700
            or not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != user
            or stat.S_IMODE(endpoint.st_mode) != 0o600):
        raise ValueError("The authentication socket must be private and owned by the sudo user.")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(address))
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        if struct.unpack("3i", credentials)[1] != user:
            raise ValueError("The authentication connection belongs to another user.")
        return connection
    except BaseException:
        connection.close()
        raise


def serve(store, input_stream=sys.stdin, output_stream=sys.stdout, *, writable=False):
    output_lock = threading.Lock()
    mutation_lock = threading.Lock()
    def send(message):
        with output_lock:
            try:
                output_stream.write(json.dumps(message) + "\n")
                output_stream.flush()
            except (BrokenPipeError, OSError):
                pass  # Finish filesystem operations even if the desktop disconnects.

    def handle(request):
        identifier = request.get("id")
        action = request.get("action")
        params = request.get("params", {})
        try:
            if action == "inventory":
                data = store.inventory()
            elif action == "doctor":
                data = store.doctor()
            elif action == "logs":
                data = store.logs(params.get("name"), params.get("log"))
            elif action == "cancel":
                if not writable:
                    raise PermissionError("Unlock the workspace store to make changes.")
                data = store.cancel_operation()
            elif action in MUTATIONS:
                if not writable:
                    raise PermissionError("Unlock the workspace store to make changes.")
                if not mutation_lock.acquire(blocking=False):
                    raise RuntimeError("Another operation is running. Wait for it to finish.")
                try:
                    code = store.operate(
                        action, params,
                        lambda value: send({"id": identifier, "event": "output", "text": value}),
                        progress=lambda value: send({"id": identifier, "event": "phase", "text": value}))
                    if code:
                        raise RuntimeError(f"{action.replace('-', ' ').capitalize()} failed (exit {code}). See the task output for details.")
                    data = {"exit_code": code}
                finally:
                    mutation_lock.release()
            else:
                raise ValueError("Unsupported desktop request.")
            send({"id": identifier, "event": "result", "ok": True, "data": data})
        except Exception as error:
            code = ("permission_required" if isinstance(error, PermissionError) else
                    "cancelled" if isinstance(error, OperationCancelled) else "operation_failed")
            send({"id": identifier, "event": "result", "ok": False, "error": str(error), "code": code})

    send({"event": "ready", "writable": writable})
    pending = threading.BoundedSemaphore(MAX_PENDING_REQUESTS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        while True:
            line = input_stream.readline(65537)
            if not line:
                break
            if len(line) > 65536:
                send({"event": "error", "error": "Request too large."})
                break
            try:
                request = load_request(line)
                if (not isinstance(request, dict) or type(request.get("id")) is not int
                        or not 0 < request["id"] <= 2147483647
                        or not isinstance(request.get("action"), str)
                        or not isinstance(request.get("params", {}), dict)):
                    raise ValueError("Invalid request.")
                # Cancellation only inspects/signals the active process. Handle
                # it on the reader thread so queued disk reads cannot delay it.
                if request["action"] == "cancel":
                    handle(request)
                elif not pending.acquire(blocking=False):
                    send({"id": request["id"], "event": "result", "ok": False,
                          "error": "The workspace helper is busy. Wait for pending requests and try again.",
                          "code": "overloaded"})
                else:
                    try:
                        future = pool.submit(handle, request)
                    except BaseException:
                        pending.release()
                        raise
                    future.add_done_callback(lambda _: pending.release())
            except (ValueError, TypeError, RecursionError) as error:
                send({"event": "error", "error": str(error)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default="/var/lib/chrootable-manager")
    parser.add_argument("--writable", action="store_true")
    parser.add_argument("--connect", help="Private Unix socket created by the terminal authentication bridge")
    args = parser.parse_args()
    if args.writable and os.geteuid() != 0:
        parser.error("Write access requires administrator authentication.")
    if args.connect and (not args.writable or os.geteuid() != 0):
        parser.error("A terminal authentication connection requires administrator write access.")
    os.umask(0o077)
    if args.connect:
        # The authentication terminal closes after sudo -b detaches this helper.
        # Its lifetime is the socket connection, including work already in flight.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        user = os.environ.get("SUDO_UID", "")
        if not re.fullmatch(r"[0-9]+", user):
            parser.error("The terminal authentication connection requires SUDO_UID.")
        try:
            with connect_to_desktop(args.connect, int(user)) as connection:
                with connection.makefile("r", encoding="utf-8") as requests:
                    with connection.makefile("w", encoding="utf-8") as responses:
                        serve(Store(args.store), requests, responses, writable=True)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    else:
        serve(Store(args.store), writable=args.writable)


if __name__ == "__main__":
    main()
