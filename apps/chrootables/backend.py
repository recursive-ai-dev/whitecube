"""Chrootables backend: runs chrootable-manager as the desktop user or through sudo.

No D-Bus, no polkit, no system daemon. Read commands run as the user whenever the
store allows it; everything else goes to a root helper (broker.py) that unlock
starts with `sudo -S`. The password goes to sudo once and is never kept; the
helper lives only as long as the window (or until it idles out) and only runs
chrootable-manager. Every argument is validated here
with the same rules the manager applies, so the UI can explain mistakes early.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
INSTALLED_MANAGER = Path('/usr/bin/chrootable-manager')
DEFAULT_STORE = Path('/var/lib/chrootable-manager')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
SIZE = re.compile(r'[1-9][0-9]{0,6}\Z')
MAX_SIZE_MIB = 1048576
LIFECYCLES = ('persistent', 'ephemeral')
SESSION_MODES = ('enter', 'try', 'offline')
# Exports are NAME-YYYYmmdd-HHMMSS-TOKEN.tar.gz; the suggestion drops the stamp.
EXPORT_STAMP = re.compile(r'-[0-9]{8}-[0-9]{6}-[A-Za-z0-9]+\Z')
# sudo prints these (with LC_ALL=C) when it would have to ask for a password.
# Privileged commands go to one root helper started with the password (broker.py):
# with timestamp_timeout=0 sudo remembers nothing, so `sudo -S -v` followed by
# `sudo -n` asked again for every command (the password loop).
SUDO_NEEDS_PASSWORD = ('a password is required', 'a terminal is required')


class ManagerError(Exception):
    """A manager command failed; str() is the manager's own explanation."""

    def __init__(self, message: str, status: int = 1, output: str = ''):
        super().__init__(message)
        self.status = status
        self.output = output


class AuthRequired(ManagerError):
    """The command needs root and sudo has no valid timestamp for this process."""


@dataclass
class Result:
    status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.status == 0

    def message(self) -> str:
        """The most useful line for a person: the manager's last error, else its last line."""
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        errors = [line for line in lines if line.startswith('Error: ')]
        line = (errors or lines or [f'chrootable-manager exited with status {self.status}'])[-1]
        return line.removeprefix('Error: ')


def manager_path() -> Path:
    """The checkout copy beside this file wins over the installed command."""
    override = os.environ.get('CHROOTABLES_MANAGER')
    if override:
        return Path(override)
    local = HERE / 'chrootable-manager'
    return local if local.is_file() else INSTALLED_MANAGER


def valid_name(text: str) -> bool:
    return bool(NAME.match(text))


def valid_size(value) -> bool:
    text = str(value)
    return bool(SIZE.match(text)) and int(text) <= MAX_SIZE_MIB


def valid_description(text: str) -> bool:
    return len(text) <= 200 and not any(ord(c) < 32 or ord(c) == 127 for c in text)


def unique_name(base: str, taken) -> str:
    """BASE, or BASE-2, BASE-3 ... when taken; always a valid name."""
    taken = set(taken)
    candidate, number = base[:64], 2
    while candidate in taken:
        candidate = f'{base[:60]}-{number}'
        number += 1
    return candidate


def suggested_import_name(archive: str | Path, taken=()) -> str:
    name = Path(archive).name
    name = name.split('.tar', 1)[0] if '.tar' in name else name.rsplit('.', 1)[0]
    name = EXPORT_STAMP.sub('', name)
    name = re.sub(r'[^A-Za-z0-9._-]', '-', name).lstrip('._-') or 'imported'
    return unique_name(name, taken)


def suggested_import_size(archive_bytes: int) -> int:
    """Room for the unpacked files: four times the archive, whole GiB, at least 2 GiB."""
    size = (archive_bytes * 4 // 1048576 + 1023) // 1024 * 1024
    return min(max(size, 2048), MAX_SIZE_MIB)


def human_bytes(value) -> str:
    value = float(value or 0)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if value < 1024:
            return f'{int(value)} B' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} TiB'


