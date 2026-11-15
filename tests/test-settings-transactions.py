#!/usr/bin/python3 -I
"""Exercise system setting write failures using real disposable files only."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader('settings_transactions', str(ROOT / 'libexec/cube-settings-helper'))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


class Transactions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, filename in [('HOSTNAME', 'hostname'), ('HOSTS', 'hosts'),
                               ('LOCALTIME', 'localtime'), ('TIMEZONE', 'timezone'),
                               ('KEYBOARD', 'keyboard'), ('SYSTEM_LOCK', 'lock'), ('ZONEINFO', 'zoneinfo')]:
            patcher = mock.patch.object(helper, name, self.root / filename)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(helper, 'require_root')
        patcher.start()
        self.addCleanup(patcher.stop)
        helper.HOSTNAME.write_text('old-name\n')
        helper.HOSTNAME.chmod(0o640)
        helper.HOSTS.write_text('127.0.0.1 localhost\n127.0.1.1 old-name\n127.0.1.10 unrelated\n')
        helper.ZONEINFO.mkdir()
        (helper.ZONEINFO / 'UTC').write_bytes(b'UTC zone fixture')
        (helper.ZONEINFO / 'Old').write_bytes(b'old zone fixture')

    def leftovers(self):
        return list(self.root.glob('.*.whitecube-*'))

    def test_hostname_is_complete_before_kernel_change_and_preserves_mode(self):
        def applied(*argv, **kwargs):
            self.assertEqual(argv, ('/usr/bin/hostname', 'new-name'))
            self.assertEqual(helper.HOSTNAME.read_text(), 'new-name\n')
            hosts = helper.HOSTS.read_text()
            self.assertIn('127.0.1.1\tnew-name\n', hosts)
            self.assertIn('127.0.1.10 unrelated\n', hosts)
            self.assertNotIn('old-name', hosts)
        with mock.patch.object(helper, 'run', side_effect=applied), mock.patch.object(helper, 'emit'):
            helper.set_hostname('new-name')
        self.assertEqual(helper.HOSTNAME.stat().st_mode & 0o777, 0o640)
        self.assertFalse(self.leftovers())

    def test_second_rename_failure_restores_first_file_and_never_applies(self):
        before = {path: path.read_bytes() for path in (helper.HOSTNAME, helper.HOSTS)}
        replace = os.replace
        failed = False
        def interrupted(source, target):
            nonlocal failed
            if target == helper.HOSTS and not failed:
                failed = True
                raise OSError('injected disk failure')
            return replace(source, target)
        with mock.patch.object(helper.os, 'replace', side_effect=interrupted), mock.patch.object(helper, 'run') as run:
            with self.assertRaisesRegex(OSError, 'injected disk failure'):
                helper.set_hostname('new-name')
        run.assert_not_called()
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(self.leftovers())

    def test_failed_runtime_apply_restores_files(self):
        before = helper.HOSTNAME.read_bytes(), helper.HOSTS.read_bytes()
        with mock.patch.object(helper, 'run', side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                helper.set_hostname('new-name')
        self.assertEqual((helper.HOSTNAME.read_bytes(), helper.HOSTS.read_bytes()), before)
        self.assertFalse(self.leftovers())

    def test_prepare_failure_does_not_change_any_destination(self):
        before = helper.HOSTNAME.read_bytes()
        stage = helper.stage_system_file
        def fail_second(path, state):
            if path == helper.HOSTS:
                raise OSError('cannot stage hosts')
            return stage(path, state)
        with mock.patch.object(helper, 'stage_system_file', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'cannot stage hosts'):
                helper.set_hostname('new-name')
        self.assertEqual(helper.HOSTNAME.read_bytes(), before)
        self.assertFalse(self.leftovers())

    def test_rollback_does_not_overwrite_an_unrelated_concurrent_edit(self):
        def fail_after_external_edit():
            helper.HOSTS.write_text('external editor retained\n')
            raise RuntimeError('apply failed')
        with self.assertRaisesRegex(RuntimeError, 'changed by another process'):
            helper.replace_system_files({helper.HOSTNAME: 'new\n', helper.HOSTS: 'new hosts\n'}, fail_after_external_edit)
        self.assertEqual(helper.HOSTNAME.read_text(), 'old-name\n')
        self.assertEqual(helper.HOSTS.read_text(), 'external editor retained\n')
        self.assertFalse(self.leftovers())

    def test_timezone_replaces_regular_localtime_and_restores_on_failure(self):
        helper.LOCALTIME.write_bytes(b'old binary timezone')
        helper.TIMEZONE.write_text('Old\n')
        replace = os.replace
        def fail_timezone(source, target):
            if target == helper.TIMEZONE:
                raise OSError('timezone cannot be written')
            return replace(source, target)
        with mock.patch.object(helper.os, 'replace', side_effect=fail_timezone):
            with self.assertRaises(OSError):
                helper.set_timezone('UTC')
        self.assertFalse(helper.LOCALTIME.is_symlink())
        self.assertEqual(helper.LOCALTIME.read_bytes(), b'old binary timezone')
        self.assertEqual(helper.TIMEZONE.read_text(), 'Old\n')
        with mock.patch.object(helper, 'emit'):
            helper.set_timezone('UTC')
        self.assertEqual(os.readlink(helper.LOCALTIME), str(helper.ZONEINFO / 'UTC'))
        self.assertEqual(helper.TIMEZONE.read_text(), 'UTC\n')

    def test_timezone_rollback_preserves_previous_symlink(self):
        helper.LOCALTIME.symlink_to(helper.ZONEINFO / 'Old')
        replace = os.replace
        def fail_timezone(source, target):
            if target == helper.TIMEZONE:
                raise OSError('cannot create timezone')
            return replace(source, target)
        with mock.patch.object(helper.os, 'replace', side_effect=fail_timezone):
            with self.assertRaises(OSError):
                helper.set_timezone('UTC')
        self.assertEqual(os.readlink(helper.LOCALTIME), str(helper.ZONEINFO / 'Old'))
        self.assertFalse(helper.TIMEZONE.exists())

    def test_keyboard_failure_restores_config_and_preserves_unrelated_lines(self):
        original = '# local settings\nXKBMODEL="pc105"\nXKBLAYOUT="us"\nXKBLAYOUT="de"\n'
        helper.KEYBOARD.write_text(original)
        with mock.patch.object(helper.os, 'access', return_value=True), mock.patch.object(helper, 'run', side_effect=RuntimeError('setupcon failed')):
            with self.assertRaisesRegex(RuntimeError, 'setupcon failed'):
                helper.console_keyboard('ca', '')
        self.assertEqual(helper.KEYBOARD.read_text(), original)
        with mock.patch.object(helper.os, 'access', return_value=True), mock.patch.object(helper, 'run'), mock.patch.object(helper, 'emit'):
            helper.console_keyboard('ca', '')
        self.assertIn('XKBMODEL="pc105"', helper.KEYBOARD.read_text())
        self.assertEqual(helper.KEYBOARD.read_text().count('XKBLAYOUT="ca"'), 2)

    def test_lock_symlink_is_refused_without_touching_target(self):
        sentinel = self.root / 'sentinel'
        sentinel.write_text('unchanged')
        helper.SYSTEM_LOCK.symlink_to(sentinel)
        with self.assertRaises(OSError):
            with helper.system_config_lock():
                self.fail('unsafe lock accepted')
        self.assertEqual(sentinel.read_text(), 'unchanged')

    def test_validation_and_missing_setupcon_do_not_open_transaction(self):
        with mock.patch.object(helper, 'system_config_lock') as lock, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                helper.set_hostname('../bad')
            with mock.patch.object(helper.os, 'access', return_value=False):
                with self.assertRaises(SystemExit):
                    helper.console_keyboard('us', '')
        lock.assert_not_called()


class HelperPowerAndSystem(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, sub in [('POWER', 'power_supply'), ('BACKLIGHT', 'backlight'),
                          ('HOSTNAME', 'hostname'), ('LOCALTIME', 'localtime'), ('TIMEZONE', 'timezone')]:
            path = self.root / sub
            if name in ('POWER', 'BACKLIGHT'):
                path.mkdir()
            patcher = mock.patch.object(helper, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_power_skips_device_batteries_and_clamps_backlight(self):
        bat = helper.POWER / 'BAT0'
        bat.mkdir()
        (bat / 'type').write_text('Battery\n')
        (bat / 'scope').write_text('System\n')
        (bat / 'capacity').write_text('80\n')
        (bat / 'status').write_text('Discharging\n')
        hid = helper.POWER / 'hid-mouse'
        hid.mkdir()
        (hid / 'type').write_text('Battery\n')
        (hid / 'scope').write_text('Device\n')
        (hid / 'capacity').write_text('45\n')
        ac = helper.POWER / 'AC'
        ac.mkdir()
        (ac / 'type').write_text('Mains\n')
        (ac / 'online').write_text('1\n')
        broken = helper.BACKLIGHT / '00_broken'
        broken.mkdir()
        (broken / 'max_brightness').write_text('0\n')
        (broken / 'brightness').write_text('5\n')
        bl = helper.BACKLIGHT / 'intel_backlight'
        bl.mkdir()
        (bl / 'max_brightness').write_text('200\n')
        (bl / 'brightness').write_text('250\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            helper.power()
        lines = buf.getvalue().splitlines()
        self.assertIn('battery\tBAT0\t80\tDischarging\t\t', lines)
        self.assertIn('ac\tAC\tonline', lines)
        self.assertIn('backlight\tintel_backlight\t100', lines)
        self.assertFalse(any('hid-mouse' in l for l in lines))
        self.assertFalse(any('00_broken' in l for l in lines))

    def test_brightness_picks_usable_backlight_and_rejects_all_zero(self):
        broken = helper.BACKLIGHT / 'acpi_video0'
        broken.mkdir()
        (broken / 'max_brightness').write_text('0\n')
        (broken / 'brightness').write_text('0\n')
        bl = helper.BACKLIGHT / 'intel_backlight'
        bl.mkdir()
        (bl / 'max_brightness').write_text('400\n')
        (bl / 'brightness').write_text('10\n')
        with mock.patch.object(helper, 'require_root'), mock.patch.object(helper, 'emit') as emit:
            helper.brightness('50')
        self.assertEqual((bl / 'brightness').read_text(), '200')
        self.assertEqual((broken / 'brightness').read_text(), '0\n')
        emit.assert_called_once_with('ok', 'brightness', '50')
        (bl / 'max_brightness').write_text('0\n')
        with mock.patch.object(helper, 'require_root'), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                helper.brightness('50')

    def test_system_reads_configured_paths_and_cli_flags(self):
        helper.HOSTNAME.write_text('cube-box\n')
        helper.TIMEZONE.write_text('America/Toronto\n')
        buf = io.StringIO()
        with mock.patch.object(helper, 'run', return_value=mock.Mock(stdout='')), contextlib.redirect_stdout(buf):
            helper.system()
        lines = buf.getvalue().splitlines()
        self.assertIn('sys\thostname\tcube-box', lines)
        self.assertIn('sys\ttimezone\tAmerica/Toronto', lines)
        for flag, expected in (('--help', 'usage:'), ('--version', 'cube-settings-helper')):
            out = io.StringIO()
            with mock.patch.object(helper.sys, 'argv', ['cube-settings-helper', flag]), contextlib.redirect_stdout(out):
                helper.main()
            self.assertIn(expected, out.getvalue())
        with mock.patch.object(helper.sys, 'argv', ['cube-settings-helper', 'no-such-cmd']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                helper.main()
            self.assertEqual(ctx.exception.code, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
