#!/usr/bin/python3 -I
"""Run editor checks on a private GTK display without touching desktop settings."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]


def main():
    daemon = shutil.which('gtk4-broadwayd')
    if not daemon:
        print('Install libgtk-4-bin for isolated GUI checks (gtk4-broadwayd is missing).', file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix='cube-editor-check-') as temporary:
        home = Path(temporary)
        runtime = home / 'runtime'
        runtime.mkdir(mode=0o700)
        environment = dict(os.environ, HOME=str(home), XDG_RUNTIME_DIR=str(runtime),
                           XDG_CONFIG_HOME=str(home / 'config'), XDG_DATA_HOME=str(home / 'data'),
                           XDG_CACHE_HOME=str(home / 'cache'), XDG_STATE_HOME=str(home / 'state'),
                           GSETTINGS_BACKEND='memory', GDK_BACKEND='broadway', BROADWAY_DISPLAY=':7',
                           GSK_RENDERER='cairo', GTK_A11Y='none', GIO_USE_VFS='local',
                           GIO_USE_VOLUME_MONITOR='unix')
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
            environment.pop(key, None)
        with (home / 'broadway.log').open('w+') as log:
            process = subprocess.Popen([daemon, '--address', '127.0.0.1', '--port', '0', ':7'],
                                       env=environment, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 8
                while process.poll() is None and not list(runtime.glob('broadway*.socket')) and time.monotonic() < deadline:
                    time.sleep(.05)
                if process.poll() is not None or not list(runtime.glob('broadway*.socket')):
                    log.seek(0)
                    print('Could not start the private GTK display:\n' + log.read(), file=sys.stderr)
                    return 2
                command = [sys.executable, '-I', '-m', 'unittest', 'discover', '-s', str(ROOT / 'tests'), '-v']
                if shutil.which('dbus-run-session'):
                    # A standard session bus autoactivates host desktop portals,
                    # which can mount FUSE document stores in our temporary home.
                    # This bus has no service directories: GUI tests need app
                    # registration, but no external desktop services.
                    config = home / 'session.conf'
                    config.write_text('<busconfig><type>session</type><listen>unix:tmpdir=' +
                                      escape(str(runtime)) + '</listen><auth>EXTERNAL</auth>'
                                      '<policy context="default"><allow send_destination="*"/>'
                                      '<allow receive_sender="*"/><allow own="*"/></policy></busconfig>')
                    command = ['dbus-run-session', '--config-file=' + str(config), '--', *command]
                return subprocess.call(command, env=environment, cwd=ROOT)
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
