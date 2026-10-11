"""Offline regression tests. No root, nftables access, sockets, or live state writes."""
import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("net_switch", str(HERE / "whitecube-net-switch"))
spec = importlib.util.spec_from_loader(loader.name, loader)
net = importlib.util.module_from_spec(spec)
loader.exec_module(net)


def table(loopback=True, handle=37):
    items = [{"table": {"family": "inet", "name": net.TABLE, "comment": net.OWNER, "handle": handle}}]
    for name in ("input", "output", "forward"):
        items.append({"chain": {"family": "inet", "table": net.TABLE, "name": name,
                                "type": "filter", "hook": name, "prio": -300, "policy": "drop"}})
        if loopback and name in ("input", "output"):
            key = "iifname" if name == "input" else "oifname"
            items.append({"rule": {"chain": name, "expr": [{"match": {"op": "==", "left": {"meta": {"key": key}}, "right": "lo"}}, {"accept": None}]}})
        items.append({"rule": {"chain": name, "expr": [{"counter": {"packets": 6, "bytes": 120}}, {"drop": None}]}})
    return {"nftables": items}


class FakeNft:
    def __init__(self, before=None):
        self.table = copy.deepcopy(before)
        self.foreign = "UFW and administrator firewall stay byte-for-byte unchanged"
        self.calls = []
        self.reject_check = False
        self.reject_apply = False
        self.corrupt_once = False
        self.fail_snapshot_after_apply = False
        self.fail_restore = False
        self.next_handle = 38

    def snapshot(self):
        if self.fail_snapshot_after_apply and any(not check for _, check in self.calls):
            self.fail_snapshot_after_apply = False
            raise net.SwitchError("read after commit failed")
        if self.table is not None:
            net.inspect_table(self.table)
        return copy.deepcopy(self.table)

    def native_snapshot(self):
        return net.render_rules(net.inspect_table(self.table)).removeprefix("create ")

    def apply(self, script, check=False):
        self.calls.append((script, check))
        if check:
            if self.reject_check:
                raise net.NftRejected("preflight rejected")
            return
        if self.reject_apply:
            raise net.NftRejected("transaction rejected")
        if self.fail_restore and len([c for c in self.calls if not c[1]]) > 1:
            raise net.SwitchError("rollback blocked")
        if "delete table" in script:
            if self.table is None or f"handle {self.table['nftables'][0]['table']['handle']}\n" not in script:
                raise net.NftRejected("table generation changed")
            self.table = None
        if "create table" in script:
            if self.table is not None:
                raise net.NftRejected("table already exists")
            self.table = table('iifname "lo"' in script, self.next_handle)
            self.next_handle += 1
        if self.corrupt_once and self.table is not None:
            self.corrupt_once = False
            self.table["nftables"][1]["chain"]["policy"] = "accept"


class MemoryState:
    def __init__(self, value=None):
        self.value = value
        self.writes = []
        self.fail_once = False

    def read(self):
        return self.value

    def write(self, value):
        self.writes.append(value)
        if self.fail_once:
            self.fail_once = False
            raise OSError("state disk full")
        self.value = value


