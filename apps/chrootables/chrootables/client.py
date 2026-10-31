"""Asynchronous Qt client for the local stdio helper.

Adapted for Whitecube: administrator access uses terminal sudo authentication.
"""
from collections import deque
import json
import sys
from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from shiboken6 import isValid
from .backend import ROOT
from .paths import store_path

# Inventory replies may contain thousands of images and recovery points. Eight
# MiB permits those replies while putting a firm limit on any incomplete frame.
# A burst may queue twice that amount; decoding yields after 100 small messages
# or 64 KiB, with one larger inventory frame allowed to make forward progress.
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_QUEUED_BYTES = 16 * 1024 * 1024
MAX_MESSAGES_PER_TICK = 100
MAX_DECODE_BYTES_PER_TICK = 64 * 1024
MAX_JSON_DEPTH = 64


def excessive_nesting(payload):
    """Count containers without counting braces inside JSON strings."""
    depth = 0
    quoted = escaped = False
    for character in payload:
        if quoted:
            if escaped:
                escaped = False
            elif character == 92:  # backslash
                escaped = True
            elif character == 34:
                quoted = False
        elif character == 34:
            quoted = True
        elif character in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif character in (93, 125):
            depth -= 1
    return False


def valid_result(action, data):
    """Check fields consumed by the desktop before invoking its callbacks."""
    def fields(value, strings=(), integers=()):
        return (isinstance(value, dict)
                and all(isinstance(value.get(key), str) for key in strings)
                and all(type(value.get(key)) is int and value[key] >= 0 for key in integers))

    def optional_fields(value, strings=(), integers=(), booleans=()):
        return (isinstance(value, dict)
                and all(key not in value or isinstance(value[key], str) for key in strings)
                and all(key not in value or type(value[key]) is int and value[key] >= 0
                        for key in integers)
                and all(key not in value or type(value[key]) is bool for key in booleans))

    if action == "logs":
        return isinstance(data, str)
    if action == "doctor":
        if (not fields(data, ("output",)) or type(data.get("ready")) is not bool
                or not optional_fields(data, integers=("errors", "warnings"))):
            return False
        if "checks" in data:
            checks = data["checks"]
            if (not isinstance(checks, list)
                    or not all(fields(check, ("id", "status")) and check["id"]
                               and check["status"] in ("ok", "info", "warning", "error")
                               and optional_fields(check, strings=("message", "remedy"))
                               for check in checks)):
                return False
            if len({check["id"] for check in checks}) != len(checks):
                return False
            for key, status in (("errors", "error"), ("warnings", "warning")):
                if key in data and data[key] != sum(check["status"] == status for check in checks):
                    return False
            if data["ready"] != all(check["status"] != "error" for check in checks):
                return False
        return "errors" not in data or data["ready"] == (data["errors"] == 0)
    if action == "cancel":
        return isinstance(data, dict) and type(data.get("cancelled")) is bool and isinstance(data.get("stage", ""), str)
    if action == "inventory":
        if not fields(data, integers=("free_bytes", "total_bytes")):
            return False
        if "storage" in data and not fields(data["storage"], integers=(
                "images_bytes", "snapshots_bytes", "exports_bytes", "logs_bytes")):
            return False
        if "operations" in data:
            if (not isinstance(data["operations"], list)
                    or not all(fields(item, ("id", "action", "name", "status", "stage", "log"))
                               and type(item.get("active")) is bool
                               and type(item.get("recoverable")) is bool
                               and optional_fields(item, integers=("reclaimable_bytes",),
                                                   booleans=("discardable", "invalid"))
                               for item in data["operations"])):
                return False
        if "exports" in data:
            if (not isinstance(data["exports"], list)
                    or not all(fields(item, ("filename", "path", "updated_at"), ("size_bytes", "allocated_bytes"))
                               for item in data["exports"])):
                return False
        if "archives" in data:
            if (not isinstance(data["archives"], list)
                    or not all(fields(item, ("id", "suite", "architecture", "updated_at"), ("size_bytes",))
                               and isinstance(item.get("profiles"), list)
                               and all(isinstance(p, str) for p in item["profiles"])
                               for item in data["archives"])):
                return False
        schemas = {
            "images": (("name", "status", "lifecycle", "suite", "profile", "architecture",
                        "created_at", "last_used_at", "origin"),
                       ("size_mib", "allocated_bytes", "snapshots")),
            "snapshots": (("name", "label", "created_at"), ("size_mib",)),
            "activity": (("time", "action", "name", "status", "log"), ()),
        }
        return (all(isinstance(data.get(key), list)
                   and all(fields(item, *schema) for item in data[key])
                   for key, schema in schemas.items())
                and all("allocated_bytes" not in item or
                        type(item["allocated_bytes"]) is int and item["allocated_bytes"] >= 0
                        for item in data["snapshots"])
                and all(optional_fields(item, booleans=("orphan",)) for item in data["snapshots"])
                and all(optional_fields(item, strings=("path",)) for item in data["images"]))
    return fields(data, integers=("exit_code",))


