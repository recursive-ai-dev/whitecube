"""Small, explicit project manifests and private, durable script run records."""
from __future__ import annotations
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import urllib.parse

ROLES = ('bash', 'web', 'build', 'clean')
IDENT = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def arguments(value, limit=64):
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(x, str) or '\0' in x or len(x) > 4096 for x in value):
        raise ValueError('Arguments must be at most 64 literal strings, each under 4097 characters')
    if sum(map(len, value)) > 32768:
        raise ValueError('Arguments are too large')
    return value

def loopback_url(value, reserved=(8787, 8788)):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(x) < 32 for x in value):
        raise ValueError('Invalid browser URL')
    url = urllib.parse.urlsplit(value)
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost') or url.username or url.password or url.fragment or not url.port or url.port in reserved:
        raise ValueError('Browser URL must use http://127.0.0.1:PORT or http://localhost:PORT on a separate port')
    return value

def confined(root, relative, directory=False):
    if not isinstance(relative, str) or '\0' in relative or '\\' in relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Expected a relative project path')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not (path.is_dir() if directory else path.is_file()):
        raise ValueError('Project path is missing or escapes its directory')
    return path

def load_manifests(root, config=None):
    """Discovery reads only JSON. Commands run only after a deliberate launch."""
    root = Path(root).resolve()
    config = Path(config) if config is not None else Path.home()/'.config/whitecube/scripts.d'
    projects, scripts, errors = {}, {}, []
    def read(path):
        if path.stat().st_size > 65536:
            raise ValueError('Manifest exceeds 64 KiB')
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError('Manifest requires version: 1')
        return value
    def base_item(item, default=None):
        if not isinstance(item, dict):
            raise ValueError('Manifest entries must be JSON objects')
        ident = item.get('id', default)
        if not isinstance(ident, str) or not IDENT.fullmatch(ident):
            raise ValueError('Invalid manifest id')
        name = item.get('name', ident)
        description = item.get('description', '')
        if not isinstance(name, str) or not isinstance(description, str) or len(name)>120 or len(description)>2000:
            raise ValueError('Invalid manifest description')
        return {'id':ident, 'name':name, 'description':description}
    def script_item(item, parent, prefix=''):
        result = base_item(item)
        script = confined(parent, item['path'])
        result['path'] = str(script.relative_to(root))
        result['args'] = arguments(item.get('args', []))
        definitions = item.get('arguments', [])
        if not (isinstance(definitions, str) and len(definitions) <= 4096):
            if not isinstance(definitions, list) or len(definitions)>64:
                raise ValueError('Invalid script argument descriptions')
            for definition in definitions:
                if isinstance(definition, str) and len(definition)<=512:
                    continue
                if not isinstance(definition, dict) or any(not isinstance(definition.get(key,''), str) or len(definition.get(key,''))>1024 for key in ('name','description','default')):
                    raise ValueError('Invalid script argument description')
        if not isinstance(item.get('argument_help',''), str) or len(item.get('argument_help',''))>4096:
            raise ValueError('Invalid argument help')
        if not isinstance(item.get('game',False), bool):
            raise ValueError('game must be true or false')
        tags = item.get('tags', [])
        if not isinstance(tags, list) or len(tags)>32 or any(not isinstance(x,str) or len(x)>80 for x in tags):
            raise ValueError('Invalid script tags')
        for key in ('arguments', 'argument_help', 'game', 'tags'):
            if key in item:
                result[key] = item[key]
        if result['id'] in scripts:
            raise ValueError('Duplicate script id')
        scripts[result['id']] = result
    for path in sorted(root.glob('*/whitecube.project.json')):
        before_projects, before_scripts = projects.copy(), scripts.copy()
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Manifest escapes Projects')
            data = read(path)
            parent = path.parent.resolve()
            result = base_item(data, path.parent.name)
            project_mode = data.get('project_mode', 'ro')
            if project_mode not in ('ro', 'rw'):
                raise ValueError('project_mode must be ro or rw; rw deliberately permits project writes')
            result.update(project_dir=str(parent), project_mode=project_mode)
            def command_spec(launch):
                if isinstance(launch, list):
                    launch = {'command':launch}
                if not isinstance(launch, dict):
                    raise ValueError('Command definition must be an object or literal argv array')
                argv = arguments(launch['command'])
                if not argv or not argv[0]:
                    raise ValueError('Command is empty')
                cwd = confined(parent, launch.get('cwd', '.'), directory=True)
                role = launch.get('container')
                if role is not None and role not in ROLES:
                    raise ValueError('Unknown container')
                if role and cwd != parent:
                    raise ValueError('Container commands start in /project; omit cwd')
                spec = {'command':argv, 'cwd':str(cwd), 'container':role}
                if launch.get('url'):
                    spec['url'] = loopback_url(launch['url'])
                return spec
            command_defs = data.get('commands', {})
            if not isinstance(command_defs, dict) or set(command_defs)-{'build','test','preview'}:
                raise ValueError('commands may define build, test and preview only')
            result['commands'] = {key:command_spec(value) for key,value in command_defs.items()}
            launch = data.get('launch')
            if launch:
                result.update(kind='command', **command_spec(launch))
            else:
                try:
                    dist = confined(parent, data.get('dist', 'dist'), directory=True)
                    confined(dist, 'index.html')
                    result.update(kind=data.get('kind', 'studio'), path=str(dist.relative_to(root)))
                except ValueError:
                    if not result['commands']:
                        raise
                    result.update(kind='workflow')
            if 'workflows' in data:
                from jobs import validate_workflow
                if not isinstance(data['workflows'], list) or len(data['workflows']) > 32:
                    raise ValueError('workflows must be a bounded array')
                result['workflows'] = [validate_workflow(w) for w in data['workflows']]
            if result['id'] in projects:
                raise ValueError('Duplicate project id')
            projects[result['id']] = result
            entries = data.get('scripts', [])
            if not isinstance(entries, list) or len(entries)>128:
                raise ValueError('scripts must be an array of at most 128 objects')
            for item in entries:
                script_item(item, parent)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            projects, scripts = before_projects, before_scripts
            errors.append({'path':str(path), 'message':str(exc)})
    if config.is_dir():
        for path in sorted(config.glob('*.json')):
            try:
                data = read(path)
                script_item(data, root)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({'path':str(path), 'message':str(exc)})
    return projects, scripts, errors

