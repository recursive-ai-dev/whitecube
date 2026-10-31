#!/usr/bin/python3 -I
"""Check Cube Chrootables' copied Qt app, helper, and Cube Strap compatibility.

Uses temporary fixture stores, fake sudo, and mocked terminal authentication; no administrator
access or downloads are needed. --no-gui skips Qt checks and rendering.
--screenshots chooses the directory for the offscreen desktop preview.
"""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / 'apps/chrootables'
MANAGER = APP / 'chrootable-manager'
# /tmp may be mounted noexec and the fake sudo must run: keep scratch files in build/.
SCRATCH = ROOT / 'build' if (ROOT / 'build').is_dir() else None
sys.path.insert(0, str(APP))
import backend  # noqa: E402

FIXTURE = r'''
set -Eeuo pipefail
source "$1"
STORE_DIR=$2; configure_paths; init_store
image() {
    truncate -s "$3M" "$IMAGE_DIR/$1.img"
    write_metadata "$IMAGE_DIR/$1.img" "$2" stable amd64 "$3" 2026-10-01T09:30:00-03:00 2026-10-09T18:05:00-03:00 "$4"
    metadata_set "$IMAGE_DIR/$1.img" release 'Debian GNU/Linux 13 (trixie)'
}
image build-lab persistent 4096 build
metadata_set "$IMAGE_DIR/build-lab.img" description 'Compiler toolchain for the firmware project'
image python-playground ephemeral 4096 python
image network-kit ephemeral 2048 network
metadata_set "$IMAGE_DIR/network-kit.img" origin build-lab
install -d -m 0700 "$SNAPSHOT_DIR/build-lab"
for label in clean-base before-upgrade; do
    truncate -s 4096M "$SNAPSHOT_DIR/build-lab/$label.img"
    copy_metadata "$IMAGE_DIR/build-lab.img" "$SNAPSHOT_DIR/build-lab/$label.img"
    metadata_set "$SNAPSHOT_DIR/build-lab/$label.img" snapshot_at 2026-10-05T11:00:00-03:00
done
printf 'archive' >"$EXPORT_DIR/build-lab-20261008-101500-Qx7Lm2.tar.gz"
printf 'abc123  build-lab-20261008-101500-Qx7Lm2.tar.gz\n' >"$EXPORT_DIR/build-lab-20261008-101500-Qx7Lm2.tar.gz.sha256"
for event in 'create build-lab ok' 'snapshot build-lab ok' 'session build-lab exit 1' 'clone build-lab ok' 'create broken failed'; do
    set -- $event
    log="$LOG_DIR/$2-$1.log"; printf '%s log\n' "$1" >"$log"
    record_event "$1" "$2" "${*:3}" "$log"
done
'''

# A stand-in for sudo modelled on this desktop's sudoers: timestamp_timeout=0, so it
# never remembers a password (`sudo -n` always refuses) unless a cache fixture exists.
# -S reads up to three password lines like real sudo ("Sorry, try again." between)
# and then runs the command as the test user. Root's view of the private store is
# stood in for by the readable copy: --store and CHROOTABLE_HOME are pointed at it.
FAKE_SUDO = r'''#!/usr/bin/python3 -I
import os, sys
directory = os.environ['FAKE_SUDO_DIR']
args = sys.argv[1:]
cached = os.path.exists(os.path.join(directory, 'cached'))
if args == ['-k']:
    raise SystemExit(0)
password = '-S' in args
while args and args[0] != '--':
    if args[0] == '-p':
        args = args[1:]
    elif args[0] == '-v' and not password:
        raise SystemExit(0 if cached else (print('sudo: a password is required', file=sys.stderr) or 1))
    args = args[1:]
args = args[1:]
if password:
    for attempt in range(3):
        line = sys.stdin.readline()
        if not line:
            print('sudo: no password was provided', file=sys.stderr)
            raise SystemExit(1)
        if line.rstrip('\n') == 'secret':
            break
        print('Sorry, try again.', file=sys.stderr, flush=True)
    else:
        print('sudo: 3 incorrect password attempts', file=sys.stderr)
        raise SystemExit(1)
elif not cached:
    print('sudo: a password is required', file=sys.stderr)
    raise SystemExit(1)
with open(os.path.join(directory, 'last-command'), 'w') as log:
    log.write(' '.join(args) + '\n')
if '--store' in args:
    args[args.index('--store') + 1] = os.environ['FAKE_SUDO_STORE']
if args and args[0] == '/usr/bin/env':
    args = args[2:]
os.environ['CHROOTABLE_HOME'] = os.environ['FAKE_SUDO_STORE']
with open(os.path.join(directory, 'helpers'), 'a') as log:
    log.write(f'{os.getpid()}\n')
os.execv(args[0], args)
'''

