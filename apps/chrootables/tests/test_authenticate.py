"""Terminal authentication boundaries without sudo, root access or a real terminal."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from chrootables import authenticate
from chrootables import broker
from chrootables.broker import connect_to_desktop


class TerminalAuthenticationTests(unittest.TestCase):
    def listener(self, directory):
        address = Path(directory) / "helper.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(2)
        return address, listener

    def pipe(self):
        descriptors = os.pipe()
        for descriptor in descriptors:
            self.addCleanup(self.close_fd, descriptor)
        return descriptors

    @staticmethod
    def close_fd(descriptor):
        try:
            os.close(descriptor)
        except OSError:
            pass

    def test_missing_terminal_and_missing_sudo_are_actionable(self):
        with patch.object(authenticate.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "supported terminal"):
                authenticate.terminal_argv(["sudo", "--", "broker"])
            with self.assertRaisesRegex(RuntimeError, "sudo is required"):
                authenticate.bridge("/tmp/absent-store")

    def test_invalid_store_never_opens_a_password_terminal(self):
        with patch.object(authenticate.subprocess, "Popen") as launch:
            for path in ("/tmp/..", "/", "/store\nunsafe", "relative"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    authenticate.bridge(path)
            launch.assert_not_called()

    def test_terminal_preserves_argument_boundaries_without_shell_or_password(self):
        executable = {"whitecube-terminal": "/custom/whitecube-terminal", "foot": "/usr/bin/foot"}
        command = ["/usr/bin/sudo", "-b", "--", "/usr/bin/python3", "-I", "-S",
                   "/application with spaces/broker.py", "--store", "/store with spaces", "--connect", "/socket path"]
        with patch.object(authenticate.shutil, "which", side_effect=executable.get):
            result = authenticate.terminal_argv(command)
        self.assertEqual(result[:4], ["/custom/whitecube-terminal", "--title", authenticate.TITLE, "--"])
        self.assertEqual(result[4:], command)
        self.assertNotIn("-S", result[4:7], "sudo must read from its controlling terminal")

    def test_missing_foot_skips_whitecube_wrapper_and_uses_another_terminal(self):
        installed = {"whitecube-terminal": "/usr/bin/whitecube-terminal", "xterm": "/usr/bin/xterm"}
        with patch.object(authenticate.shutil, "which", side_effect=installed.get):
            result = authenticate.terminal_argv(["sudo", "--", "helper"])
        self.assertEqual(result[0], "/usr/bin/xterm")
        self.assertEqual(result[-3:], ["sudo", "--", "helper"])

    def test_nonroot_connection_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            address, listener = self.listener(directory)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(address))
                with patch.object(authenticate, "peer_uid", return_value=1000):
                    with self.assertRaisesRegex(RuntimeError, "timed out"):
                        authenticate.wait_for_helper(listener, Mock(poll=Mock(return_value=None)), timeout=0.02)
                self.assertEqual(client.recv(1), b"")

    def test_terminal_failure_and_desktop_eof_cancel_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            _, listener = self.listener(directory)
            with self.assertRaisesRegex(RuntimeError, "terminal closed"):
                authenticate.wait_for_helper(listener, Mock(poll=Mock(return_value=1)), timeout=0.2)
            read, write = self.pipe()
            os.close(write)
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                authenticate.wait_for_helper(listener, Mock(poll=Mock(return_value=None)), input_fd=read, timeout=0.2)

    def test_successful_terminal_exit_can_precede_the_detached_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            address, listener = self.listener(directory)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(client.close)
            sender = threading.Thread(target=lambda: (time.sleep(0.03), client.connect(str(address))))
            sender.start()
            with patch.object(authenticate, "peer_uid", return_value=0):
                connection, pending = authenticate.wait_for_helper(listener, Mock(poll=Mock(return_value=0)), timeout=0.3)
            connection.close()
            sender.join(timeout=1)
            self.assertEqual(pending, b"")

    def test_bridge_forwards_json_and_finishes_after_desktop_eof(self):
        input_read, input_write = self.pipe()
        output_read, output_write = self.pipe()
        request = b'{"id":1,"action":"inventory"}\n'
        os.write(input_write, request)
        os.close(input_write)
        received = []
        threads = []
        commands = []
        states = []
        installed = {"sudo": "/fake/sudo", "whitecube-terminal": "/fake/whitecube-terminal", "foot": "/fake/foot"}

        def terminal(argv, **options):
            commands.append((argv, options))
            address = Path(argv[argv.index("--connect") + 1])
            states.append((address.parent.stat().st_mode & 0o777, address.stat().st_mode & 0o777))
            helper = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            helper.connect(str(address))

            def respond():
                with helper:
                    helper.sendall(b'{"event":"ready","writable":true}\n')
                    chunks = []
                    while data := helper.recv(65536):
                        chunks.append(data)
                    received.append(b"".join(chunks))
                    # EOF closes the request side; an in-flight task can still reply.
                    time.sleep(0.01)
                    helper.sendall(b'{"id":1,"event":"result","ok":true,"data":{"images":[]}}\n')
            thread = threading.Thread(target=respond)
            thread.start()
            threads.append(thread)
            return Mock(poll=Mock(return_value=0), wait=Mock(return_value=0))

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(authenticate, "runtime_directory", return_value=Path(directory)), \
                    patch.object(authenticate.shutil, "which", side_effect=installed.get), \
                    patch.object(authenticate.subprocess, "Popen", side_effect=terminal), \
                    patch.object(authenticate, "peer_uid", return_value=0), \
                    patch.dict(os.environ, {"LD_LIBRARY_PATH": "/private/compositor/libraries"}):
                authenticate.bridge("/workspace store", input_fd=input_read, output_fd=output_write, timeout=1)
            self.assertEqual(list(Path(directory).iterdir()), [], "temporary socket must be removed")
        for thread in threads:
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
        os.close(output_write)
        result = os.read(output_read, 65536)
        self.assertEqual(received, [request])
        self.assertEqual([json.loads(line)["event"] for line in result.splitlines()], ["ready", "result"])
        self.assertEqual(states, [(0o700, 0o600)])
        argv, options = commands[0]
        self.assertEqual(argv[4:7], ["/fake/sudo", "-b", "--"])
        self.assertEqual(argv[argv.index("--store") + 1], "/workspace store")
        self.assertNotIn("LD_LIBRARY_PATH", options["env"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)

    def test_failed_authentication_cleans_socket_and_stops_terminal(self):
        process = Mock(poll=Mock(return_value=None))
        read, write = self.pipe()
        os.close(write)
        installed = {"sudo": "/fake/sudo", "foot": "/fake/foot"}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(authenticate, "runtime_directory", return_value=Path(directory)), \
                    patch.object(authenticate.shutil, "which", side_effect=installed.get), \
                    patch.object(authenticate.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    authenticate.bridge("/store", input_fd=read, timeout=0.2)
            self.assertEqual(list(Path(directory).iterdir()), [])
        process.terminate.assert_called_once_with()

    def test_helper_checks_socket_ownership_permissions_and_peer_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            address, listener = self.listener(directory)
            with connect_to_desktop(address, os.getuid()) as connection:
                server, _ = listener.accept()
                with server:
                    server.sendall(b"trusted")
                    self.assertEqual(connection.recv(7), b"trusted")
            for bad_mode in (0o666, 0o640):
                address.chmod(bad_mode)
                with self.assertRaisesRegex(ValueError, "private"):
                    connect_to_desktop(address, os.getuid())
            address.chmod(0o600)
            Path(directory).chmod(0o750)
            with self.assertRaisesRegex(ValueError, "private"):
                connect_to_desktop(address, os.getuid())
            Path(directory).chmod(0o700)
            with self.assertRaisesRegex(ValueError, "private"):
                connect_to_desktop(address, os.getuid() + 1)
            with patch("chrootables.broker.struct.unpack", return_value=(1, os.getuid() + 1, 1)):
                with self.assertRaisesRegex(ValueError, "another user"):
                    connect_to_desktop(address, os.getuid())

    def test_helper_rejects_symlink_and_regular_file_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            address, _ = self.listener(directory)
            alias = Path(directory) / "alias.sock"
            alias.symlink_to(address)
            with self.assertRaisesRegex(ValueError, "private"):
                connect_to_desktop(alias, os.getuid())
            regular = Path(directory) / "regular.sock"
            regular.write_bytes(b"not a socket")
            regular.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "private"):
                connect_to_desktop(regular, os.getuid())
        with self.assertRaisesRegex(ValueError, "absolute"):
            connect_to_desktop("relative.sock", os.getuid())

    def test_socket_mode_helper_serves_inventory_and_closes_after_eof(self):
        user = os.getuid()
        messages = []
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            address, listener = self.listener(directory)
            store = Path(directory) / "absent-store"

            def desktop():
                try:
                    connection, _ = listener.accept()
                    with connection, connection.makefile("r", encoding="utf-8") as stream:
                        messages.append(json.loads(stream.readline()))
                        connection.sendall(b'{"id":1,"action":"inventory"}\n')
                        connection.shutdown(socket.SHUT_WR)
                        messages.extend(json.loads(line) for line in stream)
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=desktop)
            thread.start()
            with patch("sys.argv", ["broker.py", "--store", str(store), "--writable", "--connect", str(address)]), \
                    patch.object(broker.os, "geteuid", return_value=0), \
                    patch.dict(os.environ, {"SUDO_UID": str(user)}), \
                    patch.object(broker.signal, "signal") as ignore_hangup:
                broker.main()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(messages[0], {"event": "ready", "writable": True})
            self.assertTrue(messages[1]["ok"], messages[1])
            self.assertEqual(messages[1]["data"]["images"], [])
            self.assertFalse(store.exists())
            ignore_hangup.assert_called_once_with(broker.signal.SIGHUP, broker.signal.SIG_IGN)


if __name__ == "__main__":
    unittest.main()
