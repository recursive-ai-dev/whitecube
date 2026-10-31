"""Failure and reconnection coverage for the local helper protocol."""
import json
import time
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QProcess, QTimer
from chrootables.client import Client, valid_result, MAX_MESSAGES_PER_TICK, MAX_JSON_DEPTH


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.client = Client("/tmp/test-store")
        self.process = Mock()
        self.process.write.return_value = 1
        self.client.process = self.process
        self.client.is_ready = True
        self.addCleanup(self.client.stop)

    def read(self, *messages):
        self.process.readAllStandardOutput.return_value = b"".join(
            (json.dumps(message) + "\n").encode() for message in messages)
        self.client.read(self.process)

    def test_malformed_envelopes_fail_pending_requests_once(self):
        invalid = [None, [], {}, {"event": "ready"},
                   {"event": "ready", "writable": "false"},
                   {"event": "output", "id": 1, "text": None},
                   {"event": "phase", "id": 1, "text": []},
                   {"event": "output", "id": [], "text": "bad"},
                   {"event": "output", "id": True, "text": "bad"},
                   {"event": "output", "id": 2**40, "text": "bad"},
                   {"event": "result", "id": 1, "ok": 1},
                   {"event": "result", "id": 1, "ok": False, "error": []}]
        for message in invalid:
            with self.subTest(message=message):
                self.client._failed = False
                self.client.is_ready = True
                callback = Mock()
                self.client.request("logs", {}, callback)
                self.read(message, {"event": "ready", "writable": True})
                self.client.failure("helper exited")
                callback.assert_called_once()
                self.assertFalse(callback.call_args.args[0])
                self.assertFalse(self.client.is_ready)
                self.assertFalse(self.client.writable)
                self.assertEqual(self.client.callbacks, {})

    def test_invalid_json_and_helper_error_are_visible(self):
        for payload in (b"invalid json\n", b'{"event":"error","error":"Request too large."}\n'):
            self.client._failed = False
            self.client.is_ready = True
            callback = Mock()
            self.client.request("logs", {}, callback)
            self.process.readAllStandardOutput.return_value = payload
            self.client.read(self.process)
            callback.assert_called_once()
            self.assertFalse(callback.call_args.args[0])
            self.assertTrue(str(callback.call_args.args[1]))

    def test_malformed_result_data_does_not_reach_ui_as_success(self):
        inventory = json.loads((Path(__file__).resolve().parents[1] / "assets/demo.json").read_text())
        inventory["images"][0]["profile"] = []
        for action, data in (("inventory", None), ("inventory", inventory),
                             ("doctor", {"ready": "yes", "output": "ok"}),
                             ("logs", {}), ("create", {"exit_code": False})):
            with self.subTest(action=action):
                callback = Mock()
                identifier = self.client.request(action, {}, callback)
                self.read({"event": "result", "id": identifier, "ok": True, "data": data})
                callback.assert_called_once()
                self.assertFalse(callback.call_args.args[0])
                self.assertIn("Invalid", str(callback.call_args.args[1]))

    def test_disconnect_fails_all_pending_requests_once(self):
        callbacks = [Mock(), Mock()]
        for callback in callbacks:
            self.client.request("inventory", {}, callback)
        disconnected = Mock()
        self.client.disconnected.connect(disconnected)
        self.client.failure("helper exited")
        self.client.failure("helper exited again")
        for callback in callbacks:
            callback.assert_called_once_with(False, "helper exited")
        disconnected.assert_called_once_with("helper exited")

    def test_doctor_nested_data_and_counts_are_checked_before_callback(self):
        invalid = [
            {"checks": None}, {"checks": [{}]},
            {"checks": [{"id": "command:tar", "status": []}]},
            {"checks": [{"id": "command:tar", "status": "unknown"}]},
            {"checks": [{"id": "command:tar", "status": "error"}]},
            {"checks": [{"id": "command:tar", "status": "ok", "remedy": []}]},
            {"checks": [{"id": "same", "status": "ok"}] * 2},
            {"errors": False}, {"warnings": -1}, {"errors": "0"},
            {"checks": [], "warnings": 1}, {"errors": 1},
        ]
        for extra in invalid:
            with self.subTest(extra=extra):
                callback = Mock()
                identifier = self.client.request("doctor", {}, callback)
                self.read({"event": "result", "id": identifier, "ok": True,
                           "data": {"ready": True, "output": "Host health", **extra}})
                callback.assert_called_once()
                self.assertFalse(callback.call_args.args[0])
                self.assertIn("Invalid doctor response", str(callback.call_args.args[1]))
        self.assertTrue(valid_result("doctor", {"ready": False, "output": "Missing tar", "errors": 1,
            "warnings": 0, "checks": [{"id": "command:tar", "status": "error"}]}))
        self.assertTrue(valid_result("doctor", {"ready": True, "output": "Ready"}))

    def test_inventory_optional_fields_match_desktop_consumers(self):
        base = json.loads((Path(__file__).resolve().parents[1] / "assets/demo.json").read_text())
        base["exports"], base["archives"] = [], []
        fixture = json.dumps(base)
        operation = {"id": "a" * 32, "action": "clone", "name": "lab", "status": "interrupted",
                     "stage": "copy", "log": "/tmp/log", "active": False, "recoverable": False}
        for key, value in (("discardable", "yes"), ("invalid", "yes"), ("reclaimable_bytes", []),
                           ("reclaimable_bytes", True), ("reclaimable_bytes", -1)):
            with self.subTest(key=key, value=value):
                inventory = json.loads(fixture)
                inventory["operations"] = [{**operation, key: value}]
                self.assertFalse(valid_result("inventory", inventory))
        inventory = json.loads(fixture)
        inventory["operations"] = [{**operation, "discardable": True, "reclaimable_bytes": 1024}]
        self.assertTrue(valid_result("inventory", inventory))
        inventory["snapshots"][0]["orphan"] = "false"
        self.assertFalse(valid_result("inventory", inventory))
        inventory = json.loads(fixture)
        inventory["images"][0]["path"] = []
        self.assertFalse(valid_result("inventory", inventory))

    def test_reconnection_from_callback_ignores_remaining_old_messages(self):
        received = []
        old = self.process
        identifier = self.client.request("logs", {}, lambda *args: self.client.start())
        self.client.ready.connect(received.append)
        with patch("chrootables.client.QProcess") as process_class:
            self.read({"event": "result", "id": identifier, "ok": True, "data": "log"},
                      {"event": "ready", "writable": True})
            self.assertIs(self.client.process, process_class.return_value)
        self.assertEqual(received, [])
        self.assertFalse(self.client.is_ready)
        old.readAllStandardOutput.return_value = b'{"event":"ready","writable":true}\n'
        self.client.read(old)
        self.assertFalse(self.client.is_ready)

    def test_partial_messages_are_buffered(self):
        callback = Mock()
        identifier = self.client.request("logs", {}, callback)
        payload = (json.dumps({"event": "result", "id": identifier, "ok": True, "data": "log"}) + "\n").encode()
        self.process.readAllStandardOutput.return_value = payload[:15]
        self.client.read(self.process)
        callback.assert_not_called()
        self.process.readAllStandardOutput.return_value = payload[15:]
        self.client.read(self.process)
        callback.assert_called_once_with(True, "log")

    def test_phase_updates_do_not_consume_operation_callback(self):
        callback, phase = Mock(), Mock()
        self.client.phase.connect(phase)
        identifier = self.client.request("clone", {"name": "lab", "target": "copy"}, callback)
        self.read({"event": "phase", "id": identifier, "text": "Copying image"})
        phase.assert_called_once_with(identifier, "Copying image")
        callback.assert_not_called()
        self.read({"event": "result", "id": identifier, "ok": True, "data": {"exit_code": 0}})
        callback.assert_called_once_with(True, {"exit_code": 0})

    def test_write_failure_and_unserializable_request_do_not_leak_callbacks(self):
        callback = Mock()
        self.client.request("logs", {"invalid": object()}, callback)
        self.assertFalse(callback.call_args.args[0])
        self.process.write.assert_not_called()
        self.assertEqual(self.client.callbacks, {})
        callback.reset_mock()
        self.process.write.return_value = -1
        self.client.request("logs", {}, callback)
        self.assertFalse(callback.call_args.args[0])
        self.assertEqual(self.client.callbacks, {})
        self.assertFalse(self.client.is_ready)

    def test_oversized_complete_and_partial_frames_fail_callbacks_once(self):
        for payload in (b'x' * 129, b'x' * 129 + b'\n'):
            with self.subTest(terminated=payload.endswith(b'\n')):
                self.client._failed = False
                self.client.is_ready = True
                callback = Mock()
                self.client.request('logs', {}, callback)
                self.process.readAllStandardOutput.return_value = payload
                with patch('chrootables.client.MAX_FRAME_BYTES', 128):
                    self.client.read(self.process)
                self.client.failure('helper exited')
                callback.assert_called_once()
                self.assertFalse(callback.call_args.args[0])
                self.assertIn('too large', str(callback.call_args.args[1]))
                self.assertEqual(self.client.buffer, b'')
                self.assertEqual(self.client._queued_bytes, 0)

    def test_queued_bytes_are_bounded_even_for_many_small_frames(self):
        callback = Mock()
        self.client.request('logs', {}, callback)
        self.process.readAllStandardOutput.return_value = b'{}\n' * 100
        with patch('chrootables.client.MAX_QUEUED_BYTES', 128):
            self.client.read(self.process)
        callback.assert_called_once()
        self.assertIn('too much data', str(callback.call_args.args[1]))
        self.assertFalse(self.client._lines)

    def test_last_result_is_drained_before_process_exit_fails_connection(self):
        callback = Mock()
        identifier = self.client.request('logs', {}, callback)
        self.process.readAllStandardOutput.return_value = (
            b'{"event":"phase","id":1,"text":"working"}\n' * (MAX_MESSAGES_PER_TICK + 1)
            + (json.dumps({'event': 'result', 'id': identifier, 'ok': True, 'data': 'complete'}) + '\n').encode())
        self.client.read(self.process)
        callback.assert_not_called()
        self.process.readAllStandardOutput.return_value = b''
        self.client._process_finished(self.process, False)
        callback.assert_called_once_with(True, 'complete')
        self.assertTrue(self.client._failed)

    def test_stop_retires_without_waiting_or_deleting_a_running_process(self):
        self.process.state.return_value = QProcess.ProcessState.Running
        self.client.stop()
        self.process.closeWriteChannel.assert_called_once_with()
        self.process.waitForFinished.assert_not_called()
        self.process.terminate.assert_not_called()
        self.process.deleteLater.assert_not_called()
        self.assertTrue(self.client.retiring)
        self.process.state.return_value = QProcess.ProcessState.NotRunning
        self.client._release_process(self.process)
        self.assertFalse(self.client.retiring)
        self.process.deleteLater.assert_called_once_with()

    def test_real_slow_transport_does_not_block_reconnect_cleanup(self):
        import sys
        self.client.process = process = QProcess(self.client)
        process.start(sys.executable, ['-I', '-S', '-c', 'import time; time.sleep(30)'])
        deadline = time.monotonic() + 3
        while process.state() == QProcess.ProcessState.Starting and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertEqual(process.state(), QProcess.ProcessState.Running)
        started = time.monotonic()
        self.client.stop()
        self.assertLess(time.monotonic() - started, .1)
        tick = []
        QTimer.singleShot(0, lambda: tick.append(True))
        self.app.processEvents()
        self.assertEqual(tick, [True])
        deadline = time.monotonic() + 4
        while self.client.retiring and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(self.client.retiring)

    def test_deep_json_is_rejected_once_and_a_new_connection_can_succeed(self):
        callback = Mock()
        self.client.request('logs', {}, callback)
        payload = b'{"event":"ready","writable":true,"ignored":' + b'[' * MAX_JSON_DEPTH + b'0' + b']' * MAX_JSON_DEPTH + b'}\n'
        self.process.readAllStandardOutput.return_value = payload
        self.client.read(self.process)
        self.client.failure('helper stopped')
        callback.assert_called_once()
        self.assertIn('nested too deeply', str(callback.call_args.args[1]))
        self.assertFalse(self.client.is_ready)
        with patch('chrootables.client.QProcess') as process_class:
            self.client.start()
            fresh = process_class.return_value
            fresh.readAllStandardOutput.return_value = b'{"event":"ready","writable":false}\n'
            self.client.read(fresh)
            self.assertTrue(self.client.is_ready)

    def test_decoder_recursion_error_is_reported_without_raising_into_qt(self):
        callback = Mock()
        self.client.request('logs', {}, callback)
        self.process.readAllStandardOutput.return_value = b'{}\n'
        with patch('chrootables.client.json.loads', side_effect=RecursionError('too deeply nested')):
            self.client.read(self.process)
        callback.assert_called_once()
        self.assertFalse(callback.call_args.args[0])
        self.assertIn('Invalid response', str(callback.call_args.args[1]))

    def test_braces_and_escaped_quotes_inside_text_do_not_count_as_containers(self):
        callback = Mock()
        identifier = self.client.request('logs', {}, callback)
        text = ('[\\\"{' * 200)
        self.read({'event': 'result', 'id': identifier, 'ok': True, 'data': text})
        callback.assert_called_once_with(True, text)


class ClientLineHandlingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.client = Client("/tmp/store")
        self.process = Mock()
        self.client.process = self.process
        self.client.is_ready = True
        self.addCleanup(self.client.stop)

    def test_nested_read_from_a_callback_keeps_line_order(self):
        order = []
        chunks = [b'{"event":"phase","id":1,"text":"first"}\n{"event":"phase","id":1,"text":"second"}\n{"event":"phase","id":1,"text":"thi',
                  b'rd"}\n']

        def phase(_identifier, text):
            order.append(text)
            if text == "first":
                # A dialog's nested event loop delivers the next chunk here.
                self.process.readAllStandardOutput.return_value = chunks[1]
                self.client.read(self.process)

        self.client.phase.connect(phase)
        self.process.readAllStandardOutput.return_value = chunks[0]
        self.client.read(self.process)
        self.assertEqual(order, ["first", "second", "third"])
        self.assertEqual(self.client.buffer, b"")

    def test_partial_line_waits_for_its_remainder_on_large_bursts(self):
        seen = []
        self.client.output.connect(lambda _identifier, text: seen.append(text))
        message = json.dumps({"event": "output", "id": 1, "text": "x"}).encode() + b"\n"
        self.process.readAllStandardOutput.return_value = message * 200000 + message[:5]
        started = time.monotonic()
        self.client.read(self.process)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(self.client.buffer, message[:5])
        self.assertLess(len(''.join(seen)), 200000)
        yielded = []
        QTimer.singleShot(0, lambda: yielded.append(True))
        deadline = time.monotonic() + 20
        while self.client._lines and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertTrue(yielded, 'large output did not yield to the Qt event loop')
        self.assertFalse(self.client._lines)
        self.assertEqual("".join(seen), "x" * 200000)