def _require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def create_args(name, size, lifecycle, profile, minimum=1) -> list[str]:
    _require(valid_name(name), 'Names use 1-64 letters, digits, dots, underscores or hyphens and start with a letter or digit.')
    _require(valid_size(size) and int(size) >= minimum, f'Choose a size from {minimum} to {MAX_SIZE_MIB} MiB.')
    _require(lifecycle in LIFECYCLES, 'Choose persistent or ephemeral sessions.')
    _require(bool(re.fullmatch(r'[a-z]+', profile or '')), 'Choose a profile.')
    return ['--create', name, str(int(size)), lifecycle, profile]


def import_args(name, archive, size, lifecycle) -> list[str]:
    _require(valid_name(name), 'Names use 1-64 letters, digits, dots, underscores or hyphens and start with a letter or digit.')
    path = Path(archive)
    _require(path.is_absolute() and path.is_file(), 'Choose an archive file.')
    _require(valid_size(size), f'Choose a size from 1 to {MAX_SIZE_MIB} MiB.')
    _require(lifecycle in LIFECYCLES, 'Choose persistent or ephemeral sessions.')
    return ['--import', name, str(path), str(int(size)), lifecycle]


def workspace_args(action, name, value=None) -> list[str]:
    """Manager arguments for one action on an existing workspace."""
    _require(valid_name(name), 'Unknown workspace.')
    if action in ('clone', 'rename'):
        _require(valid_name(value or ''), 'Names use 1-64 letters, digits, dots, underscores or hyphens and start with a letter or digit.')
        _require(value != name, 'Choose a different name.')
        return [f'--{action}', name, value]
    if action == 'snapshot':
        _require(valid_name(value or ''), 'Snapshot labels follow the same rules as names.')
        return ['--snapshot', name, value]
    if action == 'describe':
        _require(valid_description(value or ''), 'A description is at most 200 characters on one line.')
        return ['--describe', name, value or '']
    if action == 'resize':
        _require(valid_size(value), f'Choose a size up to {MAX_SIZE_MIB} MiB.')
        return ['--resize', name, str(int(value))]
    if action == 'mode':
        _require(value in LIFECYCLES, 'Choose persistent or ephemeral sessions.')
        return ['--mode', name, value]
    if action in ('restore', 'drop-snapshot'):
        _require(valid_name(value or ''), 'Unknown snapshot.')
        return [f'--{action}', name, value, '--yes']
    if action in ('repair', 'delete'):
        return [f'--{action}', name, '--yes']
    if action == 'export':
        return ['--export', name]
    raise ValueError(f'Unknown action: {action}')


def terminal_argv(name: str, mode: str, manager: Path | None = None) -> list[str]:
    """A terminal running an interactive session. It closes when the shell exits
    normally and stays open to show why when the session could not start."""
    _require(valid_name(name), 'Unknown workspace.')
    _require(mode in SESSION_MODES, 'Unknown session type.')
    manager = str(manager or manager_path())
    args = {'enter': ['--enter', name], 'try': ['--try', name], 'offline': ['--enter', name, '--isolated']}[mode]
    keep_open = ('"$@"; status=$?; if [ "$status" -ne 0 ]; then '
                 'printf "\\n[Session ended with status %s. Press Enter to close.]" "$status"; read -r _; fi')
    terminal = shutil.which('whitecube-terminal') or shutil.which('foot') or 'foot'
    title = {'enter': name, 'try': f'{name} (disposable)', 'offline': f'{name} (offline)'}[mode]
    return [terminal, '--title', f'Chrootables · {title}', 'sh', '-c', keep_open, 'chrootables-session',
            'sudo', '--', manager, *args]


