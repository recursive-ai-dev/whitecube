"""Tests for whitecube-install with fake runners and a temp root. Run: python3 -I test-whitecube-install.py"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("wi", str(HERE / "tools" / "whitecube-install"))
wi = importlib.util.module_from_spec(importlib.util.spec_from_loader("wi", loader))
loader.exec_module(wi)


class Recorder:
    def __init__(self, results=None):
        self.calls, self.results = [], results or {}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        for key, value in self.results.items():
            if key in " ".join(argv):
                return value(argv) if callable(value) else value
        return mock.Mock(returncode=0, stdout="", stderr="")


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.project, self.root, self.home = base / "project", base / "root", base / "home"
        for d in (self.project / "build", self.project / "net", self.root, self.home):
            d.mkdir(parents=True)

    def build(self, newer_source=False):
        for name in wi.BINARIES:
            (self.project / "build" / name).write_text("bin")
        old = time.time() - 1000
        for name in wi.BINARIES:
            os.utime(self.project / "build" / name, (old, old))
        (self.project / "src").mkdir(exist_ok=True); (self.project / "src/whitecube.c").write_text("c")
        os.utime(self.project / "src/whitecube.c", (old - 500 if not newer_source else old + 500,) * 2)
        (self.project / "BUILD.sh").write_text("")
        os.utime(self.project / "BUILD.sh", (old - 500,) * 2)


class BuildFreshness(Tree):
    def test_missing_binaries_need_a_build(self):
        self.assertTrue(wi.sources_newer_than_build(self.project))

    def test_up_to_date_and_stale(self):
        self.build()
        self.assertFalse(wi.sources_newer_than_build(self.project))
        self.build(newer_source=True)
        self.assertTrue(wi.sources_newer_than_build(self.project))


class UserInstall(Tree):
    def test_it_refuses_to_run_as_root(self):
        with mock.patch.object(wi.os, "geteuid", return_value=0), self.assertRaises(SystemExit):
            wi.user_install(self.project, Recorder())

    def test_a_stale_build_is_rebuilt_first_then_installed(self):
        self.build(newer_source=True)
        runner = Recorder()
        with mock.patch.object(wi.os, "geteuid", return_value=1000), contextlib.redirect_stdout(io.StringIO()) as out:
            wi.user_install(self.project, runner)
        self.assertEqual([Path(c[1]).name if c[0] == "bash" else Path(c[2]).name for c in runner.calls], ["BUILD.sh", "INSTALL-USER.py"])
        self.assertIn("--apply", runner.calls[1])
        self.assertIn("sudo", out.getvalue())                    # tells you the system step, never runs sudo itself
        self.assertNotIn("sudo", " ".join(" ".join(c) for c in runner.calls))

    def test_a_failed_build_installs_nothing(self):
        self.build(newer_source=True)
        runner = Recorder({"BUILD.sh": mock.Mock(returncode=1)})
        with mock.patch.object(wi.os, "geteuid", return_value=1000), self.assertRaises(SystemExit):
            wi.user_install(self.project, runner)
        self.assertFalse(any("INSTALL-USER" in " ".join(c) for c in runner.calls))

    def test_a_current_build_is_not_rebuilt(self):
        self.build()
        runner = Recorder()
        with mock.patch.object(wi.os, "geteuid", return_value=1000), contextlib.redirect_stdout(io.StringIO()):
            wi.user_install(self.project, runner)
        self.assertEqual(len(runner.calls), 1)


class UserRollback(Tree):
    def test_files_are_restored_and_new_ones_removed(self):
        existing, created, prior = self.home / "a", self.home / "b", self.project / "prior-a"
        existing.write_text("NEW")
        created.write_text("NEW")
        prior.write_text("OLD")
        state = self.home / ".local/state/whitecube"
        state.mkdir(parents=True)
        (state / "privacy-install.json").write_text(json.dumps({"files": [
            {"path": str(existing), "before": str(prior)}, {"path": str(created), "before": None}]}))
        with mock.patch.object(wi.Path, "home", return_value=self.home), contextlib.redirect_stdout(io.StringIO()):
            wi.rollback_user(self.project)
        self.assertEqual(existing.read_text(), "OLD")
        self.assertFalse(created.exists())

    def test_without_a_record_it_says_so(self):
        with mock.patch.object(wi.Path, "home", return_value=self.home), self.assertRaisesRegex(SystemExit, "no user install record"):
            wi.rollback_user(self.project)

    def test_a_symlinked_destination_is_refused(self):
        link = self.home / "link"
        link.symlink_to(self.home / "elsewhere")
        state = self.home / ".local/state/whitecube"
        state.mkdir(parents=True)
        (state / "privacy-install.json").write_text(json.dumps({"files": [{"path": str(link), "before": None}]}))
        with mock.patch.object(wi.Path, "home", return_value=self.home), self.assertRaisesRegex(SystemExit, "symlink"):
            wi.rollback_user(self.project)


class SystemInstall(Tree):
    def setUp(self):
        super().setUp()
        (self.project / "net/whitecube-network.hardening.conf").write_text("[Service]\nPrivateDevices=yes\n")
        (self.project / "data/sudoers").mkdir(parents=True, exist_ok=True); (self.project / "data/sudoers/whitecube-settings.hardened").write_text("# rule\n")
        patcher = mock.patch.object(wi.os, "chown")
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, runner, extras=(), root_euid=0):
        with mock.patch.object(wi.os, "geteuid", return_value=root_euid), contextlib.redirect_stdout(io.StringIO()) as out:
            wi.system_install(self.project, runner, extras, self.root, "STAMP")
        return out.getvalue()

    def test_it_needs_root(self):
        with self.assertRaisesRegex(SystemExit, "needs root"):
            self.install(Recorder(), root_euid=1000)

    def test_the_base_install_is_net_apply_and_nothing_is_restarted(self):
        runner, text = Recorder(), None
        text = self.install(runner)
        self.assertEqual([c[-1] for c in runner.calls], ["--apply"])
        self.assertFalse(any("restart" in " ".join(c) for c in runner.calls))      # restart commands are only printed
        self.assertIn("systemctl restart whitecube-network.service", text)

    def test_a_failed_apply_stops_before_any_extra(self):
        runner = Recorder({"apply.sh": mock.Mock(returncode=1)})
        with self.assertRaises(SystemExit):
            self.install(runner, ["hardening"])
        self.assertFalse((self.root / "etc/systemd/system/whitecube-network.service.d/hardening.conf").exists())

    def test_the_dropin_is_installed_with_the_right_mode_and_a_new_file_needs_no_backup(self):
        self.install(Recorder(), ["hardening"])
        target = self.root / "etc/systemd/system/whitecube-network.service.d/hardening.conf"
        self.assertEqual(target.read_text(), "[Service]\nPrivateDevices=yes\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertFalse((self.root / "var/backups/whitecube").exists())

    def test_sudoers_is_validated_backed_up_and_installed_0440(self):
        old = self.root / "etc/sudoers.d/whitecube-settings"
        old.parent.mkdir(parents=True)
        old.write_text("# old rule\n")
        runner = Recorder()
        self.install(runner, ["sudoers"])
        self.assertTrue(any(c[:2] == ["visudo", "-c"] for c in runner.calls))
        self.assertEqual(old.read_text(), "# rule\n")
        self.assertEqual(old.stat().st_mode & 0o777, 0o440)
        backups = list((self.root / "var/backups/whitecube/extras-STAMP").iterdir())
        self.assertEqual([b.read_text() for b in backups], ["# old rule\n"])
        self.assertEqual((self.root / "var/backups/whitecube/extras-STAMP").stat().st_mode & 0o777, 0o700)

    def test_a_sudoers_file_that_does_not_parse_is_never_installed(self):
        runner = Recorder({"visudo": mock.Mock(returncode=1)})
        with self.assertRaisesRegex(SystemExit, "does not parse"):
            self.install(runner, ["sudoers"])
        self.assertFalse((self.root / "etc/sudoers.d/whitecube-settings").exists())

    def test_cairo_needs_a_staged_build_and_copies_only_under_usr_local(self):
        with self.assertRaisesRegex(SystemExit, "no staged cairo"):
            self.install(Recorder(), ["cairo"])
        (self.project / "build-cairo/stage/usr/local/lib").mkdir(parents=True)
        runner = Recorder()
        self.install(runner, ["cairo"])
        copy = next(c for c in runner.calls if c[0] == "cp")
        self.assertTrue(copy[-1].startswith(str(self.root / "usr/local")))
        self.assertIn(["ldconfig"], runner.calls)


class Cli(Tree):
    def test_extras_without_system_are_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            wi.main(["--with-sudoers"])

    def test_the_default_is_a_dry_run_that_changes_nothing(self):
        runner = Recorder()
        with mock.patch.object(wi, "run", runner), mock.patch.object(wi, "PROJECT", self.project), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            wi.main([])
        self.assertIn("Dry run", out.getvalue())
        self.assertFalse(any("--apply" in c for c in runner.calls))

    def test_the_system_dry_run_needs_no_root(self):
        runner = Recorder()
        with mock.patch.object(wi, "run", runner), mock.patch.object(wi, "PROJECT", self.project), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            wi.main(["--system"])
        self.assertIn("Dry run", out.getvalue())

    def test_the_script_uses_the_isolated_interpreter_and_never_calls_sudo_or_systemctl_restart_itself(self):
        source = (HERE / "tools" / "whitecube-install").read_text()
        self.assertEqual(source.splitlines()[0], "#!/usr/bin/python3 -I")
        for forbidden in ("'sudo'", '"sudo"', "'systemctl'", "'apt", "'apt-get'", "pkill"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main(verbosity=2)
