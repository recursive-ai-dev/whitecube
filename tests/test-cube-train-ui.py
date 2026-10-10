#!/usr/bin/python3 -I
"""Exercise the actual native GTK trainer on an isolated Wayland compositor."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def inside(fixture, screenshots):
    sys.path.insert(0, str(ROOT / 'apps/cube-train'))
    import app
    import engine
    fixture, screenshots = Path(fixture), Path(screenshots)
    trainer = engine.Trainer()
    run = trainer.runs_dir / 'fixture-run'
    (run / 'data').mkdir(parents=True)
    engine.atomic_json(run / 'run.json', {'steps': 10, 'model_size': '5m', 'parameters': 4_987_448,
                                          'display_name': 'Native fixture', 'batch_size': 1, 'grad_accum_steps': 1})
    engine.atomic_json(run / 'result.json', {'completed_steps': 4, 'best_val_loss': 2.4, 'stopped_early': True})
    for split in ('train', 'val'): (run / 'data' / f'{split}.bin').write_bytes(b'UI fixture data')
    (run / 'ckpt_best.pt').write_bytes(b'Never loaded or executed')
    (run / 'ckpt_latest.pt').write_bytes(b'Latest UI fixture')
    (run / 'console.log').write_text('Fixture log\nTraining paused after four steps.\n')
    (run / 'train_log.jsonl').write_text('\n'.join(json.dumps(item) for item in [
        {'step': 0, 'total_loss': 5.2}, {'step': 1, 'total_loss': 4.8}, {'step': 3, 'val_loss': 2.4}]))
    options = argparse.Namespace(page=None, screenshot=None, no_probe=True)

    class SmokeApp(app.TrainerApp):
        failure = None
        def do_activate(self):
            super().do_activate(); app.GLib.timeout_add(200, self.exercise)
        def guarded(self, function):
            try: return function()
            except BaseException:
                self.failure = traceback.format_exc(); print(self.failure, file=sys.stderr); self.quit(); return False
        def exercise(self): return self.guarded(self.actions)
        def actions(self):
            w = self.window
            assert not w.stop_button.get_sensitive()
            w.size_picker.set_selected(2); assert '4,987,448' in w.model_detail.get_text()
            w.size_picker.set_selected(3); assert '10,034,184' in w.model_detail.get_text()
            for index in range(3):
                assert w.key_pressed(None, app.Gdk.KEY_1 + index, 0, app.Gdk.ModifierType.CONTROL_MASK)
                assert w.stack.get_visible_child_name() == ('train', 'runs', 'activity')[index]
            w.run_list.select_row(w.run_list.get_row_at_index(0))
            assert w.selected_id == 'fixture-run'
            assert w.resume_button.get_sensitive() and w.best_button.get_sensitive()
            assert w.latest_button.get_sensitive() and w.delete_button.get_sensitive()
            assert '2.4000' in w.run_detail.get_text()
            assert w.resume_steps.get_value() > 4
            assert 'Fixture log' in w.last_log and len(w.losses) == 3
            w.copy_path(); assert w.get_display().get_clipboard().get_formats().to_string()
            w.rename()
            dialog = w.get_visible_dialog(); assert dialog
            found = []
            def walk(widget):
                if isinstance(widget, app.Gtk.Entry): found.append(widget)
                child = widget.get_first_child()
                while child: walk(child); child = child.get_next_sibling()
            walk(dialog)
            found[0].set_text('Renamed native run'); found[0].emit('activate')
            assert trainer.find_run('fixture-run')['name'] == 'Renamed native run'
            assert trainer.chat_command('fixture-run', 'best')[-1] == 'fixture-run/ckpt_best.pt'
            report = trainer.report('fixture-run'); assert report['configuration']['model_size'] == '5m'
            w.form['steps'].set_value(3000); w.save_draft(); assert trainer.preferences()['steps'] == 3000
            w.form['stagger_training'].set_active(True); w.form['low_priority'].set_active(True)
            w.form['burst_seconds'].set_value(20); w.form['cooldown_seconds'].set_value(15)
            w.save_draft()
            prefs = trainer.preferences()
            assert prefs['stagger_training'] and prefs['low_priority']
            assert prefs['burst_seconds'] == 20 and prefs['cooldown_seconds'] == 15
            app.GLib.timeout_add(250, self.confirmation)
            return False
        def confirmation(self): return self.guarded(self.confirmation_guarded)
        def confirmation_guarded(self):
            w = self.window
            w.delete(); dialog = w.get_visible_dialog(); assert dialog
            widgets = []
            def walk(widget):
                widgets.append(widget)
                child = widget.get_first_child()
                while child: walk(child); child = child.get_next_sibling()
            walk(dialog)
            confirm = next(widget for widget in widgets if isinstance(widget, app.Gtk.CheckButton))
            remove = next(widget for widget in widgets if isinstance(widget, app.Gtk.Button) and widget.get_label() == 'Move to Trash')
            assert not remove.get_sensitive()
            confirm.set_active(True); assert remove.get_sensitive()
            confirm.set_active(False); assert not remove.get_sensitive()
            assert not w.key_pressed(None, app.Gdk.KEY_Return, 0, app.Gdk.ModifierType.CONTROL_MASK)
            dialog.close(); assert (run / 'ckpt_latest.pt').exists()
            w.form['dataset'].set_text(str(fixture / 'missing-dataset'))
            w.start()
            self.deadline = time.monotonic() + 5
            app.GLib.timeout_add(20, self.check_error)
            return False
        def check_error(self): return self.guarded(self.error_ready)
        def error_ready(self):
            w = self.window
            if w.launching:
                assert time.monotonic() < self.deadline; return True
            assert 'existing corpus' in w.status.get_text(), w.status.get_text()
            assert w.start_button.get_sensitive() and not w.stop_button.get_sensitive()
            self.pages = iter(('train', 'runs', 'activity'))
            self.capture_next(); return False
        def capture_next(self):
            try: self.page = next(self.pages)
            except StopIteration:
                print('PASS: native model options, run selection, rename, resume targets, deletion confirmation, cooling controls, loss graph, logs, saved settings, shortcuts, report and Chat commands, invalid-start recovery')
                self.quit(); return False
            self.window.stack.set_visible_child_name(self.page)
            app.GLib.timeout_add(350, self.capture); return False
        def capture(self):
            return self.guarded(self.capture_guarded)
        def capture_guarded(self):
            app.screenshot(self.window, str(screenshots / f'cube-train-{self.page}.png'))
            self.capture_next(); return False

    application = SmokeApp(options, trainer)
    result = application.run([sys.argv[0]])
    return 1 if application.failure else result


def run(screenshots):
    screenshots = Path(screenshots).resolve(); screenshots.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(prefix='whitecube-trainer-ui-') as temp:
        fixture = Path(temp)
        for name in ('home', 'runtime', 'state', 'data', 'cache'): (fixture / name).mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(fixture / 'home'), XDG_RUNTIME_DIR=str(fixture / 'runtime'),
                   XDG_CONFIG_HOME=str(fixture / 'home/.config'), XDG_STATE_HOME=str(fixture / 'state'),
                   XDG_DATA_HOME=str(fixture / 'data'), XDG_CACHE_HOME=str(fixture / 'cache'),
                   WLR_BACKENDS='headless', WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='1',
                   GDK_BACKEND='wayland', GSK_RENDERER='cairo', NO_AT_BRIDGE='1', GSETTINGS_BACKEND='memory')
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'DBUS_SESSION_BUS_ADDRESS', 'WHITECUBE_TRAIN_OUT'):
            env.pop(key, None)
        with (fixture / 'compositor.log').open('w') as output:
            compositor = subprocess.Popen([str(ROOT / 'build/whitecube')], env=env, stdout=output, stderr=output)
            try:
                socket = fixture / 'runtime/wayland-0'; deadline = time.monotonic() + 8
                while not socket.exists() and compositor.poll() is None and time.monotonic() < deadline: time.sleep(.04)
                assert socket.exists(), (fixture / 'compositor.log').read_text()
                env['WAYLAND_DISPLAY'] = socket.name
                result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--inside', str(fixture), str(screenshots)],
                                        env=env, capture_output=True, text=True, timeout=20)
                assert result.returncode == 0 and 'Traceback' not in result.stderr, result.stdout + result.stderr
                print(result.stdout.rstrip())
                for page in ('train', 'runs', 'activity'):
                    image = screenshots / f'cube-train-{page}.png'
                    assert image.is_file() and image.read_bytes().startswith(b'\x89PNG'), f'{page} did not render'
                print('PASS: all three native GTK4 trainer pages rendered on Wayland')
            finally:
                compositor.terminate()
                try: compositor.wait(timeout=5)
                except subprocess.TimeoutExpired: compositor.kill(); compositor.wait()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--inside': raise SystemExit(inside(sys.argv[2], sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT / 'build')
    run(parser.parse_args().screenshots)
