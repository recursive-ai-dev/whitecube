"""Behavioral tests with isolated homes and mocked desktop/system actions."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import wc_suite as wc
import install


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        for key, value in {"HOME": self.home, "BIN": self.home / ".local/bin",
                           "STATE": self.home / ".local/state/wc-suite", "SYS": self.home / "sys",
                           "PROC": self.home / "proc", "CONFIG": self.home / ".config/whitecube/config",
                           "KEYS": self.home / "keys.txt"}.items():
            p = patch.object(wc, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        for redirect in (contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr)):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)
        p = patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-test", "XDG_RUNTIME_DIR": str(self.home)}, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def args(self, name, *argv):
        return wc.parser(name).parse_args(argv)

    def file(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_every_command_help_and_version_has_no_side_effect(self):
        with patch.object(wc, "run", side_effect=AssertionError("executed")), \
             patch.object(wc, "detach", side_effect=AssertionError("launched")), \
             patch.object(wc, "exec_tool", side_effect=AssertionError("exec")):
            for name in wc.COMMANDS:
                for flag in ("--help", "--version"):
                    with self.subTest(name=name, flag=flag), self.assertRaises(SystemExit) as result:
                        wc.main([name, flag])
                    self.assertEqual(result.exception.code, 0)
        self.assertFalse(wc.STATE.exists())

    def test_help_discovers_extra_executable_without_running_it(self):
        path = self.file(wc.BIN / "wc-extra", "#!/bin/sh\n# desc: Extra diagnostic\nexit 99\n")
        path.chmod(0o755)
        wc.help_command(self.args("help"))
        self.assertIn("wc-extra", self.stdout.getvalue())
        self.assertIn("Extra diagnostic", self.stdout.getvalue())
        self.assertTrue(wc.catalog()["wc-extra"]["external"])

    def test_help_command_and_search(self):
        wc.help_command(self.args("help", "wc-vol"))
        self.assertIn("--backend", self.stdout.getvalue())
        self.stdout.seek(0)
        self.stdout.truncate()
        wc.help_command(self.args("help", "--search", "audio"))
        self.assertIn("wc-mic", self.stdout.getvalue())
        self.assertNotIn("wc-off", self.stdout.getvalue())
        self.assertEqual(wc.main(["help", "does-not-exist"]), 2)

    def test_json_inventory(self):
        wc.list_commands(self.args("ls", "--json"))
        inventory = json.loads(self.stdout.getvalue())
        self.assertEqual(len(inventory), len(wc.COMMANDS))
        self.assertEqual(inventory["wc-vol"]["group"], "Audio")

    def test_missing_dependency_and_failed_process(self):
        with patch.object(wc.shutil, "which", return_value=None):
            self.assertEqual(wc.main(["notify", "test"]), 127)
        result = subprocess.CompletedProcess(["fake"], 5, "", "could not connect")
        with patch.object(wc, "executable", return_value="fake"), patch.object(wc.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(wc.Error, "could not connect"):
                wc.run(["fake"], capture=True)

    def test_numeric_validation_before_execution(self):
        with patch.object(wc, "run", side_effect=AssertionError("executed")):
            for name in ("vol", "mic", "bright"):
                for value in ("101", "-1", "99999999999999999999", "1.5", "1;touch /tmp/pwned"):
                    with self.subTest(name=name, value=value), self.assertRaises(SystemExit) as result:
                        wc.main([name, value])
                    self.assertEqual(result.exception.code, 2)

    def test_duration_validation(self):
        for value, expected in (("90", 90), ("90s", 90), ("5m", 300), ("1h30m", 5400), (".5m", None), ("0.5s", .5)):
            if expected is None:
                with self.assertRaises(argparse.ArgumentTypeError):
                    wc.duration(value)
            else:
                self.assertEqual(wc.duration(value), expected)
        for value in ("0", "-1s", "smhd", "1..2m", "nan", "1e9", "366d", "1m;ls"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                wc.duration(value)

    def test_application_arguments_preserve_backward_compatibility(self):
        with patch.object(wc, "detach") as detached:
            self.assertEqual(wc.main(["settings", "--page", "privacy"]), 0)
            self.assertEqual(detached.call_args.args[0][1:], ["--page", "privacy"])
            self.assertEqual(wc.main(["browser", "--", "--help"]), 0)
            self.assertEqual(detached.call_args.args[0][1:], ["--help"])

    def test_editor_with_arguments_without_shell_evaluation(self):
        self.file(wc.CONFIG, "audio=alsa\n")
        with patch.dict(os.environ, {"VISUAL": "", "EDITOR": "editor --wait 'a b'"}), patch.object(wc, "exec_tool") as execute:
            wc.config(self.args("config"))
        self.assertEqual(execute.call_args.args[0], ["editor", "--wait", "a b", str(wc.CONFIG)])

    def test_terminal_uses_whitecube_preferences_for_directory(self):
        directory = self.home / "work space;$(false)"
        directory.mkdir()
        with patch.object(wc.shutil, "which", return_value="/tool"), patch.object(wc, "detach") as detached:
            wc.term(self.args("term", str(directory), "--exec", "printf", "hello world"))
        self.assertEqual(detached.call_args.args[0], ["whitecube-terminal", "-D", str(directory), "--", "printf", "hello world"])

    def test_project_filters_directories_and_handles_ambiguous_names(self):
        root = self.home / "Projects"
        (root / "alpha space").mkdir(parents=True)
        (root / "alphabet").mkdir()
        self.file(root / "alpha.txt", "file")
        with patch.object(wc, "detach") as detached:
            wc.project(self.args("project", "alpha space", "--root", str(root)))
            self.assertIn(str(root / "alpha space"), detached.call_args.args[0])
            with self.assertRaisesRegex(wc.Error, "unique"):
                wc.project(self.args("project", "alpha", "--root", str(root)))
        wc.project(self.args("project", "--root", str(root), "--list"))
        self.assertNotIn("alpha.txt", self.stdout.getvalue())

    def test_project_picker_handles_newline_names(self):
        root = self.home / "Projects"
        (root / "two\nlines").mkdir(parents=True)
        label = '1: "two\\nlines"'
        result = subprocess.CompletedProcess([], 0, label + "\n", "")
        with patch.object(wc, "run", return_value=result) as run, patch.object(wc, "detach") as detached:
            wc.project(self.args("project", "--root", str(root)))
        self.assertEqual(run.call_args.kwargs["input"], label + "\n")
        self.assertIn(str(root / "two\nlines"), detached.call_args.args[0])

    def test_brightness_clamps_and_honors_device(self):
        path = wc.SYS / "class/backlight/panel"
        self.file(path / "max_brightness", "1000")
        self.file(path / "brightness", "500")
        with patch.object(wc.shutil, "which", return_value=None):
            wc.bright(self.args("bright", "0", "--device", "panel"))
            self.assertEqual((path / "brightness").read_text().strip(), "50")
            wc.bright(self.args("bright", "up", "--step", "100"))
            self.assertEqual((path / "brightness").read_text().strip(), "1000")
        with self.assertRaises(wc.Error):
            wc.bright(self.args("bright", "--device", "missing"))

    def test_brightness_never_writes_hardware_zero(self):
        path = wc.SYS / "class/backlight/panel"
        self.file(path / "max_brightness", "1")
        self.file(path / "brightness", "1")
        with patch.object(wc.shutil, "which", return_value=None):
            wc.bright(self.args("bright", "0"))
        self.assertEqual((path / "brightness").read_text().strip(), "1")

    def test_brightness_rejects_zero_maximum(self):
        path = wc.SYS / "class/backlight/panel"
        self.file(path / "max_brightness", "0")
        with self.assertRaisesRegex(wc.Error, "maximum"):
            wc.bright(self.args("bright"))

    def test_audio_auto_probe_falls_back_only_before_writes(self):
        result = subprocess.CompletedProcess([], 1, "", "no server")
        with patch.object(wc.shutil, "which", return_value="/tool"), patch.object(wc, "run", return_value=result):
            self.assertEqual(wc.audio_backend("auto"), "alsa")
        with patch.object(wc, "audio_backend", return_value="pipewire"), patch.object(wc, "run", side_effect=wc.Error("write failed")) as run:
            with self.assertRaises(wc.Error):
                wc.volume(self.args("vol", "up"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "wpctl")

    def test_pipewire_volume_limits_and_explicit_mute(self):
        with patch.object(wc, "audio_backend", return_value="pipewire"), patch.object(wc, "run") as run:
            wc.volume(self.args("vol", "up", "--step", "7"))
            self.assertEqual(run.call_args_list[0].args[0], ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "7%+"])
            run.reset_mock()
            wc.volume(self.args("mic", "off"))
            self.assertEqual(run.call_args_list[0].args[0], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"])

    def test_alsa_microphone_uses_capture_switches(self):
        for requested, setting in (("off", "nocap"), ("on", "cap")):
            with patch.object(wc, "audio_backend", return_value="alsa"), patch.object(wc, "alsa_control", return_value=(["amixer"], "Capture,0")), patch.object(wc, "run") as run:
                wc.volume(self.args("mic", requested))
                self.assertEqual(run.call_args_list[0].args[0], ["amixer", "-q", "sset", "Capture,0", setting])

    def test_alsa_explicit_selector_chooses_alsa(self):
        with patch.object(wc, "audio_backend", return_value="alsa") as backend, patch.object(wc, "alsa_control", return_value=(["amixer"], "Master")), patch.object(wc, "run"):
            wc.volume(self.args("vol", "--card", "2"))
        backend.assert_called_once_with("alsa")

    def test_power_dry_run_never_executes(self):
        with patch.object(wc, "run", side_effect=AssertionError("executed")), patch.object(wc, "lock", side_effect=AssertionError("locked")):
            for name in ("off", "reboot", "logout", "suspend"):
                self.assertEqual(wc.main([name, "--dry-run"]), 0)
        self.assertNotIn(" -f", self.stdout.getvalue().split("sudo systemctl poweroff")[0])

    def test_power_requires_noninteractive_confirmation(self):
        with patch.object(wc.sys.stdin, "isatty", return_value=False), patch.object(wc, "run") as run:
            self.assertEqual(wc.main(["off"]), 2)
            run.assert_not_called()
            self.assertEqual(wc.main(["off", "--yes"]), 0)
            run.assert_called_once_with(["sudo", "systemctl", "poweroff"], timeout=None)

    def test_suspend_aborts_when_lock_fails(self):
        with patch.object(wc, "executable", return_value="tool"), patch.object(wc, "lock", side_effect=wc.Error("lock failed")), patch.object(wc, "run") as run:
            self.assertEqual(wc.main(["suspend"]), 1)
            run.assert_not_called()

    def test_suspend_locks_before_power_operation(self):
        calls = []
        with patch.object(wc, "executable", return_value="tool"), patch.object(wc, "lock", side_effect=lambda a: calls.append("lock")), patch.object(wc, "run", side_effect=lambda *a, **kw: calls.append("suspend")):
            wc.suspend(self.args("suspend"))
        self.assertEqual(calls, ["lock", "suspend"])

    def test_logout_targets_current_uid(self):
        with patch.object(wc, "run") as run:
            wc.logout(self.args("logout", "-y"))
        self.assertEqual(run.call_args.args[0], ["pkill", "-u", str(os.getuid()), "-x", "whitecube"])

    def test_screenshot_does_not_overwrite(self):
        path = self.file(self.home / "image.png", "original")
        with patch.object(wc, "executable", return_value="tool"), patch.object(wc, "run") as run:
            with self.assertRaises(FileExistsError):
                wc.shot(self.args("shot", str(path)))
        run.assert_not_called()
        self.assertEqual(path.read_text(), "original")

    def test_screenshot_removes_failed_partial_file(self):
        path = self.home / "image.png"
        with patch.object(wc, "executable", return_value="tool"), patch.object(wc, "run", side_effect=wc.Error("failed")):
            with self.assertRaises(wc.Error):
                wc.shot(self.args("shot", str(path)))
        self.assertFalse(path.exists())

    def test_batteries_and_temperatures_include_nonstandard_device_names(self):
        power = wc.SYS / "class/power_supply/internal"
        for key, value in (("type", "Battery"), ("capacity", "67"), ("status", "Discharging")):
            self.file(power / key, value)
        sensor = wc.SYS / "class/thermal/thermal_zone0"
        self.file(sensor / "type", "cpu")
        self.file(sensor / "temp", "46500")
        self.assertEqual(wc.battery_data()[0]["device"], "internal")
        self.assertEqual(list(wc.temperature_data().values()), [46.5])

    def test_network_report_does_not_ping_by_default(self):
        with patch.object(wc, "run") as run:
            wc.net(self.args("net"))
        self.assertEqual([c.args[0][0] for c in run.call_args_list], ["ip", "ip", "ip"])

    def test_display_json_does_not_invoke_wlr_randr(self):
        path = wc.SYS / "class/drm/card0-eDP-1"
        self.file(path / "status", "connected")
        self.file(path / "modes", "1920x1080\n")
        with patch.object(wc.shutil, "which", return_value="/tool"), patch.object(wc, "run", side_effect=AssertionError("executed")):
            wc.display(self.args("display", "--json"))
        self.assertEqual(json.loads(self.stdout.getvalue())["card0-eDP-1"]["status"], "connected")

    def test_headless_launch_fails_before_spawn(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "", "DISPLAY": ""}), patch.object(wc.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(wc.Error, "display"):
                wc.detach(["tool"], "test")
        spawn.assert_not_called()

    def test_detached_early_failure_reports_private_log(self):
        process = Mock()
        process.wait.return_value = 3
        with patch.object(wc, "executable", return_value="/tool"), patch.object(wc.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(wc.Error, "status 3"):
                wc.detach(["tool"], "test")
        log = wc.STATE / "logs/test.log"
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_state_logs_reject_symlinks(self):
        wc.private_dir(wc.STATE / "logs")
        target = self.file(self.home / "target", "keep")
        (wc.STATE / "logs/test.log").symlink_to(target)
        with self.assertRaises(OSError):
            wc.private_file(wc.STATE / "logs/test.log")
        self.assertEqual(target.read_text(), "keep")

    def test_timer_cancel_never_sends_signal_to_recorded_pid(self):
        timer_id = "123456abcdef"
        path = wc.timer_path(timer_id)
        wc.write_json(path, dict(id=timer_id, status="pending", pid=os.getpid(), due=10, message="test"))
        with patch.object(os, "kill", side_effect=AssertionError("signalled")):
            wc.timer(self.args("timer", "--cancel", timer_id))
        self.assertEqual(json.loads(path.read_text())["status"], "cancelled")

    def test_timer_invalid_id_and_management_arguments(self):
        for value in ("../../etc/passwd", "all", "1234", "abcdefABCDEF"):
            with self.assertRaises(wc.Error):
                wc.timer_path(value)
        self.assertEqual(wc.main(["timer", "--list", "5m"]), 2)
        self.assertEqual(wc.main(["timer", "--json", "5m"]), 2)

    def test_timer_worker_records_delivery_failure(self):
        timer_id = "123456abcdef"
        path = wc.timer_path(timer_id)
        wc.write_json(path, dict(id=timer_id, status="pending", due=0, message="hello"))
        with patch.object(wc, "run", side_effect=wc.Error("notification failed")):
            self.assertEqual(wc.timer_worker(timer_id), 1)
        data = json.loads(path.read_text())
        self.assertEqual(data["status"], "failed")
        self.assertIn("notification failed", data["error"])

    def test_timer_worker_exits_on_cancel_without_notification(self):
        timer_id = "123456abcdef"
        path = wc.timer_path(timer_id)
        wc.write_json(path, dict(id=timer_id, status="cancelled", due=100000000000, message="hello"))
        with patch.object(wc, "run", side_effect=AssertionError("notified")):
            self.assertEqual(wc.timer_worker(timer_id), 0)

    def test_timer_marks_missing_worker_interrupted(self):
        path = wc.timer_path("123456abcdef")
        wc.write_json(path, dict(id="123456abcdef", status="pending", pid=999999999, pid_start="1", boot_id="old", due=0, message="hello"))
        wc.timer(self.args("timer", "--list", "--json"))
        self.assertEqual(json.loads(self.stdout.getvalue())[0]["status"], "interrupted")

    def test_timer_creation_spawns_detached_and_records_identity(self):
        process = Mock(pid=123456)
        with patch.object(wc, "executable", return_value="tool"), patch.object(wc.subprocess, "Popen", return_value=process) as spawn, patch.object(wc, "process_start", return_value="123"):
            wc.timer(self.args("timer", "5m", "take", "a break"))
        record = json.loads(next((wc.STATE / "timers").glob("*.json")).read_text())
        self.assertEqual(record["message"], "take a break")
        self.assertEqual(record["pid_start"], "123")
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertAlmostEqual(record["due"] - record["created"], 300)

    def test_completed_timer_not_cancellable(self):
        timer_id = "123456abcdef"
        wc.write_json(wc.timer_path(timer_id), dict(id=timer_id, status="done", due=0, message="hello"))
        with self.assertRaisesRegex(wc.Error, "already done"):
            wc.timer(self.args("timer", "--cancel", timer_id))

    def test_bash_completion_valid_and_contains_all_commands(self):
        data = install.completion_data("bash")
        for name in wc.COMMANDS:
            self.assertIn(f"wc-{name}".encode(), data)
        result = subprocess.run(["bash", "-n"], input=data, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_malformed_timer_record_is_an_operational_error(self):
        path = wc.timer_path("123456abcdef")
        self.file(path, '{"id": "123456abcdef", "status": "pending"}')
        self.assertEqual(wc.main(["timer", "--list"]), 1)
        self.assertIn("unreadable timer record", self.stderr.getvalue())
        self.assertNotIn("Traceback", self.stderr.getvalue())

    def test_prune_preserves_pending_timers(self):
        pending = wc.timer_path("123456abcdef")
        done = wc.timer_path("abcdef123456")
        wc.write_json(pending, dict(id=pending.stem, status="pending", due=1, message="hello"))
        wc.write_json(done, dict(id=done.stem, status="done", due=1, message="hello"))
        with patch.object(wc, "timer_alive", return_value=True):
            wc.timer(self.args("timer", "--prune"))
        self.assertTrue(pending.exists())
        self.assertFalse(done.exists())

    def test_clipboard_copy_failure_preserves_captured_image(self):
        path = self.home / "image.png"
        def fake_run(argv, **kwargs):
            if argv[0] == "grim":
                path.write_bytes(b"image")
            else:
                raise wc.Error("clipboard failed")
        with patch.object(wc, "executable", return_value="tool"), patch.object(wc, "run", side_effect=fake_run):
            with self.assertRaises(wc.Error):
                wc.shot(self.args("shot", str(path), "--copy"))
        self.assertEqual(path.read_bytes(), b"image")

    def test_shell_completions_ignore_unsafe_external_filenames(self):
        path = self.file(wc.BIN / "wc-x);echo-injected", "#!/bin/sh\n")
        path.chmod(0o755)
        data = install.completion_data("bash")
        self.assertNotIn(b"echo-injected", data)
        result = subprocess.run(["bash", "-n"], input=data, capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_real_timer_worker_with_fake_notification_program(self):
        import time
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.home / ".local/state")}):
            install.install(self.home)
        notification_log = self.home / "notifications.jsonl"
        fake = self.file(wc.BIN / "notify-send", "#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\nwith Path(os.environ['WC_TEST_NOTIFICATION_LOG']).open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\nsys.exit(int(os.environ.get('WC_TEST_NOTIFY_EXIT', '0')))\n")
        fake.chmod(0o755)
        env = dict(os.environ, HOME=str(self.home), XDG_STATE_HOME=str(self.home / ".local/state"),
                   WC_TEST_NOTIFICATION_LOG=str(notification_log))
        def invoke(*argv):
            return subprocess.run([str(wc.BIN / "wc-timer"), *argv], env=env, capture_output=True, text=True, timeout=5)
        def wait_for(path, status):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                data = json.loads(path.read_text())
                if data["status"] == status:
                    return data
                time.sleep(.05)
            self.fail(f"timer did not reach {status}: {data}")
        result = invoke("0.05s", "literal $(false); text")
        self.assertEqual(result.returncode, 0, result.stderr)
        first = next((wc.STATE / "timers").glob("*.json"))
        wait_for(first, "done")
        delivered = json.loads(notification_log.read_text().splitlines()[0])
        self.assertEqual(delivered, ["--", "Timer done", "literal $(false); text"])
        result = invoke("2s", "cancel this")
        self.assertEqual(result.returncode, 0, result.stderr)
        second = next(p for p in (wc.STATE / "timers").glob("*.json") if p != first)
        self.assertEqual(invoke("--cancel", second.stem).returncode, 0)
        wait_for(second, "cancelled")
        time.sleep(1.1)  # Cancellation polling wakes within one second.
        self.assertEqual(len(notification_log.read_text().splitlines()), 1)
        env["WC_TEST_NOTIFY_EXIT"] = "1"
        result = invoke("0.05s", "delivery fails")
        self.assertEqual(result.returncode, 0, result.stderr)
        third = next(p for p in (wc.STATE / "timers").glob("*.json") if p not in (first, second))
        failure = wait_for(third, "failed")
        self.assertIn("exit 1", failure["error"])

    def test_install_and_restore_originals_in_isolated_home(self):
        original = self.file(wc.BIN / "wc-help", "#!/bin/sh\necho original\n")
        original.chmod(0o700)
        extra = self.file(wc.BIN / "wc-custom", "do not change")
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.home / ".local/state")}):
            backup = install.install(self.home)
        wrapper = wc.BIN / "wc-help"
        self.assertIn("wc_suite.py", wrapper.read_text())
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)
        # Installed wrappers resolve relative to their own directory, independent of cwd.
        result = subprocess.run([str(wrapper), "vol"], cwd="/", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--backend", result.stdout)
        install.restore(backup, self.home)
        self.assertEqual(original.read_text(), "#!/bin/sh\necho original\n")
        self.assertEqual(extra.read_text(), "do not change")
        self.assertFalse((wc.BIN / "wc-mic").exists())

    def test_rollback_preserves_later_user_edits_before_any_change(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.home / ".local/state")}):
            backup = install.install(self.home)
        edited = wc.BIN / "wc-vol"
        edited.write_text("my edit")
        original_help = (wc.BIN / "wc-help").read_text()
        with self.assertRaisesRegex(ValueError, "file changed"):
            install.restore(backup, self.home)
        self.assertEqual(edited.read_text(), "my edit")
        self.assertEqual((wc.BIN / "wc-help").read_text(), original_help)

    def test_install_restores_original_symlink(self):
        target = self.file(self.home / "original-script", "#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
        wc.BIN.mkdir(parents=True)
        (wc.BIN / "wc-vol").symlink_to(target)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.home / ".local/state")}):
            backup = install.install(self.home)
        self.assertFalse((wc.BIN / "wc-vol").is_symlink())
        install.restore(backup, self.home)
        self.assertTrue((wc.BIN / "wc-vol").is_symlink())
        self.assertEqual(os.readlink(wc.BIN / "wc-vol"), str(target))

    def test_partial_install_failure_restores_already_changed_files(self):
        original = self.file(wc.BIN / "wc-help", "original")
        original.chmod(0o755)
        real_write = install.write
        def failing_write(path, data, mode):
            if path == wc.BIN / "wc-vol":
                raise OSError("simulated install failure")
            real_write(path, data, mode)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.home / ".local/state")}), patch.object(install, "write", side_effect=failing_write):
            with self.assertRaisesRegex(OSError, "simulated"):
                install.install(self.home)
        self.assertEqual(original.read_text(), "original")
        self.assertFalse((wc.BIN / "wc-mic").exists())
        self.assertFalse((self.home / ".local/lib/wc-suite/wc_suite.py").exists())


if __name__ == "__main__":
    unittest.main()
