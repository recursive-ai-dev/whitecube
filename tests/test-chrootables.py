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
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / 'apps/chrootables'
MANAGER = APP / 'chrootable-manager'
PAGES = ('workspaces', 'create', 'import', 'activity', 'health')
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

# A stand-in for sudo: -S -v accepts "secret" and remembers it; -n refuses until then.
# Like real sudo (timestamp.c: "time stamp record sid mismatch") the memory only
# counts for callers in the same session; FAKE_SUDO_NO_CACHE models timestamp_timeout=0.
FAKE_SUDO = r'''#!/bin/sh
marker="$FAKE_SUDO_DIR/authenticated"
sid=$(cut -d' ' -f6 /proc/$$/stat)
case "$1" in
    -S) read -r password
        if [ "$password" = secret ]; then [ -n "$FAKE_SUDO_NO_CACHE" ] || echo "$sid" >"$marker"; exit 0; fi
        echo "Sorry, try again." >&2; exit 1 ;;
    -n) shift
        if [ "$(cat "$marker" 2>/dev/null)" != "$sid" ]; then echo "sudo: a password is required" >&2; exit 1; fi
        [ "$1" = -v ] && exit 0
        [ "$1" = -- ] && shift
        printf '%s\n' "$*" >"$FAKE_SUDO_DIR/last-command"
        # Root can read the private store; the readable copy stands in for it.
        if [ "$1" = /usr/bin/env ]; then shift 2; fi
        CHROOTABLE_HOME=$FAKE_SUDO_STORE exec "$@" ;;
esac
exit 64
'''


def make_store(path):
    subprocess.run(['bash', '-c', FIXTURE, 'fixture', str(MANAGER), str(path)], check=True)
    return Path(path)


class BackendRules(unittest.TestCase):
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
    def test_private_store_needs_a_password_once(self):
        with tempfile.TemporaryDirectory(prefix='chrootables-sudo-', dir=SCRATCH) as temp:
            temp = Path(temp)
            readable = make_store(temp / 'readable')
            private = make_store(temp / 'private')
            private.chmod(0)
            sudo = temp / 'sudo'
            sudo.write_text(FAKE_SUDO)
            sudo.chmod(0o755)
            env = dict(CHROOTABLE_HOME=str(private), FAKE_SUDO_DIR=str(temp), FAKE_SUDO_STORE=str(readable))
            saved = {key: os.environ.get(key) for key in env}
            os.environ.update(env)
            try:
                manager = backend.Manager(MANAGER, sudo=str(sudo))
                with self.assertRaises(backend.AuthRequired):
                    manager.workspaces()
                self.assertTrue(manager.private_store)
                self.assertFalse(manager.authenticated())
                self.assertFalse(manager.authenticate('wrong'))
                self.assertTrue(manager.authenticate('secret'))
                self.assertTrue(manager.authenticated())
                self.assertEqual(len(manager.workspaces()), 3)
                # sudo resets the environment: a custom store is passed explicitly.
                self.assertIn(f'/usr/bin/env CHROOTABLE_HOME={private} ', (temp / 'last-command').read_text())
                # A sudo that never keeps the password must not send the window round the dialog again.
                (temp / 'authenticated').unlink()
                os.environ['FAKE_SUDO_NO_CACHE'] = '1'
                with self.assertRaisesRegex(backend.ManagerError, 'will not remember'):
                    manager.authenticate('secret')
            finally:
                os.environ.pop('FAKE_SUDO_NO_CACHE', None)
                private.chmod(0o700)
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


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
            assert not w.create_button.get_sensitive()
            w.set_busy(False, 'Ready.')
            assert all(widget.get_sensitive() for widget in w.action_widgets)
            self.options.screenshot = Path(screenshot)
            app.GLib.timeout_add(400, self.screenshot)
            print('PASS: workspace list, selection, filter, details and snapshots, create validation and profile sizing, '
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
                    assert w.access_label.get_text() == 'Private store · unlocked'
                    print('PASS: private store locks, rejects a wrong password and unlocks with the right one')
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