def exec_args(name: str, command: str, isolated: bool = False) -> list[str]:
    """One command inside a workspace (`--exec`), run by /bin/sh like a terminal line."""
    _require(valid_name(name), 'Unknown workspace.')
    command = (command or '').strip()
    _require(bool(command), 'Type a command to run.')
    _require(len(command) <= 4000 and '\0' not in command, 'The command is too long.')
    return ['--exec', name, *(['--isolated'] if isolated else []), '--', '/bin/sh', '-c', command]


DOCTOR_LEVELS = ('OK', 'INFO', 'WARN', 'MISSING', 'FAIL', 'ERROR')


def parse_doctor(text: str) -> list[tuple[str, str]]:
    """`--doctor` output as (LEVEL, message) rows; other lines become ('', line)."""
    rows = []
    for line in (text or '').splitlines():
        line = line.rstrip()
        match = re.match(r'\[([A-Z]+)\]\s*(.*)', line)
        if match and match.group(1) in DOCTOR_LEVELS:
            rows.append((match.group(1), match.group(2)))
        elif line.strip():
            rows.append(('', line.strip()))
    return rows


def prune_plan(snapshots: list[dict], keep: int) -> list[str]:
    """Labels to delete so only the newest KEEP snapshots remain (oldest first)."""
    ordered = sorted(snapshots, key=lambda s: (s.get('snapshot_at') or '', s.get('label') or ''))
    keep = max(0, int(keep))
    return [s['label'] for s in (ordered[:-keep] if keep else ordered)]


SORTS = {
    'name': ('Name', lambda w: (w.get('name') or '').lower(), False),
    'used': ('Last used', lambda w: '' if w.get('last_used_at') in (None, 'never', 'unknown') else w['last_used_at'], True),
    'created': ('Created', lambda w: w.get('created_at') or '', True),
    'size': ('Size', lambda w: int(w.get('size_mib') or 0), True),
}
KINDS = ('all', 'persistent', 'ephemeral', 'busy')


def arrange(workspaces: list[dict], sort: str = 'name', kind: str = 'all', query: str = '') -> list[dict]:
    """Filter by lifecycle or status and a free-text query, then sort."""
    query = (query or '').strip().lower()
    chosen = []
    for workspace in workspaces:
        if kind in LIFECYCLES and workspace.get('lifecycle') != kind:
            continue
        if kind == 'busy' and workspace.get('status') != 'busy':
            continue
        text = ' '.join(str(workspace.get(k) or '') for k in ('name', 'description', 'profile')).lower()
        if query and query not in text:
            continue
        chosen.append(workspace)
    _title, key, reverse = SORTS.get(sort, SORTS['name'])
    return sorted(chosen, key=key, reverse=reverse)


def usage_fraction(workspace: dict) -> float:
    """Allocated bytes over capacity, 0..1 (images are preallocated, so mostly 1)."""
    capacity = int(workspace.get('size_mib') or 0) * 1048576
    return min(1.0, int(workspace.get('allocated_bytes') or 0) / capacity) if capacity else 0.0


def space_problem(need_mib, free_bytes) -> str | None:
    """A sentence when the host lacks room for NEED_MIB more (keeping 1 GiB spare)."""
    if free_bytes is None:
        return None
    need = int(need_mib) * 1048576
    if need + 1073741824 > free_bytes:
        return f'Needs {human_bytes(need)}, but only {human_bytes(free_bytes)} is free on the store (1 GiB is kept spare).'
    return None


def config_dir() -> Path:
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'chrootables'


SETTING_DEFAULTS = {'idle_minutes': 15, 'sort': 'name', 'kind': 'all', 'page': 'workspaces', 'selected': None,
                    'width': 1240, 'height': 820, 'console': False, 'auto_refresh': True,
                    'safety_snapshot': True, 'history': []}


