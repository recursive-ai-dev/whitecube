"""The Wi-Fi live-test script's dead-man's switch, against stub systemctl/apply/ping. Run: python3 -I test-live-test.py"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "tools/wifi-live-test.sh"


class Lab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "calls.log"
        self.status = self.dir / "status.json"
        # What the "service" looks like after each restart: first restart = new controller, second = old one.
        self.plan = self.dir / "plan.json"

    def stub(self, name, body):
        path = self.dir / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def run_script(self, new_works, old_works=True, apply_ok=True, backup_line=True, ping_ok=True):
        states = {"new": new_works, "old": old_works}
        self.plan.write_text(json.dumps(states))
        good = json.dumps({"association_state": "COMPLETED", "ipv4_address": "10.0.0.2", "default_route": "10.0.0.1", "ssid": "x", "signal_dbm": -50})
        bad = json.dumps({"association_state": "SCANNING", "ipv4_address": None, "default_route": None})
        apply = self.stub("apply.sh", f'''echo "apply $*" >> {self.log}
if [[ "$1" == "--apply" ]]; then
  {"echo 'installed 5 file(s). Backup: /var/backups/whitecube/net-TEST'" if backup_line else "echo 'installed 5 file(s).'"}
  exit {0 if apply_ok else 1}
fi
exit 0
''')
        systemctl = self.stub("systemctl", f'''echo "systemctl $*" >> {self.log}
if [[ "$1" == "restart" ]]; then
  n=$(grep -c "^systemctl restart" {self.log})
  python3 -I - "$n" <<'PY'
import json, sys
plan = json.load(open("{self.plan}"))
works = plan["new"] if int(sys.argv[1]) == 1 else plan["old"]
open("{self.status}", "w").write('{good}' if works else '{bad}')
PY
fi
''')
        journalctl = self.stub("journalctl", 'echo "journal line: wifi: handshake did not finish"\n')
        ping = self.stub("ping", f'echo "ping $*" >> {self.log}\nexit {0 if ping_ok else 1}\n')
        env = dict(os.environ, APPLY=str(apply), SYSTEMCTL=str(systemctl), JOURNALCTL=str(journalctl), PING=str(ping),
                   STATUS=str(self.status), TIMEOUT="2", ROLLBACK_TIMEOUT="2", POLL="0.1", LIVE_TEST_NO_ROOT_CHECK="1")
        done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return done, calls


class DeadMansSwitch(Lab):
    def test_success_keeps_the_new_controller_and_does_not_roll_back(self):
        done, calls = self.run_script(new_works=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("SUCCESS", done.stdout)
        self.assertFalse([c for c in calls if "--rollback" in c])
        self.assertEqual([c for c in calls if c.startswith("systemctl restart")].__len__(), 1)
        self.assertIn("apt purge wpasupplicant", done.stdout)               # only suggested, never run

    def test_a_controller_that_never_connects_is_rolled_back_and_the_old_one_restored(self):
        done, calls = self.run_script(new_works=False, old_works=True)
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("ROLLED BACK", done.stdout)
        self.assertIn("apply --root / --rollback /var/backups/whitecube/net-TEST", calls)
        self.assertEqual(len([c for c in calls if c.startswith("systemctl restart")]), 2)   # new, then old
        self.assertIn("journal line", done.stdout)                                          # the failure is shown

    def test_the_order_is_install_then_restart_then_rollback_then_restart(self):
        _, calls = self.run_script(new_works=False)
        kinds = [c.split()[0] + (" " + c.split()[1] if c.startswith("systemctl") else "") for c in calls if not c.startswith("ping")]
        self.assertEqual(kinds, ["apply", "systemctl daemon-reload", "systemctl restart", "apply", "systemctl daemon-reload", "systemctl restart"])

    def test_if_even_the_rollback_does_not_restore_the_network_it_says_how_to_recover(self):
        done, _ = self.run_script(new_works=False, old_works=False)
        self.assertEqual(done.returncode, 4)
        self.assertIn("Manual recovery", done.stderr)
        self.assertIn("net-TEST", done.stderr)

    def test_a_failed_install_restarts_nothing(self):
        done, calls = self.run_script(new_works=True, apply_ok=False)
        self.assertEqual(done.returncode, 1)
        self.assertFalse([c for c in calls if c.startswith("systemctl")])

    def test_an_unparseable_apply_output_restarts_nothing(self):
        done, calls = self.run_script(new_works=True, backup_line=False)
        self.assertEqual(done.returncode, 1)
        self.assertFalse([c for c in calls if c.startswith("systemctl")])

    def test_associated_but_router_unreachable_is_a_failure(self):
        done, _ = self.run_script(new_works=True, old_works=True, ping_ok=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("SUCCESS", done.stdout)

    def test_it_refuses_to_run_without_root(self):
        env = dict(os.environ)
        env.pop("LIVE_TEST_NO_ROOT_CHECK", None)
        if os.geteuid() == 0:
            self.skipTest("running as root")
        done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 2)

    def test_the_script_never_removes_packages_or_stops_other_things(self):
        source = SCRIPT.read_text()
        for forbidden in ("apt-get", "pkill", "rm -rf", "systemctl stop", "systemctl disable"):
            self.assertNotIn(forbidden, source.replace('echo "When you are satisfied', ""), forbidden)


if __name__ == "__main__":
    unittest.main(verbosity=2)
