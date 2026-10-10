#!/usr/bin/python3 -I
"""Launch real apps against stale hidden placements on a private Wayland session."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
APPS = (
    ('cube-settings', [str(ROOT / 'build/cube-settings')]),
    ('foot', [str(ROOT / 'bin/whitecube-terminal'), 'sh', '-c', 'mc']),
    ('dev.woez.CubeHash', ['/usr/bin/python3', str(ROOT / 'apps/cube-apps/cube_hash.py')]),
    ('dev.woez.CubeLock', ['/usr/bin/python3', str(ROOT / 'apps/cube-apps/cube_lock.py')]),
    ('dev.woez.CubePdf', ['/usr/bin/python3', str(ROOT / 'apps/cube-apps/cube_pdf.py')]),
    ('dev.woez.CubeRename', ['/usr/bin/python3', str(ROOT / 'apps/cube-apps/cube_rename.py')]),
    ('org.whitecube.Trainer', [str(ROOT / 'bin/cube-train'), '--no-probe']),
    ('org.whitecube.CubeEditor', [str(ROOT / 'bin/cube-editor')]),
    ('org.whitecube.Chrootables', [str(ROOT / 'bin/chrootables')]),
)


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class AppLaunch(unittest.TestCase):
    def test_new_windows_are_visible_and_have_distinct_wayland_ids(self):
        with tempfile.TemporaryDirectory(prefix='whitecube-launch-') as temporary:
            home = Path(temporary)
            runtime = home / 'runtime'
            runtime.mkdir(mode=0o700)
            state = home / 'state/whitecube'
            state.mkdir(parents=True, mode=0o700)
            layout = state / 'native-layout.txt'
            # The active workspace is 2, the desktop is showing, and every
            # previous window was hidden on workspace 0.
            layout.write_text('WC_LAYOUT_1 2 1\n' + ''.join(
                f'W 0 1 0 0 60 40 800 700 {identity.encode().hex()} -\n'
                for identity, _ in APPS))
            environment = dict(os.environ, HOME=str(home), XDG_RUNTIME_DIR=str(runtime),
                               XDG_CONFIG_HOME=str(home / 'config'), XDG_STATE_HOME=str(home / 'state'),
                               XDG_DATA_HOME=str(home / 'data'), WLR_BACKENDS='headless',
                               WLR_HEADLESS_OUTPUTS='1', WLR_RENDERER='pixman',
                               GDK_BACKEND='wayland', GTK_A11Y='none', GSETTINGS_BACKEND='memory')
            for variable in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'CUBE_SHOT'):
                environment.pop(variable, None)
            with (home / 'compositor.log').open('w+') as log:
                compositor = subprocess.Popen([str(ROOT / 'build/whitecube')], env=environment,
                                              stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 8
                    while not list(runtime.glob('wayland-*')):
                        self.assertIsNone(compositor.poll(), 'Headless compositor exited')
                        self.assertLess(time.monotonic(), deadline, 'Wayland socket missing')
                        time.sleep(.05)
                    sockets = [p for p in runtime.glob('wayland-*') if p.is_socket()]
                    self.assertTrue(sockets)
                    environment['WAYLAND_DISPLAY'] = sockets[0].name
                    for identity, command in APPS:
                        with self.subTest(app=identity), (home / 'app.log').open('w+') as app_log:
                            if identity == 'foot' and shutil.which('mc') is None:
                                self.skipTest('Midnight Commander is not installed')
                            client = subprocess.Popen(command, env=environment, stdout=app_log, stderr=app_log)
                            try:
                                deadline = time.monotonic() + 8
                                while True:
                                    lines = layout.read_text().splitlines()
                                    rows = [line.split() for line in lines if line.startswith('W ')]
                                    row = next((r for r in rows if r[9] == identity.encode().hex()
                                                and r[1] == '2' and r[2] == '0'), None)
                                    if row:
                                        self.assertEqual(lines[0], 'WC_LAYOUT_1 2 0')
                                        self.assertEqual(row[5:9], ['60', '40', '800', '700'])
                                        if identity == 'foot':
                                            # A terminal mapping alone is insufficient: the
                                            # actual file manager must survive startup in its PTY.
                                            time.sleep(.5)
                                            self.assertIsNone(client.poll(), 'Midnight Commander terminal exited')
                                            processes = [line.split(maxsplit=2) for line in subprocess.check_output(
                                                ['ps', '-eo', 'pid=,ppid=,comm='], text=True).splitlines()]
                                            descendants = {str(client.pid)}
                                            while True:
                                                children = {pid for pid, parent, _ in processes if parent in descendants}
                                                if children <= descendants:
                                                    break
                                                descendants.update(children)
                                            self.assertTrue(any(command == 'mc' and pid in descendants
                                                                for pid, _, command in processes),
                                                            'Midnight Commander did not start inside Foot')
                                        break
                                    if client.poll() is not None or time.monotonic() >= deadline:
                                        app_log.seek(0)
                                        self.fail(f'{identity} did not map visibly with its own ID: '
                                                  f'{app_log.read()}\n{layout.read_text()}')
                                    time.sleep(.05)
                            finally:
                                stop(client)
                finally:
                    stop(compositor)


if __name__ == '__main__':
    unittest.main()