class SwitchTests(unittest.TestCase):
    def switch(self, before=None, persistent=None):
        return net.Switch(FakeNft(before), MemoryState(persistent))

    def test_off_and_on_preserve_foreign_state(self):
        switch = self.switch()
        foreign = switch.nft.foreign
        self.assertIn("OFF", switch.change(True))
        self.assertTrue(net.inspect_table(switch.nft.table))
        self.assertIn("ON", switch.change(False))
        self.assertIsNone(switch.nft.table)
        self.assertEqual(switch.nft.foreign, foreign)
        self.assertTrue(all("flush ruleset" not in script for script, _ in switch.nft.calls))

    def test_off_idempotent_with_counter_values(self):
        switch = self.switch(table())
        switch.change(True)
        self.assertEqual(switch.nft.calls, [])
        self.assertEqual(switch.state.writes, [])

    def test_on_idempotent(self):
        switch = self.switch()
        switch.change(False)
        self.assertEqual(switch.nft.calls, [])
        self.assertEqual(switch.state.writes, [])

    def test_absolute_lockdown_and_reenable_loopback(self):
        switch = self.switch()
        switch.change(True, allow_loopback=False)
        self.assertIs(net.inspect_table(switch.nft.table), False)
        switch.change(True, allow_loopback=True)
        self.assertIs(net.inspect_table(switch.nft.table), True)

    def test_persistence_is_explicit_and_on_clears_it(self):
        switch = self.switch()
        switch.change(True)
        self.assertIsNone(switch.state.value)
        switch.change(True, persist=True)
        self.assertTrue(net.persistent_setting(switch.state.value))
        switch.change(True, allow_loopback=False)
        self.assertFalse(net.persistent_setting(switch.state.value))
        switch.change(False)
        self.assertIsNone(switch.state.value)

    def test_boot_without_marker_does_not_inspect_firewall(self):
        switch = self.switch()
        with mock.patch.object(switch.nft, "snapshot", side_effect=AssertionError("unexpected nft access")):
            self.assertIn("no persistent", switch.change(False, boot=True))

    def test_boot_restores_saved_loopback_without_changing_marker(self):
        content = b'{"version": 1, "allow_loopback": false}\n'
        switch = self.switch(persistent=content)
        switch.change(False, boot=True)
        self.assertFalse(net.inspect_table(switch.nft.table))
        self.assertFalse(net.persistent_setting(switch.state.value))

    def test_invalid_boot_state_fails_before_nft(self):
        switch = self.switch(persistent=b"broken")
        with mock.patch.object(switch.nft, "snapshot", side_effect=AssertionError("unexpected nft access")):
            with self.assertRaisesRegex(net.SwitchError, "Invalid persistent"):
                switch.change(False, boot=True)

    def test_on_recovers_malformed_marker(self):
        switch = self.switch(table(), b"broken")
        switch.change(False)
        self.assertIsNone(switch.state.value)

    def test_name_collision_refused_before_state_changes(self):
        foreign = table()
        foreign["nftables"][0]["table"]["comment"] = "somebody else"
        for off in (True, False):
            switch = self.switch(foreign)
            with self.assertRaisesRegex(net.SwitchError, "ownership"):
                switch.change(off, persist=True)
            self.assertEqual(switch.nft.calls, [])
            self.assertEqual(switch.state.writes, [])

    def test_dormant_table_is_not_off_and_is_repaired(self):
        dormant = table()
        dormant["nftables"][0]["table"]["flags"] = ["dormant"]
        switch = self.switch(dormant)
        with self.assertRaisesRegex(net.SwitchError, "does not match"):
            switch.status()
        switch.change(True)
        self.assertTrue(net.inspect_table(switch.nft.table))

    def test_unexpected_accept_rule_is_not_a_complete_lockdown(self):
        modified = table()
        modified["nftables"].append({"rule": {"chain": "output", "expr": [{"accept": None}]}})
        self.assertIsNone(net.inspect_table(modified))

    def test_preflight_failure_changes_nothing(self):
        switch = self.switch()
        switch.nft.reject_check = True
        with self.assertRaises(net.NftRejected):
            switch.change(True, persist=True)
        self.assertIsNone(switch.nft.table)
        self.assertEqual(switch.state.writes, [])

    def test_state_failure_does_not_touch_firewall(self):
        switch = self.switch()
        switch.state.fail_once = True
        with self.assertRaisesRegex(OSError, "disk full"):
            switch.change(True, persist=True)
        self.assertTrue(all(check for _, check in switch.nft.calls))
        self.assertIsNone(switch.state.value)

    def test_atomic_rejection_restores_marker_without_rewriting_firewall(self):
        switch = self.switch()
        switch.nft.reject_apply = True
        with self.assertRaises(net.NftRejected):
            switch.change(True, persist=True)
        self.assertIsNone(switch.state.value)
        self.assertIsNone(switch.nft.table)
        self.assertEqual(len(switch.nft.calls), 2)

    def test_failed_postcheck_rolls_back_new_table_and_marker(self):
        switch = self.switch()
        switch.nft.corrupt_once = True
        with self.assertRaisesRegex(net.SwitchError, "verification"):
            switch.change(True, persist=True)
        self.assertIsNone(switch.nft.table)
        self.assertIsNone(switch.state.value)

    def test_failed_postcheck_restores_previous_owned_table(self):
        switch = self.switch(table(False))
        switch.nft.corrupt_once = True
        with self.assertRaises(net.SwitchError):
            switch.change(True, persist=True)
        self.assertIs(net.inspect_table(switch.nft.table), False)
        self.assertIsNone(switch.state.value)

    def test_rollback_failure_is_reported(self):
        switch = self.switch()
        switch.nft.corrupt_once = True
        switch.nft.fail_restore = True
        with self.assertRaisesRegex(net.SwitchError, "firewall rollback failed"):
            switch.change(True, persist=True)

    def test_failed_on_postcheck_restores_prior_table_and_persistence(self):
        marker = b'{"version": 1, "allow_loopback": true}'
        switch = self.switch(table(), marker)
        switch.nft.fail_snapshot_after_apply = True
        with self.assertRaisesRegex(net.SwitchError, "read after"):
            switch.change(False)
        self.assertTrue(net.inspect_table(switch.nft.table))
        self.assertEqual(switch.state.value, marker)

    def test_status_distinguishes_absent_off_and_pending_boot(self):
        self.assertIn("ON", self.switch().status())
        self.assertIn("OFF", self.switch(table()).status())
        self.assertIn("scheduled for next boot", self.switch(persistent=b'{"version":1,"allow_loopback":true}').status())


