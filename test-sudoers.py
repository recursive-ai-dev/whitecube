"""Checks the staged hardened sudoers rule against the helper and the UI. Run: python3 -I test-sudoers.py"""
import fnmatch
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
HELPER = "/usr/local/libexec/cube-settings-helper"


def free_patterns(path):
    text = (HERE / path).read_text()
    alias = re.search(r"Cmnd_Alias CUBE_FREE = (.*?)\n%", text, re.S).group(1).replace("\\\n", " ")
    return [entry.strip() for entry in alias.split(",") if entry.strip()]


def allowed_without_password(patterns, *argv):
    line = " ".join([HELPER, *argv])
    return any(fnmatch.fnmatchcase(line, pattern) for pattern in patterns)


class HardenedRule(unittest.TestCase):
    def setUp(self):
        self.free = free_patterns("sudoers-whitecube-settings.hardened")

    def test_the_file_parses_with_the_real_visudo(self):
        if not shutil.which("visudo"):
            self.skipTest("no visudo")
        done = subprocess.run(["visudo", "-c", "-f", str(HERE / "sudoers-whitecube-settings.hardened")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_what_the_top_bar_and_settings_use_stays_passwordless(self):
        for argv in (("services",), ("firewall",), ("net-scan",), ("net-reconnect",), ("net-connect", "0123456789abcdef"),
                     ("brightness", "5"), ("brightness", "55"), ("brightness", "100"),
                     ("power-action", "suspend"), ("power-action", "reboot"), ("power-action", "poweroff")):
            self.assertTrue(allowed_without_password(self.free, *argv), argv)

    def test_saving_and_forgetting_networks_now_need_a_password(self):
        self.assertFalse(allowed_without_password(self.free, "net-add"))
        self.assertFalse(allowed_without_password(self.free, "net-forget", "0123456789abcdef"))

    def test_arguments_are_pinned(self):
        for argv in (("net-connect", "../../etc"), ("net-connect", "0123456789abcde"), ("net-connect", "0123456789abcdefg"),
                     ("net-connect", "0123456789ABCDEF"), ("net-connect",), ("brightness", "101"), ("brightness", "5 5"),
                     ("brightness", "-1"), ("power-action", "hibernate"), ("power-action", "poweroff now"), ("power-action",),
                     ("service", "start", "x"), ("hostname", "x"), ("firewall-set", "disable"), ("services", "extra")):
            self.assertFalse(allowed_without_password(self.free, *argv), argv)

    def test_everything_still_works_with_a_password(self):
        text = (HERE / "sudoers-whitecube-settings.hardened").read_text()
        self.assertIn(f"%sudo ALL=(root) {HELPER}\n", text)

    def test_the_old_rule_is_what_the_hardened_one_tightens(self):
        old = free_patterns("sudoers-whitecube-settings")
        self.assertTrue(allowed_without_password(old, "net-add"))                     # the exposure being removed
        self.assertTrue(allowed_without_password(old, "net-connect", "anything at all"))

    def test_the_ui_asks_for_the_password_for_exactly_those_two_actions(self):
        source = (HERE / "cube-settings.c").read_text()
        self.assertIn('make_pending("Save network", P_PASS, 1, "net-add")', source)
        self.assertIn('make_pending("Forget network", P_PASS, 2, "net-forget"', source)

    def test_every_helper_call_the_bar_makes_is_covered(self):
        bar = (HERE / "cube-status.c").read_text()
        calls = set(re.findall(r'helper\(JOB_\w+, true, "([\w-]+)"', bar))
        self.assertTrue(calls <= {"brightness", "power-action", "net-reconnect", "net-connect"}, calls)
        for call in calls:
            self.assertTrue(any(f"{HELPER} {call}" in pattern for pattern in self.free), call)


if __name__ == "__main__":
    unittest.main(verbosity=2)
