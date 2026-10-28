#!/usr/bin/env python3
"""Exercise real launcher processes without a display, audio service or real compositor."""
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'bin/whitecube-session'
SESSION = runpy.run_path(str(SOURCE))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='session-test-', dir=ROOT / 'build')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.commands = self.root / 'bin'
        self.commands.mkdir()
        self.launcher = self.commands / 'whitecube-session'
        self.launcher.write_bytes(SOURCE.read_bytes())
        self.launcher.chmod(0o755)
        self.capture = self.root / 'capture.json'
        self.compositor = self.commands / 'whitecube-compositor'
        self.compositor.write_text('''#!/usr/bin/python3
import json, os, pathlib, signal, sys, time
pathlib.Path(os.environ['CAPTURE_FILE']).write_text(json.dumps({'argv': sys.argv[1:], 'env':dict(os.environ), 'pid':os.getpid()}))
if os.environ.get('WAIT'):
    while True: time.sleep(.1)
print('fake compositor output')
raise SystemExit(int(os.environ.get('EXIT_STATUS','0')))
''')
        self.compositor.chmod(0o755)
        self.env = {**os.environ, 'HOME': str(self.home), 'XDG_STATE_HOME': str(self.root / 'state'),
                    'XDG_RUNTIME_DIR': str(self.runtime), 'CAPTURE_FILE': str(self.capture),
                    'DISPLAY': ':77', 'WAYLAND_DISPLAY': 'parent-wayland-7',
                    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/stale/session'}
        self.state = self.root / 'state/whitecube'

    def launch(self, *args, **extra):
        return subprocess.run([str(self.launcher), *args], env={**self.env, **extra},
                              text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=5)

    def wait_capture(self):
        limit = time.monotonic() + 5
        while not self.capture.exists() and time.monotonic() < limit:
            time.sleep(.02)
        self.assertTrue(self.capture.exists(), 'fake compositor did not start')
        return json.loads(self.capture.read_text())

    def test_launch_sets_wayland_environment_and_rotates_private_logs(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.wait_capture()
        env = record['env']
        self.assertEqual(env['XDG_SESSION_TYPE'], 'wayland')
        self.assertEqual(env['XDG_CURRENT_DESKTOP'], 'whitecube')
        self.assertEqual(env['XDG_RUNTIME_DIR'], str(self.runtime))
        for stale in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS'):
            self.assertNotIn(stale, env)
        self.assertEqual(record['argv'][0], '-s')
        self.assertIn('cube-status', record['argv'][1])
        log = self.state / 'session.log'
        self.assertIn('fake compositor output', log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        original = log.read_bytes()
        self.assertEqual(self.launch().returncode, 0)
        self.assertEqual((self.state / 'session.log.old').read_bytes(), original)
        self.assertEqual((self.state / 'failed-starts').read_text(), '')

    def test_check_reports_prerequisites_without_starting_compositor(self):
        result = self.launch('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['compositor'], str(self.compositor))
        self.assertEqual(report['runtime'], str(self.runtime))
        self.assertEqual(report['recent_failures'], 0)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.state / 'session.log').exists())

    def test_safe_mode_omits_all_startup_clients(self):
        result = self.launch('--safe-mode')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wait_capture()['argv'], [])

    def test_explicit_nested_backend_keeps_parent_wayland_socket(self):
        result = self.launch('--safe-mode', WLR_BACKENDS='wayland')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wait_capture()['env']['WAYLAND_DISPLAY'], 'parent-wayland-7')

    def test_crash_guard_blocks_fourth_start_and_safe_mode_recovers(self):
        for _ in range(3):
            result = self.launch(EXIT_STATUS='12')
            self.assertEqual(result.returncode, 12, result.stderr)
        original = self.capture.read_bytes()
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn('3 failed starts within 60 seconds', result.stderr)
        self.assertIn('--safe-mode', result.stderr)
        self.assertEqual(self.capture.read_bytes(), original)
        # A login manager can retry immediately after the refusal. Keep blocking
        # those retries instead of treating the first refusal as a reset.
        for _ in range(2):
            result = self.launch()
            self.assertEqual(result.returncode, 1)
            self.assertIn('3 failed starts within 60 seconds', result.stderr)
            self.assertEqual(self.capture.read_bytes(), original)
        # Recreate the guard to prove safe mode bypasses it, then resets it.
        now = int(time.time())
        (self.state / 'failed-starts').write_text(f'{now}\n' * 3)
        self.assertEqual(self.launch('--safe-mode').returncode, 0)
        self.assertEqual((self.state / 'failed-starts').read_text(), '')

    def test_expired_crash_guard_allows_another_start(self):
        self.state.mkdir(mode=0o700, parents=True)
        old = int(time.time()) - 61
        (self.state / 'failed-starts').write_text(f'{old}\n' * 3)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.capture.exists())
        self.assertEqual((self.state / 'failed-starts').read_text(), '')

    def test_explicit_crash_guard_reset_does_not_start_desktop(self):
        self.state.mkdir(mode=0o700, parents=True)
        (self.state / 'failed-starts').write_text('1\n2\n')
        result = self.launch('--reset-crash-guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / 'failed-starts').read_text(), '')
        self.assertFalse(self.capture.exists())

    def test_duplicate_session_refused_and_termination_reaches_compositor(self):
        process = subprocess.Popen([str(self.launcher)], env={**self.env, 'WAIT': '1'},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        record = self.wait_capture()
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn('already running', result.stderr)
        process.send_signal(signal.SIGTERM)
        _, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143, errors)
        with self.assertRaises(ProcessLookupError):
            os.kill(record['pid'], 0)
        self.assertEqual((self.state / 'failed-starts').read_text(), '')
        self.assertEqual(self.launch().returncode, 0)

    def test_insecure_runtime_is_rejected_without_chmod(self):
        self.runtime.chmod(0o777)
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn('0700', result.stderr)
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o777)
        self.assertFalse(self.capture.exists())

    def test_runtime_symlink_is_rejected(self):
        link = self.root / 'runtime-link'
        link.symlink_to(self.runtime, target_is_directory=True)
        result = self.launch(XDG_RUNTIME_DIR=str(link))
        self.assertEqual(result.returncode, 1)
        self.assertIn('no symlinks', result.stderr)
        self.assertFalse(self.capture.exists())

    def test_relative_runtime_is_rejected(self):
        result = self.launch(XDG_RUNTIME_DIR='relative/path')
        self.assertEqual(result.returncode, 1)
        self.assertIn('absolute path', result.stderr)

    def test_log_symlink_does_not_overwrite_target(self):
        self.state.mkdir(mode=0o700, parents=True)
        target = self.root / 'precious'
        target.write_text('keep this')
        (self.state / 'session.log').symlink_to(target)
        self.assertEqual(self.launch().returncode, 1)
        self.assertEqual(target.read_text(), 'keep this')
        self.assertFalse(self.capture.exists())

    def test_hardlinked_record_is_rejected_before_truncation(self):
        target = self.root / 'precious'
        target.write_text('keep this')
        link = self.root / 'hardlink'
        os.link(target, link)
        with self.assertRaises(SESSION['SessionError']):
            SESSION['private_file'](link, os.O_WRONLY | os.O_TRUNC)
        self.assertEqual(target.read_text(), 'keep this')

    def test_guard_ignores_malformed_future_and_stale_records(self):
        path = self.root / 'failed-starts'
        path.write_text('garbage\n1000\n941\n940\n1001\n\n')
        self.assertEqual(SESSION['recent_failures'](path, 1000), [1000, 941])

    def test_fallback_refuses_preexisting_symlink_without_changing_target(self):
        candidate = self.root / f'xdg-runtime-{os.getuid()}'
        candidate.symlink_to(self.runtime, target_is_directory=True)
        original = SESSION['private_directory']

        def validate(path, create=False):
            if str(path).startswith('/run/user/'):
                raise SESSION['SessionError']('simulate a machine without logind')
            return original(path, create)

        with mock.patch.dict(SESSION['runtime_directory'].__globals__, private_directory=validate):
            runtime = SESSION['runtime_directory']({'TMPDIR': str(self.root)})
        self.assertNotEqual(runtime, candidate)
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
        self.assertTrue(candidate.is_symlink())
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)

    def test_audio_honors_custom_config_directory_and_whitespace(self):
        config = self.root / 'config/whitecube'
        config.mkdir(parents=True)
        (config / 'config').write_text('  audio = pipewire\n')
        result = self.launch(XDG_CONFIG_HOME=str(config.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('pipewire', self.wait_capture()['argv'][1])

    def test_missing_compositor_is_reported_before_launch(self):
        self.compositor.unlink()
        with mock.patch.dict(SESSION['find_compositor'].__globals__, os=mock.Mock(wraps=os)) as scope:
            scope['os'].access.return_value = False
            with self.assertRaisesRegex(SESSION['SessionError'], 'missing or not executable'):
                SESSION['find_compositor'](self.launcher)

    def test_unlaunchable_compositor_failures_trigger_crash_guard(self):
        self.compositor.write_text('#!/missing-whitecube-interpreter\n')
        for _ in range(3):
            result = self.launch()
            self.assertEqual(result.returncode, 1)
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn('3 failed starts within 60 seconds', result.stderr)
        self.assertFalse(self.capture.exists())


if __name__ == '__main__':
    unittest.main()
