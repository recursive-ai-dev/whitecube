import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from chrootables.backend import OperationCancelled, Store, build_command, name, read_regular, shared_folder, session_command, terminal_command, deliver_export, MANAGER, PROFILES, _OperationLogTail
from chrootables.broker import serve


class ValidationTests(unittest.TestCase):
    def test_rejects_path_traversal_and_shell_input(self):
        for value in ("../root", "/tmp/image", "-option", "a;b", "$(id)", "a\nb", "", "a" * 65, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                name(value)

    def test_create_has_immutable_argument_boundaries(self):
        args, env = build_command("create", {"name": "python-lab", "size_mib": 4096, "lifecycle": "ephemeral", "profile": "python", "suite": "trixie"})
        self.assertEqual(args[1:], ["--create", "python-lab", "4096", "ephemeral", "python"])
        self.assertEqual(env, {"CHROOTABLE_DEBIAN_SUITE": "trixie"})

    def test_destructive_operations_require_explicit_confirmation(self):
        for action in ("delete", "repair", "restore", "drop-snapshot"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                build_command(action, {"name": "lab", "target": "before"})
            args, _ = build_command(action, {"name": "lab", "target": "before", "confirmed": True})
            self.assertEqual(args[-1], "--yes")

    def test_invalid_profile_release_mode_size(self):
        base = {"name": "lab", "size_mib": 4096}
        for field, value in [("suite", "x;id"), ("profile", "untrusted"), ("lifecycle", "other"), ("size_mib", True), ("size_mib", 0), ("size_mib", 1048577), ("size_mib", "4096")]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                build_command("create", {**base, field: value})

    def test_all_advertised_profiles_are_accepted_for_create(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                args, _ = build_command("create", {"name": "lab", "size_mib": 4096, "profile": profile})
                self.assertEqual(args[-1], profile)

    def test_unknown_operations_rejected(self):
        for action in ("exec", "worker", "bash", "--help"):
            with self.assertRaises(ValueError):
                build_command(action, {})

    def test_terminal_flags_preserve_disposable_and_network_choice(self):
        with patch("chrootables.backend.shutil.which", return_value="/usr/bin/konsole"):
            result = terminal_command("lab", Path("/var/lib/a store"), disposable=True, isolated=True)
        self.assertEqual(result[-3:], ["--try", "lab", "--isolated"])
        self.assertIn("CHROOTABLE_HOME=/var/lib/a store", result)

    def test_session_command_is_the_core_of_external_terminals(self):
        keyword = dict(disposable=True, isolated=True)
        with patch("chrootables.backend.shutil.which", return_value="/usr/bin/konsole"):
            wrapped = terminal_command("lab", Path("/var/lib/a store"), **keyword)
        core = session_command("lab", Path("/var/lib/a store"), **keyword)
        self.assertEqual(wrapped[wrapped.index(str(MANAGER)):], core[core.index(str(MANAGER)):])
        self.assertEqual(core[:3], ["sudo", "/usr/bin/env", "CHROOTABLE_AUTO_INSTALL=0"])
        self.assertIn("CHROOTABLE_HOME=/var/lib/a store", core)

    def test_session_command_restricts_devices_when_requested(self):
        core = session_command("lab", restricted_dev=True)
        self.assertIn("--restricted-dev", core)
        self.assertNotIn("--restricted-dev", session_command("lab"))
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                session_command("lab", restricted_dev=value)

    def test_shared_folder_resolves_directory_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "project files"
            folder.mkdir()
            alias = Path(directory) / "alias"
            alias.symlink_to(folder, target_is_directory=True)
            self.assertEqual(shared_folder(str(alias)), str(folder))
            alias.unlink()
            alias.symlink_to("/", target_is_directory=True)
            with self.assertRaises(ValueError):
                shared_folder(str(alias))
            alias.unlink()
            unusual = Path(directory) / "project\nfiles"
            unusual.mkdir()
            alias.symlink_to(unusual, target_is_directory=True)
            with self.assertRaises(ValueError):
                shared_folder(str(alias))

    def test_shared_folder_rejects_invalid_or_unavailable_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "file"
            file.touch()
            missing = str(Path(directory) / "missing")
            for value in (None, True, "", "relative/folder", "/", missing, str(file),
                          directory + "\x00", directory + "\n", directory + "\r"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    shared_folder(value)

    def test_shared_folder_argument_boundaries_across_terminals_and_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "project files; $(echo unsafe),draft"
            folder.mkdir()
            modes = [(False, False, "--enter"), (False, True, "--enter-isolated"),
                     (True, False, "--try"), (True, True, "--try")]
            for terminal in ("konsole", "gnome-terminal", "xterm"):
                for disposable, isolated, action in modes:
                    with self.subTest(terminal=terminal, disposable=disposable, isolated=isolated):
                        with patch("chrootables.backend.shutil.which",
                                   side_effect=lambda candidate: "/usr/bin/" + candidate if candidate == terminal else None):
                            command = terminal_command("lab", disposable=disposable, isolated=isolated,
                                                       share=str(folder), read_only=True)
                        args = command[command.index(str(MANAGER)) + 1:]
                        expected = [action, "lab"]
                        if disposable and isolated:
                            expected.append("--isolated")
                        self.assertEqual(args, expected + ["--share", str(folder), "--read-only"])

    def test_terminal_rejects_read_only_without_folder_and_non_boolean_values(self):
        with self.assertRaises(ValueError):
            terminal_command("lab", read_only=True)
        for value in (1, "false", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                terminal_command("lab", read_only=value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        for folder in ("images", "logs", "snapshots"):
            (self.path / folder).mkdir()
        self.store = Store(self.path)

    def image(self, name="lab"):
        target = self.path / "images" / (name + ".img")
        with target.open("wb") as stream:
            stream.truncate(1024 * 1048576)
        Path(str(target) + ".meta").write_text("lifecycle=ephemeral\nsuite=trixie\narchitecture=amd64\nprofile=python\ncreated_at=2026-10-16T10:00:00\nlast_used_at=never\n")
        return target

    def test_doctor_preserves_structured_report_and_readiness(self):
        for ready in (True, False):
            report = {"schema_version": 1, "ready": ready, "output": "Host health",
                      "host": "Linux", "architecture": "x86_64", "version": "1.0.0",
                      "store": str(self.path), "errors": 0 if ready else 1, "warnings": 0,
                      "checks": [{"id": "archive-keys", "status": "ok" if ready else "error",
                                  "message": "Archive keys", "remedy": "Run --setup"}],
                      "storage": {"available": True, "free_bytes": 1024, "total_bytes": 2048}}
            result = subprocess.CompletedProcess([], 0 if ready else 1, json.dumps(report), "")
            with patch("chrootables.backend.subprocess.run", return_value=result) as run:
                self.assertEqual(self.store.doctor(), report)
            self.assertEqual(run.call_args.args[0][-2:], ["--doctor", "--json"])

    def test_doctor_rejects_invalid_or_inconsistent_cli_reports(self):
        reports = [(2, "", "doctor invocation failed"), (0, "not json", ""),
                   (0, json.dumps({"schema_version": 1, "ready": False,
                                   "output": "invalid", "checks": []}), "")]
        for code, output, error in reports:
            with self.subTest(code=code, output=output):
                result = subprocess.CompletedProcess([], code, output, error)
                with patch("chrootables.backend.subprocess.run", return_value=result):
                    with self.assertRaises(RuntimeError):
                        self.store.doctor()

    def test_doctor_rejects_bad_checks_counts_and_storage_fields(self):
        report = {"schema_version": 1, "ready": True, "output": "Host health", "errors": 0,
                  "warnings": 0, "host": "Linux", "architecture": "x86_64", "version": "1.0.0",
                  "store": str(self.path), "checks": [],
                  "storage": {"available": True, "free_bytes": 1024, "total_bytes": 2048}}
        valid_check = {"id": "a", "status": "ok", "message": "Ready", "remedy": ""}
        invalid = [{"checks": [None]}, {"checks": [{}]}, {"checks": ["text"]},
                   {"checks": [{**valid_check, "status": "unknown"}]},
                   {"checks": [{**valid_check, "remedy": []}]},
                   {"checks": [valid_check, valid_check]},
                   {"warnings": 1}, {"schema_version": True}, {"errors": False},
                   {"storage": {"available": 1, "free_bytes": 0, "total_bytes": 0}},
                   {"storage": {"available": True, "free_bytes": 3000, "total_bytes": 2000}}]
        for change in invalid:
            with self.subTest(change=change):
                result = subprocess.CompletedProcess([], 0, json.dumps({**report, **change}), "")
                with patch("chrootables.backend.subprocess.run", return_value=result):
                    with self.assertRaises(RuntimeError):
                        self.store.doctor()

    def test_real_cli_inventory_and_snapshot_metadata(self):
        image = self.image()
        folder = self.path / "snapshots/deleted-lab"
        folder.mkdir()
        (folder / "before.img").touch()
        (folder / "before.img.meta").write_text("snapshot_at=2026-10-16T10:00:00\n")
        (self.path / "activity.tsv").write_text("2026-10-16T10:00:00\tcreate\tlab\tok\t/log\n")
        data = self.store.inventory()
        self.assertEqual(data["images"][0]["size_mib"], 1024)
        self.assertEqual(data["images"][0]["lifecycle"], "ephemeral")
        self.assertTrue(data["snapshots"][0]["orphan"])
        self.assertEqual(data["activity"][0]["action"], "create")
        self.assertGreater(data["free_bytes"], 0)

    def test_busy_lock_visible_in_real_inventory(self):
        import fcntl
        image = self.image()
        with Path(str(image) + ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.store.inventory()["images"][0]["status"], "busy")
        self.assertEqual(self.store.inventory()["images"][0]["status"], "ready")

    def test_legacy_image_and_symlink_ignored(self):
        image = self.image()
        Path(str(image) + ".meta").unlink()
        Path(str(image) + ".mode").write_text("ephemeral\n")
        (self.path / "images/alias.img").symlink_to(image)
        data = self.store.inventory()["images"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["lifecycle"], "ephemeral")

    def test_symlink_store_refused(self):
        alias = self.path / "alias"
        alias.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(ValueError):
            Store(alias).inventory()

    def test_safe_bounded_log_reads(self):
        log = self.path / "test.log"
        log.write_text("0123456789")
        self.assertEqual(read_regular(log, 4), "6789")
        self.assertEqual(read_regular(log, offset=6), "6789")
        self.assertEqual(read_regular(log, offset=10), "")
        link = self.path / "link"
        link.symlink_to(log)
        with self.assertRaises(OSError):
            read_regular(link)

    def test_explicit_log_offsets_do_not_jump_to_the_tail(self):
        log = self.path / "burst.log"
        log.write_text("prefix" + "x" * 300000)
        self.assertEqual(read_regular(log, 6, offset=0), "prefix")
        self.assertEqual(read_regular(log, 4), "xxxx")

    def operation_record(self, identifier="a" * 32, **changes):
        directory = self.path / "operations"
        directory.mkdir(exist_ok=True)
        state = {"format": "1", "id": identifier, "pid": "2147483647", "process_start": "1",
                 "status": "running", "stage": "publish-prepared", "action": "clone", "name": "lab"}
        state.update(changes)
        path = directory / (identifier + ".state")
        path.write_text("".join(f"{key}={value}\n" for key, value in state.items()))
        return path

    def test_invalid_operation_records_remain_visible_for_discard(self):
        path = self.operation_record()
        original = path.read_text()
        damaged = ["damaged record\n", original + "name=other\n", original + "unexpected=value\n",
                   original.replace("format=1", "format=2"), original.replace("pid=2147483647", "pid=0"),
                   original.replace("stage=publish-prepared", "stage=" + "x" * 49),
                   original.replace("name=lab", "name=../lab"), original.replace("process_start=1", "process_start=no"),
                   original.replace("id=" + "a" * 32, "id=" + "b" * 32), "x" * (256 * 1024 + 1)]
        for content in damaged:
            with self.subTest(content=content[:100]):
                path.write_text(content)
                records = self.store._scan_operations()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["id"], "a" * 32)
                self.assertTrue(records[0]["invalid"])
                self.assertTrue(records[0]["discardable"])
                self.assertFalse(records[0]["recoverable"])
                self.assertEqual(records[0]["reclaimable_bytes"], 0)
        path.write_text(original)
        repaired = self.store._scan_operations()[0]
        self.assertNotIn("invalid", repaired)
        self.assertTrue(repaired["recoverable"])

    def test_operation_recovery_eligibility_matches_cli_listing(self):
        for index, status in enumerate(("running", "completed", "failed", "interrupted", "recovered")):
            self.operation_record(f"{index:032x}", status=status)
        command = 'source "$1"; OPERATIONS_DIR="$2/operations"; LOG_DIR="$2/logs"; list_operations json'
        result = subprocess.run(["bash", "-c", command, "operation-list-test", str(MANAGER), str(self.path)],
                                check=True, capture_output=True, text=True)
        expected = json.loads(result.stdout)
        actual = self.store._scan_operations()
        for backend_record, cli_record in zip(actual, expected, strict=True):
            for field in ("id", "status", "active", "recoverable", "discardable"):
                self.assertEqual(backend_record[field], cli_record[field], (field, cli_record))

    def test_inventory_cache_reuses_unchanged_state_and_tracks_locks_and_mutations(self):
        import fcntl
        import chrootables.backend as backend_mod
        image = self.image()
        real_run = subprocess.run
        with patch("chrootables.backend.subprocess.run", side_effect=real_run) as mocked_run:
            first = self.store.inventory()
            second = self.store.inventory()
            self.assertEqual(first["images"], second["images"])
            self.assertEqual(mocked_run.call_count, 1)
            with Path(str(image) + ".lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = self.store.inventory()
                self.assertEqual(busy["images"][0]["status"], "busy")
                self.assertEqual(mocked_run.call_count, 1)
            ready = self.store.inventory()
            self.assertEqual(ready["images"][0]["status"], "ready")
            self.assertEqual(mocked_run.call_count, 1)
            Path(str(image) + ".meta").write_text("lifecycle=persistent\nsuite=bookworm\narchitecture=amd64\nprofile=minimal\ncreated_at=2026-10-16T10:00:00\nlast_used_at=never\n")
            updated = self.store.inventory()
            self.assertEqual(updated["images"][0]["lifecycle"], "persistent")
            self.assertEqual(updated["images"][0]["suite"], "bookworm")
            self.assertEqual(mocked_run.call_count, 2)

    def test_missing_store_is_empty_without_creating_it(self):
        missing = self.path / "missing"
        self.assertEqual(Store(missing).inventory()["images"], [])
        self.assertFalse(missing.exists())

    def test_concurrent_inventory_requests_share_one_cache_refresh(self):
        self.image()
        start = threading.Barrier(2)
        real_run = subprocess.run

        def slow_inventory(*args, **kwargs):
            time.sleep(0.05)
            return real_run(*args, **kwargs)

        def inventory():
            start.wait(timeout=5)
            return self.store.inventory()

        with patch("chrootables.backend.subprocess.run", side_effect=slow_inventory) as run:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(inventory) for _ in range(2)]
                results = [future.result(timeout=5) for future in futures]
        self.assertEqual(results[0]["images"], results[1]["images"])
        self.assertEqual(run.call_count, 1)

    def test_cached_inventory_does_not_block_on_fifo_lock(self):
        image = self.image()
        os.mkfifo(str(image) + ".lock")
        script = """import sys
from chrootables.backend import Store
store = Store(sys.argv[1])
assert store.inventory()['images'][0]['status'] == 'ready'
assert store.inventory()['images'][0]['status'] == 'ready'
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.path)],
                                cwd=MANAGER.parent, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_selected_operation_log_is_exact_and_confined(self):
        first = self.path / "logs/lab-create.log"
        second = self.path / "logs/lab-clone.log"
        first.write_text("creation details")
        second.write_text("clone details")
        (self.path / "activity.tsv").write_text(f"now\tcreate\tlab\tok\t{first}\nnow\tclone\tlab\tok\t{second}\n")
        self.assertIn("creation details", self.store.logs("lab", str(first)))
        self.assertNotIn("clone details", self.store.logs("lab", str(first)))
        with self.assertRaises(ValueError):
            self.store.logs("lab", "/etc/shadow")
        with self.assertRaises(ValueError):
            self.store.logs("different", str(first))

    def test_broker_read_only_rejects_mutations(self):
        messages = io.StringIO()
        serve(self.store, io.StringIO(json.dumps({"id": 1, "action": "delete", "params": {"name": "lab", "confirmed": True}}) + "\n"), messages)
        output = [json.loads(line) for line in messages.getvalue().splitlines()]
        self.assertFalse(output[-1]["ok"])
        self.assertIn("Unlock", output[-1]["error"])
        self.assertEqual(output[-1]["code"], "permission_required")

    def test_broker_inventory_protocol(self):
        self.image()
        messages = io.StringIO()
        serve(self.store, io.StringIO('{"id":1,"action":"inventory"}\n'), messages)
        output = [json.loads(line) for line in messages.getvalue().splitlines()]
        self.assertTrue(output[-1]["ok"])
        self.assertEqual(output[-1]["data"]["images"][0]["name"], "lab")

    def test_broker_cancellation_bypasses_saturated_workers(self):
        store = Mock()
        all_started, cancelled = threading.Event(), threading.Event()
        started = 0
        lock = threading.Lock()

        def doctor():
            nonlocal started
            with lock:
                started += 1
                if started == 4:
                    all_started.set()
            if not cancelled.wait(3):
                raise RuntimeError("Cancellation was blocked behind disk reads")
            return {"ready": True}

        def cancel():
            cancelled.set()
            return {"cancelled": True, "stage": "copying"}

        class Requests(io.StringIO):
            def readline(self, *args):
                line = super().readline(*args)
                if '"cancel"' in line:
                    if not all_started.wait(3):
                        raise RuntimeError("Read workers did not start")
                return line

        store.doctor.side_effect = doctor
        store.cancel_operation.side_effect = cancel
        requests = [json.dumps({"id": index + 1, "action": "doctor"}) for index in range(4)]
        requests.append(json.dumps({"id": 5, "action": "cancel"}))
        output = io.StringIO()
        serve(store, Requests("\n".join(requests) + "\n"), output, writable=True)
        results = [json.loads(line) for line in output.getvalue().splitlines() if '"result"' in line]
        self.assertEqual(len(results), 5)
        self.assertTrue(all(item["ok"] for item in results), results)
        store.cancel_operation.assert_called_once()

    def test_broker_bounds_pending_work_and_reports_overload(self):
        store = Mock()
        release, overloaded = threading.Event(), threading.Event()
        store.doctor.side_effect = lambda: (release.wait(3), {"ready": True})[1]

        class Output(io.StringIO):
            def write(self, text):
                result = super().write(text)
                if '"overloaded"' in text:
                    overloaded.set()
                return result

        requests = "".join(json.dumps({"id": index + 1, "action": "doctor"}) + "\n" for index in range(30))
        output = Output()
        worker = threading.Thread(target=serve, args=(store, io.StringIO(requests), output))
        with patch("chrootables.broker.MAX_PENDING_REQUESTS", 4):
            worker.start()
            try:
                self.assertTrue(overloaded.wait(2), "Expected a prompt overload response")
                self.assertLessEqual(store.doctor.call_count, 4)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        results = [json.loads(line) for line in output.getvalue().splitlines() if '"result"' in line]
        self.assertEqual(len(results), 30)
        self.assertEqual(len({item["id"] for item in results}), 30)
        self.assertTrue(any(item.get("code") == "overloaded" for item in results))

    def test_broker_rejects_invalid_request_contracts(self):
        requests = [{"id": value, "action": "doctor"} for value in (True, 0, -1, 2147483648, "1")]
        requests += [{"id": 1, "action": []}, {"id": 1, "action": "doctor", "params": []}]
        for request in requests:
            with self.subTest(request=request):
                store, output = Mock(), io.StringIO()
                serve(store, io.StringIO(json.dumps(request) + "\n"), output)
                self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["event"], "error")
                store.doctor.assert_not_called()

    def test_privileged_export_uses_invoking_users_supplementary_groups(self):
        source = self.path / "source.tar.gz"
        source.write_bytes(b"payload")
        account = Mock(pw_name="export-user", pw_gid=2100)
        with patch.dict(os.environ, {"PKEXEC_UID": "2000"}), \
                patch("chrootables.backend.os.geteuid", return_value=0), \
                patch("chrootables.backend.pwd.getpwuid", return_value=account), \
                patch("chrootables.backend.os.getgrouplist", return_value=[2100, 2200, 2300]) as groups, \
                patch("chrootables.backend.subprocess.run", return_value=Mock(returncode=0)) as run:
            deliver_export(source, self.path / "destination.tar.gz")
        groups.assert_called_once_with("export-user", 2100)
        self.assertEqual(run.call_args.kwargs["user"], 2000)
        self.assertEqual(run.call_args.kwargs["group"], 2100)
        self.assertEqual(run.call_args.kwargs["extra_groups"], [2100, 2200, 2300])

    def test_terminal_authenticated_export_drops_to_sudo_user(self):
        source = self.path / "source.tar.gz"
        source.write_bytes(b"payload")
        account = Mock(pw_name="desktop-user", pw_gid=2100)
        with patch.dict(os.environ, {"SUDO_UID": "2000", "PKEXEC_UID": "0"}), \
                patch("chrootables.backend.os.getuid", return_value=0), \
                patch("chrootables.backend.os.geteuid", return_value=0), \
                patch("chrootables.backend.pwd.getpwuid", return_value=account) as user, \
                patch("chrootables.backend.os.getgrouplist", return_value=[2100, 2200]), \
                patch("chrootables.backend.subprocess.run", return_value=Mock(returncode=0)) as run:
            deliver_export(source, self.path / "saved by desktop user.tar.gz")
        user.assert_called_once_with(2000)
        self.assertEqual(run.call_args.kwargs["user"], 2000)
        self.assertEqual(run.call_args.kwargs["group"], 2100)
        self.assertEqual(run.call_args.kwargs["extra_groups"], [2100, 2200])

    def test_root_export_without_desktop_identity_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("chrootables.backend.os.getuid", return_value=0):
            with self.assertRaisesRegex(ValueError, "invoking user's identity"):
                deliver_export(self.path / "archive.tar.gz", self.path / "destination.tar.gz")

    @unittest.skipIf(os.getuid() == 0, "Tests the user-side export writer")
    def test_export_delivers_and_refuses_overwrite(self):
        source = self.path / "source.tar.gz"
        source.write_bytes(b"archive payload")
        target = self.path / "saved archive.tar.gz"
        deliver_export(source, target)
        self.assertEqual(target.read_bytes(), b"archive payload")
        with self.assertRaises(RuntimeError):
            deliver_export(source, target)
        self.assertEqual(target.read_bytes(), b"archive payload")

    @unittest.skipIf(os.getuid() == 0, "Tests the user-side export writer")
    def test_failed_export_copy_removes_partial_destination_and_retains_source(self):
        source = self.path / "source.tar.gz"
        source.write_bytes(b"original archive")
        target = self.path / "partial.tar.gz"
        real_run = subprocess.run
        for failure in ("OSError(28, 'No space left on device')", "KeyboardInterrupt()"):
            with self.subTest(failure=failure):
                def inject_copy_failure(argv, **kwargs):
                    injected = ("def failed_copy(source, destination, length):\n"
                                "    destination.write(b'partial')\n"
                                "    raise " + failure + "\n"
                                "def unsupported_copy(*args):\n"
                                "    raise OSError(18, 'Cross-device link')\n"
                                "os.copy_file_range = unsupported_copy\n"
                                "shutil.copyfileobj = failed_copy\n")
                    argv = list(argv)
                    argv[4] = argv[4].replace("source, destination =", injected + "source, destination =", 1)
                    return real_run(argv, **kwargs)
                with patch("chrootables.backend.subprocess.run", side_effect=inject_copy_failure):
                    with self.assertRaisesRegex(RuntimeError, "saving the chosen copy failed"):
                        deliver_export(source, target)
                self.assertFalse(target.exists())
                self.assertEqual(source.read_bytes(), b"original archive")

    def test_inventory_failure_does_not_publish_a_new_cache_signature(self):
        image = self.image()
        initial = self.store.inventory()
        signature = self.store._images_sig
        metadata_path = Path(str(image) + ".meta")
        metadata_path.write_text(metadata_path.read_text() + "origin=changed\n")
        with patch("chrootables.backend.subprocess.run",
                   return_value=Mock(returncode=1, stderr="image disappeared", stdout="")):
            with self.assertRaisesRegex(PermissionError, "image disappeared"):
                self.store.inventory()
        self.assertEqual(self.store._images_sig, signature)
        self.assertEqual(self.store._images_cache, initial["images"])
        self.assertEqual(self.store.inventory()["images"][0]["origin"], "changed")

    def test_snapshot_disappearing_during_scan_is_omitted_with_consistent_counts(self):
        self.image()
        folder = self.path / "snapshots/lab"
        folder.mkdir()
        snapshot = folder / "checkpoint.img"
        snapshot.touch()
        real_scandir = os.scandir

        class VanishingScan:
            def __enter__(self):
                self.scan = real_scandir(folder)
                return self.scan.__enter__()

            def __exit__(self, *args):
                self.scan.__exit__(*args)
                snapshot.unlink()

        def scandir(path):
            return VanishingScan() if Path(path) == folder else real_scandir(path)

        with patch("chrootables.backend.os.scandir", side_effect=scandir):
            report = self.store.inventory()
        self.assertEqual(report["snapshots"], [])
        self.assertEqual(report["images"][0]["snapshots"], 0)
        self.assertEqual(self.store.inventory()["snapshots"], [])

    def test_image_disappearing_during_stat_does_not_fail_inventory(self):
        image = self.image()
        real_scandir = os.scandir
        removed = False

        class VanishingEntry:
            def __init__(self, entry):
                self.entry = entry
                self.name = entry.name

            def is_symlink(self):
                return False

            def is_file(self, **options):
                return True

            def stat(self, **options):
                nonlocal removed
                image.unlink()
                removed = True
                raise FileNotFoundError(str(image))

        class Scan:
            def __enter__(self):
                self.actual = real_scandir(image.parent)
                return iter(VanishingEntry(entry) if entry.name == image.name else entry for entry in self.actual)

            def __exit__(self, *args):
                self.actual.close()

        with patch("chrootables.backend.os.scandir", side_effect=lambda path: Scan() if Path(path) == image.parent else real_scandir(path)):
            report = self.store.inventory()
        self.assertTrue(removed)
        self.assertEqual(report["images"], [])

    def test_export_rejects_fifo_and_directory_before_creating_destination(self):
        fifo = self.path / "archive.tar.gz"
        os.mkfifo(fifo)
        target = self.path / "copy.tar.gz"
        with patch("chrootables.backend.subprocess.run") as launch:
            for source in (fifo, self.path):
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, "regular"):
                    deliver_export(source, target)
            launch.assert_not_called()
        self.assertFalse(target.exists())

    def test_broker_rejects_deep_json_and_processes_the_following_request(self):
        nested = '{"id":1,"action":"inventory","params":{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}}\n'
        messages = io.StringIO()
        serve(self.store, io.StringIO(nested + '{"id":2,"action":"inventory"}\n'), messages)
        output = [json.loads(line) for line in messages.getvalue().splitlines()]
        self.assertEqual(output[1]["event"], "error")
        self.assertTrue(next(item for item in output if item.get("id") == 2)["ok"])

    def test_broker_reports_spawn_failure_and_keeps_serving(self):
        request = json.dumps({"id": 1, "action": "clone", "params": {"name": "lab", "target": "copy"}})
        request += '\n{"id":2,"action":"doctor"}\n'
        output = io.StringIO()
        with patch("chrootables.backend.subprocess.Popen", side_effect=OSError("cannot launch manager")), \
                patch.object(self.store, "doctor", return_value={"ready": True, "output": "ready"}):
            serve(self.store, io.StringIO(request), output, writable=True)
        results = {message["id"]: message for line in output.getvalue().splitlines()
                   if (message := json.loads(line)).get("event") == "result"}
        self.assertFalse(results[1]["ok"])
        self.assertEqual(results[1]["code"], "operation_failed")
        self.assertIn("cannot launch manager", results[1]["error"])
        self.assertTrue(results[2]["ok"])

    def test_operation_failure_is_reported_and_streamed(self):
        fake = self.path / "fake-manager"
        fake.write_text("#!/bin/sh\nprintf 'diagnostic output\\n'\nexit 17\n")
        fake.chmod(0o755)
        output = []
        with patch("chrootables.backend.MANAGER", fake), \
                patch("chrootables.backend.subprocess.Popen", wraps=subprocess.Popen) as launch:
            code = self.store.operate("clone", {"name": "lab", "target": "copy"}, output.append)
        self.assertEqual(code, 17)
        self.assertIn("diagnostic output", "".join(output))
        self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_operation_log_append_during_read_is_streamed_once(self):
        operation_id = "a" * 32
        log = self.path / "logs" / (operation_id + ".log")
        log.write_text("first\n")
        process = Mock(stdout=io.StringIO(), returncode=0)
        process.poll.side_effect = [None, 0]
        real_open = os.open
        appended = False

        def append_before_open(path, flags, *args, **kwargs):
            nonlocal appended
            if Path(path) == log and not appended:
                appended = True
                with log.open("a") as stream:
                    stream.write("second\n")
            return real_open(path, flags, *args, **kwargs)

        output = []
        with patch("chrootables.backend.subprocess.Popen", return_value=process), \
             patch("chrootables.backend.uuid.uuid4", return_value=Mock(hex=operation_id)), \
             patch("chrootables.backend.time.time", return_value=0), \
             patch("chrootables.backend.time.sleep"), \
             patch("chrootables.backend.os.open", side_effect=append_before_open):
            code = self.store.operate("clone", {"name": "lab", "target": "copy"}, output.append)
        self.assertEqual(code, 0)
        self.assertEqual("".join(output), "first\nsecond\n")

    def test_completed_operation_drains_large_log_and_decodes_chunk_boundaries(self):
        identifier = "c" * 32
        log = self.path / "logs" / (identifier + ".log")
        expected = "begin\n" + "x" * (256 * 1024 - 7) + "€" + "é" * (1024 * 1024) + "\nend\n"
        log.write_text(expected, encoding="utf-8")
        process = Mock(stdout=io.StringIO(), returncode=0)
        process.poll.return_value = 0
        output = []
        with patch("chrootables.backend.subprocess.Popen", return_value=process), \
                patch("chrootables.backend.uuid.uuid4", return_value=Mock(hex=identifier)):
            self.assertEqual(self.store.operate("clone", {"name": "lab", "target": "copy"}, output.append), 0)
        self.assertGreater(len(output), 4)
        self.assertEqual("".join(output), expected)

    def test_streaming_log_preserves_split_characters_and_flushes_incomplete_tail(self):
        log = self.path / "logs/stream.log"
        output = []
        tail = _OperationLogTail(log, output.append)
        log.write_bytes(b"first \xe2")
        tail.read()
        self.assertEqual("".join(output), "first ")
        with log.open("ab") as stream:
            stream.write(b"\x82")
        tail.read()
        self.assertEqual("".join(output), "first ")
        with log.open("ab") as stream:
            stream.write(b"\xac last\xe2")
        tail.read(final=True)
        self.assertEqual("".join(output), "first € last�")

    def test_streaming_log_resets_decoder_on_truncation_or_replacement(self):
        log = self.path / "logs/stream.log"
        for replace in (False, True):
            with self.subTest(replace=replace):
                log.write_bytes(b"old\xe2")
                output = []
                tail = _OperationLogTail(log, output.append)
                tail.read()
                if replace:
                    other = log.with_suffix(".replacement")
                    other.write_bytes(b"new")
                    other.replace(log)
                else:
                    log.write_bytes(b"n")
                tail.read(final=True)
                self.assertEqual("".join(output), "old�" + ("new" if replace else "n"))

    def test_streaming_log_limits_live_poll_and_rejects_special_files_without_leaks(self):
        log = self.path / "logs/stream.log"
        expected = "x" * (2 * 1024 * 1024 + 7)
        log.write_text(expected)
        output = []
        tail = _OperationLogTail(log, output.append)
        tail.read()
        self.assertEqual(len("".join(output)), 1024 * 1024)
        tail.read(final=True)
        self.assertEqual("".join(output), expected)
        fifo = self.path / "fifo"
        os.mkfifo(fifo)
        before = len(os.listdir("/proc/self/fd"))
        for path in (self.path, fifo):
            _OperationLogTail(path, output.append).read(final=True)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_operation_tails_only_its_log_and_forwards_matching_phases(self):
        operation_id = "b" * 32
        (self.path / "logs" / (operation_id + ".log")).write_text("own log\n")
        (self.path / "logs/other-workspace.log").write_text("unrelated output\n")
        stream = ("CHROOTABLE_EVENT " + json.dumps({"id": operation_id, "stage": "Copying image"}) + "\n"
                  "CHROOTABLE_EVENT " + json.dumps({"id": "c" * 32, "stage": "Other task"}) + "\n"
                  "normal output\n")
        process = Mock(stdout=io.StringIO(stream), returncode=0)
        process.poll.return_value = 0
        output, phases = [], []
        with patch("chrootables.backend.subprocess.Popen", return_value=process) as spawn, \
                patch("chrootables.backend.uuid.uuid4", return_value=Mock(hex=operation_id)), \
                patch("chrootables.backend.os.scandir", side_effect=AssertionError("must not scan logs")):
            code = self.store.operate("clone", {"name": "lab", "target": "copy"}, output.append, progress=phases.append)
        self.assertEqual(code, 0)
        self.assertEqual(phases, ["Copying image"])
        text = "".join(output)
        self.assertIn("own log", text)
        self.assertIn("normal output", text)
        self.assertNotIn("unrelated output", text)
        self.assertEqual(spawn.call_args.kwargs["env"]["CHROOTABLE_OPERATION_ID"], operation_id)

    def test_storage_summary_counts_snapshots_exports_and_logs_without_following_links(self):
        image = self.image()
        folder = self.path / "snapshots/lab"
        folder.mkdir()
        snapshot = folder / "clean.img"
        snapshot.write_bytes(b"snapshot" * 1024)
        exports = self.path / "exports"
        exports.mkdir()
        archive = exports / "lab.tar.gz"
        archive.write_bytes(b"archive" * 1024)
        (exports / "outside-link").symlink_to(image)
        log = self.path / "logs/operation.log"
        log.write_text("operation output")
        report = self.store.inventory()
        self.assertEqual(report["snapshots"][0]["allocated_bytes"], snapshot.stat().st_blocks * 512)
        self.assertEqual(report["storage"], {
            "images_bytes": image.stat().st_blocks * 512,
            "snapshots_bytes": snapshot.stat().st_blocks * 512,
            "exports_bytes": archive.stat().st_blocks * 512,
            "logs_bytes": log.stat().st_blocks * 512})
        new_archive = exports / "second.tar.gz"
        new_archive.write_bytes(b"second export")
        refreshed = self.store.inventory()["storage"]
        self.assertEqual(refreshed["exports_bytes"], (archive.stat().st_blocks + new_archive.stat().st_blocks) * 512)

    def test_resolver_setup_does_not_use_uninitialized_local(self):
        work = self.path / "work"
        root = self.path / "root"
        work.mkdir()
        (root / "etc").mkdir(parents=True)
        script = 'source "$1"; umask 077; mount() { return 0; }; setup_resolv_conf "$2" "$3"'
        result = subprocess.run(["bash", "-c", script, "resolver-test", str(MANAGER), str(work), str(root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nameserver", (work / "resolv.conf").read_text())
        self.assertTrue((root / "etc/resolv.conf").exists())
        self.assertEqual((work / "resolv.conf").stat().st_mode & 0o777, 0o644)

    def test_requirements_hash_locked_and_minimal_scope(self):
        req_text = (MANAGER.parent / "requirements.txt").read_text()
        self.assertIn("PySide6-Essentials==6.11.2", req_text)
        self.assertIn("shiboken6==6.11.2", req_text)
        self.assertNotIn("PySide6-Addons", req_text)
        self.assertNotIn(">=", req_text)
        self.assertEqual(req_text.count("--hash=sha256:"), 10)

    def test_suite_and_mirror_supply_chain_validation(self):
        script = '''
source "$1"
valid_suite trixie && valid_suite bookworm && valid_suite stable || exit 10
! valid_suite "--no-check-gpg" || exit 11
! valid_suite $'trixie\nlifecycle=ephemeral' || exit 12
valid_mirror "https://deb.debian.org/debian" || exit 13
! valid_mirror "http://deb.debian.org/debian" || exit 14
! valid_mirror "file:///tmp/mirror" || exit 15
! valid_mirror "--no-check-gpg" || exit 16
! write_metadata "$2/img" persistent $'trixie\norigin=injected' amd64 1024 now never minimal || exit 17
'''
        result = subprocess.run(["bash", "-c", script, "supply-chain-test", str(MANAGER), str(self.path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"exit {result.returncode}: {result.stderr}")

    def test_new_commands_inventory_collections_and_cancellation(self):
        archive = self.path / "exports/lab-20261015-120000.tar.gz"
        archive.parent.mkdir(exist_ok=True)
        archive.write_bytes(b"archive-data")
        cache_dir = self.path / "archive/trixie-amd64"
        cache_dir.mkdir(parents=True)
        (cache_dir / "base.tgz").write_bytes(b"cached-root")
        (cache_dir / "mirror").write_text("https://deb.debian.org/debian\n")
        op_id = "e" * 32
        ops_dir = self.path / "operations"
        ops_dir.mkdir(exist_ok=True)
        partial = self.path / "images/.copy.ABC123.partial"
        partial.parent.mkdir(exist_ok=True)
        partial.write_bytes(b"x" * 8192)
        (ops_dir / f"{op_id}.state").write_text(
            f"format=1\nid={op_id}\npid=2147483647\nprocess_start=1\nstatus=running\nstage=publish-metadata\n"
            "action=clone\nname=lab\npending=images/.copy.ABC123.partial\ndestination=images/copy.img\n")
        manager = str(MANAGER)
        command, _ = build_command("import", {"name": "imported", "archive": str(archive), "size_mib": 512,
                                              "lifecycle": "persistent", "profile": "minimal"})
        self.assertEqual(command, [manager, "--import", "imported", str(archive.resolve()), "512", "persistent", "minimal"])
        command, _ = build_command("import", {"name": "imported", "archive": str(archive)})
        self.assertEqual(command, [manager, "--import", "imported", str(archive.resolve()), "", "persistent", "minimal"])
        for action, argument in (("recover-operation", op_id), ("discard-operation", op_id)):
            self.assertEqual(build_command(action, {"id": op_id, "confirmed": True})[0],
                             [manager, "--" + action, argument, "--yes"])
            with self.assertRaisesRegex(ValueError, "confirmation"):
                build_command(action, {"id": op_id})
        self.assertEqual(build_command("prune-logs", {"keep": 25, "confirmed": True})[0],
                         [manager, "--prune-logs", "25", "--yes"])
        self.assertEqual(build_command("delete-export", {"filename": archive.name, "confirmed": True})[0],
                         [manager, "--delete-export", archive.name, "--yes"])
        self.assertEqual(build_command("archive-sync", {"suite": "trixie"})[0], [manager, "--archive-sync", "trixie"])
        store = Store(self.path)
        report = store.inventory()
        self.assertEqual(len(report["operations"]), 1)
        operation = report["operations"][0]
        self.assertTrue(operation["recoverable"])
        self.assertTrue(operation["discardable"])
        self.assertGreaterEqual(operation["reclaimable_bytes"], 4096)
        self.assertEqual(report["exports"][0]["filename"], archive.name)
        self.assertEqual(report["archives"][0]["suite"], "trixie")
        with patch("chrootables.backend.shutil.which", side_effect=lambda name: "/usr/bin/alacritty" if name == "alacritty" else None):
            command = terminal_command("lab", self.path)
        self.assertEqual(command[:4], ["alacritty", "--hold", "--title", "Cube Chrootables · lab"])

    def test_cancellation_is_limited_to_actions_and_stages_that_can_stop_safely(self):
        store = Store(self.path)
        with self.assertRaisesRegex(RuntimeError, "No running operation"):
            store.cancel_operation()
        process = Mock()
        process.poll.return_value = None
        process.pid = 2147483647
        store._active_process = process
        for action, stage in (("clone", "publish-metadata"), ("clone", "publish-prepared"), ("resize", "expanding"),
                              ("resize", "checking-filesystem"),
                              ("repair", "repairing-filesystem"), ("delete", "deleting"), ("setup", "starting"),
                              ("restore", "published")):
            with self.subTest(action=action, stage=stage), patch("chrootables.backend.os.killpg") as killpg:
                store._active_action, store._active_stage = action, stage
                with self.assertRaisesRegex(RuntimeError, "cannot be stopped"):
                    store.cancel_operation()
                killpg.assert_not_called()
        with patch("chrootables.backend.os.killpg") as killpg:
            store._active_action, store._active_stage = "clone", "copying"
            self.assertTrue(store.cancel_operation()["cancelled"])
            killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        with patch("chrootables.backend.os.killpg", side_effect=ProcessLookupError):
            with self.assertRaisesRegex(RuntimeError, "No running"):
                store.cancel_operation()

    def manager_wrapper(self, overrides):
        """A real manager process tree whose privileged commands are replaced."""
        wrapper = self.path / "wrapper"
        wrapper.write_text(f"""#!/usr/bin/env bash
source {shlex.quote(str(MANAGER))}
require_root() {{ :; }}
check_prerequisites() {{ :; }}
{overrides}
main "$@"
""")
        wrapper.chmod(0o755)
        for directory in ("images", "logs", "exports", "snapshots", "operations"):
            (self.path / directory).mkdir(exist_ok=True)
        (self.path / "images/lab.img").write_bytes(b"image")
        return wrapper

    def run_clone_and_cancel(self, overrides, *, wait_for_stage, wait_for_file=None, after_cancel=None):
        wrapper = self.manager_wrapper(overrides)
        store = Store(self.path)
        outcome = {}
        started = threading.Event()

        def progress(stage):
            if stage == wait_for_stage:
                started.set()

        def run():
            try:
                outcome["code"] = store.operate("clone", {"name": "lab", "target": "copy"}, lambda _: None, progress=progress)
            except OperationCancelled as error:
                outcome["cancelled"] = str(error)

        with patch("chrootables.backend.MANAGER", wrapper):
            worker = threading.Thread(target=run)
            begin = time.monotonic()
            worker.start()
            self.assertTrue(started.wait(10), "operation never reached " + wait_for_stage)
            while wait_for_file is not None and not wait_for_file.exists():
                self.assertLess(time.monotonic() - begin, 10, "child never became ready")
                time.sleep(0.01)
            cancel = store.cancel_operation()
            if after_cancel:
                after_cancel()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertTrue(cancel["cancelled"])
        return outcome, time.monotonic() - begin

    def test_cancel_stops_the_real_process_tree_and_removes_staged_files(self):
        outcome, elapsed = self.run_clone_and_cancel(
            'cp() { command sleep 60; }', wait_for_stage="copying")
        self.assertIn("cancelled", outcome, outcome)
        self.assertLess(elapsed, 10)
        self.assertFalse((self.path / "images/copy.img").exists())
        self.assertEqual(list((self.path / "images").glob(".copy.*")), [])
        operations = [line for path in (self.path / "operations").glob("*.state") for line in path.read_text().splitlines()]
        self.assertIn("status=failed", operations)
        # The child inherits the store path in its environment; none may survive.
        marker = ("CHROOTABLE_HOME=" + str(self.path)).encode()
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    if (entry / "cmdline").read_bytes().startswith(b"sleep\0"):
                        self.assertNotIn(marker, (entry / "environ").read_bytes(), entry.name)
                except OSError:
                    continue

    def test_cancel_repeats_until_a_child_that_missed_the_signal_stops(self):
        # A signal landing just before the shell forks misses the child, and the shell
        # defers its own trap until that child exits. Surviving one signal models this.
        ready = self.path / "child-ready"
        child = (f'seen=0; trap "((seen++)) && exit 143" TERM; : >{shlex.quote(str(ready))}; '
                 'for tick in {1..400}; do sleep 0.05; done')
        outcome, elapsed = self.run_clone_and_cancel(
            f"cp() {{ bash -c {shlex.quote(child)}; }}", wait_for_stage="copying", wait_for_file=ready)
        self.assertIn("cancelled", outcome, outcome)
        self.assertLess(elapsed, 10)
        self.assertEqual(list((self.path / "images").glob(".copy.*")), [])

    def test_worker_cleanup_cannot_be_interrupted_by_a_repeated_signal(self):
        # Cancel repeats its signal, and cleanup is what unmounts the image.
        body = MANAGER.read_text().split("\nworker_cleanup() {\n", 1)[1]
        self.assertEqual(body.split("\n")[1], "    protect_from_signals", body[:80])

    def test_cancel_loses_the_race_against_publication_and_reports_success(self):
        # The desktop saw "copying", but the manager is already past the point of no
        # return: the signal is ignored and the work really finishes.
        ready = self.path / 'publication-protected'
        release = self.path / 'publication-release'
        os.mkfifo(release)

        def release_publication():
            # A nonblocking writer fails promptly if a regression killed the
            # protected reader, rather than hanging this test during cleanup.
            descriptor = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(descriptor, b'continue\n')
            finally:
                os.close(descriptor)

        outcome, _ = self.run_clone_and_cancel(
            f'''cp() {{
                local publication_fd
                protect_from_signals
                exec {{publication_fd}}<> {shlex.quote(str(release))}
                : > {shlex.quote(str(ready))}
                IFS= read -r _ <&"$publication_fd"
                exec {{publication_fd}}>&-
            }}''', wait_for_stage="copying", wait_for_file=ready, after_cancel=release_publication)
        self.assertEqual(outcome.get("code"), 0, outcome)
        self.assertTrue((self.path / "images/copy.img").exists())
        self.assertEqual(list((self.path / "images").glob(".copy.*")), [])

    def test_inventory_survives_non_utf8_metadata(self):
        images = self.path / "images"
        images.mkdir(exist_ok=True)
        (images / "latin.img").write_bytes(b"image")
        (images / "latin.img.meta").write_bytes(b"lifecycle=persistent\norigin=import:caf\xe9.tar.gz\n")
        report = Store(self.path).inventory()
        self.assertEqual(report["images"][0]["name"], "latin")
        self.assertIn("�", report["images"][0]["origin"])

    def test_retained_export_is_resolved_by_name_in_the_broker(self):
        store = Store(self.path)
        exports = self.path / "exports"
        exports.mkdir(exist_ok=True)
        (exports / "lab-1.tar.gz").write_bytes(b"x")
        captured = {}

        def fake_popen(args, **_):
            captured["args"] = args
            raise OSError("stop here")

        with patch("chrootables.backend.subprocess.Popen", fake_popen), self.assertRaises(OSError):
            store.operate("import", {"name": "restored", "export": "lab-1.tar.gz"}, lambda _: None)
        self.assertEqual(captured["args"][2:5], ["restored", str((exports / "lab-1.tar.gz").resolve()), ""])
        for unsafe in ("../secret.tar.gz", "/etc/x.tar.gz", "a/b.tar.gz", "not-an-archive"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                store.operate("import", {"name": "restored", "export": unsafe}, lambda _: None)

    def test_pruned_log_has_a_clear_message(self):
        logs = self.path / "logs"
        logs.mkdir(exist_ok=True)
        path = logs / ("a" * 32 + ".log")
        (self.path / "activity.tsv").write_text(f"2026-10-30\tclone\tlab\tok\t{path}\n")
        with self.assertRaisesRegex(RuntimeError, "pruned"):
            Store(self.path).logs("lab", str(path))

    def test_sessions_fall_back_to_pkexec_only_without_sudo(self):
        with patch("chrootables.backend.shutil.which", side_effect=lambda tool: "/usr/bin/" + tool):
            self.assertEqual(session_command("lab", self.path)[0], "sudo")
        with patch("chrootables.backend.shutil.which", side_effect=lambda tool: None):
            self.assertEqual(session_command("lab", self.path)[0], "sudo")
        with patch("chrootables.backend.shutil.which", side_effect=lambda tool: "/usr/bin/pkexec" if tool == "pkexec" else None), \
                patch.dict(os.environ, {"TERM": "xterm-kitty", "LANG": "fr_FR.UTF-8"}):
            command = session_command("lab", self.path)
            self.assertEqual(command[:4], ["pkexec", "/usr/bin/env", "TERM=xterm-kitty", "LANG=fr_FR.UTF-8"])
            self.assertIn("--enter", command)

    def test_broker_reports_cancellation_with_its_own_code(self):
        store = Mock()
        store.operate.side_effect = OperationCancelled("Clone was cancelled.")
        output = io.StringIO()
        serve(store, io.StringIO(json.dumps({"id": 1, "action": "clone", "params": {}}) + "\n"), output, writable=True)
        result = [json.loads(line) for line in output.getvalue().splitlines()][-1]
        self.assertEqual((result["ok"], result["code"]), (False, "cancelled"))


if __name__ == "__main__":
    unittest.main()