def load_settings() -> dict:
    settings = dict(SETTING_DEFAULTS)
    try:
        path = config_dir() / 'settings.json'
        if path.stat().st_size < 262144:
            data = json.loads(path.read_text(encoding='utf-8'))
            for key, default in SETTING_DEFAULTS.items():
                if key in data and (default is None or isinstance(data[key], type(default))):
                    settings[key] = data[key]
    except (OSError, ValueError):
        pass
    settings['history'] = [h for h in settings['history'] if isinstance(h, str)][:20]
    if settings['sort'] not in SORTS:
        settings['sort'] = 'name'
    if settings['kind'] not in KINDS:
        settings['kind'] = 'all'
    return settings


def save_settings(settings: dict) -> None:
    directory = config_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = directory / 'settings.json.tmp'
        temporary.write_text(json.dumps({k: settings.get(k, v) for k, v in SETTING_DEFAULTS.items()}, indent=1),
                             encoding='utf-8')
        temporary.replace(directory / 'settings.json')
    except OSError:
        pass


def store_free_bytes(store: Path | None = None) -> int | None:
    """Free space where workspaces live. statvfs works on the private store path
    itself, because only its parent directories must be searchable."""
    store = Path(store or os.environ.get('CHROOTABLE_HOME') or DEFAULT_STORE)
    for candidate in (store, *store.parents):
        try:
            info = os.statvfs(candidate)
            return info.f_bavail * info.f_frsize
        except OSError:
            continue
    return None


def summarize(workspaces: list[dict]) -> dict:
    return {
        'total': len(workspaces),
        'persistent': sum(1 for w in workspaces if w.get('lifecycle') == 'persistent'),
        'ephemeral': sum(1 for w in workspaces if w.get('lifecycle') == 'ephemeral'),
        'busy': sum(1 for w in workspaces if w.get('status') == 'busy'),
        'allocated': sum(int(w.get('allocated_bytes') or 0) for w in workspaces),
        'snapshots': sum(int(w.get('snapshots') or 0) for w in workspaces),
    }


