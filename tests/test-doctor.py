"""Tests for whitecube-doctor against fake filesystem roots. Run: python3 -I test-doctor.py"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("doctor", str(HERE / "bin" / "whitecube-doctor"))
doctor = importlib.util.module_from_spec(importlib.util.spec_from_loader("doctor", loader))
loader.exec_module(doctor)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root, self.home, self.project = base / "root", base / "home", base / "project"
        for d in (self.root, self.home, self.project):
            d.mkdir()
        self.env = doctor.Env(self.project, self.root, self.home)

    def put(self, path, text, base=None):
        target = (base or self.root) / str(path).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def levels(self, findings):
        return {f["name"]: f["level"] for f in findings}


class FirewallTests(Fixture):
    def test_enabled_with_dropped_output_is_a_failure_with_the_remedy(self):
        self.put("etc/default/ufw", 'DEFAULT_OUTPUT_POLICY="DROP"\n')
        self.put("etc/ufw/ufw.conf", "ENABLED=yes\n")
        found = doctor.check_firewall(self.env)[0]
        self.assertEqual(found["level"], "FAIL")
        self.assertIn("ufw default allow outgoing", found["fix"])

    def test_disabled_with_dropped_output_is_a_warning_because_enabling_would_cut_the_network(self):
        self.put("etc/default/ufw", 'DEFAULT_OUTPUT_POLICY="REJECT"\n')
        self.put("etc/ufw/ufw.conf", "ENABLED=no\n")
        self.assertEqual(doctor.check_firewall(self.env)[0]["level"], "WARN")

    def test_allowed_output_is_fine_and_missing_ufw_is_only_informational(self):
        self.put("etc/default/ufw", 'DEFAULT_OUTPUT_POLICY="ACCEPT"\n')
        self.put("etc/ufw/ufw.conf", "ENABLED=yes\n")
        self.assertEqual(doctor.check_firewall(self.env)[0]["level"], "ok")
        (self.root / "etc/default/ufw").unlink()
        self.assertEqual(doctor.check_firewall(self.env)[0]["level"], "info")


class NetworkTests(Fixture):
    def net(self):
        return mock.Mock(status=mock.Mock(return_value={"diagnostic_code": "online"}))

    def status(self, **kw):
        data = {"association_state": "COMPLETED", "signal_dbm": -55, "ipv4_address": "10.0.0.2", "dns_ok": True,
                "connectivity_ok": True, "updated_epoch": time.time(), **kw}
        self.put("run/whitecube-net/status.json", json.dumps(data))

    def test_a_healthy_link(self):
        self.status()
        found = doctor.check_network(self.env, self.net())[0]
        self.assertEqual(found["level"], "ok")
        self.assertIn("online", found["detail"])

    def test_no_status_file_means_the_service_is_not_running(self):
        self.assertEqual(doctor.check_network(self.env, self.net())[0]["level"], "WARN")

    def test_a_stale_status_file_is_a_failure(self):
        self.status(updated_epoch=time.time() - 300)
        self.assertEqual(doctor.check_network(self.env, self.net())[0]["level"], "FAIL")

    def test_connected_but_no_internet_is_a_warning(self):
        self.status(connectivity_ok=False)
        self.assertEqual(doctor.check_network(self.env, self.net())[0]["level"], "WARN")

    def test_not_associated_is_a_warning(self):
        self.status(association_state="SCANNING")
        self.assertEqual(doctor.check_network(self.env, self.net())[0]["level"], "WARN")

    def test_a_crashing_status_call_does_not_hide_the_link_state(self):
        self.status()
        broken = mock.Mock(status=mock.Mock(side_effect=ValueError))
        self.assertEqual(doctor.check_network(self.env, broken)[0]["level"], "ok")

    def test_without_cube_net_it_says_so(self):
        self.assertEqual(doctor.check_network(self.env, None)[0]["level"], "WARN")


class OtherChecks(Fixture):
    def test_rival_managers_are_named_and_dhcpcd_is_not_a_rival(self):
        net = mock.Mock(competing_processes=mock.Mock(return_value=["dhcpcd", "NetworkManager"]))
        found = doctor.check_competitors(self.env, net)[0]
        self.assertEqual(found["level"], "WARN")
        self.assertIn("NetworkManager", found["detail"])
        self.assertNotIn("dhcpcd", found["detail"])
        net.competing_processes.return_value = ["dhcpcd"]
        self.assertEqual(doctor.check_competitors(self.env, net)[0]["level"], "ok")

    def test_older_copies_are_reported_only_once_the_package_is_installed(self):
        self.put('/usr/local/bin/cube-status', 'old')
        self.assertEqual(doctor.check_shadowing(self.env), [])
        self.put('/usr/bin/whitecube-compositor', 'new')
        found = doctor.check_shadowing(self.env)[0]
        self.assertEqual(found['level'], 'WARN')
        self.assertIn('whitecube-migrate', found['fix'])
        (self.root / 'usr/local/bin/cube-status').unlink()
        self.assertEqual(doctor.check_shadowing(self.env)[0]['level'], 'ok')

    def test_leftover_wpa_supplicant_is_advice_not_an_alarm(self):
        self.assertEqual(doctor.check_wpa_leftovers(self.env)[0]["level"], "ok")
        self.put("usr/sbin/wpa_supplicant", "")
        found = doctor.check_wpa_leftovers(self.env)[0]
        self.assertEqual(found["level"], "info")
        self.assertIn("apt purge wpasupplicant", found["fix"])

    def test_drift_reports_stale_and_missing_installed_copies_and_ignores_unbuilt_sources(self):
        self.put("net/cube-net", "new", self.project)
        self.put("usr/bin/cube-net", "old")
        self.put("net/cube-net-dns", "same", self.project)
        self.put("usr/libexec/whitecube/cube-net-dns", "same")
        self.put("data/foot.ini", "x", self.project)                 # source present, nothing installed
        found = doctor.check_drift(self.env)[0]
        self.assertEqual(found["level"], "WARN")
        self.assertIn("differs from source: cube-net", found["detail"])
        self.assertIn("not installed: foot.ini", found["detail"])
        self.assertNotIn("cube-net-dns", found["detail"])
        self.assertNotIn("whitecube,", found["detail"])         # build/ outputs do not exist, so they are not compared

    def test_matching_installs_are_ok(self):
        self.put("net/cube-net", "same", self.project)
        self.put("usr/bin/cube-net", "same")
        self.assertEqual(doctor.check_drift(self.env)[0]["level"], "ok")

    def test_the_drift_check_never_installs_anything(self):
        self.put("net/cube-net", "new", self.project)
        doctor.check_drift(self.env)
        self.assertFalse((self.root / "usr/bin/cube-net").exists())

    def test_group_wide_passwordless_sudo_is_pointed_out(self):
        self.put("data/sudoers/whitecube-settings", "%sudo ALL=(root) NOPASSWD: CUBE_FREE\n", self.project)
        self.assertEqual(doctor.check_sudoers(self.env)[0]["level"], "info")
        self.put("data/sudoers/whitecube-settings", "%sudo ALL=(root) /usr/local/libexec/x\n", self.project)
        self.assertEqual(doctor.check_sudoers(self.env)[0]["level"], "ok")

    def test_x11_libraries_in_installed_binaries_are_flagged_with_the_fix(self):
        for name in ("whitecube", "cube-status"):
            self.put(f".local/bin/{name}", "", self.home)
        for text, level in (("libxcb.so.1 => /lib/libxcb.so.1\n", "WARN"), ("libwayland-client.so.0 => /lib/x\n", "ok")):
            with mock.patch.object(doctor.subprocess, "run", return_value=mock.Mock(stdout=text)):
                found = doctor.check_xfree(self.env)[0]
            self.assertEqual(found["level"], level)
        with mock.patch.object(doctor.subprocess, "run", return_value=mock.Mock(stdout="libX11.so.6 => /lib/x\n")):
            self.assertIn("build-xfree-cairo.sh", doctor.check_xfree(self.env)[0]["fix"])

    def test_no_installed_binaries_means_nothing_to_flag(self):
        self.assertEqual(doctor.check_xfree(self.env)[0]["level"], "ok")

    def test_x11_in_the_environment_is_flagged(self):
        with mock.patch.dict(doctor.os.environ, {"DISPLAY": ":0"}):
            self.assertEqual(doctor.check_session(self.env)[0]["level"], "WARN")


class RunTests(Fixture):
    def test_one_crashing_check_does_not_hide_the_others(self):
        with mock.patch.object(doctor, "check_firewall", side_effect=RuntimeError("boom")):
            results = doctor.run_checks(self.env)
        names = self.levels(results)
        self.assertEqual(names["doctor"], "WARN")
        self.assertIn("installed copies", names)

    def test_exit_status_and_json(self):
        self.put("etc/default/ufw", 'DEFAULT_OUTPUT_POLICY="DROP"\n')
        self.put("etc/ufw/ufw.conf", "ENABLED=yes\n")
        with mock.patch.object(doctor.Path, "home", return_value=self.home), \
                mock.patch.object(doctor, "Env", lambda project: self.env), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = doctor.main(["--json", "--project", str(self.project)])
        self.assertEqual(code, 1)
        self.assertTrue(any(f["level"] == "FAIL" for f in json.loads(out.getvalue())))

    def test_the_command_is_read_only_and_uses_the_isolated_interpreter(self):
        source = (HERE / "bin" / "whitecube-doctor").read_text()
        self.assertEqual(source.splitlines()[0], "#!/usr/bin/python3 -I")
        for forbidden in ("write_text", "unlink(", "rmtree", "os.remove", ".chmod(", "shutil.copy", "sudo systemctl restart"):
            self.assertNotIn(forbidden, source.split("def main")[0].replace("'sudo systemctl status", ""), forbidden)


if __name__ == "__main__":
    unittest.main(verbosity=2)
