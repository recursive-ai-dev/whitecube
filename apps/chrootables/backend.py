"""Chrootables backend: runs chrootable-manager as the desktop user or through sudo.

No D-Bus, no polkit, no daemon. Read commands run as the user whenever the store
allows it; everything else goes through sudo. A password typed into the app is
handed to `sudo -S -v` once and never kept: sudo's own timestamp covers the
`sudo -n` calls that follow from this process. Every argument is validated here
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
import threading

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


class Manager:
    """Runs chrootable-manager. Thread-safe: each call is one child process."""

    def __init__(self, path: Path | None = None, sudo: str | None = None):
        self.path = Path(path) if path else manager_path()
        self.sudo = sudo or os.environ.get('CHROOTABLES_SUDO') or 'sudo'
        # Set once the store refused an unprivileged read; later reads go to sudo directly.
        self.private_store = False
        self._lock = threading.Lock()

    def argv(self, args: list[str], privileged: bool) -> list[str]:
        command = [str(self.path), *args]
        if not privileged:
            return command
        home = os.environ.get('CHROOTABLE_HOME')
        if home:          # sudo resets the environment; pass a custom store explicitly
            command = ['/usr/bin/env', f'CHROOTABLE_HOME={home}', *command]
        return [self.sudo, '-n', '--', *command]

    @staticmethod
    def _env() -> dict:
        # sudo's own messages in English, so password prompts can be recognised.
        return dict(os.environ, LC_ALL='C.UTF-8')

    def run(self, args: list[str], privileged: bool = False, on_line=None, timeout: float | None = None) -> Result:
        """Run one command. on_line receives each stderr line as it arrives
        (progress messages); stdout is returned whole."""
        argv = self.argv(args, privileged)
        if on_line is None:
            try:
                done = subprocess.run(argv, capture_output=True, text=True, env=self._env(), stdin=subprocess.DEVNULL,
                                      start_new_session=True, timeout=timeout)
            except FileNotFoundError as error:
                return Result(127, '', f'Error: {error.filename} is not installed')
            except subprocess.TimeoutExpired:
                return Result(124, '', 'Error: the command took too long')
            return Result(done.returncode, done.stdout, done.stderr)
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                       text=True, env=self._env(), start_new_session=True, bufsize=1)
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
    def authenticated(self) -> bool:
        """True when sudo would run a command now without asking."""
        try:
            return subprocess.run([self.sudo, '-n', '-v'], capture_output=True, env=self._env(), stdin=subprocess.DEVNULL,
                                  start_new_session=True, timeout=15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def authenticate(self, password: str) -> bool:
        """Validate the password with sudo once; the password is not stored."""
        try:
            done = subprocess.run([self.sudo, '-S', '-p', '', '-v'], input=password + '\n', capture_output=True, text=True,
                                  env=self._env(), start_new_session=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0

    @staticmethod
    def _needs_password(result: Result) -> bool:
        return result.status != 0 and any(text in result.stderr for text in SUDO_NEEDS_PASSWORD)

    # --- commands ---------------------------------------------------------
    def _read(self, args: list[str]) -> Result:
        """As the user first; through sudo once the store turned out to be private."""
        if not self.private_store:
            result = self.run(args)
            if result.ok or 'not readable' not in result.stderr:
                return result
            with self._lock:
                self.private_store = True
        result = self.run(args, privileged=True)
        if self._needs_password(result):
            raise AuthRequired('Your workspaces are in a private store. Unlock with your password to manage them.')
        return result

    def read_json(self, args: list[str]):
        """A --json read of the store."""
        result = self._read(args)
        if not result.ok:
            raise ManagerError(result.message(), result.status, result.stderr)
        return self._parse(result)

    def read_text(self, args: list[str]) -> str:
        """Plain output of a read-only command that never needs the store (--doctor)."""
        result = self.run(args)
        return (result.stdout + result.stderr).strip()

    def change(self, args: list[str], on_line=None) -> Result:
        """A command that needs root. Raises AuthRequired when sudo would ask."""
        result = self.run(args, privileged=True, on_line=on_line)
        if self._needs_password(result):
            raise AuthRequired('Enter your password to continue.')
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
    def workspaces(self) -> list[dict]:
        return sorted(self.read_json(['--list', '--json']), key=lambda w: w.get('name', '').lower())

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
