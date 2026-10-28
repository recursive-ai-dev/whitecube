#!/usr/bin/python3 -I
"""Launch installed Whitecube programs in a disposable Debian runtime.

Requires a prepared Debian builder rootfs and Bubblewrap. No host installation,
services, home directory, active desktop, checkout libraries or network are used.
"""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import wave


def run(command, *, input=None, timeout=30):
    result = subprocess.run(command, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{command[0]} exited {result.returncode}: {(result.stdout + result.stderr)[-4000:]}')
    return result


def wait_for(predicate, description, children=(), timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, child in children:
            if child.poll() is not None:
                raise RuntimeError(f'{name} exited {child.returncode} before {description}')
        if predicate():
            return
        time.sleep(.05)
    raise RuntimeError('timed out waiting for ' + description)


def snapshot(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    return dict(parser['settings']) if parser.has_section('settings') else {}


def attached(path):
    return path.exists() and re.search(r'wl_surface[@#]\d+\.attach\(wl_buffer[@#]', path.read_text(errors='replace')) is not None


def check_png(path):
    with path.open('rb') as stream:
        header = stream.read(24)
    if (len(header) < 24 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR'
            or not int.from_bytes(header[16:20], 'big') or not int.from_bytes(header[20:24], 'big')):
        raise RuntimeError(f'invalid or empty PNG capture: {path.name}')


def inside():
    output = Path('/report')
    home, runtime = Path.home(), Path(os.environ['XDG_RUNTIME_DIR'])
    for directory in (home, runtime, output / 'preferences'):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    checks = []
    children = []
    streams = []
    report = {
        'verification': 'failed', 'checks': checks,
        'scope': 'installed absolute paths in a read-only Debian runtime; fresh home/runtime directories, isolated PID/network namespaces and private /dev',
        'limitations': [
            'Headless Pixman output; physical display, input, GPU and audio hardware are not exercised.',
            'Chat uses --no-sandbox only in this isolated smoke namespace; Chromium sandbox operation requires a real configured installation.',
            'Runtime comes from the Debian builder and contains build dependencies; this does not establish minimal Depends completeness.',
            'Package configuration scripts, system services, login-manager integration and real model/API requests are not exercised.',
        ],
    }

    def launch(name, command, debug=False):
        log = (output / f'{name}.log').open('w')
        streams.append(log)
        env = dict(os.environ)
        if debug:
            env['WAYLAND_DEBUG'] = 'client'
        child = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        children.append((name, child))
        return child

    try:
        run(['/usr/bin/whitecube-session', '--check'])
        checks.append('installed session startup prerequisites')
        run(['/usr/bin/cube-status', '--selftest'])
        checks.append('installed status selftest')
        doctor = run(['/usr/bin/whitecube-doctor', '--json', '--strict', '--check', 'package', '--check', 'libraries'])
        (output / 'doctor.json').write_text(doctor.stdout)
        checks.append('installed package paths and runtime library checks')

        profile = {'format': 'org.whitecube.preferences', 'version': 1,
                   'settings': {'tab-w': 252, 'clock-format': '12h', 'clock-seconds': True}}
        run(['/usr/bin/cube-preferences', 'import'], input=json.dumps(profile))
        exported = json.loads(run(['/usr/bin/cube-preferences', 'export']).stdout)
        if exported != profile:
            raise RuntimeError('installed preference import/export did not round trip')
        (output / 'settings-profile.json').write_text(json.dumps(exported, indent=2) + '\n')
        checks.append('installed typed preference import/export with real dconf')

        session = launch('launcher', ['/usr/bin/whitecube-session', '--safe-mode'])
        display = runtime / 'wayland-0'
        wait_for(lambda: display.exists() and stat.S_ISSOCK(display.stat().st_mode),
                 'headless Wayland socket', [('session', session)])
        os.environ['WAYLAND_DISPLAY'] = 'wayland-0'
        checks.append('installed session and headless compositor')

        panel = launch('status', ['/usr/bin/cube-status'])
        wallpaper = launch('wallpaper', ['/usr/bin/cube-ca'])
        wait_for(lambda: snapshot(output / 'preferences/status.ini').get('clock_format') == '12h',
                 'panel preference consumption', [('session', session), ('status', panel), ('wallpaper', wallpaper)])
        if snapshot(output / 'preferences/compositor.ini').get('tab_w') != '252':
            raise RuntimeError('installed compositor did not load saved preferences')
        run(['/usr/bin/cube-preferences', 'set', 'tab-w', '264'])
        wait_for(lambda: snapshot(output / 'preferences/compositor.ini').get('tab_w') == '264',
                 'live compositor preference update', [('session', session), ('status', panel)])
        checks.append('installed panel/wallpaper and live preference consumption')

        settings = launch('settings', ['/usr/bin/cube-settings', '--page', 'advanced'], debug=True)
        wait_for(lambda: attached(output / 'settings.log'), 'Settings window buffer',
                 [('session', session), ('settings', settings)])
        run(['/usr/bin/cube-settings', '--render', 'advanced', '/report/settings.png'])
        check_png(output / 'settings.png')
        checks.append('installed Settings Wayland window and PNG render')

        html = Path('/tmp/smoke.html')
        html.write_text('<!doctype html><html><title>Installed browser smoke</title><body><h1>Whitecube installed</h1><p>Private worker render.</p></body></html>')
        run(['/usr/bin/whitecube-browser', '--render', str(html), '/report/browser.png', '800x600'])
        check_png(output / 'browser.png')
        checks.append('installed Browser renderer/network worker discovery and PNG render')

        sound = Path('/tmp/smoke.wav')
        with wave.open(str(sound), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b'\x00\x00' * 16000)
        player = launch('player', ['/usr/bin/cube-player', '--software', '--silent-test', '--quit-after', '3',
                                   '--capture', '/report/player.png', '--report', '/report/player.ini', str(sound)])
        if player.wait(timeout=15):
            raise RuntimeError('installed Player failed; inspect player.log')
        playback = configparser.ConfigParser()
        playback.read(output / 'player.ini')
        if not playback.getboolean('Playback', 'ever_loaded') or playback.getfloat('Playback', 'max_position') <= .1:
            raise RuntimeError('installed Player did not decode and advance the WAV fixture')
        check_png(output / 'player.png')
        checks.append('installed Player Wayland window, real WAV playback, null audio and PNG capture')

        chat = launch('chat', ['/usr/bin/desktop-chat', '--show', '--no-sandbox', '--disable-gpu',
                               '--disable-dev-shm-usage'], debug=True)
        wait_for(lambda: attached(output / 'chat.log'), 'Chat window buffer',
                 [('session', session), ('chat', chat)], timeout=25)
        checks.append('installed Chat launcher, bundled Electron and Wayland window buffer')
        time.sleep(.5)
        for name, child in children:
            if name != 'player' and child.poll() is not None:
                raise RuntimeError(f'{name} exited unexpectedly with status {child.returncode}')
        report['verification'] = 'passed'
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, configparser.Error) as error:
        report['error'] = str(error)
        print('installed smoke failed: ' + str(error), file=sys.stderr)
    finally:
        for name, child in reversed(children):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for stream in streams:
            stream.close()
        state = Path(os.environ['XDG_STATE_HOME']) / 'whitecube'
        for name in ('session.log', 'native-layout.txt'):
            if (state / name).is_file():
                shutil.copy2(state / name, output / name)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'installed smoke: {report["verification"]} ({len(checks)} checks)')
    return 0 if report['verification'] == 'passed' else 1


def outside(args):
    started = time.monotonic()
    rootfs, destination = args.rootfs.resolve(strict=True), args.output.absolute()
    if not (rootfs / 'usr/bin/python3').exists():
        raise RuntimeError('--rootfs must name a prepared Debian runtime root')
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError('--output must be absent or an empty directory')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for executable in ('bwrap', 'cp', 'dpkg-deb'):
        if not shutil.which(executable):
            raise RuntimeError(f'required host tool is missing: {executable}')
    with tempfile.TemporaryDirectory(prefix='whitecube-installed-smoke-') as temporary:
        temporary = Path(temporary)
        runtime = temporary / 'runtime'
        runtime.mkdir()
        # Copy system runtime paths only: never copy a builder user's home or
        # temporary configuration. The copy prevents changes to the builder too.
        system = [rootfs / name for name in ('usr', 'etc', 'bin', 'sbin', 'lib', 'lib64') if (rootfs / name).exists()]
        run(['cp', '-a', '--reflink=auto', '--', *map(str, system), str(runtime)], timeout=180)
        for name in ('home', 'run', 'tmp', 'var/tmp', 'dev', 'proc', 'sys', 'report'):
            (runtime / name).mkdir(parents=True, exist_ok=True)
        (runtime / 'smoke.py').touch()
        if args.deb:
            stage = temporary / 'package'
            run(['dpkg-deb', '--extract', str(args.deb.resolve(strict=True)), str(stage)], timeout=180)
        else:
            stage = args.stage.resolve(strict=True)
        for name in ('usr', 'etc'):
            if not (stage / name).is_dir():
                raise RuntimeError(f'package stage is missing {name}')
            run(['cp', '-a', '--reflink=auto', '--', str(stage / name), str(runtime)], timeout=180)
        command = [
            'bwrap', '--clearenv', '--ro-bind', str(runtime), '/',
            '--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp', '--tmpfs', '/run',
            '--tmpfs', '/home', '--tmpfs', '/var/tmp', '--tmpfs', '/sys',
            '--bind', str(destination), '/report',
            '--ro-bind', str(Path(__file__).resolve()), '/smoke.py',
            '--unshare-user', '--uid', str(os.getuid()), '--gid', str(os.getgid()),
            '--unshare-pid', '--unshare-net', '--unshare-ipc', '--new-session', '--die-with-parent',
            '--setenv', 'PATH', '/usr/bin:/bin:/usr/sbin:/sbin',
            '--setenv', 'HOME', '/home/smoke', '--setenv', 'LANG', 'C.UTF-8',
            '--setenv', 'XDG_CONFIG_HOME', '/home/smoke/.config',
            '--setenv', 'XDG_STATE_HOME', '/home/smoke/.local/state',
            '--setenv', 'XDG_CACHE_HOME', '/home/smoke/.cache',
            '--setenv', 'XDG_RUNTIME_DIR', '/run/whitecube-smoke',
            '--setenv', 'XDG_SESSION_TYPE', 'wayland',
            '--setenv', 'XDG_CURRENT_DESKTOP', 'whitecube',
            '--setenv', 'WHITECUBE_SETTINGS_REPORT', '/report/preferences',
            '--setenv', 'WLR_BACKENDS', 'headless', '--setenv', 'WLR_RENDERER', 'pixman',
            '--setenv', 'WLR_HEADLESS_OUTPUTS', '1',
            '--chdir', '/', '/usr/bin/python3', '-I', '/smoke.py', '--inside',
        ]
        result = subprocess.run(command, timeout=180)
        report_path = destination / 'report.json'
        if report_path.is_file():
            report = json.loads(report_path.read_text())
            if args.deb:
                with args.deb.open('rb') as package:
                    digest = hashlib.file_digest(package, 'sha256').hexdigest()
                report['source'] = {'deb': str(args.deb.resolve()), 'filename': args.deb.name, 'sha256': digest}
            else:
                report['source'] = {'stage': str(stage)}
            report['elapsed_seconds'] = round(time.monotonic() - started, 2)
            report_path.write_text(json.dumps(report, indent=2) + '\n')
        print(f'Installed runtime logs and report: {destination}')
        return result.returncode


def main():
    if sys.argv[1:] == ['--inside']:
        return inside()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rootfs', type=Path, required=True, help='cached Debian builder rootfs')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--stage', type=Path, help='finished package staging directory')
    source.add_argument('--deb', type=Path, help='finished .deb to extract into the disposable root')
    parser.add_argument('--output', type=Path, required=True, help='new or empty directory for logs, captures and JSON report')
    args = parser.parse_args()
    try:
        return outside(args)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'installed-smoke: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