class Broker:
    """The window's end of broker.py running as root (see that file for the protocol).

    Started once with the password (or with `sudo -n` when sudo needs none). Every
    request is one JSON line; replies are matched by id, so several threads can
    use it at once. on_close(reason) runs (on the reader thread) when it exits."""

    def __init__(self, argv: list[str], env: dict, on_close=None):
        self.argv = argv
        self.env = env
        self.on_close = on_close
        self.process = None
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.errors: list[str] = []
        self.idle_seconds = 0
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next = 0
        self.closed_reason = ''

    @property
    def alive(self) -> bool:
        return self.ready.is_set() and self.process is not None and self.process.poll() is None

    def start(self, password: str | None, timeout: float = 30) -> None:
        """Raises AuthRequired (wrong or missing password) or ManagerError."""
        try:
            self.process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, bufsize=1, env=self.env,
                                            process_group=0)
        except OSError as error:
            raise ManagerError(f'{error.filename or self.argv[0]} is not installed', 127) from error
        threading.Thread(target=self._read_errors, daemon=True).start()
        threading.Thread(target=self._read_replies, daemon=True).start()
        try:
            if password is not None:
                self.process.stdin.write(password + '\n')
                self.process.stdin.flush()
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while not self.ready.is_set() and not self.failed.is_set() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.ready.wait(.2)     # a ready line may still be in the pipe
                break
            self.ready.wait(.05)
        if self.ready.is_set():
            return
        # Wrong password: sudo would ask again on the same pipe. Closing it ends sudo.
        self.close(wait=3)
        text = ''.join(self.errors)
        if any(marker in text for marker in SUDO_NEEDS_PASSWORD) or password is not None and (
                'Sorry, try again' in text or 'incorrect password' in text or 'no password was provided' in text):
            raise AuthRequired('Enter your password to continue.' if password is None else 'That password was not accepted.')
        if 'not in the sudoers' in text or 'not allowed to execute' in text or 'may not run sudo' in text:
            raise ManagerError('This account may not use sudo for chrootable-manager.')
        line = next((l for l in reversed(text.splitlines()) if l.strip()), '')
        raise ManagerError(line.strip() or 'The root helper did not start.')

    def _read_errors(self):
        for line in self.process.stderr:
            self.errors.append(line)
            del self.errors[:-40]
            if 'Sorry, try again' in line or 'incorrect password' in line or any(m in line for m in SUDO_NEEDS_PASSWORD):
                self.failed.set()

    def _read_replies(self):
        for raw in self.process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get('ready'):
                self.idle_seconds = int(message.get('idle') or 0)
                self.ready.set()
                continue
            if message.get('idle'):
                self.closed_reason = 'idle'
                continue
            with self._lock:
                slot = self._pending.get(message.get('id'))
            if not slot:
                continue
            if 'line' in message:
                if slot['on_line']:
                    try:
                        slot['on_line'](str(message['line']))
                    except Exception:       # a UI callback must not kill the reader
                        pass
            elif 'status' in message:
                slot['result'] = Result(int(message['status']), str(message.get('stdout', '')), str(message.get('stderr', '')))
                slot['done'].set()
        self.process.wait()
        with self._lock:
            waiting = list(self._pending.values())
        for slot in waiting:
            if slot['result'] is None:
                slot['result'] = Result(125, '', 'Error: the root helper stopped (locked)')
            slot['done'].set()
        if self.ready.is_set() and self.on_close:
            self.on_close(self.closed_reason or 'exit')

    def request(self, message: dict, on_line=None, on_start=None) -> Result:
        with self._lock:
            self._next += 1
            request_id = self._next
            slot = {'on_line': on_line, 'done': threading.Event(), 'result': None}
            self._pending[request_id] = slot
        if on_start:
            on_start(request_id)
        try:
            with self._write_lock:
                self.process.stdin.write(json.dumps(dict(message, id=request_id)) + '\n')
                self.process.stdin.flush()
        except (OSError, ValueError, AttributeError):
            with self._lock:
                self._pending.pop(request_id, None)
            raise AuthRequired('The root helper is not running. Unlock to continue.')
        slot['done'].wait()
        with self._lock:
            self._pending.pop(request_id, None)
        return slot['result']

    def close(self, wait: float = 0) -> None:
        """Close the helper's stdin: it finishes running operations and exits."""
        if not self.process:
            return
        self.closed_reason = self.closed_reason or 'locked'
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        if wait:
            try:
                self.process.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                pass


