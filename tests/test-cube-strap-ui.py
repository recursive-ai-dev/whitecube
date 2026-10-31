#!/usr/bin/python3 -I
"""Drive the native Cube Strap window on an isolated Wayland compositor.

sudo, cube-strap-build, chrootable-manager and whitecube are fakes in a temporary bin
directory: the test builds a chroot cube and a VM cube end to end, a failing build, and
renders every page, without root, network, debootstrap or the real stores.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parent.parent

FAKES = {
    'sudo': r'''#!/bin/sh
# No timestamp cache: only the initial -S authentication can start a helper.
case " $* " in *" -S "*) ;; *) echo 'sudo: a password is required' >&2; exit 1 ;; esac
while [ $# -gt 0 ]; do case $1 in --) shift; exec "$@" ;; -S) read -r _ || exit 1; shift ;; *) shift ;; esac; done
''',
    'cube-strap-build': r'''#!/bin/bash
set -eu
while (($#)); do case $1 in --config) config=$2; shift 2 ;; --workdir) work=$2; shift 2 ;; --kind) kind=$2; shift 2 ;; *) shift 2 ;; esac; done
[[ $(stat -c %a "$config") == 600 ]] || { echo "config mode is not 600"; exit 9; }
name=$(sed -n 's/^hostname = "\(.*\)"/\1/p' "$config")
cp "$config" "$FIXTURE/last.conf"; rm -f "$config"
printf '\e[1;36m═══ Executing Debian Bootstrap (debootstrap -> trixie on amd64) ═══\e[0m\n'
[[ $name != failbox ]] || { echo "[x] debootstrap failed"; exit 3; }
echo '═══ Configuring System Inside Target Chroot ═══'
if [[ $kind == vm ]]; then truncate -s 64M "$work/$name.img"; else tar -czf "$work/$name.tar.gz" --files-from /dev/null; fi
echo '═══ Debian Installation Complete! ═══'
''',
    'chrootable-manager': r'''#!/bin/sh
case $1 in
  --list) echo '[{"name": "existing"}]' ;;
  --import) case $3 in *.tar.gz) ;; *) echo 'expected gzip archive' >&2; exit 3 ;; esac
            gzip -t -- "$3" || exit 3
            printf '%s\n' "$@" > "$FIXTURE/import.args"; echo "Importing $2" >&2 ;;
  *) echo "unexpected $*" >&2; exit 2 ;;
esac
''',
    'cube-chrootables': '''#!/bin/sh
printf '%s\\n' cube-chrootables > "$FIXTURE/chrootables.launch"
''',
    'chrootables': '''#!/bin/sh
printf '%s\\n' old-launcher > "$FIXTURE/chrootables.launch"
exit 99
''',
    'whitecube': r'''#!/bin/sh
case $1 in
  import) mkdir -p "$WHITECUBE_HOME/images/$5"; cp "$4" "$WHITECUBE_HOME/images/$5/disk.img"
          echo '{"type": "ext4", "disk": "disk.img"}' > "$WHITECUBE_HOME/images/$5/meta.json" ;;
  run) printf '%s\n' "$@" > "$FIXTURE/run.args" ;;
esac
''',
}


def inside(fixture, screenshots):
    sys.path.insert(0, str(ROOT / 'apps/cube-strap'))
    import app
    import strap
    fixture, screenshots = Path(fixture), Path(screenshots)
    # The broker's fixed adjacent builder path points at a disposable fixture;
    # production helpers never accept an executable path from the desktop.
    app.backend.HERE = fixture / 'apps/chrootables'
    options = argparse.Namespace(page=None, screenshot=None)

    class SmokeApp(app.StrapApp):
        failure = None

        def do_activate(self):
            super().do_activate()
            self.steps = iter(self.script())
            app.GLib.timeout_add(300, self.advance)

        def advance(self):
            try:
                wait = next(self.steps)
            except StopIteration:
                self.quit()
                return False
            except BaseException:
                self.failure = traceback.format_exc()
                print(self.failure, file=sys.stderr)
                self.quit()
                return False
            app.GLib.timeout_add(wait, self.advance)
            return False

        def wait_build(self):
            deadline = time.monotonic() + 8
            while self.window.building:
                assert time.monotonic() < deadline, 'build did not finish'
                yield 50

        def script(self):
            w = self.window
            assert self.manager.authenticate('secret'), 'initial helper authentication failed'
            w.open_chrootables()
            deadline = time.monotonic() + 3
            while not (fixture / 'chrootables.launch').exists():
                assert time.monotonic() < deadline, 'Cube Chrootables did not launch'
                yield 20
            assert (fixture / 'chrootables.launch').read_text().strip() == 'cube-chrootables'
            w.name.set_text('')
            assert not w.build_button.get_sensitive()
            yield 50
            assert w.workspaces == {'existing'}, w.workspaces
            w.name.set_text('existing'); w.password.set_text('secret1')
            assert not w.build_button.get_sensitive() and 'already exists' in w.problem.get_text()
            w.name.set_text('devbox')
            assert w.build_button.get_sensitive(), w.problem.get_text()
            assert w.lifecycle_row.get_visible() and not w.vm_row.get_visible()
            w.start()
            yield 100
            yield from self.wait_build()
            assert w.password.get_text() == '', 'password must be cleared once the build starts'
            item = strap.load_history()[-1]
            assert (item['name'], item['kind'], item['status']) == ('devbox', 'chroot', 'built'), item
            assert (fixture / 'import.args').exists(), w.log_text()[-1500:]
            args = (fixture / 'import.args').read_text().split()
            assert args[0:2] == ['--import', 'devbox'] and args[3:] == ['4096', 'persistent'], args
            assert args[2].endswith('/devbox.tar.gz'), args
            assert not Path(args[2]).exists(), 'the build directory is removed after import'
            conf = (fixture / 'last.conf').read_text()
            assert 'target_mode = "tar"' in conf and 'kernel = "7"' in conf
            assert 'secret1' not in json.dumps(strap.load_history()) + json.dumps(strap.load_draft())
            assert 'Importing devbox' in w.log_text()
            assert w.stack.get_visible_child_name() == 'cubes'
            assert w.progress.get_fraction() == 1

            w.kind_buttons['vm'].set_active(True)
            assert w.vm_row.get_visible() and not w.lifecycle_row.get_visible()
            assert int(w.size.get_value()) == strap.DEFAULT_SIZE_GIB['vm']
            w.recipe.set_selected(w.recipe_keys.index('server'))
            w.name.set_text('vmbox'); w.password.set_text('secret2')
            w.start()
            yield 100
            yield from self.wait_build()
            assert strap.load_history()[-1]['status'] == 'built', strap.load_history()[-1]
            assert strap.vm_exists('vmbox')
            assert 'image_size = "12G"' in (fixture / 'last.conf').read_text()
            yield 300
            run = (fixture / 'run.args').read_text().split()
            assert run[:2] == ['run', 'vmbox'] and run[-2:] == ['--forward', '2222:22'], run
            w.name.set_text('vmbox'); w.password.set_text('secret2')
            assert 'already exists' in w.problem.get_text()

            w.name.set_text('failbox')
            w.start()
            yield 100
            yield from self.wait_build()
            assert strap.load_history()[-1]['status'] == 'failed'
            assert 'debootstrap failed' in w.log_text() and 'failed' in w.status.get_text()
            assert (strap.STATE / 'logs/vm-failbox.log').is_file()
            assert not list(strap.CACHE.iterdir()), 'failed builds leave no build directory'

            w.fill_cubes()
            rows, row = 0, w.cube_list.get_first_child()
            while row:
                rows, row = rows + 1, row.get_next_sibling()
            assert rows == 3, rows
            w.forget('failbox', 'vm')
            assert [item['name'] for item in strap.load_history()] == ['devbox', 'vmbox']
            for page in ('build', 'cubes', 'log'):
                w.stack.set_visible_child_name(page)
                yield 350
                app.screenshot(w, str(screenshots / f'cube-strap-{page}.png'))
            print('PASS: validation, sudo unlock path, chroot build and Chrootables import, VM build, '
                  'whitecube import and boot with SSH forward, failed build cleanup, history, forget')

    application = SmokeApp(options)
    result = application.run([sys.argv[0]])
    return 1 if application.failure else result


def run(screenshots):
    screenshots = Path(screenshots).resolve(); screenshots.mkdir(exist_ok=True, parents=True)
    # The fakes must be executable, and /tmp may be mounted noexec.
    with tempfile.TemporaryDirectory(prefix='cube-strap-ui-', dir=ROOT / 'build') as temp:
        fixture = Path(temp)
        for name in ('home', 'runtime', 'state', 'cache', 'bin', 'store'):
            (fixture / name).mkdir(mode=0o700)
        for name, text in FAKES.items():
            # chrootable-manager runs under the Chrootables root helper, which clears the environment.
            if name in ('chrootable-manager', 'cube-strap-build'):
                text = text.replace('$FIXTURE', str(fixture))
            (fixture / 'bin' / name).write_text(text)
            (fixture / 'bin' / name).chmod(0o755)
        (fixture / 'apps/chrootables').mkdir(parents=True)
        (fixture / 'apps/cube-strap').mkdir()
        shutil.copyfile(ROOT / 'apps/chrootables/broker.py', fixture / 'apps/chrootables/broker.py')
        shutil.copyfile(fixture / 'bin/cube-strap-build', fixture / 'apps/cube-strap/cube-strap-build')
        (fixture / 'apps/cube-strap/cube-strap-build').chmod(0o755)
        (fixture / 'apps/cube-strap/deb-strap.sh').write_text('# fixture bootstrap\n')
        env = dict(os.environ, HOME=str(fixture / 'home'), XDG_RUNTIME_DIR=str(fixture / 'runtime'),
                   XDG_CONFIG_HOME=str(fixture / 'home/.config'), XDG_STATE_HOME=str(fixture / 'state'),
                   XDG_CACHE_HOME=str(fixture / 'cache'), FIXTURE=str(fixture),
                   PATH=f'{fixture / "bin"}:{os.environ.get("PATH", "/usr/bin:/bin")}',
                   CHROOTABLES_SUDO=str(fixture / 'bin/sudo'), CHROOTABLES_MANAGER=str(fixture / 'bin/chrootable-manager'),
                   WHITECUBE_HOME=str(fixture / 'store'),
                   WLR_BACKENDS='headless', WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='1',
                   GDK_BACKEND='wayland', GSK_RENDERER='cairo', NO_AT_BRIDGE='1', GSETTINGS_BACKEND='memory')
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
            env.pop(key, None)
        with (fixture / 'compositor.log').open('w') as output:
            compositor = subprocess.Popen([str(ROOT / 'build/whitecube')], env=env, stdout=output, stderr=output)
            try:
                socket = fixture / 'runtime/wayland-0'; deadline = time.monotonic() + 8
                while not socket.exists() and compositor.poll() is None and time.monotonic() < deadline: time.sleep(.04)
                assert socket.exists(), (fixture / 'compositor.log').read_text()
                env['WAYLAND_DISPLAY'] = socket.name
                result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--inside', str(fixture), str(screenshots)],
                                        env=env, capture_output=True, text=True, timeout=40)
                assert result.returncode == 0 and 'Traceback' not in result.stderr, result.stdout + result.stderr
                print(result.stdout.rstrip())
                for page in ('build', 'cubes', 'log'):
                    image = screenshots / f'cube-strap-{page}.png'
                    assert image.is_file() and image.read_bytes().startswith(b'\x89PNG'), f'{page} did not render'
                print('PASS: all three Cube Strap pages rendered on Wayland')
            finally:
                compositor.terminate()
                try: compositor.wait(timeout=5)
                except subprocess.TimeoutExpired: compositor.kill(); compositor.wait()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--inside': raise SystemExit(inside(sys.argv[2], sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT / 'build')
    run(parser.parse_args().screenshots)
