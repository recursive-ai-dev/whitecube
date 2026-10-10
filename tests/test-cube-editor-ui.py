#!/usr/bin/python3 -I
"""Cube Editor GUI smoke checks on a private, disposable Wayland compositor.

Creates small project/recipe fixtures, exercises safe UI actions, and renders all
six pages. It never opens an authenticated worker or connects to the user desktop.
Run after BUILD.sh; --screenshots chooses the directory for preview artifacts.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parent.parent
PAGES = ('source', 'customize', 'files', 'build', 'test', 'activity')


def inside_actions(fixture, screenshot):
    sys.path.insert(0, str(ROOT / 'apps/cube-editor'))
    import app
    import engine

    fixture = Path(fixture)
    source = fixture / 'fixture.iso'
    source.write_bytes(b'GUI fixture, never extracted or executed\n')
    project = engine.Project.create(source, 'GUI fixture', base_dir=fixture / 'projects', output_dir=fixture / 'release')
    options = argparse.Namespace(project=str(project.directory), page=None, screenshot=None)

    class SmokeApp(app.EditorApp):
        failure = None

        def do_activate(self):
            super().do_activate()
            app.GLib.timeout_add(250, self.exercise)

        def guarded(self, callback):
            try:
                return callback()
            except BaseException:
                self.failure = traceback.format_exc()
                print(self.failure, file=sys.stderr)
                self.quit()
                return False

        def exercise(self):
            return self.guarded(self.actions)

        def actions(self):
            w = self.window
            assert w.project.directory == project.directory
            assert not w.extract_button.get_sensitive(), 'Uninspected source unlocked extraction'
            w.project.save(inspection={'can_rebuild': True})
            w.hydrate()
            assert w.extract_button.get_sensitive()
            assert not w.build_button.get_sensitive()
            project.rootfs.mkdir()
            w.refresh_buttons()
            assert not w.build_button.get_sensitive(), 'Incomplete extraction unlocked build'
            (project.directory / '.extracted').touch()
            w.refresh_buttons()
            assert w.build_button.get_sensitive() and w.apply_button.get_sensitive()
            assert not w.extract_button.get_sensitive()
            for index, page in enumerate(PAGES):
                assert w.on_key(None, app.Gdk.KEY_1 + index, 0, app.Gdk.ModifierType.CONTROL_MASK)
                assert w.stack.get_visible_child_name() == page
            w.add_recipe('Tools', ['git', 'curl'], ['apt-get clean'])
            w.add_recipe('Tools again', ['git'], ['apt-get clean'])
            assert w.recipe()['install_packages'] == ['git', 'curl']
            assert w.recipe()['commands'] == ['apt-get clean']
            payload = fixture / 'hello.txt'
            payload.write_text('Inside the image\n')
            w.entries['payload_source'].set_text(str(payload))
            w.entries['payload_destination'].set_text('/etc/cube-test.txt')
            w.add_payload()
            assert len(w.payloads) == 1
            w.entries['payload_destination'].set_text('/etc/../outside')
            w.add_payload()
            assert len(w.payloads) == 1, 'Unsafe overlay accepted'
            w.remove_payload(0)
            assert not w.payloads
            desktop = fixture / 'desktop.deb'
            desktop.write_bytes(b'GUI-only desktop package fixture')
            assert w.on_drop(None, app.Gio.File.new_for_path(str(desktop)), 0, 0)
            assert w.inject_switch.get_active() and w.recipe()['desktop_deb'] == str(desktop)
            w.entries['name'].set_text('Saved from the GUI')
            w.autosave()
            restored = engine.Project.load(project.directory)
            assert restored.data['name'] == 'Saved from the GUI'
            assert restored.data['recipe']['install_packages'] == ['git', 'curl']
            w.navigate('build')
            assert 'mksquashfs' in w.text(w.plan_view) and 'xorriso' in w.text(w.plan_view)
            previous = w.recipe()
            w.set_busy(True, 'Fixture operation')
            assert not w.entries['name'].get_sensitive() and not w.build_button.get_sensitive()
            assert not w.on_drop(None, app.Gio.File.new_for_path(str(source)), 0, 0)
            w.add_recipe('Blocked', ['nano'], [])
            assert w.recipe() == previous
            w.set_busy(False)
            w.palette()
            dialog = w.get_visible_dialog()
            assert dialog is not None and dialog.get_title() == 'Commands'
            descendants = []

            def walk(widget):
                descendants.append(widget)
                child = widget.get_first_child()
                while child:
                    walk(child)
                    child = child.get_next_sibling()

            walk(dialog)
            search = next(widget for widget in descendants if isinstance(widget, app.Gtk.SearchEntry))
            listing = next(widget for widget in descendants if isinstance(widget, app.Gtk.ListBox))
            search.set_text('Copy recipe JSON')
            search.emit('search-changed')
            row = listing.get_first_child()
            assert row is not None and row.get_next_sibling() is None
            row.get_child().emit('clicked')
            assert w.get_display().get_clipboard().get_formats().to_string(), 'Copy action left clipboard empty'
            release = project.output_path
            release.parent.mkdir()
            release.write_bytes(b'GUI checksum fixture')
            Path(str(release) + '.sha256').write_text(hashlib.sha256(release.read_bytes()).hexdigest() + '  fixture.iso\n')
            w.refresh_release()
            assert w.verify_button.get_sensitive()
            w.verify_release()
            self.deadline = time.monotonic() + 5
            app.GLib.timeout_add(30, self.check_verified)
            return False

        def check_verified(self):
            return self.guarded(self.verified)

        def verified(self):
            w = self.window
            if w.busy:
                assert time.monotonic() < self.deadline, 'Checksum verification hung'
                return True
            assert w.status.get_text() == 'The release matches its SHA-256 checksum.'
            project.output_path.write_bytes(b'Modified ISO fixture')
            w.verify_release()
            self.deadline = time.monotonic() + 5
            app.GLib.timeout_add(30, self.check_mismatch)
            return False

        def check_mismatch(self):
            return self.guarded(self.mismatch)

        def mismatch(self):
            w = self.window
            if w.busy:
                assert time.monotonic() < self.deadline, 'Mismatch verification hung'
                return True
            assert w.status.get_text() == 'Release verification failed.'
            assert any('Checksum mismatch' in line for line in w.log_lines)
            w.navigate('customize')
            self.options.screenshot = Path(screenshot)
            app.GLib.timeout_add(350, self.screenshot)
            print('PASS: project hydration, extraction readiness, navigation shortcuts, recipe deduplication, safe overlays, desktop drop, autosave, build plan, busy controls, command palette, clipboard, and checksum success/mismatch')
            return False

    application = SmokeApp(options)
    result = application.run([sys.argv[0]])
    return 1 if application.failure else result


def run_smoke(screenshots):
    screenshots = Path(screenshots).resolve()
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cube-editor-ui-') as temp:
        fixture = Path(temp)
        for name in ('home', 'runtime', 'state', 'data', 'cache'):
            (fixture / name).mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(fixture / 'home'), XDG_RUNTIME_DIR=str(fixture / 'runtime'),
                   XDG_CONFIG_HOME=str(fixture / 'home/.config'), XDG_STATE_HOME=str(fixture / 'state'),
                   XDG_DATA_HOME=str(fixture / 'data'), XDG_CACHE_HOME=str(fixture / 'cache'),
                   GSETTINGS_BACKEND='memory', GSETTINGS_SCHEMA_DIR=str(ROOT / 'build/schemas'),
                   WLR_BACKENDS='headless', WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='1',
                   GDK_BACKEND='wayland', GSK_RENDERER='cairo', NO_AT_BRIDGE='1')
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
            env.pop(key, None)
        with (fixture / 'compositor.log').open('w') as log:
            compositor = subprocess.Popen([str(ROOT / 'build/whitecube')], env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 8
                socket = fixture / 'runtime/wayland-0'
                while not socket.exists() and compositor.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                assert socket.exists(), (fixture / 'compositor.log').read_text()
                env['WAYLAND_DISPLAY'] = socket.name
                for page in PAGES:
                    image = screenshots / f'cube-editor-{page}.png'
                    image.unlink(missing_ok=True)
                    result = subprocess.run([str(ROOT / 'bin/cube-editor'), '--page', page, '--screenshot', str(image)],
                                            env=env, capture_output=True, text=True, timeout=12)
                    assert result.returncode == 0 and 'Traceback' not in result.stderr, result.stderr
                    assert image.is_file() and image.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), f'{page} screenshot missing'
                image = screenshots / 'cube-editor-project.png'
                image.unlink(missing_ok=True)
                result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--inside',
                                         str(fixture), str(image)], env=env, capture_output=True, text=True, timeout=15)
                assert result.returncode == 0 and 'Traceback' not in result.stderr, result.stdout + result.stderr
                assert image.is_file(), 'Loaded-project screenshot missing'
                print(result.stdout.rstrip())
                print('PASS: all six GTK4 pages rendered on an isolated headless Wayland session')
            finally:
                if compositor.poll() is None:
                    compositor.terminate()
                try:
                    compositor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    compositor.kill()
                    compositor.wait()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--inside':
        raise SystemExit(inside_actions(sys.argv[2], sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT / 'build')
    args = parser.parse_args()
    run_smoke(args.screenshots)