def valid_message(message):
    if not isinstance(message, dict):
        return False
    event = message.get("event")
    if event == "ready":
        return type(message.get("writable")) is bool
    if event == "error":
        return isinstance(message.get("error"), str)
    identifier = message.get("id")
    if type(identifier) is not int or not 0 < identifier <= 2147483647:
        return False
    if event in ("output", "phase"):
        return isinstance(message.get("text"), str)
    if event == "result" and type(message.get("ok")) is bool:
        return ("data" in message if message["ok"] else
                isinstance(message.get("error"), str)
                and (message.get("code") is None or isinstance(message["code"], str)))
    return False


class BackendError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Client(QObject):
    ready = Signal(bool)
    disconnected = Signal(str)
    output = Signal(int, str)
    phase = Signal(int, str)
    stopped = Signal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = str(store_path(store))
        self.process = None
        self.callbacks = {}
        self.counter = 0
        self.buffer = b""
        self._lines = deque()
        self._queued_bytes = 0
        self._drain_process = None
        self._exit_error = None
        self.writable = False
        self.is_ready = False
        self.requested_writable = False
        self._failed = False
        self._retiring = set()

    def start(self, writable=False):
        if not isValid(self):
            return
        self.stop()
        if not isValid(self):
            return
        self.buffer = b""
        self._lines.clear()
        self._queued_bytes = 0
        self._exit_error = None
        self.requested_writable = writable
        self._failed = False
        process = QProcess(self)
        self.process = process
        args = ["-I", "-S", str(ROOT / "chrootables" / "broker.py"), "--store", self.store]
        if writable:
            # Whitecube has no graphical polkit agent. Sudo reads the password
            # in its own terminal; the bridge only transports helper messages.
            program = sys.executable
            args = ["-I", "-S", str(ROOT / "chrootables" / "authenticate.py"), "--store", self.store]
        else:
            program = sys.executable
        process.readyReadStandardOutput.connect(lambda: self.read(process))
        process.errorOccurred.connect(lambda _: self.failure("Could not start the helper. Check that Python and sudo are installed.") if process is self.process else None)
        process.finished.connect(lambda code, status: self._process_finished(process, writable))
        process.start(program, args)

    def failure(self, message):
        if self._failed or not isValid(self):
            return
        self._failed = True
        self.buffer = b""
        self._lines.clear()
        self._queued_bytes = 0
        self.is_ready = self.writable = False
        process = self.process
        callbacks, self.callbacks = self.callbacks, {}
        for callback in callbacks.values():
            if not isValid(self):
                break
            callback(False, message)
        if process is self.process and isValid(self):
            if process is not None:
                self._retire_process(process)
            if isValid(self):
                self.disconnected.emit(message)

    def read(self, process):
        if not self._connection_valid(process):
            return
        # Split once per chunk rather than once per line. Pending lines live on the
        # client because a callback may open a dialog whose event loop re-enters
        # read(); that nested call must continue them in order.
        chunk = bytes(process.readAllStandardOutput())
        if self._queued_bytes + len(self.buffer) + len(chunk) > MAX_QUEUED_BYTES:
            self.failure("The workspace helper sent too much data. Reconnect and try again.")
            return
        lines = (self.buffer + chunk).split(b"\n")
        self.buffer = lines.pop()
        if len(self.buffer) > MAX_FRAME_BYTES or any(len(line) > MAX_FRAME_BYTES for line in lines):
            self.failure("The workspace helper response is too large. Reconnect and try again.")
            return
        self._lines.extend(lines)
        self._queued_bytes += sum(len(line) + 1 for line in lines)
        self._drain_lines(process)

    def _process_finished(self, process, writable):
        if not self._connection_valid(process):
            return
        self._exit_error = ("Terminal authentication was cancelled or the helper closed. Unlock store to try again."
                            if writable else "The workspace helper stopped.")
        # Qt may report exit while the final stdout chunk still contains results.
        self.read(process)

    def _schedule_drain(self, process):
        if self._drain_process is process:
            return
        self._drain_process = process
        def resume():
            if self._drain_process is process:
                self._drain_process = None
            if self._connection_valid(process):
                self._drain_lines(process)
        QTimer.singleShot(0, resume)

    def _drain_lines(self, process):
        pending_id = None
        pending_chunks = []
        messages = decoded_bytes = 0

        def flush_output():
            nonlocal pending_id, pending_chunks
            if pending_chunks:
                identifier, value = pending_id, "".join(pending_chunks)
                pending_id = None
                pending_chunks = []
                if self._connection_valid(process):
                    self.output.emit(identifier, value)

        while (self._connection_valid(process) and self._lines
               and messages < MAX_MESSAGES_PER_TICK and decoded_bytes < MAX_DECODE_BYTES_PER_TICK):
            line = self._lines.popleft()
            self._queued_bytes -= len(line) + 1
            messages += 1
            decoded_bytes += len(line) + 1
            if excessive_nesting(line):
                self.failure("The workspace helper response is nested too deeply. Reconnect and try again.")
                return
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                self.failure("Invalid response from the workspace helper. Reconnect and try again.")
                return
            if not valid_message(message):
                self.failure("Invalid response from the workspace helper. Reconnect and try again.")
                return
            event = message.get("event")
            if event == "output":
                msg_id = message.get("id")
                if pending_chunks and pending_id != msg_id:
                    flush_output()
                    if not self._connection_valid(process):
                        return
                pending_id = msg_id
                pending_chunks.append(message.get("text", ""))
                continue
            flush_output()
            if not self._connection_valid(process):
                return
            if event == "ready":
                self.writable = message["writable"]
                self.is_ready = True
                self.ready.emit(self.writable)
            elif event == "phase":
                self.phase.emit(message["id"], message["text"])
            elif event == "result":
                callback = self.callbacks.pop(message.get("id"), None)
                if callback:
                    callback(message["ok"], message.get("data") if message["ok"] else BackendError(message.get("error"), message.get("code")))
            elif event == "error":
                self.failure(message["error"])
                return
        flush_output()
        if not self._connection_valid(process):
            return
        if self._lines:
            self._schedule_drain(process)
        elif self._exit_error:
            self.failure("Incomplete response from the workspace helper." if self.buffer else self._exit_error)

    def request(self, action, params, callback):
        if not self.is_ready:
            callback(False, "The workspace helper is not connected.")
            return -1
        self.counter += 1
        identifier = self.counter
        try:
            payload = (json.dumps({"id": identifier, "action": action, "params": params}) + "\n").encode()
        except (TypeError, ValueError, RecursionError) as error:
            callback(False, BackendError("Invalid request: " + str(error)))
            return -1

        def checked(ok, data):
            if ok and not valid_result(action, data):
                callback(False, BackendError("Invalid " + action + " response from the workspace helper."))
            else:
                callback(ok, data)

        self.callbacks[identifier] = checked
        if self.process.write(payload) == -1:
            self.failure("Could not send the request to the workspace helper.")
        return identifier

    def stop(self):
        self.is_ready = self.writable = False
        self.buffer = b""
        self._lines.clear()
        self._queued_bytes = 0
        self._exit_error = None
        if self.process:
            process, self.process = self.process, None
            self._retire_process(process)
        callbacks, self.callbacks = self.callbacks, {}
        if isValid(self):
            for callback in callbacks.values():
                if not isValid(self):
                    break
                callback(False, "Connection changed. Refresh and try again.")
        if not self._retiring and isValid(self):
            self.stopped.emit()

    def _connection_valid(self, process):
        return (isValid(self) and process is not None and process is self.process
                and isValid(process) and not self._failed)

    @property
    def retiring(self):
        return bool(self._retiring)

    def _retire_process(self, process):
        if not isValid(self) or not isValid(process):
            self._forget_process(process)
            return
        if process in self._retiring:
            return
        self._retiring.add(process)
        # Keep ownership until finished; deleting a live QProcess kills it and
        # can block its owning GUI thread. The desktop waits asynchronously for
        # these transports at shutdown. Root filesystem tasks live in the sudo
        # helper and finish after transport EOF, even if the relay must stop.
        process.finished.connect(lambda *_: self._release_process(process))
        process.destroyed.connect(lambda *_: self._forget_process(process))
        # PySide 6.10 can crash while introspecting a native return-valued
        # method as a slot when another QProcess method is mocked. Plain Python
        # callbacks also avoid relying on native slot-overload resolution here.
        process.readyReadStandardOutput.connect(lambda: self._discard_retired_output(process))
        process.readyReadStandardError.connect(lambda: self._discard_retired_output(process))
        process.closeWriteChannel()
        if not self._retirement_valid(process):
            return
        if process.state() == QProcess.ProcessState.NotRunning:
            self._release_process(process)
        else:
            QTimer.singleShot(1500, lambda: self._terminate_retired(process))

    def _terminate_retired(self, process):
        if not self._retirement_valid(process):
            return
        if process.state() != QProcess.ProcessState.NotRunning:
            process.terminate()
            QTimer.singleShot(1000, lambda: self._kill_retired(process))
        else:
            self._release_process(process)

    def _kill_retired(self, process):
        if self._retirement_valid(process):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
            else:
                self._release_process(process)

    def _release_process(self, process):
        if not self._retirement_valid(process) or process.state() != QProcess.ProcessState.NotRunning:
            return
        self._forget_process(process, delete=True)

    def _retirement_valid(self, process):
        if process not in self._retiring:
            return False
        if not isValid(self) or not isValid(process):
            self._forget_process(process)
            return False
        return True

    def _discard_retired_output(self, process):
        if self._retirement_valid(process):
            process.readAllStandardOutput()
            process.readAllStandardError()

    def _forget_process(self, process, *, delete=False):
        if process not in self._retiring:
            return
        self._retiring.remove(process)
        if self.process is process:
            self.process = None
        if delete and isValid(process):
            process.deleteLater()
        if self.process is None and not self._retiring and isValid(self):
            self.stopped.emit()