class Manager:
    """Runs chrootable-manager. Thread-safe: unprivileged calls are one child process
    each; privileged calls go to one root helper (Broker) started by unlock."""

    def __init__(self, path: Path | None = None, sudo: str | None = None, idle: int | None = None):
        self.path = Path(path) if path else manager_path()
        self.sudo = sudo or os.environ.get('CHROOTABLES_SUDO') or 'sudo'
        self.idle = idle if idle is not None else int(os.environ.get('CHROOTABLES_IDLE') or 900)
        # Set once the store refused an unprivileged read; later reads go to the helper directly.
        self.private_store = False
        self.broker: Broker | None = None
        self.on_lock = None          # called with a reason when the helper exits by itself
        self.running: set[int] = set()
        self._lock = threading.Lock()

    def argv(self, args: list[str], privileged: bool) -> list[str]:
        """The command line for an unprivileged run, or the sudo form for reference
        (privileged commands actually go through the root helper)."""
        command = [str(self.path), *args]
        if not privileged:
            return command
        home = os.environ.get('CHROOTABLE_HOME')
        if home:          # sudo resets the environment; pass a custom store explicitly
            command = ['/usr/bin/env', f'CHROOTABLE_HOME={home}', *command]
        return [self.sudo, '-n', '--', *command]

    def broker_argv(self, password: bool) -> list[str]:
        # -k makes sudo read the password even when it still holds a timestamp, so the
        # line written to its stdin can never reach the helper as input.
        auth = ['-S', '-k', '-p', ''] if password else ['-n']
        command = [sys.executable or '/usr/bin/python3', '-I', str(HERE / 'broker.py'), str(self.path),
                   '--idle', str(int(self.idle))]
        home = os.environ.get('CHROOTABLE_HOME')
        if home:
            command += ['--store', home]
        return [self.sudo, *auth, '--', *command]

    @staticmethod
    def _env() -> dict:
        # sudo's own messages in English, so password prompts can be recognised.
        return dict(os.environ, LC_ALL='C.UTF-8')

    def run(self, args: list[str], privileged: bool = False, on_line=None, timeout: float | None = None,
            merge: bool = False, passive: bool = False) -> Result:
        """Run one command. on_line receives each stderr line as it arrives
        (progress messages; with merge, stdout lines too); stdout is returned whole."""
        if privileged:
            broker = self._broker()
            started = []
            message = {'run': list(args)}
            if merge:
                message['merge'] = True
            if passive:
                message['passive'] = True
            result = broker.request(message, on_line,
                                    on_start=lambda i: (started.append(i), self.running.add(i)))
            for request_id in started:
                self.running.discard(request_id)
            return result
        argv = self.argv(args, privileged)
        if on_line is None:
            try:
                done = subprocess.run(argv, capture_output=True, text=True, env=self._env(), stdin=subprocess.DEVNULL,
                                      process_group=0, timeout=timeout)
            except FileNotFoundError as error:
                return Result(127, '', f'Error: {error.filename} is not installed')
            except subprocess.TimeoutExpired:
                return Result(124, '', 'Error: the command took too long')
            return Result(done.returncode, done.stdout, done.stderr)
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                       text=True, env=self._env(), process_group=0, bufsize=1)
        except FileNotFoundError as error:
            return Result(127, '', f'Error: {error.filename} is not installed')
        stdout_parts = []
        reader = threading.Thread(target=lambda: stdout_parts.append(process.stdout.read()), daemon=True)
        reader.start()
        errors = []
        for line in process.stderr:
            errors.append(line)
            on_line(line.rstrip('\n'))
        process.wait()
        reader.join()
        return Result(process.returncode, ''.join(stdout_parts), ''.join(errors))

    # --- authentication -------------------------------------------------
    def _broker(self) -> Broker:
        """The running helper, or one started without a password; else AuthRequired."""
        with self._lock:
            broker = self.broker
        if broker and broker.alive:
            return broker
        self._start(None)
        return self.broker

    def _start(self, password: str | None) -> None:
        broker = Broker(self.broker_argv(password is not None), self._env(), self._closed)
        broker.start(password)
        with self._lock:
            old, self.broker = self.broker, broker
        if old:
            old.close()

    def _closed(self, reason: str):
        with self._lock:
            current = self.broker
        if current and not current.alive and self.on_lock:
            self.on_lock(reason)

    def unlocked(self) -> bool:
        """True while the root helper runs (no sudo call)."""
        broker = self.broker
        return bool(broker and broker.alive)

    def authenticated(self) -> bool:
        """True when privileged commands can run now without asking: the helper runs,
        or sudo started it without a password."""
        if self.unlocked():
            return True
        try:
            self._start(None)
            return True
        except ManagerError:
            return False

    def authenticate(self, password: str) -> bool:
        """Start the root helper with this password. The password goes to sudo once
        and is not stored. False when sudo rejected it."""
        try:
            self._start(password)
        except AuthRequired:
            return False
        return True

    def lock(self) -> bool:
        """Stop the root helper and forget any sudo timestamp, so the next privileged
        command asks again. True when sudo no longer runs without a password."""
        with self._lock:
            broker, self.broker = self.broker, None
        if broker:
            broker.close(wait=10)
        try:
            subprocess.run([self.sudo, '-k'], capture_output=True, env=self._env(), stdin=subprocess.DEVNULL,
                           process_group=0, timeout=15)
            done = subprocess.run([self.sudo, '-n', '-v'], capture_output=True, env=self._env(),
                                  stdin=subprocess.DEVNULL, process_group=0, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode != 0

    def cancel(self) -> bool:
        """Ask the helper to stop every running privileged command (SIGTERM; the
        manager cleans up partial files). True when something was cancelled."""
        broker = self.broker
        ids = list(self.running)
        if not broker or not broker.alive or not ids:
            return False
        return any(broker.request({'cancel': request_id}).ok for request_id in ids)

    def idle_seconds(self) -> int:
        broker = self.broker
        return broker.idle_seconds if broker else 0

    @staticmethod
    def _needs_password(result: Result) -> bool:
        return result.status != 0 and any(text in result.stderr for text in SUDO_NEEDS_PASSWORD)

    # --- commands ---------------------------------------------------------
    def _read(self, args: list[str], passive: bool = False) -> Result:
        """As the user first; through the root helper once the store turned out to be
        private. Passive reads (background refreshes) never start the helper and do
        not count as activity for its idle lock."""
        if not self.private_store:
            result = self.run(args)
            if result.ok or 'not readable' not in result.stderr:
                return result
            with self._lock:
                self.private_store = True
        if passive and not self.unlocked():
            raise AuthRequired('Your workspaces are in a private store. Unlock with your password to manage them.')
        try:
            return self.run(args, privileged=True, passive=passive)
        except AuthRequired as error:
            raise AuthRequired('Your workspaces are in a private store. Unlock with your password to manage them.') from error

    def read_json(self, args: list[str], passive: bool = False):
        """A --json read of the store."""
        result = self._read(args, passive)
        if not result.ok:
            raise ManagerError(result.message(), result.status, result.stderr)
        return self._parse(result)

    def read_text(self, args: list[str]) -> str:
        """Plain output of a read-only command that never needs the store (--doctor)."""
        result = self.run(args)
        return (result.stdout + result.stderr).strip()

    def change(self, args: list[str], on_line=None, merge: bool = False) -> Result:
        """A command that needs root. Raises AuthRequired when the helper is not
        running and sudo would ask for a password."""
        result = self.run(args, privileged=True, on_line=on_line, merge=merge)
        if not result.ok:
            raise ManagerError(result.message(), result.status, result.stderr + result.stdout)
        return result

    @staticmethod
    def _parse(result: Result):
        text = result.stdout.replace('\r', '')
        start = min((i for i in (text.find('['), text.find('{')) if i >= 0), default=-1)
        if start < 0:
            raise ManagerError('chrootable-manager returned no data.', result.status, result.stderr)
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError as error:
            raise ManagerError(f'Unreadable manager output: {error}', result.status, text) from error

    # --- conveniences used by the window ------------------------------------
    def workspaces(self, passive: bool = False) -> list[dict]:
        return sorted(self.read_json(['--list', '--json'], passive), key=lambda w: w.get('name', '').lower())

    def snapshots(self, name: str) -> list[dict]:
        _require(valid_name(name), 'Unknown workspace.')
        return self.read_json(['--snapshots', name, '--json'])

    def exports(self) -> list[dict]:
        return self.read_json(['--exports', '--json'])

    def activity(self, count: int = 100) -> list[dict]:
        return list(reversed(self.read_json(['--activity', str(int(count)), '--json'])))

    def profiles(self) -> list[dict]:
        result = self.run(['--profiles', '--json'])
        if not result.ok:
            raise ManagerError(result.message(), result.status, result.stderr)
        return self._parse(result)

    def doctor(self) -> str:
        return self.read_text(['--doctor'])

    def logs(self, name: str) -> str:
        _require(valid_name(name), 'Unknown workspace.')
        result = self._read(['--logs', name])
        return (result.stdout if result.ok else result.message()).strip()
