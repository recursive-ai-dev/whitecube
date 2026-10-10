"""Read-only session checks and explicitly requested, bounded offline lab probes."""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

BASE = Path(__file__).resolve().parent
ROLES = ('bash', 'web', 'build', 'clean')
BIN = Path('/usr/bin')
SMOKE_REPORT = Path('/var/lib/whitecube/health/containers.json')
THERMAL = Path('/sys/class/thermal')
BINARY_NAMES = ('whitecube', 'cube-ca', 'cube-scripts', 'whitecube-web', 'cube-net', 'cube-lab')


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return 'unknown'


def revision():
    digest = hashlib.sha256()
    for name in ('health.py', 'server.py', 'workspace_data.py'):
        try:
            digest.update((BASE / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:16]


def state_directory():
    state = os.environ.get('XDG_STATE_HOME')
    return (Path(state) if state and Path(state).is_absolute() else Path.home() / '.local/state') / 'whitecube'


def system_metrics():
    temperatures = []
    for path in THERMAL.glob('thermal_zone*/temp'):
        try:
            value = int(path.read_text().strip()) / 1000
            if -20 <= value <= 150:
                temperatures.append(value)
        except (OSError, ValueError):
            pass
    try:
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError:
        load = 0.0
    temperature = max(temperatures, default=None)
    return {'load_per_cpu': round(load, 3), 'temperature_c': temperature,
            'thermal_throttled': temperature is not None and temperature >= 75,
            'temperature_note': 'Hottest readable sensor; unknown when no sensor is available.'}


def check(ident, label, state, detail):
    return {'id': ident, 'label': label, 'state': state, 'detail': str(detail)}


def bounded_command(argv, timeout=5):
    """No shell; bound both the command and its children when a probe times out."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True,
                               env=dict(os.environ, LC_ALL='C', PATH='/usr/bin:/usr/sbin:/usr/bin:/sbin:/bin'))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)
        raise TimeoutError(f'Probe exceeded {timeout} seconds') from None
    return process.returncode, stdout.decode(errors='replace'), stderr.decode(errors='replace')


def helper_json(name, command):
    executable = BIN / name
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError(f'{name} is not installed')
    code, output, errors = bounded_command([str(executable), command], timeout=5)
    if code:
        raise RuntimeError(errors.strip()[:400] or f'{name} {command} exited {code}')
    return json.loads(output)


def wayland_check():
    display = os.environ.get('WAYLAND_DISPLAY')
    if not display:
        return check('wayland', 'Wayland compositor', 'pending', 'Run in the Whitecube session to check its display connection.')
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    if not Path(display).is_absolute() and not runtime:
        return check('wayland', 'Wayland compositor', 'fail', 'WAYLAND_DISPLAY is set but XDG_RUNTIME_DIR is missing.')
    path = Path(display) if Path(display).is_absolute() else Path(runtime) / display
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            # wl_display.sync(new_id=2); require the corresponding callback.done.
            connection.sendall(struct.pack('=III', 1, (12 << 16) | 0, 2))
            pending = bytearray()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                part = connection.recv(4096)
                if not part:
                    break
                pending.extend(part)
                while len(pending) >= 8:
                    object_id, header = struct.unpack_from('=II', pending)
                    size, opcode = header >> 16, header & 0xffff
                    if size < 8 or size > 65536 or size % 4:
                        raise ValueError('Malformed Wayland reply')
                    if len(pending) < size:
                        break
                    if object_id == 2 and opcode == 0 and size == 12:
                        return check('wayland', 'Wayland compositor', 'pass', f'Compositor answered a Wayland sync roundtrip on {path.name}.')
                    del pending[:size]
            raise RuntimeError('No sync callback from compositor')
    except (OSError, ValueError, RuntimeError) as exc:
        return check('wayland', 'Wayland compositor', 'fail', str(exc))


def pty_check():
    master = slave = None
    try:
        master, slave = pty.openpty()
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                 'test -t 0 && test -t 1 && printf WHITECUBE_PTY_OK'],
                                stdin=slave, stdout=slave, stderr=slave, timeout=3,
                                close_fds=True, start_new_session=True)
        os.close(slave)
        slave = None
        ready, _, _ = select.select([master], [], [], 1)
        output = os.read(master, 4096) if ready else b''
        if result.returncode or b'WHITECUBE_PTY_OK' not in output:
            raise RuntimeError('Bash did not complete a PTY input/output check')
        return check('bash-pty', 'Bash script panel terminal', 'pass', 'Bash received terminal stdin/stdout and returned the expected marker through a real PTY.')
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return check('bash-pty', 'Bash script panel terminal', 'fail', str(exc))
    finally:
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)


def network_checks():
    result = []
    for command in ('check', 'status'):
        ident = 'dongle-dependencies' if command == 'check' else 'dongle-identity'
        label = 'Dongle driver and firmware' if command == 'check' else 'Attached USB dongle'
        try:
            info = helper_json('cube-net', command)
            if not isinstance(info, dict):
                raise ValueError('Unexpected network status')
            if command == 'check':
                ready = info.get('dependencies_ready') is True
                result.append(check(ident, label, 'pass' if ready else 'warn',
                                    'Existing driver, firmware and network tools are present.' if ready else
                                    'Some existing driver, firmware or userspace dependencies are missing; cube-net check has details. No modules were installed or loaded.'))
            else:
                present = info.get('present') is True
                device = info.get('device') or {}
                result.append(check(ident, label, 'pass' if present else 'pending',
                                    f'USB {device.get("usb_id", "unknown")}: driver/MAC identity matched.' if present else
                                    'The exact configured USB/driver/MAC combination is not currently attached.'))
                association = info.get('association_state', 'unknown')
                associated = association in ('COMPLETED', 'completed', 'connected')
                result.append(check('dongle-connection', 'Wi-Fi association', 'pass' if associated else 'pending',
                                    f'State: {association}. This checks association, not Internet reachability.'))
        except FileNotFoundError as exc:
            result.append(check(ident, label, 'pending', str(exc)))
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            result.append(check(ident, label, 'warn', str(exc)))
    return result


def project_checks(projects):
    try:
        # Avoid a server/health import cycle; inspection never launches a manifest.
        from server import Catalog, discover_projects, Problem
        catalog = Catalog(discover_projects(projects))
        result = [check('project-manifests', 'Project launcher manifests', 'warn' if catalog.errors else 'pass',
                        '; '.join(map(str, catalog.errors))[:1000] if catalog.errors else 'Project and script manifest validation completed.')]
        for ident, item in catalog.projects.items():
            label = f'Project: {item["name"]}'
            if item.get('kind') == 'command':
                if item.get('container'):
                    result.append(check('project-' + ident, label, 'pass', f'Manifest valid; command runs on explicit launch in {item["container"]} Lab.'))
                else:
                    executable = item['command'][0]
                    available = shutil.which(executable) if '/' not in executable else os.access(Path(item['cwd']) / executable, os.X_OK)
                    result.append(check('project-' + ident, label, 'pass' if available else 'warn',
                                        'Manifest and host command are available; no project was started.' if available else f'Launch executable unavailable: {executable}'))
            else:
                try:
                    catalog.project_root(ident)
                    result.append(check('project-' + ident, label, 'pass', 'Browser build index exists inside its allowed project directory.'))
                except Problem:
                    result.append(check('project-' + ident, label, 'pending', 'Browser build is not available at its configured path.'))
        missing = []
        for ident in catalog.scripts:
            try:
                catalog.script(ident)
            except Problem:
                missing.append(ident)
        result.append(check('script-library', 'Script library files', 'warn' if missing else 'pass',
                            'Unavailable: ' + ', '.join(missing) if missing else f'{len(catalog.scripts)} script files are available; none were launched.'))
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [check('project-manifests', 'Project launcher manifests', 'fail', str(exc))]


def image_stamp(role):
    path = Path('/var/lib/whitecube-labs') / f'wc-{role}.raw'
    try:
        info = path.stat()
        return [info.st_ino, info.st_size, info.st_mtime_ns]
    except OSError:
        return None


def read_smoke_report():
    try:
        info = SMOKE_REPORT.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 65536:
            return None
        value = json.loads(SMOKE_REPORT.read_text())
        if value.get('boot_id') != boot_id() or value.get('revision') != revision():
            return None
        return value
    except (OSError, ValueError, AttributeError):
        return None


def container_checks():
    try:
        status = helper_json('cube-lab', 'status')
        if not isinstance(status, list) or any(not isinstance(row, dict) for row in status):
            raise ValueError('Unexpected container status')
        states = {row.get('id'): row for row in status}
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        return [check('container-' + role, role.title() + ' Lab', 'pending', str(exc)) for role in ROLES]
    smoke = read_smoke_report() or {}
    smoke_rows = smoke.get('results', {})
    result = []
    for role in ROLES:
        row = states.get(role, {'state': 'not-provisioned', 'detail': 'No image reported.'})
        state = row.get('state')
        result.append(check('container-' + role, role.title() + ' Lab image',
                            'pass' if state == 'ready' else 'pending' if state == 'not-provisioned' else 'fail',
                            str(row.get('detail', state)) + ' Runtime startup is checked separately.'))
        prior = smoke_rows.get(role, {})
        same_image = prior.get('image_stamp') is not None and prior.get('image_stamp') == image_stamp(role)
        if state == 'ready' and same_image and prior.get('state') in ('pass', 'fail'):
            result.append(check('container-runtime-' + role, role.title() + ' Lab runtime', prior['state'], prior['detail']))
        else:
            result.append(check('container-runtime-' + role, role.title() + ' Lab runtime', 'pending',
                                'Run sudo cube-check --containers for bounded offline runtime probes after provisioning.'))
    return result


def health_report(projects=None):
    missing = [name for name in BINARY_NAMES if not os.access(BIN / name, os.X_OK)]
    if not shutil.which('foot'):
        missing.append('foot')
    checks = [check('desktop-binaries', 'Desktop and panel programs', 'pending' if missing else 'pass',
                    'Not installed or executable: ' + ', '.join(missing) if missing else 'Compositor, field, foot and workspace helpers are executable.'),
              wayland_check(), pty_check()]
    nodes = list(Path('/dev/dri').glob('renderD*'))
    readable = [p.name for p in nodes if os.access(p, os.R_OK | os.W_OK)]
    checks.append(check('graphics-access', 'Graphics render access', 'pass' if readable else 'pending',
                        'Usable render node permissions: ' + ', '.join(readable) + '. Visual rendering still needs an on-screen check.' if readable else
                        'No accessible DRM render node found; software rendering may be in use.'))
    checks.extend(project_checks(projects))
    checks.extend(network_checks())
    checks.extend(container_checks())
    counts = {state: sum(row['state'] == state for row in checks) for state in ('pass', 'warn', 'fail', 'pending')}
    return {'checks': checks, 'summary': counts, 'ok': not counts['fail'], 'complete': not (counts['fail'] or counts['pending'] or counts['warn']),
            'generated_utc': utc_now(), 'boot_id': boot_id(), 'revision': revision(),
            'metrics': system_metrics(), 'note': 'Live checks are read-only. Pending runtime checks are not a claim of successful startup.'}


def protected_root_directory(path):
    path = Path(path)
    for part in [*reversed(path.parents), path]:
        try:
            part.mkdir(mode=0o755)
        except FileExistsError:
            pass
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Container health storage must have root-owned, unwritable parents without symlinks.')


def write_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_container_probes():
    if os.geteuid() != 0:
        raise RuntimeError('Run sudo cube-check --containers from your terminal to perform the offline runtime checks.')
    # A deliberate CLI request is the only entry to this function; health_report never calls it.
    executable = BIN / 'cube-lab'
    if not executable.is_file():
        raise RuntimeError('cube-lab is not installed')
    states = helper_json('cube-lab', 'status')
    if not isinstance(states, list):
        raise RuntimeError('Invalid container status')
    by_role = {row['id']: row for row in states}
    results = {}
    for role in ROLES:
        row = by_role.get(role, {})
        if row.get('state') != 'ready':
            results[role] = {'state': 'pending', 'detail': 'Image is not ready; no runtime was started.', 'image_stamp': None}
            continue
        argv = [str(executable), 'enter', role, '--offline', '--command', '/bin/bash', '--noprofile', '--norc', '-c',
                'test -r /etc/debian_version && test -x /bin/bash && printf WHITECUBE_LAB_OK']
        try:
            code, output, errors = bounded_command(argv, timeout=25)
            passed = code == 0 and 'WHITECUBE_LAB_OK' in output
            results[role] = {'state': 'pass' if passed else 'fail', 'detail':
                             'Offline container started, Bash ran and exited successfully.' if passed else
                             f'Offline runtime probe failed (exit {code}): {(errors or output)[-500:]}',
                             'image_stamp': image_stamp(role)}
        except (OSError, RuntimeError, TimeoutError) as exc:
            results[role] = {'state': 'fail', 'detail': str(exc), 'image_stamp': image_stamp(role)}
    value = {'boot_id': boot_id(), 'revision': revision(), 'generated_utc': utc_now(), 'results': results}
    protected_root_directory(SMOKE_REPORT.parent)
    write_json(SMOKE_REPORT, value, 0o644)
    return value


def first_boot_report(projects=None):
    destination = state_directory() / 'firstboot.json'
    try:
        cached = json.loads(destination.read_text())
        if cached.get('boot_id') == boot_id() and cached.get('revision') == revision():
            return cached
    except (OSError, ValueError, AttributeError):
        pass
    report = health_report(projects)
    write_json(destination, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='Print the complete report as JSON')
    parser.add_argument('--first-boot', action='store_true', help='Save once per boot/revision under your local state directory')
    parser.add_argument('--containers', action='store_true', help='Explicitly start and exit each ready lab offline; requires root')
    parser.add_argument('--projects', type=Path)
    args = parser.parse_args(argv)
    if args.first_boot and args.containers:
        parser.error('--first-boot is read-only; run --containers separately')
    try:
        if args.containers:
            report = run_container_probes()
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                for role, row in report['results'].items():
                    print(f'{row["state"].upper():7} {role.title()} Lab: {row["detail"]}')
            return int(any(row['state'] == 'fail' for row in report['results'].values()))
        report = first_boot_report(args.projects) if args.first_boot else health_report(args.projects)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            for row in report['checks']:
                print(f'{row["state"].upper():7} {row["label"]}: {row["detail"]}')
            print('Report:', state_directory() / 'firstboot.json' if args.first_boot else 'live checks')
        return int(bool(report['summary']['fail']))
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f'cube-check: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
