#!/usr/bin/python3
"""Exercise all 37 settings in a disposable dconf + headless Wayland session.

Requires permission for local D-Bus/Wayland Unix sockets. Never connects to the
active desktop or changes the account's real configuration.
"""
import configparser
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
BUILD = Path(os.environ.get('WHITECUBE_TEST_BUILD', str(ROOT / 'build')))
HELPER = Path(os.environ.get('WHITECUBE_TEST_HELPER', str(ROOT / 'bin' / 'cube-preferences')))
SCHEMAS = Path(os.environ.get('WHITECUBE_TEST_SCHEMA_DIR', str(BUILD / 'schemas')))
if '--installed' in sys.argv:
    BUILD = Path.home() / '.local/bin'
    HELPER = BUILD / 'cube-preferences'
    SCHEMAS = Path.home() / '.local/share/whitecube/schemas'


def run(*args, input=None, ok=True):
    result = subprocess.run([str(x) for x in args], input=input, text=True, capture_output=True, timeout=20)
    if ok:
        assert result.returncode == 0, (args, result.stdout, result.stderr)
    else:
        assert result.returncode != 0, (args, 'invalid change accepted')
    return result


def snapshot(name):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(os.environ['WHITECUBE_SETTINGS_REPORT']) / name)
    return dict(parser['settings']) if parser.has_section('settings') else {}


def wait_for(predicate, description):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Timed out: ' + description)


