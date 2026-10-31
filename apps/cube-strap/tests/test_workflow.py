"""Persistent-build authentication, cancellation and UI completion regressions."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

APP = Path(__file__).resolve().parents[1]
COMPATIBILITY = APP.parent / 'chrootables'
sys.path.insert(0, str(APP))
import strap


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


backend = module('strap_workflow_backend', COMPATIBILITY / 'backend.py')
broker = module('strap_workflow_broker', COMPATIBILITY / 'broker.py')


class PersistentBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='strap-persistent-', dir=APP.parent.parent / 'build'
                                                     if (APP.parent.parent / 'build').is_dir() else None)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.apps = self.directory / 'apps'
        self.compatibility = self.apps / 'chrootables'
        self.builder = self.apps / 'cube-strap'
        self.compatibility.mkdir(parents=True)
        self.builder.mkdir()
        shutil.copyfile(COMPATIBILITY / 'broker.py', self.compatibility / 'broker.py')
        (self.builder / 'deb-strap.sh').write_text('# fixed bundled bootstrap\n')
        helper = self.builder / 'cube-strap-build'
        helper.write_text('''#!/usr/bin/python3 -I
import json, pathlib, signal, sys, time
arguments = dict(zip(sys.argv[1::2], sys.argv[2::2]))
directory = pathlib.Path(arguments['--workdir'])
(directory / 'arguments.json').write_text(json.dumps(arguments))
if (directory / 'slow').exists():
    def cancel(*_):
        (directory / 'cleaned').write_text('cleaned')
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, cancel)
    print('working', flush=True)
    while True: time.sleep(.01)
else:
    print('bootstrap finished', flush=True)
''')
        helper.chmod(0o755)
        self.sudo = self.directory / 'sudo'
        self.sudo.write_text('''#!/usr/bin/python3 -I
import os, pathlib, sys
args = sys.argv[1:]
if '-S' not in args:
    print('sudo: a password is required', file=sys.stderr)
    raise SystemExit(1)
if sys.stdin.readline().rstrip('\\n') != 'secret':
    print('Sorry, try again.', file=sys.stderr)
    raise SystemExit(1)
with pathlib.Path(__file__).with_name('authentications').open('a') as output: output.write('authenticated\\n')
args = args[args.index('--') + 1:]
os.environ['SUDO_UID'] = str(os.getuid())
os.environ['SUDO_GID'] = str(os.getgid())
os.execv(args[0], args)
''')
        self.sudo.chmod(0o755)
        manager = self.compatibility / 'manager'
        manager.write_text('''#!/usr/bin/python3 -I
import pathlib, signal, sys, time
directory = pathlib.Path(__file__).parent
if sys.argv[1] == '--import' and (directory / 'slow-import').exists():
    def cancel(*_):
        (directory / 'import-cleaned').write_text('cleaned')
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, cancel)
    print('importing', file=sys.stderr, flush=True)
    while True: time.sleep(.01)
print('manager ' + sys.argv[1], flush=True)
''')
        manager.chmod(0o755)
        paths = patch.object(backend, 'HERE', self.compatibility)
        paths.start()
        self.addCleanup(paths.stop)
        self.manager = backend.Manager(manager, sudo=str(self.sudo), idle=0)
        self.addCleanup(self.close_manager)
        self.workdir = self.directory / 'build-with_spaces'
        self.workdir.mkdir(mode=0o700)
        (self.workdir / 'cube.conf').write_text('user_password = "not logged"\n')
        (self.workdir / 'cube.conf').chmod(0o600)

    def close_manager(self):
        if self.manager.broker:
            self.manager.broker.close(wait=3)
            self.manager.broker.process.stdout.close()
            self.manager.broker.process.stderr.close()

    def test_one_unlock_builds_and_imports_when_every_separate_sudo_call_is_refused(self):
        self.assertTrue(self.manager.authenticate('secret'))
        denied = subprocess.run([str(self.sudo), '-n', '--', 'true'], capture_output=True, text=True, timeout=3)
        self.assertNotEqual(denied.returncode, 0)
        lines = []
        result = self.manager.build({'workdir': str(self.workdir), 'kind': 'chroot'}, on_line=lines.append)
        self.assertTrue(result.ok)
        self.assertIn('bootstrap finished', lines)
        self.assertTrue(self.manager.change(['--import', 'lab', '/archive.tar.gz', '1024', 'persistent'], merge=True).ok)
        self.assertEqual((self.directory / 'authentications').read_text().splitlines(), ['authenticated'])
        arguments = json.loads((self.workdir / 'arguments.json').read_text())
        self.assertEqual(arguments['--config'], str(self.workdir / 'cube.conf'))
        self.assertEqual(arguments['--debstrap'], str(self.builder / 'deb-strap.sh'))
        self.assertEqual(arguments['--owner'], f'{os.getuid()}:{os.getgid()}')

    def test_build_cancellation_uses_the_running_request_and_waits_for_cleanup(self):
        self.assertTrue(self.manager.authenticate('secret'))
        (self.workdir / 'slow').touch()
        started = threading.Event()
        errors = []

        def build():
            try:
                self.manager.build({'workdir': str(self.workdir), 'kind': 'chroot'},
                                   on_line=lambda line: started.set() if line == 'working' else None)
            except backend.ManagerError as error:
                errors.append(error)

        thread = threading.Thread(target=build)
        thread.start()
        self.assertTrue(started.wait(3))
        self.assertTrue(self.manager.cancel())
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors[0].status, 143)
        self.assertTrue((self.workdir / 'cleaned').exists())
        self.assertEqual(self.manager.running, set())

    def test_build_schema_cannot_override_commands_paths_or_owner(self):
        self.assertTrue(self.manager.authenticate('secret'))
        valid = {'workdir': str(self.workdir), 'kind': 'chroot'}
        for changes in ({'helper': '/bin/sh'}, {'owner': '0:0'}, {'kind': 'shell'},
                        {'workdir': 'relative'}, {'workdir': str(self.workdir) + '\n'}):
            with self.subTest(changes=changes):
                result = self.manager.broker.request({'build': dict(valid, **changes)})
                self.assertEqual(result.status, 64)
        (self.workdir / 'cube.conf').chmod(0o644)
        self.assertEqual(self.manager.broker.request({'build': valid}).status, 64)
        self.assertFalse((self.workdir / 'arguments.json').exists())

    def test_import_cancellation_uses_the_same_helper_and_keeps_cleanup(self):
        self.assertTrue(self.manager.authenticate('secret'))
        (self.compatibility / 'slow-import').touch()
        started = threading.Event()
        errors = []

        def importing():
            try:
                self.manager.change(['--import', 'lab', '/archive.tar.gz', '1024', 'persistent'],
                                    on_line=lambda line: started.set() if line == 'importing' else None)
            except backend.ManagerError as error:
                errors.append(error)

        thread = threading.Thread(target=importing)
        thread.start()
        self.assertTrue(started.wait(3))
        self.assertTrue(self.manager.cancel())
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors[0].status, 143)
        self.assertTrue((self.compatibility / 'import-cleaned').exists())
        self.assertEqual(self.manager.running, set())

    def test_legacy_broker_survives_deep_json_and_accepts_the_next_request(self):
        self.assertTrue(self.manager.authenticate('secret'))
        nested = '{"id":900,"run":' + '[' * 2000 + '0' + ']' * 2000 + '}\n'
        with self.manager.broker._write_lock:
            self.manager.broker.process.stdin.write(nested)
            self.manager.broker.process.stdin.flush()
        self.assertEqual(self.manager.broker.request({'ping': True}).stdout, 'pong')


class WindowWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.app = module('strap_workflow_app', APP / 'app.py')
        except ImportError as error:
            raise unittest.SkipTest('GTK bindings unavailable: ' + str(error))

    def test_cancel_during_import_reaches_the_persistent_manager(self):
        cancelled = threading.Event()
        window = SimpleNamespace(process=None, building=True, cancelled=False,
                                 manager=Mock(cancel=Mock(side_effect=cancelled.set)), status=Mock())
        self.app.StrapWindow.cancel(window)
        self.assertTrue(cancelled.wait(2))
        self.assertTrue(window.cancelled)
        window.manager.cancel.assert_called_once_with()

    def test_history_failure_always_releases_build_controls_and_reports_error(self):
        plan = strap.Plan(name='lab', password='secret')
        window = SimpleNamespace(cancelled=False, manager=Mock(), say=Mock(), step=Mock(),
                                 save_log=Mock(), finished=Mock(), build_line=Mock())
        window.manager.build.return_value.ok = True
        with tempfile.TemporaryDirectory(prefix='build-') as directory:
            workdir = Path(directory)
            with patch.object(strap, 'new_workdir', return_value=workdir), \
                    patch.object(strap, 'record', side_effect=OSError(28, 'No space left on device')), \
                    patch.object(self.app.GLib, 'idle_add') as dispatch:
                self.app.StrapWindow.build_worker(window, plan)
        callback, saved_plan, status, detail = dispatch.call_args.args
        self.assertEqual((callback, saved_plan, status), (window.finished, plan, 'built'))
        self.assertIn('Could not save build history', detail)
        self.assertIn('No space left', detail)


if __name__ == '__main__':
    unittest.main()
