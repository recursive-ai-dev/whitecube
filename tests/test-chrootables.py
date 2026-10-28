#!/usr/bin/python3 -I
"""Chrootables checks: backend rules, the manager's JSON contract, the sudo flow, and a
GUI smoke run on a private, disposable Wayland compositor.

Uses a fixture store owned by the test user and a fake sudo, so it needs no root,
downloads nothing and never touches /var/lib/chrootable-manager.
Run after BUILD.sh; --screenshots chooses the directory for preview artifacts.
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
import traceback
import unittest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / 'apps/chrootables'
MANAGER = APP / 'chrootable-manager'
PAGES = ('workspaces', 'create', 'import', 'run', 'activity', 'health')
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
# never remembers a password (`sudo -n` always refuses) unless FAKE_SUDO_CACHE is set.
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


class Icons(unittest.TestCase):
    def test_every_icon_the_window_names_exists_in_the_theme(self):
        themes = [Path('/usr/share/icons/Adwaita'), Path('/usr/share/icons/hicolor')]
        if not themes[0].is_dir():
            self.skipTest('the Adwaita icon theme is not installed')
        import re
        names = set(re.findall(r"'([a-z-]+-symbolic)'", (APP / 'app.py').read_text()))
        missing = [name for name in sorted(names)
                   if not any(any(theme.rglob(f'{name}.svg')) or any(theme.rglob(f'{name}.png')) for theme in themes)]
        self.assertEqual(missing, [])


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

    def test_listing_details_snapshots_exports_activity_and_profiles(self):
        workspaces = self.manager.workspaces()
        self.assertEqual([w['name'] for w in workspaces], ['build-lab', 'network-kit', 'python-playground'])
        lab = workspaces[0]
        self.assertEqual((lab['profile'], lab['lifecycle'], lab['size_mib'], lab['snapshots']), ('build', 'persistent', 4096, 2))
        self.assertEqual(lab['description'], 'Compiler toolchain for the firmware project')
        self.assertEqual(lab['release'], 'Debian GNU/Linux 13 (trixie)')
        self.assertEqual(workspaces[1]['origin'], 'build-lab')
        self.assertEqual(sorted(s['label'] for s in self.manager.snapshots('build-lab')), ['before-upgrade', 'clean-base'])
        exports = self.manager.exports()
        self.assertEqual((exports[0]['workspace'], exports[0]['sha256']), ('build-lab', 'abc123'))
        activity = self.manager.activity()
        self.assertEqual(activity[0]['name'], 'broken')
        self.assertIn('exit 1', [entry['status'] for entry in activity])
        self.assertEqual([p['name'] for p in self.manager.profiles()], ['minimal', 'build', 'python', 'network'])
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
        self.assertEqual(len(manager.snapshots('build-lab')), 2)
        # Concurrent requests share the one helper and get their own answers.
        results = {}
        threads = [threading.Thread(target=lambda n=n: results.__setitem__(n, manager.snapshots(n)))
                   for n in ('build-lab', 'network-kit', 'python-playground')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({n: len(v) for n, v in results.items()}, {'build-lab': 2, 'network-kit': 0, 'python-playground': 0})
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


def inside_gui(fixture, screenshot):
    """Runs inside the private compositor: exercise the window, then save a screenshot."""
    import app
    fixture = Path(fixture)
    options = argparse.Namespace(page=None, screenshot=None)

    class SmokeApp(app.ChrootablesApp):
        failure = None

        def do_activate(self):
            super().do_activate()
            self.deadline = time.monotonic() + 10
            app.GLib.timeout_add(100, self.guarded, self.loaded)

        def guarded(self, step):
            try:
                return step()
            except BaseException:
                self.failure = traceback.format_exc()
                print(self.failure, file=sys.stderr)
                self.quit()
                return False

        def loaded(self):
            w = self.window
            if len(w.workspaces) < 3 or len(w.profile_buttons) < 4:
                assert time.monotonic() < self.deadline, 'workspaces or profiles never loaded'
                return True
            assert not w.lock_card.get_visible() and w.split.get_visible()
            assert w.selected == 'build-lab'
            row = w.workspace_list.get_row_at_index(1)
            w.workspace_list.select_row(row)
            assert w.selected == 'network-kit'
            w.filter.set_text('firmware')
            w.fill_list()
            assert w.workspace_list.get_row_at_index(0).workspace == 'build-lab'
            assert w.workspace_list.get_row_at_index(1) is None
            w.filter.set_text('')
            w.fill_list()
            w.kind_buttons['ephemeral'].set_active(True)
            assert w.list_count.get_text() == '2 of 3', w.list_count.get_text()
            w.kind_buttons['all'].set_active(True)
            w.sort_choice.set_selected(list(app.backend.SORTS).index('size'))
            assert [w.workspace_list.get_row_at_index(i).workspace for i in range(3)][0] in ('build-lab', 'python-playground')
            w.sort_choice.set_selected(0)
            w.navigate('run')
            model = w.run_target.get_model()
            assert [model.get_string(i) for i in range(model.get_n_items())] == ['build-lab', 'network-kit', 'python-playground']
            w.select_run_target('network-kit')
            assert w.run_target_name() == 'network-kit' and 'vanish' in w.run_mode.get_text()
            assert not w.run_button.get_sensitive()
            w.run_command.set_text('uname -a')
            assert w.run_button.get_sensitive()
            w.console_toggle.set_active(True)
            assert w.console_revealer.get_reveal_child()
            w.console_toggle.set_active(False)
            w.navigate('create')
            assert w.create_name.get_text() == 'minimal-lab'
            w.profile_buttons['build'].set_active(True)
            assert w.create_size.get_value() == 4096
            assert w.create_size.get_adjustment().get_lower() == 1536
            assert w.create_name.get_text() == 'build-lab-2', w.create_name.get_text()
            w.create_name.set_text('build-lab')
            assert not w.create_button.get_sensitive() and 'already exists' in w.create_hint.get_text()
            w.create_name.set_text('bad name')
            assert not w.create_button.get_sensitive()
            w.create_name.set_text('firmware')
            assert w.create_button.get_sensitive()
            w.navigate('import')
            assert not w.import_button.get_sensitive()
            w.use_archive(fixture / 'store/exports/build-lab-20261008-101500-Qx7Lm2.tar.gz')
            assert w.import_name.get_text() == 'build-lab-2' and w.import_button.get_sensitive()
            w.navigate('activity')
            for page in PAGES:
                assert w.on_key(None, app.Gdk.KEY_1 + PAGES.index(page), 0, app.Gdk.ModifierType.CONTROL_MASK)
                assert w.stack.get_visible_child_name() == page
            w.navigate('workspaces')
            w.workspace_list.select_row(w.workspace_list.get_row_at_index(0))
            app.GLib.timeout_add(100, self.guarded, self.details)
            return False

        def details(self):
            w = self.window
            texts = []

            def walk(widget):
                if isinstance(widget, app.Gtk.Label):
                    texts.append(widget.get_text())
                child = widget.get_first_child()
                while child:
                    walk(child)
                    child = child.get_next_sibling()
            walk(w.detail)
            if 'clean-base' not in texts:
                assert time.monotonic() < self.deadline, 'snapshots never loaded'
                return True
            assert 'Compiler toolchain for the firmware project' in texts and 'Open shell' in texts
            assert any('trixie' in text for text in texts)
            w.set_busy(True, 'Fixture operation')
            assert not any(widget.get_sensitive() for widget in w.action_widgets)
            assert not w.create_button.get_sensitive() and w.cancel_button.get_visible()
            w.start_job('Queued fixture', ['--list'])
            assert len(w.queue) == 1 and w.queue_label.get_visible()
            w.drop_queue('Fixture')
            assert not w.queue and not w.queue_label.get_visible()
            w.set_busy(False, 'Ready.')
            assert not w.cancel_button.get_visible()
            assert all(widget.get_sensitive() for widget in w.action_widgets)
            self.options.screenshot = Path(screenshot)
            app.GLib.timeout_add(400, self.screenshot)
            print('PASS: workspace list, selection, filter, kind and sort, run page, console, queue, details and snapshots, create validation and profile sizing, '
                  'import suggestions, page shortcuts and busy controls')
            return False

    application = SmokeApp(options)
    result = application.run([sys.argv[0]])
    return 1 if application.failure else result


def inside_locked(fixture, screenshot):
    """A private store: the lock card shows, a wrong password keeps it, the right one unlocks."""
    import app
    options = argparse.Namespace(page=None, screenshot=None)

    class LockedApp(app.ChrootablesApp):
        failure = None

        def do_activate(self):
            super().do_activate()
            self.deadline = time.monotonic() + 10
            self.stage = 'locked'
            app.GLib.timeout_add(100, self.step)

        def step(self):
            try:
                w = self.window
                assert time.monotonic() < self.deadline, f'stuck at {self.stage}'
                if self.stage == 'locked':
                    if not w.lock_card.get_visible():
                        return True
                    assert w.unlock_button.get_visible() and not w.split.get_visible()
                    w.password.set_text('wrong')
                    w.unlock_with(w.password)
                    self.stage = 'wrong'
                    self.wait_until = time.monotonic() + 1.5
                    return True
                if self.stage == 'wrong':
                    if time.monotonic() < self.wait_until:
                        return True
                    assert w.lock_card.get_visible(), 'a wrong password unlocked the store'
                    w.password.set_text('secret')
                    w.unlock_with(w.password)
                    self.stage = 'unlocking'
                    return True
                if self.stage == 'unlocking':
                    if w.lock_card.get_visible() or len(w.workspaces) < 3:
                        return True
                    assert w.access_label.get_text().startswith('Private store · unlocked'), w.access_label.get_text()
                    assert w.lock_button.get_visible() and not w.unlock_button.get_visible()
                    print('PASS: private store locks, rejects a wrong password and unlocks with the right one, '
                          'with a sudo that remembers nothing (timestamp_timeout=0)')
                    self.options.screenshot = Path(screenshot)
                    app.GLib.timeout_add(300, self.screenshot)
                    return False
            except BaseException:
                self.failure = traceback.format_exc()
                print(self.failure, file=sys.stderr)
                self.quit()
            return False

    application = LockedApp(options)
    result = application.run([sys.argv[0]])
    return 1 if application.failure else result


def run_gui(screenshots):
    compositor_binary = ROOT / 'build/whitecube'
    if not compositor_binary.is_file():
        print('SKIP: build/whitecube is missing; run BUILD.sh for the GUI smoke test')
        return
    screenshots = Path(screenshots).resolve()
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='chrootables-ui-', dir=SCRATCH) as temp:
        fixture = Path(temp)
        for name in ('home', 'runtime', 'state', 'data', 'cache'):
            (fixture / name).mkdir(mode=0o700)
        store = make_store(fixture / 'store')
        private = make_store(fixture / 'private')
        sudo = fixture / 'sudo'
        sudo.write_text(FAKE_SUDO)
        sudo.chmod(0o755)
        env = dict(os.environ, HOME=str(fixture / 'home'), XDG_RUNTIME_DIR=str(fixture / 'runtime'),
                   XDG_CONFIG_HOME=str(fixture / 'home/.config'), XDG_STATE_HOME=str(fixture / 'state'),
                   XDG_DATA_HOME=str(fixture / 'data'), XDG_CACHE_HOME=str(fixture / 'cache'),
                   GSETTINGS_BACKEND='memory', GSETTINGS_SCHEMA_DIR=str(ROOT / 'build/schemas'),
                   WLR_BACKENDS='headless', WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='1',
                   GDK_BACKEND='wayland', GSK_RENDERER='cairo', NO_AT_BRIDGE='1',
                   CHROOTABLE_HOME=str(store), CHROOTABLES_MANAGER=str(MANAGER), CHROOTABLES_SUDO=str(sudo),
                   FAKE_SUDO_DIR=str(fixture), FAKE_SUDO_STORE=str(store))
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
            env.pop(key, None)
        with (fixture / 'compositor.log').open('w') as log:
            compositor = subprocess.Popen([str(compositor_binary)], env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 8
                socket = fixture / 'runtime/wayland-0'
                while not socket.exists() and compositor.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                if not socket.exists():
                    raise SystemExit('FAIL: the private compositor did not start: ' + (fixture / 'compositor.log').read_text()[-800:])
                env['WAYLAND_DISPLAY'] = socket.name
                for page in PAGES:
                    image = screenshots / f'chrootables-{page}.png'
                    image.unlink(missing_ok=True)
                    result = subprocess.run([str(ROOT / 'bin/chrootables'), '--page', page, '--screenshot', str(image)],
                                            env=env, capture_output=True, text=True, timeout=20)
                    if result.returncode or not image.is_file() or image.stat().st_size < 4096:
                        raise SystemExit(f'FAIL: page {page} did not render:\n{result.stdout}{result.stderr}')
                for mode, target, extra in (('--inside', 'chrootables-details.png', {}),
                                            ('--inside-locked', 'chrootables-locked.png', {'CHROOTABLE_HOME': str(private)})):
                    image = screenshots / target
                    image.unlink(missing_ok=True)
                    if extra:
                        private.chmod(0)
                    try:
                        result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), mode, str(fixture), str(image)],
                                                env=dict(env, **extra), capture_output=True, text=True, timeout=30)
                    finally:
                        private.chmod(0o700)
                    print(result.stdout.rstrip())
                    if result.returncode or not image.is_file():
                        raise SystemExit(f'FAIL: {mode} smoke run:\n{result.stdout}{result.stderr}')
                print(f'PASS: all {len(PAGES)} Chrootables pages rendered on an isolated headless Wayland session')
            finally:
                if compositor.poll() is None:
                    compositor.terminate()
                try:
                    compositor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    compositor.kill()
                    compositor.wait()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('--inside', '--inside-locked'):
        sys.path.insert(0, str(APP))
        runner = inside_gui if sys.argv[1] == '--inside' else inside_locked
        raise SystemExit(runner(sys.argv[2], sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT / 'build')
    parser.add_argument('--no-gui', action='store_true', help='Only the backend and manager checks')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    outcome = unittest.TextTestRunner(verbosity=1).run(suite)
    if not outcome.wasSuccessful():
        raise SystemExit(1)
    if not args.no_gui:
        run_gui(args.screenshots)