def inside():
    root = Path(os.environ['WHITECUBE_SETTINGS_REPORT'])
    schema = ET.parse(ROOT / 'data/schemas/org.whitecube.desktop.gschema.xml').find('schema')
    baseline = {}
    for key in schema.findall('key'):
        value = key.findtext('default')
        if key.get('type') == 's':
            value = value[1:-1]
        elif key.get('type') == 'b':
            value = str(int(value == 'true'))
        baseline[key.get('name').replace('-', '_')] = value
    config = Path.home() / '.config/whitecube/config'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(''.join(f'{k}={v}\n' for k, v in baseline.items()))
    fixture = config.read_bytes()
    # Deliberately start consumers without a session bus: this is Whitecube's
    # production session model. dconf reads must see writes on another bus.
    consumer_env = dict(os.environ, WLR_BACKENDS='headless', WLR_RENDERER='pixman',
                        WLR_HEADLESS_OUTPUTS='2', WAYLAND_DISPLAY='')
    consumer_env.pop('DBUS_SESSION_BUS_ADDRESS', None)
    consumer_env.pop('DISPLAY', None)
    children = []
    with (root / 'compositor.log').open('w') as clog, (root / 'status.log').open('w') as slog:
        try:
            compositor = subprocess.Popen([str(BUILD / 'whitecube')], env=consumer_env, stdout=clog, stderr=clog)
            children.append(compositor)
            wait_for(lambda: (Path(os.environ['XDG_RUNTIME_DIR']) / 'wayland-0').exists(), 'Wayland startup')
            consumer_env['WAYLAND_DISPLAY'] = 'wayland-0'
            status = subprocess.Popen([str(BUILD / 'cube-status')], env=consumer_env, stdout=slog, stderr=slog)
            children.append(status)
            wait_for(lambda: snapshot('status.ini'), 'bar startup')
            original = {**snapshot('compositor.ini'), **snapshot('status.ini')}
            changes = {
                'tab_w': '240', 'tab_h': '52', 'grid_step': '128', 'bg': '112233', 'grid': '223344',
                'tabbar': '334455', 'border': '445566', 'unfocused': '556677',
                'terminal': 'test-terminal --literal', 'launcher': 'test-launcher', 'browser_cmd': 'test-browser',
                'settings_cmd': 'test-settings', 'help_cmd': 'test-help', 'chat_cmd': 'test-chat',
                'chat_app_id': 'test-chat-id', 'scripts_cmd': 'test-scripts', 'scripts_app_id': 'test-scripts-id',
                'xkb_model': 'pc105', 'xkb_layout': 'us', 'xkb_variant': 'intl', 'xkb_options': 'caps:escape',
                'repeat_rate': '0', 'repeat_delay': '1700', 'privacy_hide_titles': '1',
                'privacy_hide_network': '1', 'privacy_remember_layout': '0', 'privacy_save_titles': '0',
                'privacy_clipboard': '0', 'privacy_clear_clipboard_on_lock': '0', 'privacy_lock_minutes': '120',
                'pointer_speed': '65', 'natural_scroll': '1', 'left_handed': '1', 'tap_to_click': '1',
                'disable_while_typing': '0', 'clock_format': '12h', 'clock_seconds': '1',
            }
            bool_keys = {k.get('name').replace('-', '_') for k in schema.findall('key') if k.get('type') == 'b'}
            input_ints = {'natural_scroll', 'left_handed', 'tap_to_click', 'disable_while_typing'}
            for key, value in changes.items():
                run(HELPER, 'set', key, value)
                raw = run('dconf', 'read', '/org/whitecube/desktop/' + key.replace('_', '-')).stdout.strip()
                assert raw, key + ' not persisted'
                expected = ('true' if value == '1' else 'false') if key in bool_keys - input_ints else value
                def applied():
                    state = {**snapshot('compositor.ini'), **snapshot('status.ini')}
                    return state.get(key) == expected
                wait_for(applied, key + ' not consumed live')
                assert compositor.poll() is None and status.poll() is None
            clock = snapshot('status.ini')['clock_text']
            assert re.match(r'\d{2}:\d{2}:\d{2}(AM|PM)', clock), clock
            assert config.read_bytes() == fixture, 'dconf editor changed inherited config'
            before = run('dconf', 'dump', '/org/whitecube/desktop/').stdout
            run(HELPER, 'set-many', input='tab_w\t300\nrepeat_rate\t999\n', ok=False)
            run(HELPER, 'set', 'xkb_layout', 'notareallayout', ok=False)
            run(HELPER, 'set', 'xkb_options', 'caps:escape,ctrl:nocaps', ok=False)
            assert run('dconf', 'dump', '/org/whitecube/desktop/').stdout == before, 'invalid batch partially committed'
            run(HELPER, 'set-many', input='tab_w\t280\ntab_h\t48\n')
            wait_for(lambda: snapshot('compositor.ini').get('tab_w') == '280' and snapshot('status.ini').get('tab_h') == '48', 'grouped save')
            # Raw dconf writes also apply live (there is no polling bridge file).
            run('dconf', 'write', '/org/whitecube/desktop/repeat-rate', 'int32 42')
            wait_for(lambda: snapshot('compositor.ini').get('repeat_rate') == '42', 'external dconf edit')
            for key in changes:
                run(HELPER, 'reset', key)
            wait_for(lambda: all({**snapshot('compositor.ini'), **snapshot('status.ini')}.get(k) == original[k] for k in changes), 'reset inheritance')
            assert not run('dconf', 'dump', '/org/whitecube/desktop/').stdout.strip()
            assert config.read_bytes() == fixture
            # The writer must also work with no ambient session bus of its own.
            env = dict(os.environ); env.pop('DBUS_SESSION_BUS_ADDRESS', None)
            isolated = subprocess.run([str(HELPER), 'set', 'tab_w', '260'], env=env, text=True, capture_output=True, timeout=20)
            assert isolated.returncode == 0, isolated.stderr
            wait_for(lambda: snapshot('compositor.ini').get('tab_w') == '260', 'short-lived writer bus')
            run(HELPER, 'reset', 'tab_w')
            run(ROOT / 'build/settings-test', '--transactions', HELPER)
            # Exercise actual Settings window creation on the private Wayland socket.
            with (root / 'settings.log').open('w') as log:
                panel = subprocess.Popen([str(BUILD / 'cube-settings'), '--page', 'advanced'], env=consumer_env, stdout=log, stderr=log)
                children.append(panel); time.sleep(.5)
                assert panel.poll() is None, 'Advanced window failed to open'
            print('PASS: all 37 preferences persist, reach the live consumers, and reset correctly; atomic rejection, external writes, bus-free session, clock output and Wayland settings window verified')
        except BaseException:
            for name in ('compositor.log', 'status.log', 'settings.log'):
                if (root / name).exists():
                    print(name, (root / name).read_text()[-5000:], file=sys.stderr)
            raise
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)


if __name__ == '__main__':
    if '--inside' in sys.argv:
        inside()
    else:
        with tempfile.TemporaryDirectory(prefix='whitecube-settings-integration-') as temp:
            root = Path(temp)
            for name in ('home', 'runtime', 'state', 'report'):
                (root / name).mkdir(mode=0o700)
            env = dict(os.environ, HOME=str(root / 'home'), XDG_CONFIG_HOME=str(root / 'home/.config'),
                       XDG_STATE_HOME=str(root / 'state'), XDG_RUNTIME_DIR=str(root / 'runtime'),
                       GSETTINGS_SCHEMA_DIR=str(SCHEMAS), GSETTINGS_BACKEND='dconf',
                       WHITECUBE_TEST_BUILD=str(BUILD), WHITECUBE_TEST_HELPER=str(HELPER),
                       WHITECUBE_SETTINGS_REPORT=str(root / 'report'), LC_ALL='C.UTF-8')
            env.pop('DBUS_SESSION_BUS_ADDRESS', None)
            result = subprocess.run(['dbus-run-session', '--', sys.executable, '-I', str(Path(__file__).resolve()), '--inside'], env=env)
            sys.exit(result.returncode)
