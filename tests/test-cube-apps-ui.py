#!/usr/bin/python3 -I
"""Exercise the real Cube file widgets on an isolated headless Wayland desktop."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]


def inside(fixture, screenshots):
    sys.path.insert(0, str(ROOT / 'apps/cube-apps'))
    import cube_theme as ct
    import cube_hash as ch
    import cube_rename as cr
    import cube_pdf as cp
    import cube_lock as cl
    import pikepdf
    fixture, screenshots = Path(fixture), Path(screenshots)
    source = fixture / 'input.pdf'
    with pikepdf.new() as pdf:
        pdf.add_blank_page(); pdf.add_blank_page(); pdf.save(source)
    text = fixture / 'a.txt'; text.write_text('Cube test')
    hash_input = fixture / 'checksum-input.txt'; hash_input.write_text('Stable hash fixture')

    class Smoke(ct.CubeApp):
        window_class = ch.HashWindow
        failure = None

        def guarded(self, callback):
            try: return callback()
            except BaseException:
                self.failure = traceback.format_exc(); print(self.failure, file=sys.stderr); self.quit(); return False

        def do_activate(self):
            super().do_activate()
            ct.GLib.timeout_add(150, lambda: self.guarded(self.exercise))

        def exercise(self):
            assert self.lookup_action('open') and self.lookup_action('run')
            assert self.get_accels_for_action('app.run')
            self.hash = self.window()
            self.hash.add_files([None, hash_input]); self.hash.compute()
            assert self.hash.busy and not self.hash.go.get_sensitive()
            self.hash.add_files([source]); assert len(self.hash.files.paths) == 1
            self.rename = cr.RenameWindow(self)
            self.rename.add_files([text]); self.rename.prefix.set_text('new-')
            self.rename.do_rename(); assert self.rename.undo
            assert (fixture / 'new-a.txt').read_text() == 'Cube test'
            text.write_text('new file')
            self.rename.do_undo(); assert self.rename.undo and text.read_text() == 'new file'
            text.unlink(); self.rename.do_undo(); assert text.read_text() == 'Cube test'
            self.pdf = cp.PdfToolsWindow(self)
            assert self.pdf.stack.get_child_by_name('rotate')
            self.pdf.stack.set_visible_child_name('rotate'); self.pdf.add_files([source])
            self.pdf.tabs['rotate'].pages.set_text('1')
            self.pdf.tabs['rotate'].start()
            assert self.pdf.tabs['rotate'].busy and not self.pdf.tabs['rotate'].go.get_sensitive()
            self.lock = cl.PdfLockWindow(self)
            self.lock.add_files([source]); self.lock.pw.set_text('é' * 64); self.lock.pw2.set_text('é' * 64)
            assert not self.lock.go.get_sensitive()
            self.lock.pw.set_text('test password'); self.lock.pw2.set_text('test password')
            assert self.lock.go.get_sensitive()
            self.lock.on_encrypt(None); assert self.lock.busy and not self.lock.go.get_sensitive()
            self.deadline = time.monotonic() + 8
            ct.GLib.timeout_add(30, lambda: self.guarded(self.completed))
            return False

        def completed(self):
            if self.hash.busy or self.lock.busy or self.pdf.tabs['rotate'].busy:
                assert time.monotonic() < self.deadline; return True
            assert 'Hashed 1' in self.hash.status.get_text()
            assert self.hash.results and self.hash.copy_all.get_sensitive()
            assert self.lock.pw.get_text() == self.lock.pw2.get_text() == ''
            assert (fixture / 'input-locked.pdf').exists()
            with pikepdf.open(fixture / 'input-rotated.pdf') as pdf:
                assert int(pdf.pages[0].obj['/Rotate']) == 90
                assert int(pdf.pages[1].obj.get('/Rotate', 0)) == 0
            self.captures = iter((('cube-hash',self.hash), ('cube-rename',self.rename), ('cube-pdf',self.pdf), ('cube-lock',self.lock)))
            return self.capture_next()

        def capture_next(self):
            try: self.name, self.capture_window = next(self.captures)
            except StopIteration:
                print('PASS: actual GTK file apps — accelerators, busy gating, hashing, safe rename/undo conflicts, PDF rotation, password validation/clearing and four rendered windows')
                self.quit(); return False
            self.capture_window.present()
            ct.GLib.timeout_add(250, lambda: self.guarded(self.capture))
            return False

        def capture(self):
            ct.screenshot(self.capture_window, str(screenshots / (self.name + '-0.7.0.png')))
            return self.capture_next()

    application = Smoke('dev.woez.CubeReview')
    result = application.run([sys.argv[0]])
    return 1 if application.failure else result


def run(screenshots):
    screenshots = Path(screenshots).resolve(); screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='whitecube-file-ui-') as tmp:
        fixture = Path(tmp)
        for name in ('home','runtime','state','data','cache'): (fixture / name).mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(fixture / 'home'), XDG_RUNTIME_DIR=str(fixture / 'runtime'),
                   XDG_CONFIG_HOME=str(fixture / 'home/.config'), XDG_STATE_HOME=str(fixture / 'state'),
                   XDG_DATA_HOME=str(fixture / 'data'), XDG_CACHE_HOME=str(fixture / 'cache'),
                   WLR_BACKENDS='headless', WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='1',
                   GDK_BACKEND='wayland', GSK_RENDERER='cairo', GTK_A11Y='none', GSETTINGS_BACKEND='memory')
        for name in ('DISPLAY','WAYLAND_DISPLAY','WAYLAND_SOCKET','DBUS_SESSION_BUS_ADDRESS','CUBE_SHOT'): env.pop(name, None)
        with (fixture / 'compositor.log').open('w') as log:
            compositor = subprocess.Popen([str(ROOT / 'build/whitecube')],env=env,stdout=log,stderr=log)
            try:
                deadline = time.monotonic() + 8
                while not (fixture / 'runtime/wayland-0').exists() and compositor.poll() is None and time.monotonic() < deadline: time.sleep(.04)
                assert (fixture / 'runtime/wayland-0').exists(), 'private compositor did not start'
                env['WAYLAND_DISPLAY'] = 'wayland-0'
                result = subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),'--inside',str(fixture),str(screenshots)],env=env,capture_output=True,text=True,timeout=20)
                assert result.returncode == 0, result.stdout + result.stderr
                print(result.stdout.rstrip())
            finally:
                compositor.terminate()
                try: compositor.wait(timeout=5)
                except subprocess.TimeoutExpired: compositor.kill(); compositor.wait(timeout=5)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--inside': raise SystemExit(inside(sys.argv[2],sys.argv[3]))
    run(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'build')