def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.record-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

class RunStore:
    """One file per run: native and browser launches cannot lose each other's history."""
    def __init__(self, state):
        self.path = Path(state)/'runs'
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.stat().st_uid != os.getuid():
            raise RuntimeError('Unsafe run history directory')
        self.path.chmod(0o700)

    def start(self, script_id, name, args, terminal_id, argv, cwd):
        ident = secrets.token_hex(16)
        record = {'id':ident, 'script_id':script_id, 'name':name, 'args':arguments(args), 'terminal_id':terminal_id,
                  'started_at':now(), 'finished_at':None, 'exit_code':None, 'status':'running',
                  'argv':arguments(argv, limit=128), 'cwd':str(cwd), 'owner_pid':os.getpid(),
                  'owner_identity':process_identity(os.getpid())}
        atomic_json(self.path/(ident+'.json'), record)
        return self.path/(ident+'.json')

    @staticmethod
    def public(path):
        record = json.loads(path.read_text())
        result = path.with_suffix('.result')
        if result.is_file():
            record.update(json.loads(result.read_text()))
        elif record.get('status') == 'running':
            try:
                os.kill(record['owner_pid'], 0)
                if record.get('owner_identity') != process_identity(record['owner_pid']):
                    record.update(status='interrupted', finished_at=None, exit_code=None)
            except ProcessLookupError:
                record.update(status='interrupted', finished_at=None, exit_code=None)
            except PermissionError:
                pass
        return {k:v for k,v in record.items() if k not in ('argv', 'cwd', 'owner_pid', 'owner_identity')}

    def history(self):
        records = []
        paths = []
        for path in self.path.glob('*.json'):
            try:
                paths.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                pass
        paths = [path for _,path in sorted(paths, reverse=True)]
        for path in paths:
            try:
                record = self.public(path)
                # Bound retained history; never remove a job that is still running.
                if len(records) >= 1000 and record.get('status') != 'running':
                    path.unlink(missing_ok=True)
                    path.with_suffix('.result').unlink(missing_ok=True)
                else:
                    records.append(record)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return records

def process_identity(pid):
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        started = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
        return boot+':'+started
    except (OSError, IndexError):
        return None