class BoundaryTests(unittest.TestCase):
    def test_nft_errors_never_mean_absent(self):
        with mock.patch.object(net.Nft, "run", side_effect=net.NftRejected("permission denied")):
            with self.assertRaisesRegex(net.SwitchError, "permission denied"):
                net.Nft().snapshot()

    def test_nft_uses_fixed_binary_no_shell_and_timeout(self):
        result = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(net.subprocess, "run", return_value=result) as run:
            net.Nft().apply("table script")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/sbin/nft", "-f", "-"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["input"], "table script")
        self.assertGreater(kwargs["timeout"], 0)

    def test_help_and_dry_run_never_access_state_or_nft(self):
        with mock.patch.object(net.Nft, "run", side_effect=AssertionError("nft")), mock.patch.object(net.State, "read", side_effect=AssertionError("state")), mock.patch.object(net, "execution_lock", side_effect=AssertionError("lock")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(net.main(["off", "--dry-run", "--persist"]), 0)
            self.assertEqual(net.main(["on", "--dry-run"]), 0)
            with self.assertRaises(SystemExit) as result:
                net.main(["off", "--help"])
            self.assertEqual(result.exception.code, 0)

    def test_nonroot_mutation_rejected_before_lock(self):
        with mock.patch.object(net.os, "geteuid", return_value=1000), mock.patch.object(net, "execution_lock", side_effect=AssertionError("lock")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(net.main(["off"]), 2)

    def test_missing_boot_support_rejects_before_change(self):
        with mock.patch.object(net.Path, "is_dir", return_value=False):
            with self.assertRaisesRegex(net.SwitchError, "systemd"):
                net.validate_persistence()

    def test_masked_boot_unit_rejects_persistence(self):
        result = subprocess.CompletedProcess([], 1, "masked\n", "")
        with mock.patch.object(net.Path, "is_dir", return_value=True), mock.patch.object(net.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(net.SwitchError, "enabled"):
                net.validate_persistence()

    def test_vendor_boot_dependency_is_accepted(self):
        result = subprocess.CompletedProcess([], 1, "disabled\n", "")
        installed = Path("/usr/lib/systemd/system/whitecube-net-lockdown.service")
        with mock.patch.object(net.Path, "is_dir", return_value=True), mock.patch.object(net.subprocess, "run", return_value=result), mock.patch.object(net.os.path, "lexists", side_effect=lambda p: str(p).startswith("/usr/lib/")), mock.patch.object(net.Path, "is_symlink", return_value=True), mock.patch.object(net.Path, "is_file", return_value=True), mock.patch.object(net.Path, "resolve", return_value=installed):
            net.validate_persistence()

    def test_masked_vendor_dependency_is_rejected(self):
        result = subprocess.CompletedProcess([], 1, "disabled\n", "")
        def resolve(path):
            return Path("/dev/null") if ".wants" in str(path) else path
        with mock.patch.object(net.Path, "is_dir", return_value=True), mock.patch.object(net.subprocess, "run", return_value=result), mock.patch.object(net.os.path, "lexists", return_value=True), mock.patch.object(net.Path, "is_symlink", return_value=True), mock.patch.object(net.Path, "is_file", return_value=True), mock.patch.object(net.Path, "resolve", resolve):
            with self.assertRaises(net.SwitchError):
                net.validate_persistence()

    def test_state_atomic_permissions_and_remove(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "state.json"
            state = net.State(path)
            self.assertIsNone(state.read())
            state.write(b"first")
            self.assertEqual(state.read(), b"first")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            state.write(b"second")
            self.assertEqual(state.read(), b"second")
            state.write(None)
            self.assertIsNone(state.read())

    def test_state_symlink_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "target"
            target.write_text("untouched")
            path = Path(temporary) / "state"
            path.symlink_to(target)
            with self.assertRaises(OSError):
                net.State(path).read()
            self.assertEqual(target.read_text(), "untouched")

    def test_shared_nonblocking_lock_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock"
            with net.execution_lock(path):
                with self.assertRaisesRegex(net.SwitchError, "Another"):
                    with net.execution_lock(path):
                        self.fail("lock acquired twice")
            path.unlink()
            path.symlink_to(Path(temporary) / "target")
            with self.assertRaises(OSError):
                with net.execution_lock(path):
                    self.fail("symlink lock opened")

    def test_malformed_configuration_fails_readonly(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config"
            for content in ('[]', '{"allow_loopback":"false"}', '{"unknown":1}', '{"interface":"$(bad)"}', 'not json'):
                path.write_text(content)
                with self.assertRaises(net.SwitchError):
                    net.configuration(path)


if __name__ == "__main__":
    unittest.main()
