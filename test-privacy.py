#!/usr/bin/python3 -I
"""Isolated privacy integration tests. Never reads or changes the real home."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HELPER = Path(__file__).with_name("cube-privacy")
loader = importlib.machinery.SourceFileLoader("cube_privacy", str(HELPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
privacy = importlib.util.module_from_spec(spec)
loader.exec_module(privacy)


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="whitecube-privacy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.config = self.home / ".config"
        self.data = self.home / ".local/share"
        self.cache = self.home / ".cache"
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
               "XDG_DATA_HOME": str(self.data), "XDG_CACHE_HOME": str(self.cache),
               "XDG_STATE_HOME": str(self.home / ".local/state"), "PATH": "/nonexistent"}
        self.environment = mock.patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def firefox(self, legacy=False, two=False):
        root = self.home / ".mozilla/firefox" if legacy else self.config / "mozilla/firefox"
        self.write(root / "profiles.ini",
                   "[Profile0]\nName=main\nIsRelative=1\nPath=main.default\nDefault=1\n"
                   + ("[Profile1]\nName=other\nIsRelative=1\nPath=other.default\nDefault=1\n" if two else ""))
        (root / "main.default").mkdir()
        if two:
            (root / "other.default").mkdir()
        return root / "main.default"

    def records(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            privacy.status()
        records = [line.split("\t") for line in output.getvalue().splitlines()]
        self.assertTrue(all(len(row) == 4 for row in records))
        return {(row[0], row[1]): row[2:] for row in records}

    def test_status_empty_home_has_no_side_effects(self):
        records = self.records()
        self.assertEqual(records["setting", "remember_recent"][0], "unmanaged")
        self.assertEqual(records["setting", "microphone_muted"][0], "unsupported")
        self.assertEqual(records["setting", "firefox_https_only"][0], "unsupported")
        self.assertEqual(records["action", "clipboard"][0], "unsupported")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_gtk_changes_preserve_custom_sections_comments_and_private_permissions(self):
        gtk3, gtk4 = privacy.gtk_paths()
        original = "# custom theme\n[Settings]\ngtk-theme-name=Midnight\n# Keep me\n[Extra]\nhello=world\n"
        self.write(gtk3, original)
        privacy.set_value("remember_recent", False)
        self.assertIn("# custom theme\n", gtk3.read_text())
        self.assertIn("# Keep me\n", gtk3.read_text())
        self.assertEqual(privacy.ini(gtk3.read_text()).get("Extra", "hello"), "world")
        for path in (gtk3, gtk4):
            self.assertEqual(privacy.gtk_value(path.read_text()), "0")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        privacy.set_value("remember_recent", True)
        self.assertEqual(self.records()["setting", "remember_recent"][0], "1")
        self.assertEqual(gtk3.read_text().count("gtk-recent-files-enabled"), 1)

    def test_gtk_mixed_and_malformed_are_honest(self):
        gtk3, gtk4 = privacy.gtk_paths()
        self.write(gtk3, "[Settings]\ngtk-recent-files-enabled=false\n")
        self.assertEqual(self.records()["setting", "remember_recent"][0], "mixed")
        self.write(gtk4, "[Settings]\ngtk-recent-files-enabled=maybe\n")
        self.assertEqual(self.records()["setting", "remember_recent"][0], "error")

    def test_preflight_rejects_malformed_gtk_before_any_write(self):
        gtk3, gtk4 = privacy.gtk_paths()
        original = "[Settings]\ngtk-theme-name=keep\n"
        self.write(gtk3, original)
        self.write(gtk4, "broken INI")
        with self.assertRaises(privacy.configparser.Error):
            privacy.set_value("remember_recent", False)
        self.assertEqual(gtk3.read_text(), original)

    def test_gtk_setting_without_final_newline(self):
        edited = privacy.gtk_edit("[Settings]\ngtk-theme-name=keep", False)
        self.assertEqual(privacy.gtk_value(edited), "0")
        self.assertEqual(privacy.ini(edited).get("Settings", "gtk-theme-name"), "keep")

    def test_multifile_failure_restores_prior_gtk_preferences(self):
        gtk3, gtk4 = privacy.gtk_paths()
        original = "[Settings]\ngtk-theme-name=keep\n"
        self.write(gtk3, original)
        real_replace = privacy.replace

        def fail_second(path, content, previous):
            if path == gtk4:
                raise OSError("simulated disk error")
            real_replace(path, content, previous)

        with mock.patch.object(privacy, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(privacy.PrivacyError, "original preferences restored"):
                privacy.set_value("remember_recent", False)
        self.assertEqual(gtk3.read_text(), original)
        self.assertFalse(gtk4.exists())

    def test_multifile_failure_removes_new_file_and_preserves_outside_edits(self):
        profile = self.firefox(two=True)
        first = profile / "user.js"
        second = profile.parent / "other.default/user.js"
        real_replace = privacy.replace

        def fail_second(path, content, previous):
            if path == second:
                raise OSError("simulated disk error")
            real_replace(path, content, previous)

        with mock.patch.object(privacy, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(privacy.PrivacyError, "original preferences restored"):
                privacy.set_value("firefox_https_only", True)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

        def edit_first_and_fail_second(path, content, previous):
            if path == second:
                first.write_text("// concurrent outside edit\n")
                raise OSError("simulated disk error")
            real_replace(path, content, previous)

        with mock.patch.object(privacy, "replace", side_effect=edit_first_and_fail_second):
            with self.assertRaisesRegex(privacy.PrivacyError, "could not restore 1 file"):
                privacy.set_value("firefox_https_only", True)
        self.assertEqual(first.read_text(), "// concurrent outside edit\n")

    def test_failure_after_atomic_rename_also_rolls_back(self):
        path = self.write(self.home / "prefs", "original")
        real_replace = privacy.replace
        calls = []

        def fail_after_write(path, content, previous):
            real_replace(path, content, previous)
            calls.append(content)
            if len(calls) == 1:
                raise OSError("simulated directory fsync failure")

        with mock.patch.object(privacy, "replace", side_effect=fail_after_write):
            with self.assertRaisesRegex(privacy.PrivacyError, "original preferences restored"):
                privacy.apply_edits([(path, "original", "updated")])
        self.assertEqual(path.read_text(), "original")

    def test_symlink_files_and_ancestors_are_refused(self):
        target = self.write(self.root / "outside.ini", "[Settings]\ngtk-theme-name=outside\n")
        gtk3, _ = privacy.gtk_paths()
        gtk3.parent.mkdir(parents=True)
        gtk3.symlink_to(target)
        with self.assertRaises(OSError):
            privacy.set_value("remember_recent", False)
        self.assertIn("outside", target.read_text())
        gtk3.unlink()
        gtk3.parent.rmdir()
        gtk3.parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            privacy.read(gtk3)

    def test_replace_detects_concurrent_edit_and_leaves_no_temp_files(self):
        path = self.write(self.home / "prefs", "changed")
        with self.assertRaisesRegex(privacy.PrivacyError, "changed during"):
            privacy.replace(path, "new", "old")
        self.assertEqual(path.read_text(), "changed")
        self.assertEqual(list(self.home.iterdir()), [path])

    def test_oversized_and_nonregular_files_refused_without_blocking(self):
        path = self.write(self.home / "large", "x" * (privacy.MAX_FILE + 1))
        with self.assertRaises(privacy.PrivacyError):
            privacy.read(path)
        pipe = self.home / "pipe"
        os.mkfifo(pipe)
        with self.assertRaises(privacy.PrivacyError):
            privacy.read(pipe)

    def test_firefox_preserves_original_and_each_control_round_trips(self):
        profile = self.firefox()
        original = '// custom preferences\nuser_pref("custom.value", "retain");\n'
        self.write(profile / "user.js", original)
        for key in privacy.FIREFOX_KEYS:
            for enabled in (True, False):
                privacy.set_value(key, enabled)
                current = (profile / "user.js").read_text()
                base, controls = privacy.firefox_parse(current)
                self.assertEqual(base, original)
                self.assertIs(controls[key], enabled)
                self.assertEqual(current.count(privacy.BEGIN), 1)
                for preference, value in privacy.firefox_preferences(key, enabled).items():
                    self.assertIn("user_pref(" + privacy.json.dumps(preference) + ", " + privacy.json.dumps(value) + ");", current)
        self.assertEqual(stat.S_IMODE((profile / "user.js").stat().st_mode), 0o600)
        records = self.records()
        for key in privacy.FIREFOX_KEYS:
            self.assertEqual(records["setting", key][0], "0")

    def test_browser_unmanaged_is_not_assumed_disabled(self):
        profile = self.firefox()
        self.write(profile / "user.js", 'user_pref("dom.security.https_only_mode", true);\n')
        self.assertEqual(self.records()["setting", "firefox_https_only"][0], "unmanaged")

    def test_firefox_install_default_wins_over_other_profile_default(self):
        profile = self.firefox(two=True)
        ini_path = profile.parent / "profiles.ini"
        self.write(ini_path, ini_path.read_text() + "[InstallABC]\nDefault=main.default\nLocked=1\n")
        self.assertEqual(privacy.firefox_profiles(), [profile])
        privacy.set_value("firefox_https_only", True)
        self.assertFalse((profile.parent / "other.default/user.js").exists())

    def test_legacy_firefox_and_multiple_profile_mixed_status(self):
        profile = self.firefox(legacy=True, two=True)
        self.write(profile / "user.js", privacy.firefox_block({"firefox_https_only": True}))
        self.assertEqual(self.records()["setting", "firefox_https_only"][0], "mixed")
        privacy.set_value("firefox_https_only", False)
        self.assertEqual(self.records()["scope", "firefox"][0], "2")
        self.assertEqual(self.records()["setting", "firefox_https_only"][0], "0")

    def test_firefox_ambiguous_and_escaping_profiles_rejected(self):
        profile = self.firefox(two=True)
        ini_path = profile.parent / "profiles.ini"
        self.write(ini_path, ini_path.read_text().replace("Default=1\n", ""))
        self.assertEqual(privacy.firefox_profiles(), [])
        with self.assertRaises(privacy.PrivacyError):
            privacy.set_value("firefox_https_only", True)
        self.write(ini_path, "[Profile0]\nIsRelative=1\nPath=../../outside\nDefault=1\n")
        with self.assertRaisesRegex(privacy.PrivacyError, "contains"):
            privacy.firefox_profiles()

    def test_firefox_manually_changed_managed_block_is_not_overwritten(self):
        profile = self.firefox()
        invalid = privacy.firefox_block({"firefox_https_only": True}).replace(
            'user_pref("dom.security.https_only_mode", true);',
            'user_pref("dom.security.https_only_mode", false);')
        self.write(profile / "user.js", invalid)
        with self.assertRaisesRegex(privacy.PrivacyError, "edited"):
            privacy.set_value("firefox_prefetch", False)
        self.assertEqual((profile / "user.js").read_text(), invalid)
        self.assertEqual(self.records()["setting", "firefox_https_only"][0], "error")

    def test_firefox_later_custom_override_reports_error(self):
        profile = self.firefox()
        self.write(profile / "user.js", privacy.firefox_block({"firefox_https_only": True})
                   + 'user_pref("dom.security.https_only_mode", false);\n')
        self.assertEqual(self.records()["setting", "firefox_https_only"][0], "error")

    def test_truncated_firefox_marker_is_refused(self):
        with self.assertRaisesRegex(privacy.PrivacyError, "malformed"):
            privacy.firefox_parse(privacy.BEGIN.rstrip("\n"))

    def test_firefox_commented_out_controls_and_unterminated_strings_are_refused(self):
        block = privacy.firefox_block({"firefox_https_only": True})
        for contents in ("/* open comment\n" + block + "*/", "/* open comment", 'user_pref("unfinished'):
            with self.assertRaisesRegex(privacy.PrivacyError, "unterminated"):
                privacy.firefox_parse(contents)
        custom = '/* keep */\nuser_pref("custom", "https://example.invalid/path");\n// final comment'
        self.assertEqual(privacy.firefox_parse(custom), (custom, {}))

    def test_clear_recent_is_exact_and_refuses_symlink(self):
        recent = self.write(self.data / "recently-used.xbel", "recent document paths")
        document = self.write(self.home / "Documents/important.txt", "keep")
        privacy.clear("recent")
        self.assertFalse(recent.exists())
        self.assertEqual(document.read_text(), "keep")
        recent.symlink_to(document)
        with self.assertRaises(privacy.PrivacyError):
            privacy.clear("recent")
        self.assertEqual(document.read_text(), "keep")

    def test_clear_thumbnails_never_follows_symlinks_or_touches_unrelated_data(self):
        root = self.cache / "thumbnails"
        preview = self.write(root / "normal/abc.png", "preview")
        self.write(root / "fail/generator/failed.png", "failure")
        keep = self.write(root / "unrelated/data", "keep")
        external = self.write(self.home / "Pictures/photo.png", "original")
        (root / "normal/link").symlink_to(external.parent, target_is_directory=True)
        privacy.clear("thumbnails")
        self.assertFalse(preview.exists())
        self.assertFalse((root / "normal/link").is_symlink())
        self.assertEqual(external.read_text(), "original")
        self.assertEqual(keep.read_text(), "keep")

    def test_clear_cache_refuses_symlink_root_and_bounds_work(self):
        outside = self.write(self.home / "Pictures/preview.png", "keep")
        root = self.cache / "thumbnails"
        root.mkdir(parents=True)
        (root / "normal").symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            privacy.clear("thumbnails")
        self.assertEqual(outside.read_text(), "keep")
        (root / "normal").unlink()
        self.write(root / "normal/a", "a")
        self.write(root / "normal/b", "b")
        with mock.patch.object(privacy, "MAX_CACHE_ENTRIES", 1):
            with self.assertRaisesRegex(privacy.PrivacyError, "entries"):
                privacy.clear("thumbnails")

    def test_microphone_fallback_mixed_state_and_verified_change(self):
        which = lambda name: "/mock/" + name
        no_server = subprocess.CompletedProcess([], 2, "", "no server")
        mixed = subprocess.CompletedProcess([], 0, "Front Left: Capture 30 [on]\nFront Right: Capture 30 [off]\n", "")
        muted = subprocess.CompletedProcess([], 0, "Front Left: Capture 30 [off]\nFront Right: Capture 30 [off]\n", "")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(privacy.shutil, "which", side_effect=which):
            with mock.patch.object(privacy, "command", side_effect=[no_server, mixed]):
                self.assertEqual(privacy.microphone()[0], "mixed")
            with mock.patch.object(privacy, "command", side_effect=[no_server, mixed, ok, no_server, muted]) as run:
                privacy.set_value("microphone_muted", True)
                self.assertEqual(run.call_args_list[2].args[0], ["/mock/amixer", "sset", "Capture", "nocap"])

    def test_microphone_failed_or_unverified_change_never_reports_success(self):
        enabled = subprocess.CompletedProcess([], 0, "Volume: 1.00\n", "")
        failed = subprocess.CompletedProcess([], 1, "", "denied")
        with mock.patch.object(privacy.shutil, "which", return_value="/mock/wpctl"):
            with mock.patch.object(privacy, "command", side_effect=[enabled, failed]):
                with self.assertRaisesRegex(privacy.PrivacyError, "failed"):
                    privacy.set_value("microphone_muted", True)
            with mock.patch.object(privacy, "command", side_effect=[enabled, enabled, enabled]):
                with self.assertRaisesRegex(privacy.PrivacyError, "verified"):
                    privacy.set_value("microphone_muted", True)

    def test_clipboard_requires_session_and_reports_server_failure(self):
        with self.assertRaises(privacy.PrivacyError):
            privacy.clear("clipboard")
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-test"}):
            with mock.patch.object(privacy.shutil, "which", return_value="/mock/wl-copy"):
                with mock.patch.object(privacy, "command", return_value=None):
                    with self.assertRaisesRegex(privacy.PrivacyError, "failed"):
                        privacy.clear("clipboard")
                with mock.patch.object(privacy, "command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                    self.assertIn("primary selection are unaffected", privacy.clear("clipboard"))
                    run.assert_called_once_with(["/mock/wl-copy", "--clear"])

    def test_timeout_and_argv_are_safe(self):
        with mock.patch.object(privacy.subprocess, "run", side_effect=subprocess.TimeoutExpired("wpctl", 3)):
            self.assertIsNone(privacy.command(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"]))
        with self.assertRaises(privacy.PrivacyError):
            privacy.set_value("unknown; echo bad", True)
        with self.assertRaises(privacy.PrivacyError):
            privacy.clear("../../Documents")

    def test_lock_prevents_overlapping_edits(self):
        with privacy.change_lock():
            with self.assertRaisesRegex(privacy.PrivacyError, "in progress"):
                with privacy.change_lock():
                    self.fail("lock should be exclusive")

    @unittest.skipIf(os.geteuid() == 0, "CLI intentionally refuses root")
    def test_cli_end_to_end_and_argument_validation(self):
        for args in (["status"], ["set", "remember_recent", "0"], ["status"]):
            result = subprocess.run([sys.executable, "-I", str(HELPER)] + args,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("setting\tremember_recent\t0\t", result.stdout)
        for args in (["set", "remember_recent", "false"], ["clear", "all"], []):
            result = subprocess.run([sys.executable, "-I", str(HELPER)] + args,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
