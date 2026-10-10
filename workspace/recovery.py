"""Private, bounded Whitecube settings backups; never copy images or credentials."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time
import threading
from functools import wraps

MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 512
ID = re.compile(r"[a-z0-9-]{1,80}\Z")
NAME = re.compile(r"[A-Za-z0-9_. -]{1,128}\Z")
SENSITIVE = re.compile(r"(?:password|passwd|passphrase|credential|private.?key|api.?key|access.?token|refresh.?token|secret|authorization|authentication|^auth$|(?:^|[_-])token(?:$|[_-]))", re.I)
STATE_FILES = {'desktop-layout.json', 'accessibility.json', 'ca-seeds.json', 'workflows.json',
               'native-layout.txt', 'native-names.json'}


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _check_components(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('Symlinks are not permitted in backup or restore paths')
    return path


def _read(path, limit=MAX_FILE):
    _check_components(path)
    parent = _parent_fd(path)
    try:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError('Backup files must be bounded regular files')
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        if len(data) > limit:
            raise ValueError('Backup file exceeded its size limit')
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (metadata.st_size, metadata.st_mtime_ns):
            raise ValueError('Source changed while it was being backed up')
        return data
    finally:
        os.close(fd)


def _reject_credentials(data, logical):
    if logical.endswith('.json'):
        value = json.loads(data)
        def inspect(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if SENSITIVE.search(str(key)):
                        raise ValueError('Credential-like keys are excluded from settings backups')
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)
            elif isinstance(item, str) and re.search(r'--(?:password|passwd|token|secret)(?:=|\s)|://[^\s/:]+:[^\s/@]+@', item, re.I):
                raise ValueError('Credential arguments and credential URLs are excluded')
        inspect(value)
    elif logical == 'config/config':
        for line in data.decode('utf-8').splitlines():
            if not line.lstrip().startswith('#'):
                if SENSITIVE.search(line.partition('=')[0]) or re.search(r'--(?:password|passwd|token|secret)(?:=|\s)|://[^\s/:]+:[^\s/@]+@', line, re.I):
                    raise ValueError('Credential-like settings and command arguments are excluded')


def _parent_fd(path, create=False):
    """Walk every ancestor through directory descriptors, rejecting symlink races."""
    path = Path(path).absolute()
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            os.close(parent)
            parent = child
        return parent
    except BaseException:
        os.close(parent)
        raise


def _replace(path, data):
    path = _check_components(path)
    parent = _parent_fd(path, create=True)
    name = '.whitecube-restore-' + secrets.token_hex(12)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            previous = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(previous.st_mode):
                raise ValueError('Restore destination must be a regular file')
        except FileNotFoundError:
            pass
        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


class RecoveryConsole:
    def __init__(self, home=None, projects=None, state=None, backup_root=None):
        self.home = Path(home or Path.home()).absolute()
        self.state = Path(state or self.home / '.local/state/whitecube').absolute()
        self.projects = Path(projects or self.home / 'Projects').absolute()
        self.config = self.home / '.config/whitecube'
        self.ca = self.state.parent / 'whitecube-ca'
        self.root = Path(backup_root or self.state / 'backups').absolute()
        _check_components(self.root)
        self._secret = secrets.token_bytes(32)
        self._lock = threading.RLock()

    def _path(self, logical):
        if not isinstance(logical, str) or '\\' in logical or '\x00' in logical:
            raise ValueError('Invalid backup path')
        parts = logical.split('/')
        if parts == ['config', 'config'] or parts == ['config', 'scripts-foot.ini']:
            return self.config / parts[-1]
        if len(parts) == 3 and parts[:2] == ['config', 'scripts.d'] and NAME.fullmatch(parts[2]) and parts[2].endswith('.json'):
            return self.config / 'scripts.d' / parts[2]
        if len(parts) == 2 and parts[0] == 'state' and parts[1] in STATE_FILES:
            return self.state / parts[1]
        if len(parts) == 2 and parts[0] == 'ca' and NAME.fullmatch(parts[1]) and not parts[1].startswith('.'):
            return self.ca / parts[1]
        if len(parts) == 3 and parts[0] == 'projects' and NAME.fullmatch(parts[1]) and not parts[1].startswith('.') and parts[2] == 'whitecube.project.json':
            return self.projects / parts[1] / parts[2]
        raise ValueError('Backup path is outside the Whitecube settings allowlist')

    def _sources(self):
        names = ['config/config', 'config/scripts-foot.ini'] + ['state/' + name for name in sorted(STATE_FILES)]
        for directory, prefix, suffix in [(self.config / 'scripts.d', 'config/scripts.d', '.json'),
                                           (self.ca, 'ca', None)]:
            _check_components(directory)
            if directory.exists():
                for path in sorted(directory.iterdir()):
                    if not path.name.startswith('.') and (not suffix or path.name.endswith(suffix)):
                        names.append(prefix + '/' + path.name)
        _check_components(self.projects)
        if self.projects.exists():
            for path in sorted(self.projects.iterdir()):
                if not path.name.startswith('.') and (path / 'whitecube.project.json').exists():
                    names.append('projects/' + path.name + '/whitecube.project.json')
        if len(names) > MAX_FILES:
            raise ValueError('Too many settings files for a bounded backup')
        return names

    def _directory(self, identifier):
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise ValueError('Invalid backup identifier')
        return _check_components(self.root / identifier)

    @_serialized
    def get_retention(self):
        path = self.root / 'retention.json'
        if not path.exists():
            return 10
        result = json.loads(_read(path)).get('retention')
        if type(result) is not int or not 1 <= result <= 50:
            raise ValueError('Invalid saved backup retention')
        return result

    @_serialized
    def create(self, label='', retention=None, _recovery=False):
        retention = self.get_retention() if retention is None else retention
        if not isinstance(label, str) or len(label) > 120 or not isinstance(retention, int) or isinstance(retention, bool) or not 1 <= retention <= 50:
            raise ValueError('Label is at most 120 characters; retention is 1..50')
        records, payloads, excluded, total = [], [], [], 0
        for logical in self._sources():
            path = self._path(logical)
            if not path.exists() and not path.is_symlink():
                continue
            try:
                data = _read(path)
                _reject_credentials(data, logical)
            except (ValueError, json.JSONDecodeError, UnicodeError) as exc:
                excluded.append({'path': logical, 'reason': str(exc)})
                continue
            total += len(data)
            if total > MAX_TOTAL:
                raise ValueError('Settings backup exceeds the 32 MiB total limit')
            records.append({'path': logical, 'size': len(data), 'sha256': _digest(data), 'file': str(len(payloads))})
            payloads.append(data)
        _check_components(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if not _recovery:
            _replace(self.root / 'retention.json', json.dumps({'retention': retention}).encode())
        identifier = time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + secrets.token_hex(4)
        directory = self._directory(identifier)
        directory.mkdir(mode=0o700)
        try:
            (directory / 'files').mkdir(mode=0o700)
            for number, data in enumerate(payloads):
                _replace(directory / 'files' / str(number), data)
            manifest = {'version': 1, 'id': identifier, 'created': time.time(), 'label': label,
                        'recovery': _recovery, 'files': records, 'excluded': excluded,
                        'scope': 'Whitecube settings, seeds, workflow definitions and project manifests; no credentials, terminal logs, project source or container images'}
            _replace(directory / 'manifest.json', json.dumps(manifest, indent=2).encode())
            self.verify(identifier)
        except BaseException:
            shutil.rmtree(directory)
            raise
        regular = [item for item in self.list() if not item.get('recovery') and not item.get('invalid')]
        for old in regular[retention:]:
            self.delete(old['id'])
        return {**manifest, 'bytes': total, 'verified': True}

    def _load(self, identifier):
        directory = self._directory(identifier)
        manifest = json.loads(_read(directory / 'manifest.json', MAX_FILE))
        if manifest.get('version') != 1 or manifest.get('id') != identifier or not isinstance(manifest.get('files'), list) or len(manifest['files']) > MAX_FILES:
            raise ValueError('Invalid backup manifest')
        seen, total, payloads = set(), 0, []
        for number, record in enumerate(manifest['files']):
            if not isinstance(record, dict) or record.get('file') != str(number) or record.get('path') in seen:
                raise ValueError('Invalid or duplicate backup record')
            self._path(record.get('path'))
            seen.add(record['path'])
            data = _read(directory / 'files' / str(number))
            if len(data) != record.get('size') or not hmac.compare_digest(_digest(data), str(record.get('sha256'))):
                raise ValueError('Backup verification failed: size or checksum mismatch')
            _reject_credentials(data, record['path'])
            total += len(data)
            if total > MAX_TOTAL:
                raise ValueError('Backup exceeds the total size limit')
            payloads.append(data)
        return manifest, payloads

    @_serialized
    def verify(self, identifier):
        manifest, payloads = self._load(identifier)
        return {'id': identifier, 'verified': True, 'files': len(payloads), 'bytes': sum(map(len, payloads)), 'excluded': manifest.get('excluded', [])}

    @_serialized
    def list(self):
        _check_components(self.root)
        if not self.root.exists():
            return []
        items = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if ID.fullmatch(directory.name):
                try:
                    data = json.loads(_read(directory / 'manifest.json'))
                    items.append({key: data.get(key) for key in ('id', 'created', 'label', 'recovery', 'scope')} | {'files': len(data.get('files', []))})
                except (OSError, ValueError, TypeError):
                    items.append({'id': directory.name, 'invalid': True})
        items.sort(key=lambda item: item.get('created') if isinstance(item.get('created'), (int, float)) else 0, reverse=True)
        return items

    @_serialized
    def review(self, identifier):
        manifest, payloads = self._load(identifier)
        changes = []
        for record, data in zip(manifest['files'], payloads):
            path = _check_components(self._path(record['path']))
            current_data = _read(path) if path.exists() else None
            if current_data is not None:
                # A restore must not destroy an edit that recovery would exclude.
                _reject_credentials(current_data, record['path'])
            current = _digest(current_data) if current_data is not None else None
            changes.append({'path': record['path'], 'current_sha256': current,
                            'saved_sha256': _digest(data), 'size': len(data),
                            'action': 'unchanged' if current == _digest(data) else 'replace' if current else 'create'})
        signed = json.dumps({'id': identifier, 'manifest': manifest, 'changes': changes}, sort_keys=True).encode()
        token = hmac.new(self._secret, signed, hashlib.sha256).hexdigest()
        return {'id': identifier, 'changes': changes, 'review_token': token,
                'scope': manifest['scope'], 'excluded': manifest.get('excluded', []),
                'requires_confirmation': True, 'commands_will_run': False}

    @_serialized
    def restore(self, identifier, review_token):
        review = self.review(identifier)
        if not isinstance(review_token, str) or not hmac.compare_digest(review['review_token'], review_token):
            raise ValueError('Restore review expired or state changed; review the changes again')
        manifest, payloads = self._load(identifier)
        recovery = self.create('Recovery before restoring ' + identifier, _recovery=True)
        originals, applied = [], []
        for record in manifest['files']:
            path = _check_components(self._path(record['path']))
            originals.append(_read(path) if path.exists() else None)
        try:
            # Recheck the entire review after recovery creation, before any writes.
            if not hmac.compare_digest(self.review(identifier)['review_token'], review_token):
                raise ValueError('State changed during recovery preparation; review again')
            for record, data, old in zip(manifest['files'], payloads, originals):
                path = self._path(record['path'])
                if old == data:
                    continue
                _replace(path, data)
                applied.append((path, old))
        except BaseException:
            for path, old in reversed(applied):
                if old is None:
                    _check_components(path).unlink(missing_ok=True)
                else:
                    _replace(path, old)
            raise
        return {'id': identifier, 'restored': len(applied), 'recovery_backup': recovery['id'],
                'commands_started': False, 'reload_required': True}

    @_serialized
    def delete(self, identifier):
        directory = self._directory(identifier)
        # Validate all components and payloads before recursive removal.
        self._load(identifier)
        for path in directory.rglob('*'):
            _check_components(path)
        shutil.rmtree(directory)
        return {'id': identifier, 'deleted': True}