# A manager stand-in for cancel tests: --slow sleeps until SIGTERM, then cleans up.
SLOW_MANAGER = '''#!/bin/bash
case $1 in
    --slow) trap 'echo "cleaned up" >&2; exit 143' TERM; echo "working" >&2; sleep 30 & wait; exit 0 ;;
    --echo) shift; printf '%s\\n' "$@"; echo "to stderr" >&2 ;;
    *) echo "Error: unknown" >&2; exit 2 ;;
esac
'''


def make_store(path):
    subprocess.run(['bash', '-c', FIXTURE, 'fixture', str(MANAGER), str(path)], check=True)
    return Path(path)


class BackendRules(unittest.TestCase):
    def test_installed_manager_is_the_private_bundled_helper(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True), patch.object(backend, 'HERE', Path('/usr/share/whitecube/chrootables')):
            self.assertEqual(backend.manager_path(), Path('/usr/libexec/whitecube/chrootable-manager'))

    def test_names_sizes_and_descriptions_follow_the_manager(self):
        for good in ('a', 'build-lab', 'A.1_x', 'x' * 64):
            self.assertTrue(backend.valid_name(good), good)
        for bad in ('', '-x', '.hidden', 'a b', 'x' * 65, 'a/b', 'é'):
            self.assertFalse(backend.valid_name(bad), bad)
        self.assertTrue(backend.valid_size(1) and backend.valid_size('1048576'))
        for bad in (0, '01', 1048577, 'abc', '1.5', -1):
            self.assertFalse(backend.valid_size(bad), bad)
        self.assertTrue(backend.valid_description('') and backend.valid_description('Toolchain'))
        self.assertFalse(backend.valid_description('x' * 201) or backend.valid_description('a\tb'))

    def test_arguments_are_built_and_validated(self):
        self.assertEqual(backend.create_args('lab', 4096, 'persistent', 'build', 1536),
                         ['--create', 'lab', '4096', 'persistent', 'build'])
        with self.assertRaises(ValueError):
            backend.create_args('lab', 1000, 'persistent', 'build', 1536)
        with self.assertRaises(ValueError):
            backend.create_args('../x', 4096, 'persistent', 'build')
        self.assertEqual(backend.workspace_args('restore', 'lab', 'clean'), ['--restore', 'lab', 'clean', '--yes'])
        self.assertEqual(backend.workspace_args('delete', 'lab'), ['--delete', 'lab', '--yes'])
        self.assertEqual(backend.workspace_args('describe', 'lab', ''), ['--describe', 'lab', ''])
        self.assertEqual(backend.workspace_args('resize', 'lab', 8192), ['--resize', 'lab', '8192'])
        for action, value in (('clone', 'lab'), ('rename', 'bad name'), ('mode', 'sometimes'), ('snapshot', '-x'), ('bogus', None)):
            with self.assertRaises(ValueError):
                backend.workspace_args(action, 'lab', value)
        with tempfile.NamedTemporaryFile(suffix='.tar.xz') as archive:
            self.assertEqual(backend.import_args('imp', archive.name, 2048, 'ephemeral'),
                             ['--import', 'imp', archive.name, '2048', 'ephemeral'])
        with self.assertRaises(ValueError):
            backend.import_args('imp', 'relative.tar', 2048, 'ephemeral')

    def test_suggestions(self):
        self.assertEqual(backend.unique_name('build-lab', ['build-lab', 'build-lab-2']), 'build-lab-3')
        self.assertEqual(backend.suggested_import_name('/x/build-lab-20261008-101500-Qx7Lm2.tar.gz'), 'build-lab')
        self.assertEqual(backend.suggested_import_name('/x/.odd name!.tar.zst'), 'odd-name-')
        self.assertEqual(backend.suggested_import_name('/x/rootfs.tar.xz', ['rootfs']), 'rootfs-2')
        self.assertEqual(backend.suggested_import_size(10), 2048)
        self.assertEqual(backend.suggested_import_size(3 * 1024 ** 3), 12288)
        self.assertEqual(backend.human_bytes(0), '0 B')
        self.assertEqual(backend.human_bytes(1536 * 1024 ** 2), '1.5 GiB')

    def test_sessions_open_in_a_terminal_that_runs_the_manager_through_sudo(self):
        argv = backend.terminal_argv('lab', 'offline', Path('/usr/bin/chrootable-manager'))
        self.assertEqual(argv[-6:], ['sudo', '--', '/usr/bin/chrootable-manager', '--enter', 'lab', '--isolated'])
        self.assertIn('--title', argv)
        with self.assertRaises(ValueError):
            backend.terminal_argv('lab', 'root-everything')

    def test_privileged_commands_go_through_sudo_without_a_password_argument(self):
        manager = backend.Manager(Path('/usr/bin/chrootable-manager'), sudo='sudo')
        self.assertEqual(manager.argv(['--list', '--json'], True), ['sudo', '-n', '--', '/usr/bin/chrootable-manager', '--list', '--json'])
        self.assertEqual(manager.argv(['--list', '--json'], False), ['/usr/bin/chrootable-manager', '--list', '--json'])

    def test_run_command_arguments(self):
        self.assertEqual(backend.exec_args('lab', ' uname -a '), ['--exec', 'lab', '--', '/bin/sh', '-c', 'uname -a'])
        self.assertEqual(backend.exec_args('lab', 'ls', isolated=True)[:3], ['--exec', 'lab', '--isolated'])
        for name, command in (('lab', ''), ('lab', '   '), ('bad name', 'ls'), ('lab', 'x' * 4001)):
            with self.assertRaises(ValueError):
                backend.exec_args(name, command)

    def test_doctor_report_is_parsed_into_levels(self):
        rows = backend.parse_doctor('CHROOTABLE / Host health\n\n[OK] Host commands\n[INFO] overlay\n[MISSING] debootstrap\n')
        self.assertEqual(rows, [('', 'CHROOTABLE / Host health'), ('OK', 'Host commands'), ('INFO', 'overlay'),
                                ('MISSING', 'debootstrap')])

    def test_prune_keeps_the_newest_snapshots(self):
        snapshots = [{'label': 'b', 'snapshot_at': '2026-10-02'}, {'label': 'a', 'snapshot_at': '2026-10-01'},
                     {'label': 'c', 'snapshot_at': '2026-10-03'}]
        self.assertEqual(backend.prune_plan(snapshots, 1), ['a', 'b'])
        self.assertEqual(backend.prune_plan(snapshots, 3), [])
        self.assertEqual(backend.prune_plan(snapshots, 0), ['a', 'b', 'c'])

    def test_list_filters_and_sorts(self):
        spaces = [{'name': 'b', 'lifecycle': 'persistent', 'size_mib': 10, 'last_used_at': 'never', 'status': 'ready'},
                  {'name': 'a', 'lifecycle': 'ephemeral', 'size_mib': 30, 'last_used_at': '2026-10-02', 'status': 'busy',
                   'description': 'Firmware'},
                  {'name': 'C', 'lifecycle': 'persistent', 'size_mib': 20, 'last_used_at': '2026-10-01', 'status': 'ready'}]
        names = lambda items: [w['name'] for w in items]
        self.assertEqual(names(backend.arrange(spaces)), ['a', 'b', 'C'])
        self.assertEqual(names(backend.arrange(spaces, 'size')), ['a', 'C', 'b'])
        self.assertEqual(names(backend.arrange(spaces, 'used')), ['a', 'C', 'b'])
        self.assertEqual(names(backend.arrange(spaces, kind='persistent')), ['b', 'C'])
        self.assertEqual(names(backend.arrange(spaces, kind='busy')), ['a'])
        self.assertEqual(names(backend.arrange(spaces, query='firm')), ['a'])

    def test_space_check_keeps_a_spare_gibibyte(self):
        gib = 1073741824
        self.assertIsNone(backend.space_problem(1024, None))
        self.assertIsNone(backend.space_problem(1024, 3 * gib))
        self.assertIn('Needs 2.0 GiB', backend.space_problem(2048, 2 * gib))

    def test_settings_round_trip_and_reject_bad_values(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            saved = os.environ.get('XDG_CONFIG_HOME')
            os.environ['XDG_CONFIG_HOME'] = temp
            try:
                self.assertEqual(backend.load_settings()['idle_minutes'], 15)
                settings = backend.load_settings()
                settings.update(idle_minutes=30, sort='size', history=['ls'])
                backend.save_settings(settings)
                self.assertEqual(backend.load_settings()['sort'], 'size')
                self.assertEqual(backend.load_settings()['history'], ['ls'])
                (Path(temp) / 'chrootables/settings.json').write_text('{"sort": "evil", "idle_minutes": "x", "history": [1, "ok"]}')
                loaded = backend.load_settings()
                self.assertEqual((loaded['sort'], loaded['idle_minutes'], loaded['history']), ('name', 15, ['ok']))
            finally:
                if saved is None:
                    os.environ.pop('XDG_CONFIG_HOME', None)
                else:
                    os.environ['XDG_CONFIG_HOME'] = saved

    def test_result_messages_prefer_the_manager_error(self):
        result = backend.Result(1, '', 'Creating x\nError: Not enough free space in /var\n')
        self.assertEqual(result.message(), 'Not enough free space in /var')



class ManagerContract(unittest.TestCase):
    """The JSON the window depends on, produced by the real manager."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='chrootables-test-', dir=SCRATCH)
        cls.store = make_store(Path(cls.temp.name) / 'store')
        os.environ['CHROOTABLE_HOME'] = str(cls.store)
        cls.manager = backend.Manager(MANAGER)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop('CHROOTABLE_HOME', None)
        cls.temp.cleanup()

    def test_manager_script_is_valid_bash(self):
        subprocess.run(['bash', '-n', str(MANAGER)], check=True)

    def test_cube_strap_listing_health_and_logs_use_the_copied_manager(self):
        workspaces = self.manager.workspaces()
        self.assertEqual([w['name'] for w in workspaces], ['build-lab', 'network-kit', 'python-playground'])
        lab = workspaces[0]
        self.assertEqual((lab['profile'], lab['lifecycle'], lab['size_mib'], lab['snapshots']), ('build', 'persistent', 4096, 2))
        self.assertEqual(lab['suite'], 'stable')
        self.assertEqual(workspaces[1]['origin'], 'build-lab')
        self.assertIn('Host health', self.manager.doctor())
        self.assertIn('clone log', self.manager.logs('build-lab'))
        self.assertEqual(backend.summarize(workspaces)['ephemeral'], 2)

    def test_privileged_commands_report_the_manager_error(self):
        # Through a sudo that runs the command unprivileged, the manager itself refuses.
        fake = Path(self.temp.name) / 'sudo-pass'
        fake.write_text('#!/bin/sh\n[ "$1" = -n ] && shift; [ "$1" = -- ] && shift; exec "$@"\n')
        fake.chmod(0o755)
        manager = backend.Manager(MANAGER, sudo=str(fake))
        with self.assertRaises(backend.ManagerError) as caught:
            manager.change(['--delete', 'build-lab', '--yes'])
        self.assertIn('needs root', str(caught.exception))


class SudoFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chrootables-sudo-', dir=SCRATCH)
        temp = self.dir = Path(self.temp.name)
        self.readable = make_store(temp / 'readable')
        self.private = make_store(temp / 'private')
        self.private.chmod(0)
        self.sudo = temp / 'sudo'
        self.sudo.write_text(FAKE_SUDO)
        self.sudo.chmod(0o755)
        env = dict(CHROOTABLE_HOME=str(self.private), FAKE_SUDO_DIR=str(temp), FAKE_SUDO_STORE=str(self.readable))
        self.saved = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            if manager.broker:
                manager.broker.close(wait=5)
        self.private.chmod(0o700)
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def manager(self, path=MANAGER, **options):
        manager = backend.Manager(path, sudo=str(self.sudo), **options)
        self.managers.append(manager)
        return manager

    def test_one_password_unlocks_every_command_even_when_sudo_remembers_nothing(self):
        manager = self.manager()
        with self.assertRaises(backend.AuthRequired):
            manager.workspaces()
        self.assertTrue(manager.private_store)
        self.assertFalse(manager.authenticated())
        self.assertFalse(manager.authenticate('wrong'))
        self.assertFalse(manager.unlocked())
        self.assertTrue(manager.authenticate('secret'))
        # The regression: with timestamp_timeout=0 every later command used to ask again.
        for _ in range(3):
            self.assertEqual(len(manager.workspaces()), 3)
        # Concurrent requests share the one helper and get their own answers.
        results = {}
        threads = [threading.Thread(target=lambda n=n: results.__setitem__(n, manager.workspaces()))
                   for n in ('build-lab', 'network-kit', 'python-playground')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({n: len(v) for n, v in results.items()}, {'build-lab': 3, 'network-kit': 3, 'python-playground': 3})
        self.assertEqual(len((self.dir / 'helpers').read_text().split()), 1, 'more than one sudo for one unlock')
        # sudo resets the environment: a custom store is passed explicitly.
        self.assertIn(f'--store {self.private}', (self.dir / 'last-command').read_text())
        self.assertIn('broker.py', (self.dir / 'last-command').read_text())

    def test_lock_stops_the_helper_and_the_store_asks_again(self):
        manager = self.manager()
        closed = []
        manager.on_lock = closed.append
        self.assertTrue(manager.authenticate('secret'))
        self.assertTrue(manager.unlocked())
        self.assertTrue(manager.lock())
        self.assertFalse(manager.unlocked())
        with self.assertRaises(backend.AuthRequired):
            manager.workspaces()

    def test_passive_reads_never_start_the_helper(self):
        manager = self.manager()
        with self.assertRaises(backend.AuthRequired):
            manager.workspaces()
        with self.assertRaises(backend.AuthRequired):
            manager.workspaces(passive=True)
        self.assertFalse((self.dir / 'helpers').exists())

    def test_cached_sudo_starts_the_helper_without_a_password(self):
        (self.dir / 'cached').touch()
        manager = self.manager()
        self.assertEqual(len(manager.workspaces()), 3)
        self.assertTrue(manager.unlocked())

    def test_idle_helper_exits_and_reports_it(self):
        manager = self.manager(idle=1)
        closed = threading.Event()
        reasons = []
        manager.on_lock = lambda reason: (reasons.append(reason), closed.set())
        self.assertTrue(manager.authenticate('secret'))
        self.assertTrue(closed.wait(15), 'the idle helper never exited')
        self.assertEqual(reasons, ['idle'])
        self.assertFalse(manager.unlocked())

    def test_cancel_stops_a_running_command_and_lets_it_clean_up(self):
        slow = self.dir / 'slow-manager'
        slow.write_text(SLOW_MANAGER)
        slow.chmod(0o755)
        manager = self.manager(slow)
        self.assertTrue(manager.authenticate('secret'))
        lines, outcome = [], {}

        def work():
            try:
                manager.change(['--slow'], on_line=lines.append)
            except backend.ManagerError as error:
                outcome['error'] = error
        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.monotonic() + 10
        while 'working' not in lines and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(manager.cancel())
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome['error'].status, 143)
        self.assertIn('cleaned up', lines)
        # merge streams stdout lines too
        streamed = []
        result = manager.change(['--echo', 'a b', 'c'], on_line=streamed.append, merge=True)
        self.assertEqual(streamed, ['a b', 'c', 'to stderr'])
        self.assertIn('a b', result.stdout)

    def test_helper_refuses_malformed_requests_and_stray_input(self):
        manager = self.manager()
        self.assertTrue(manager.authenticate('secret'))
        broker = manager.broker
        with broker._write_lock:
            broker.process.stdin.write('not json, maybe a password\n')
            broker.process.stdin.flush()
        self.assertEqual(broker.request({'run': ['no-dashes']}).status, 64)
        self.assertEqual(broker.request({'run': 'string'}).status, 64)
        self.assertEqual(broker.request({'run': ['--list', 'x\0y']}).status, 64)
        self.assertEqual(broker.request({'ping': True}).stdout, 'pong')


class CopiedApplicationContract(unittest.TestCase):
    def test_launcher_reports_copied_application_version(self):
        from chrootables import __version__
        result = subprocess.run([str(ROOT / 'bin/cube-chrootables'), '--version'],
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(result.stdout.strip(), __version__)

    def test_launcher_without_desktop_reports_failure_without_traceback(self):
        environment = dict(os.environ)
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'QT_QPA_PLATFORM'):
            environment.pop(key, None)
        result = subprocess.run([str(ROOT / 'bin/cube-chrootables')], env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('desktop', result.stderr.lower())
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('Gtk-CRITICAL', result.stderr)

    def test_manager_capabilities_match_copied_application(self):
        from chrootables.backend import CAPABILITIES, MANAGER as copied_manager, PROFILES
        self.assertEqual(copied_manager, MANAGER)
        report = subprocess.run([str(MANAGER), '--capabilities', '--json'],
                                capture_output=True, text=True, check=True, timeout=10)
        capabilities = json.loads(report.stdout)
        self.assertEqual(capabilities, CAPABILITIES)
        self.assertEqual(capabilities['schema_version'], 1)
        self.assertEqual(set(PROFILES), {'minimal', 'build', 'python', 'network', 'rust', 'go', 'web'})

    def test_read_only_helper_reads_absent_store_without_creating_it(self):
        with tempfile.TemporaryDirectory(prefix='cube-chrootables-helper-', dir=SCRATCH) as temporary:
            store = Path(temporary) / 'absent-store'
            requests = ''.join(json.dumps(request) + '\n' for request in (
                {'id': 1, 'action': 'inventory'},
                {'id': 2, 'action': 'doctor'},
                {'id': 3, 'action': 'delete', 'params': {'name': 'lab', 'confirmed': True}},
            ))
            result = subprocess.run([sys.executable, '-I', '-S', str(APP / 'chrootables/broker.py'),
                                     '--store', str(store)], input=requests,
                                    env={**os.environ, 'CHROOTABLE_AUTO_INSTALL': '0'},
                                    capture_output=True, text=True, check=True, timeout=20)
            messages = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(messages[0], {'event': 'ready', 'writable': False})
            responses = {message['id']: message for message in messages if message.get('event') == 'result'}
            self.assertTrue(responses[1]['ok'], responses[1])
            self.assertEqual(responses[1]['data']['images'], [])
            self.assertTrue(responses[2]['ok'], responses[2])
            self.assertIsInstance(responses[2]['data']['ready'], bool)
            self.assertFalse(responses[3]['ok'], responses[3])
            self.assertEqual(responses[3]['code'], 'permission_required')
            self.assertFalse(store.exists(), 'reading an absent store created it')


class CubeFrontendIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance() or QApplication([])

    def test_window_uses_cube_identity_and_separate_preferences(self):
        from unittest.mock import patch
        from PySide6.QtCore import QSettings
        from chrootables.app import Window
        with tempfile.TemporaryDirectory(prefix='cube-chrootables-settings-', dir=SCRATCH) as temporary:
            settings = QSettings(str(Path(temporary) / 'test.ini'), QSettings.Format.IniFormat)
            with patch('chrootables.app.QSettings', return_value=settings) as factory:
                window = Window(demo=True)
            try:
                factory.assert_called_once_with('whitecube', 'cube-chrootables')
                self.assertEqual(window.windowTitle(), 'Cube Chrootables')
                self.assertGreater(window.workspaces.count(), 0)
            finally:
                window.close()
                self.application.processEvents()
            self.assertFalse(Path(settings.fileName()).exists(), 'demo wrote desktop preferences')

    def test_application_sets_cube_desktop_identity(self):
        from unittest.mock import patch
        from chrootables import app
        with patch.object(sys, 'argv', ['cube-chrootables', '--demo']), \
                patch.object(app, 'QApplication') as application, patch.object(app, 'Window') as window:
            application.return_value.exec.return_value = 0
            self.assertEqual(app.main(), 0)
        application.return_value.setApplicationName.assert_called_once_with('Cube Chrootables')
        application.return_value.setDesktopFileName.assert_called_once_with('cube-chrootables')
        window.return_value.show.assert_called_once()

    def test_root_launch_is_rejected_before_qt_initialization(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from unittest.mock import patch
        from chrootables import app
        error = StringIO()
        with patch.object(sys, 'argv', ['cube-chrootables']), \
                patch.dict(os.environ, {'QT_QPA_PLATFORM': 'wayland', 'WAYLAND_DISPLAY': 'wayland-0'}), \
                patch.object(app.os, 'geteuid', return_value=0), \
                patch.object(app, 'QApplication') as application, redirect_stderr(error):
            self.assertEqual(app.main(), 1)
        application.assert_not_called()
        self.assertIn('normal desktop user', error.getvalue())

    def test_read_only_window_uses_real_async_helper_for_absent_store(self):
        from unittest.mock import patch
        from PySide6.QtCore import QSettings
        from chrootables.app import Window
        with tempfile.TemporaryDirectory(prefix='cube-chrootables-async-', dir=SCRATCH) as temporary:
            store = Path(temporary) / 'absent-store'
            settings = QSettings(str(Path(temporary) / 'test.ini'), QSettings.Format.IniFormat)
            with patch('chrootables.app.QSettings', return_value=settings):
                window = Window(store)
            try:
                window.show()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    self.application.processEvents()
                    if window.client.is_ready and window.metrics[0].text() == '0':
                        break
                    time.sleep(.01)
                self.assertTrue(window.client.is_ready, 'read-only helper did not start')
                self.assertEqual(window.metrics[0].text(), '0')
                self.assertFalse(window.client.writable)
                self.assertEqual(window.library_stack.currentIndex(), 1)
                self.assertFalse(store.exists())
            finally:
                window.close()
                self.application.processEvents()

    def test_demo_filter_navigation_and_create_validation(self):
        from unittest.mock import patch
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QDialog
        from chrootables.app import CreateDialog, Window
        with tempfile.TemporaryDirectory(prefix='cube-chrootables-actions-', dir=SCRATCH) as temporary:
            settings = QSettings(str(Path(temporary) / 'test.ini'), QSettings.Format.IniFormat)
            with patch('chrootables.app.QSettings', return_value=settings):
                window = Window(demo=True)
            try:
                window.search.setText('python')
                self.assertEqual(window.workspaces.count(), 1)
                self.assertEqual(window.selected, 'python-playground')
                window.navigate(1)
                self.assertEqual(window.recovery_table.rowCount(), 3)
                self.assertFalse(window.restore_button.isEnabled())
                window.navigate(2)
                self.assertEqual(window.activity_table.rowCount(), 3)
                dialog = CreateDialog({'exists'}, window)
                try:
                    dialog.name_input.setText('exists')
                    dialog.validate()
                    self.assertIn('already', dialog.error.text())
                    dialog.name_input.setText('new-python')
                    dialog.profile.setCurrentIndex(2)
                    self.assertEqual(dialog.size_input.value(), 4)
                    dialog.validate()
                    self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                    self.assertEqual(dialog.parameters()['profile'], 'python')
                finally:
                    dialog.close()
            finally:
                window.close()
                self.application.processEvents()


class OptionalQtTestCoverage(unittest.TestCase):
    @unittest.skip('PySide6.QtTest is unavailable; install python3-pyside6.qttest or use full PySide6')
    def test_additional_desktop_and_terminal_interactions(self):
        pass


def run_gui(screenshots):
    """Render the real launcher without connecting to or modifying the desktop."""
    screenshots = Path(screenshots).resolve()
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cube-chrootables-ui-', dir=SCRATCH) as temporary:
        fixture = Path(temporary)
        store = fixture / 'absent-store'
        environment = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                       'CHROOTABLE_AUTO_INSTALL': '0', 'CHROOTABLE_HOME': str(store),
                       'HOME': str(fixture / 'home'), 'XDG_CONFIG_HOME': str(fixture / 'config'),
                       'XDG_DATA_HOME': str(fixture / 'data'), 'XDG_STATE_HOME': str(fixture / 'state'),
                       'XDG_CACHE_HOME': str(fixture / 'cache')}
        for option, name in (('--demo', 'cube-chrootables.png'), (None, 'cube-chrootables-empty.png')):
            image = screenshots / name
            image.unlink(missing_ok=True)
            command = [str(ROOT / 'bin/cube-chrootables'), '--store', str(store), '--screenshot', str(image)]
            if option:
                command.append(option)
            result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
            if result.returncode or not image.is_file() or image.stat().st_size < 4096:
                raise SystemExit(f'FAIL: Qt desktop did not render {name}:\n{result.stdout}{result.stderr}')
        if store.exists():
            raise SystemExit('FAIL: launching the desktop created an absent store')
        print('PASS: copied Qt desktop demo and absent store rendered through cube-chrootables')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT / 'build')
    parser.add_argument('--no-gui', action='store_true', help='Only backend, manager, and helper checks')
    args = parser.parse_args()
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(case) for case in (
        BackendRules, ManagerContract, SudoFlow, CopiedApplicationContract))
    patterns = ['test_backend.py', 'test_shared_folder.py', 'test_authenticate.py',
                'test_cli_operations.py', 'test_cli_stability.py', 'test_paths.py']
    if not args.no_gui:
        patterns += ['test_client.py', 'test_capture.py']
        suite.addTests(loader.loadTestsFromTestCase(CubeFrontendIntegration))
        try:
            from PySide6.QtTest import QTest
        except ImportError:
            suite.addTests(loader.loadTestsFromTestCase(OptionalQtTestCoverage))
        else:
            patterns += ['test_desktop.py', 'test_terminal.py']
    for pattern in patterns:
        suite.addTests(loader.discover(str(APP / 'tests'), pattern=pattern))
    outcome = unittest.TextTestRunner(verbosity=1).run(suite)
    if not outcome.wasSuccessful():
        raise SystemExit(1)
    if not args.no_gui:
        run_gui(args.screenshots)
