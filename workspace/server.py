#!/usr/bin/env python3
"""Whitecube local browser workspace. Python standard library; no pip/npm.

The terminal API and project apps deliberately use separate loopback origins.
Terminal requests require a random session token, exact Host and same Origin.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import errno
import fcntl
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
from pathlib import Path
import pty
import re
import secrets
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.parse
import urllib.request
from workspace_data import arguments, load_manifests, loopback_url, RunStore
from session_broker import BrokerError

BASE = Path(__file__).resolve().parent
MAX_BODY = 131072
MAX_OUTPUT = 4 * 1024 * 1024
STATIC_EXTENSIONS = {'.html','.js','.mjs','.css','.json','.png','.jpg','.jpeg','.webp','.gif','.svg','.ico','.woff','.woff2','.ttf','.otf','.mp3','.ogg','.wav','.mp4','.webm','.glb','.gltf','.bin','.wasm','.txt'}


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def safe_file(root: Path, relative: str) -> Path:
    """No dotfiles, parent traversal or symlinks escaping the selected dist."""
    if '\\' in relative or '\x00' in relative:
        raise Problem(404, 'File not found')
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(x.startswith('.') for x in parts):
        raise Problem(404, 'File not found')
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise Problem(404, 'File not found')
    return candidate


def discover_projects(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    for path in [Path.home()/'Projects']:
        if path.is_dir():
            return path.resolve()
    return Path.home()/'Projects'


class Catalog:
    def __init__(self, projects, path=BASE/'catalog.json'):
        self.root = Path(projects).resolve()
        self.path = Path(path)
        self.reload()

    def reload(self):
        data = json.loads(self.path.read_text())
        projects = {p['id']: p for p in data['projects']}
        scripts = {p['id']: p for p in data['scripts']}
        added_projects, added_scripts, errors = load_manifests(self.root)
        projects.update(added_projects)
        scripts.update(added_scripts)
        # Publish fully assembled dictionaries: request threads never iterate
        # a dictionary that discovery is changing in place.
        self.projects, self.scripts, self.errors = projects, scripts, errors

    def project_root(self, ident):
        item = self.projects.get(ident)
        if not item:
            raise Problem(404, 'Unknown project')
        if item.get('kind') in ('command','workflow'):
            raise Problem(404, 'Command projects are served by their own process')
        root = (self.root / item['path']).resolve()
        if not root.is_relative_to(self.root) or not (root/'index.html').is_file():
            raise Problem(404, 'Project build not available')
        return root

    def script(self, ident):
        item = self.scripts.get(ident)
        if not item:
            raise Problem(404, 'Unknown script')
        root = BASE/'bin' if 'builtin' in item else self.root
        path = safe_file(root, item.get('builtin', item.get('path')))
        return path

    @staticmethod
    def public_project(item):
        result = {k:v for k,v in item.items() if k not in ('path','command','cwd','project_dir','commands','workflows')}
        result['commands'] = {key:{k:v for k,v in spec.items() if k in ('container','url')} for key,spec in item.get('commands', {}).items()}
        if item.get('project_mode') == 'rw':
            result['sharing_scope'] = 'Writable: entire selected project at /project, including its files. Other projects are excluded.'
        return result

    def public(self, apps_port):
        projects, scripts = [], []
        labs = lab_status() if any('lab' in p for p in self.scripts.values()) else {}
        for ident, item in self.projects.items():
            if item.get('kind') in ('command','workflow'):
                projects.append(self.public_project(item))
                continue
            try:
                self.project_root(ident)
            except Problem:
                continue
            projects.append(self.public_project(item) | {'url':f'http://127.0.0.1:{apps_port}/apps/{ident}/index.html'})
        for ident, item in self.scripts.items():
            try:
                self.script(ident)
            except Problem:
                continue
            public = {k:v for k,v in item.items() if k not in ('path','builtin')}
            if 'lab' in item:
                state = labs.get(item['lab'], {}).get('state', 'unknown')
                public['state'] = state
                if state != 'ready':
                    public['description'] += ' Not provisioned yet; run sudo cube-lab provision --install-tools on the NVMe.' if state == 'not-provisioned' else ' Check cube-lab status before entering.'
            scripts.append(public)
        return {'projects':projects, 'scripts':scripts, 'manifest_errors':self.errors}


class Terminal:
    def __init__(self, cwd, script=None, cols=100, rows=30, job=None, ident=None):
        self.id = ident or secrets.token_hex(16)
        self.job = job
        self.lock = threading.RLock()
        self.buffer = bytearray()
        self.start = 0
        self.exited = False
        self._closed = False
        self.exit_code = None
        self.last_access = time.monotonic()
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        env = dict(os.environ, TERM='xterm-256color', COLORTERM='truecolor', PS1='\\[\\e[1;37m\\]cube\\[\\e[0m\\] \\w $ ', PYTHONUNBUFFERED='1')
        # The session token never enters the child process environment.
        argv = [sys.executable, str(BASE/'server.py'), '_shell']
        if job:
            argv.extend(['--job', str(job)])
        elif script:
            argv.extend(['--script', str(script)])
        try:
            self.process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, close_fds=True, start_new_session=True)
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(self.master, False)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                if self._closed:
                    break
                try:
                    ready, _, _ = select.select([self.master], [], [], 0.2)
                except (OSError, ValueError):
                    if self._closed:
                        break
                    raise
                if not ready:
                    if self.process.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    if exc.errno == errno.EAGAIN:
                        continue
                    raise
                if not chunk:
                    break
                with self.lock:
                    self.buffer.extend(chunk)
                    if len(self.buffer) > MAX_OUTPUT:
                        removed = len(self.buffer) - MAX_OUTPUT
                        del self.buffer[:removed]
                        self.start += removed
        finally:
            with self.lock:
                self.exited = True
                self.exit_code = self.process.poll()

    def output(self, offset):
        self.last_access = time.monotonic()
        with self.lock:
            reset = offset < self.start
            pos = min(max(offset, self.start), self.start + len(self.buffer))
            raw = bytes(self.buffer[pos-self.start:pos-self.start+262144])
            drained = pos + len(raw) >= self.start + len(self.buffer)
            result = {'output':base64.b64encode(raw).decode(), 'offset':pos+len(raw), 'exited':self.exited and drained, 'truncated':reset, 'exit_code':self.exit_code}
            if getattr(self, 'job', None):
                try:
                    job = RunStore.public(self.job)
                    result.update(job_status=job['status'], job_exit_code=job['exit_code'])
                except (OSError, ValueError, KeyError):
                    pass
            if getattr(self, 'preview_url', None):
                result['preview_url'] = self.preview_url
                result['preview_ready'] = self.preview_ready()
            return result

    def preview_ready(self):
        if getattr(self, 'job', None):
            try:
                status = RunStore.public(self.job)
                if status['status'] != 'running' and (status['status'] != 'finished' or status['exit_code'] != 0):
                    self._preview_ready = False
                    return False
            except (OSError, ValueError, KeyError):
                return False
        if time.monotonic() - getattr(self, '_preview_checked', 0) < 2:
            return getattr(self, '_preview_ready', False)
        self._preview_checked = time.monotonic()
        self._preview_ready = False
        try:
            class NoPreviewRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args, **_kwargs):
                    return None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoPreviewRedirect())
            request = urllib.request.Request(loopback_url(self.preview_url), method='HEAD')
            with opener.open(request, timeout=.3) as response:
                self._preview_ready = 200 <= response.status < 400
        except (OSError, ValueError):
            pass
        return self._preview_ready

    def write(self, data):
        self.last_access = time.monotonic()
        with self.lock:
            if self.exited:
                raise Problem(410, 'Terminal has exited')
            pending = memoryview(data)
            deadline = time.monotonic() + 2
            while pending:
                if time.monotonic() > deadline:
                    raise Problem(429, 'Terminal input is busy')
                try:
                    n = os.write(self.master, pending)
                    pending = pending[n:]
                except BlockingIOError:
                    select.select([], [self.master], [], 0.1)

    def resize(self, cols, rows):
        cols, rows = int(cols), int(rows)
        if not 2 <= cols <= 500 or not 1 <= rows <= 200:
            raise Problem(400, 'Terminal dimensions out of range')
        with self.lock:
            if not self.exited:
                fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def _session_groups(self):
        """Include disowned jobs, while never signalling another PTY session."""
        groups = set()
        for stat in Path('/proc').glob('[0-9]*/stat'):
            try:
                # comm may contain spaces and parentheses. Fields after its
                # closing ')' are state, parent PID, process group, session ID.
                fields = stat.read_text().rsplit(')', 1)[1].split()
                group, session = int(fields[2]), int(fields[3])
                if session == self.process.pid and fields[0] != 'Z' and group > 1 and group != os.getpgrp():
                    groups.add(group)
            except (OSError, IndexError, ValueError):
                continue
        return groups

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self.exited = True
            # Hang up the actual TTY first; privileged sudo/container monitors
            # receive the kernel's hangup even if our UID cannot signal them.
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = -1
        # Bash may have disowned background groups or jobs that ignore HUP.
        # Re-read membership before each escalation so only this session's
        # remaining groups are signalled, including a foreground game.
        for sig, grace in ((signal.SIGHUP, 0.15), (signal.SIGTERM, 0.35), (signal.SIGKILL, 0)):
            groups = self._session_groups()
            if not groups:
                break
            for group in groups:
                try:
                    os.killpg(group, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            if grace:
                deadline = time.monotonic() + grace
                while time.monotonic() < deadline and self._session_groups():
                    time.sleep(0.02)
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        with self.lock:
            self.exit_code = self.process.returncode
        self.reader.join(timeout=1)
        if self.job and not self.job.with_suffix('.result').exists():
            from workspace_data import atomic_json, now
            atomic_json(self.job.with_suffix('.result'), {'finished_at':now(), 'exit_code':None, 'status':'interrupted'})


class Workspace:
    def __init__(self, projects, port=8787, apps_port=8788, token=None, persistent=False):
        self.port, self.apps_port = port, apps_port
        self.token = token or (browser_token(runtime_dir()) if persistent else secrets.token_urlsafe(32))
        self.catalog = Catalog(projects)
        self.terminals = {}
        self.lock = threading.Lock()
        self.runs = RunStore(runtime_dir())
        from desktop_state import DesktopStore
        from jobs import WorkflowStore
        self.desktop = DesktopStore(runtime_dir())
        self.workflows = WorkflowStore(runtime_dir())
        from recovery import RecoveryConsole
        self.recovery = RecoveryConsole(projects=self.catalog.root, state=runtime_dir())
        self.broker = None
        if persistent:
            from session_broker import SessionBroker
            self.broker = SessionBroker(runtime_dir())

    def authorize(self, headers, api=False):
        if headers.get('Host') != f'127.0.0.1:{self.port}':
            raise Problem(403, 'Invalid Host')
        origin = headers.get('Origin')
        if origin is not None and origin != f'http://127.0.0.1:{self.port}':
            raise Problem(403, 'Cross-origin request denied')
        if headers.get('Sec-Fetch-Site') == 'cross-site':
            raise Problem(403, 'Cross-site request denied')
        if api and not hmac.compare_digest(headers.get('X-Whitecube-Token', ''), self.token):
            raise Problem(401, 'Open this workspace with whitecube-web open to authenticate')

    def new_terminal(self, data, command=None, name=None, cwd=None, script_id=None, preview_url=None):
        script = self.catalog.script(data['script']) if data.get('script') else None
        args = arguments(data.get('args', []))
        if script:
            item = self.catalog.scripts[data['script']]
            if 'args' not in data:
                args = arguments(item.get('args', []))
            command = ['/bin/bash', str(script), *args]
            name, script_id = item['name'], item['id']
        cwd = cwd or (script.parent if script else Path.home())
        with self.lock:
            broker = getattr(self, 'broker', None)
            count = len(broker.sessions()) if broker else len(self.terminals)
            if count >= 8:
                raise Problem(409, 'Eight sessions are open. Explicitly terminate one to continue.')
            ident = secrets.token_hex(16)
            job = self.runs.start(script_id, name, args, ident, command, cwd) if command else None
            try:
                if broker:
                    broker.create(cwd=str(cwd), job=str(job) if job else None, ident=ident, name=name or 'Bash', preview_url=preview_url)
                    terminal = broker.terminal(ident)
                else:
                    terminal = Terminal(cwd, job=job, ident=ident)
                    terminal.preview_url = preview_url
            except BaseException:
                if job:
                    from workspace_data import atomic_json, now
                    atomic_json(job.with_suffix('.result'), {'finished_at':now(), 'exit_code':None, 'status':'launch-failed'})
                raise
            self.terminals[terminal.id] = terminal
        return {'id':terminal.id, 'run_id':job.stem if job else None, 'name':name or 'Bash'}

    def action(self, data):
        action = data.get('action')
        if action in ('network-manage','network-scan','network-add','network-reconnect'):
            argv = ['sudo','/usr/bin/cube-net',action.removeprefix('network-')]
        elif action in ('network-connect','network-remove'):
            profile = data.get('profile')
            if not isinstance(profile, str) or not re.fullmatch(r'[a-f0-9]{16}', profile):
                raise Problem(400, 'Invalid saved network profile')
            argv = ['sudo','/usr/bin/cube-net',action.removeprefix('network-'),profile]
        elif action in ('lab-snapshot','lab-restore','lab-enter','lab-network','lab-limits'):
            lab = data.get('lab')
            if lab not in ('bash','web','build','clean'):
                raise Problem(400, 'Unknown lab')
            operation = 'resources' if action == 'lab-limits' else ('network-mode' if action == 'lab-network' else action.removeprefix('lab-'))
            argv = ['sudo','/usr/bin/cube-lab',operation,lab]
            if action in ('lab-snapshot','lab-restore'):
                name = data.get('name')
                if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,47}', name):
                    raise Problem(400, 'Invalid snapshot name')
                argv.append(name)
            if action == 'lab-limits':
                import math
                cpu, memory = data.get('cpu'), data.get('memory_mib')
                if isinstance(cpu, bool) or not isinstance(cpu, (int,float)) or not math.isfinite(cpu) or not .1 <= cpu <= 128 or type(memory) is not int or not 128 <= memory <= 65536:
                    raise Problem(400, 'CPU must be .1 to 128 cores; RAM must be 128 to 65536 MiB')
                argv.extend(['--cpu',str(cpu),'--memory-mib',str(memory)])
            if action == 'lab-network':
                if data.get('mode') not in ('shared','offline'):
                    raise Problem(400, 'Network mode must be shared or offline')
                argv.append(data['mode'])
        else:
            raise Problem(400, 'Unknown control action')
        return self.new_terminal({}, command=argv, name=action.replace('-',' ').title(), script_id='action:'+action)

    def launch_project(self, ident):
        self.catalog.reload()
        item = self.catalog.projects.get(ident)
        if not item or item.get('kind') != 'command':
            raise Problem(404, 'Unknown command project')
        argv, cwd = list(item['command']), item['cwd']
        if item.get('container'):
            argv = ['sudo','/usr/bin/cube-lab','enter',item['container'],'--project',item['project_dir'],'--project-mode',item.get('project_mode','ro'),'--command',*argv]
        url = loopback_url(item['url'], reserved=(self.port,self.apps_port)) if item.get('url') else None
        result = self.new_terminal({}, command=argv, cwd=cwd, name=item['name'], script_id='project:'+ident)
        return result | {'url':url, 'name':item['name']}

    def project_run(self, data):
        self.catalog.reload()
        item = self.catalog.projects.get(data.get('id'))
        action = data.get('action')
        if not item or action not in ('build','test','preview') or action not in item.get('commands', {}):
            raise Problem(404, 'Project action is not defined in its manifest')
        spec = item['commands'][action]
        lab = data.get('lab', spec.get('container'))
        if lab == 'host':
            lab = None
        if lab is not None and lab not in ('bash','web','build','clean'):
            raise Problem(400, 'Unknown lab')
        argv, cwd = list(spec['command']), spec['cwd']
        if lab:
            if Path(cwd) != Path(item['project_dir']):
                raise Problem(400, 'Lab actions must start at the project root')
            argv = ['sudo','/usr/bin/cube-lab','enter',lab,'--project',item['project_dir'],'--project-mode',item.get('project_mode','ro'),'--command',*argv]
        url = loopback_url(spec['url'], reserved=(self.port,self.apps_port)) if action == 'preview' and spec.get('url') else None
        result = self.new_terminal({}, command=argv, cwd=cwd, name=item['name']+' '+action, script_id='project:'+item['id']+':'+action, preview_url=url)
        return result | {'url':url, 'preview':action == 'preview'}

    def workflow_list(self):
        values = {w['id']:w for p in self.catalog.projects.values() for w in p.get('workflows', [])}
        values.update({w['id']:w for w in self.workflows.list()})
        return list(values.values())

    def queue_workflow(self, data):
        self.catalog.reload()
        workflow = next((w for w in self.workflow_list() if w['id'] == data.get('id')), None)
        if not workflow:
            raise Problem(404, 'Unknown workflow')
        if not self.broker:
            raise Problem(503, 'Persistent broker is required for queued workflows')
        specifications = []
        for step in workflow['steps']:
            script = self.catalog.script(step['script'])
            item = self.catalog.scripts[step['script']]
            args = arguments(step.get('args', item.get('args', [])))
            specifications.append({'name':item['name'], 'argv':['/bin/bash',str(script),*args], 'args':args, 'cwd':str(script.parent)})
        return self.broker.request('queue', workflow=workflow, steps=specifications)

    def frame_sources(self):
        origins = {f'http://127.0.0.1:{self.apps_port}'}
        for item in self.catalog.projects.values():
            urls = [item.get('url')] + [spec.get('url') for spec in item.get('commands', {}).values()]
            for url in filter(None, urls):
                try:
                    parsed = urllib.parse.urlsplit(loopback_url(url, reserved=(self.port,self.apps_port)))
                    origins.add(f'{parsed.scheme}://{parsed.netloc}')
                except ValueError:
                    pass
        return ' '.join(sorted(origins))

    def api(self, method, path, query, data):
        if path == '/api/sessions' and method == 'GET':
            broker = getattr(self, 'broker', None)
            return {'sessions':broker.sessions() if broker else [{'id':t.id, 'name':'Bash', 'exited':t.exited} for t in self.terminals.values()]}
        for endpoint, getter, setter in [('layout','get_layout','save_layout'), ('settings','get_settings','save_settings'), ('seeds','get_seeds','save_seeds')]:
            if path == '/api/'+endpoint:
                if method == 'GET':
                    value = getattr(self.desktop, getter)()
                    return {'seeds':value, 'exists':(self.desktop.state/'ca-seeds.json').exists()} if endpoint == 'seeds' else value
                if method == 'POST':
                    value = getattr(self.desktop, setter)(data.get('seeds', []) if endpoint == 'seeds' else data)
                    return {'seeds':value, 'exists':(self.desktop.state/'ca-seeds.json').exists()} if endpoint == 'seeds' else value
        if path.startswith('/api/backups'):
            console = self.recovery
            if path == '/api/backups' and method == 'GET':
                return {'backups':console.list(), 'retention':console.get_retention()}
            if path == '/api/backups' and method == 'POST':
                return console.create(data.get('label',''), data.get('retention'))
            if path == '/api/backups/verify' and method == 'POST':
                return console.verify(data['id'])
            if path == '/api/backups/review' and method == 'POST':
                return console.review(data['id'])
            if path == '/api/backups/restore' and method == 'POST':
                return console.restore(data['id'], data['review_token'])
        if path == '/api/workflows':
            if method == 'GET':
                self.catalog.reload()
                return {'workflows':self.workflow_list()}
            if method == 'POST':
                return self.workflows.save(data)
        if path == '/api/workflows/run' and method == 'POST':
            return self.queue_workflow(data)
        if path == '/api/jobs' and method == 'GET':
            return self.broker.request('jobs') if getattr(self, 'broker', None) else {'jobs':[]}
        cancelled = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/cancel', path)
        if cancelled and method == 'POST':
            return self.broker.request('cancel', id=cancelled[1])
        if path == '/api/project/run' and method == 'POST':
            return self.project_run(data)
        if path == '/api/catalog' and method == 'GET':
            if hasattr(self.catalog, 'reload'):
                self.catalog.reload()
            result = self.catalog.public(self.apps_port)
            result['network'] = network_status()
            return result
        if path == '/api/history' and method == 'GET':
            return {'runs':self.runs.history()}
        if path == '/api/labs' and method == 'GET':
            return {'labs':list(lab_status().values())}
        if path == '/api/network' and method == 'GET':
            return network_status()
        if path == '/api/system' and method == 'GET':
            from health import system_metrics
            return system_metrics()
        if path == '/api/health' and method == 'GET':
            from health import health_report
            return health_report(projects=self.catalog.root)
        if path == '/api/action' and method == 'POST':
            return self.action(data)
        project = re.fullmatch(r'/api/project/([a-z0-9-]+)/launch', path)
        if project and method == 'POST':
            return self.launch_project(project[1])
        if path == '/api/terminal' and method == 'POST':
            return self.new_terminal(data)
        match = re.fullmatch(r'/api/terminal/([a-f0-9]{32})(?:/(input|resize|run))?', path)
        if not match:
            raise Problem(404, 'Unknown API route')
        ident, action = match.groups()
        terminal = self.broker.terminal(ident) if getattr(self, 'broker', None) else self.terminals.get(ident)
        if terminal is None:
            raise Problem(404, 'Terminal not found; start a new session')
        if method == 'GET' and not action:
            return terminal.output(int(query.get('offset', ['0'])[0]))
        if method == 'DELETE' and not action:
            with self.lock:
                self.terminals.pop(ident, None)
            terminal.close()
            return {'closed':True}
        if method == 'POST' and action == 'input':
            try:
                raw = base64.b64decode(data['data'], validate=True)
            except (ValueError, binascii.Error):
                raise Problem(400, 'Invalid input encoding')
            if len(raw) > 65536:
                raise Problem(413, 'Input too large')
            terminal.write(raw)
            return {'ok':True}
        if method == 'POST' and action == 'resize':
            terminal.resize(data['cols'], data['rows'])
            return {'ok':True}
        if action == 'run':
            raise Problem(409, 'Start a separate terminal with {script: id} to preserve running jobs')
        raise Problem(405, 'Method not allowed')

    def close(self):
        # Workspace shutdown detaches. The independent broker owns live PTYs.
        if getattr(self, 'broker', None):
            self.terminals.clear()
            return
        for terminal in list(self.terminals.values()):
            terminal.close()
        self.terminals.clear()


def lab_status():
    for path in (Path('/usr/bin/cube-lab'), BASE/'containers/cube-lab'):
        if path.is_file():
            try:
                result = subprocess.run([sys.executable, str(path), 'status'], capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    value = json.loads(result.stdout)
                    if isinstance(value, list):
                        return {item['id']:item for item in value if isinstance(item, dict) and item.get('id') in ('bash','web','build','clean')}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    return {}


def network_status():
    for path in (Path('/usr/bin/cube-net'), BASE/'network/cube-net'):
        if path.is_file():
            try:
                result = subprocess.run([sys.executable, str(path), 'status'], capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    return json.loads(result.stdout)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    return {'state':'unknown', 'usb_id':'2001:3329', 'driver':'rtw88_8822cu', 'message':'Dongle status unavailable'}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'Whitecube/1.0'

    def log_message(self, *_args):
        pass  # Tokens, script input and query strings never enter access logs.

    def reply(self, status, body, content_type='application/json; charset=utf-8', apps=False):
        if isinstance(body, (dict,list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Cache-Control','no-store' if content_type.startswith('application/json') else 'no-cache')
        if not apps:
            self.send_header('X-Frame-Options','DENY')
            sources = self.server.workspace.frame_sources()
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src %s; object-src 'none'; base-uri 'none'; frame-ancestors 'none'" % sources)
        else:
            self.send_header('Content-Security-Policy', f"frame-ancestors http://127.0.0.1:{self.server.workspace.port}")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def handle_request(self):
        try:
            self.connection.settimeout(10)
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            work = self.server.workspace
            if self.server.apps:
                if self.headers.get('Host') != f'127.0.0.1:{work.apps_port}':
                    raise Problem(403, 'Invalid Host')
                if self.command not in ('GET','HEAD'):
                    raise Problem(405, 'Read-only project assets')
                match = re.fullmatch(r'/apps/([a-z0-9-]+)/(.*)', path)
                if not match:
                    raise Problem(404, 'Project not found')
                ident, rel = match.groups()
                asset = safe_file(work.catalog.project_root(ident), rel or 'index.html')
                if asset.suffix.lower() not in STATIC_EXTENSIONS:
                    raise Problem(404, 'File not found')
                body = asset.read_bytes()
                if asset.suffix == '.html':
                    # Built Vite projects often use /assets; scope to their own dist.
                    body = body.replace(b'"/assets/', f'"/apps/{ident}/assets/'.encode()).replace(b"'/assets/", f"'/apps/{ident}/assets/".encode())
                return self.reply(200,body,mimetypes.guess_type(asset.name)[0] or 'application/octet-stream',apps=True)
            work.authorize(self.headers, api=path.startswith('/api/'))
            if path.startswith('/api/'):
                if self.command not in ('GET','POST','DELETE'):
                    raise Problem(405, 'Method not allowed')
                data = {}
                if self.command == 'POST':
                    if self.headers.get_content_type() != 'application/json':
                        raise Problem(415, 'Use application/json')
                    length = int(self.headers.get('Content-Length','0'))
                    limit = 1048704 if path == '/api/seeds' else MAX_BODY
                    if length < 0 or length > limit:
                        raise Problem(413, 'Request too large')
                    data = json.loads(self.rfile.read(length) or b'{}')
                    if not isinstance(data, dict):
                        raise Problem(400, 'Expected a JSON object')
                return self.reply(200,work.api(self.command,path,query,data))
            if self.command not in ('GET','HEAD'):
                raise Problem(405, 'Read-only assets')
            asset = safe_file(BASE/'web', 'index.html' if path=='/' else path.lstrip('/'))
            return self.reply(200,asset.read_bytes(),mimetypes.guess_type(asset.name)[0] or 'application/octet-stream')
        except (Problem, BrokerError) as exc:
            self.reply(exc.status, {'error':exc.message if isinstance(exc, Problem) else str(exc)})
        except (ValueError, KeyError, TypeError):
            self.reply(400, {'error':'Invalid request'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.reply(500, {'error':'Workspace operation failed; inspect the local service log'})

    do_GET = do_POST = do_DELETE = do_HEAD = do_OPTIONS = handle_request


def browser_token(state):
    """Private credential reused across server restarts, excluded from backups."""
    path = Path(state)/'browser.token'
    try:
        fd = os.open(path, os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        try:
            fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd,'w') as out:
                out.write(token)
                out.flush()
                os.fsync(out.fileno())
            return token
        except FileExistsError:
            return browser_token(state)
    with os.fdopen(fd) as handle:
        metadata = os.fstat(handle.fileno())
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 or metadata.st_size > 128:
            raise RuntimeError('Unsafe browser credential file')
        token = handle.read()
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise RuntimeError('Invalid browser credential file')
        return token


def runtime_dir():
    path = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'whitecube'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError('Unsafe Whitecube state directory')
    path.chmod(0o700)
    return path


def serve(args):
    if args.port == args.apps_port:
        raise RuntimeError('Workspace and projects need different ports')
    state = runtime_dir()
    lock = open(state/'server.lock','a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('Whitecube browser workspace is already running')
    work = Workspace(discover_projects(args.projects),args.port,args.apps_port,persistent=True)
    servers = []
    started = []
    info_file = state/'server.json'
    try:
        for port, apps in [(args.port,False),(args.apps_port,True)]:
            server = http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler)
            server.daemon_threads = True
            server.workspace, server.apps = work, apps
            servers.append(server)
        info = {'pid':os.getpid(),'port':args.port,'apps_port':args.apps_port,'token':work.token,'projects':str(work.catalog.root)}
        fd = os.open(info_file, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd,'w') as out:
            os.fchmod(out.fileno(),0o600)
            json.dump(info,out)
        for server in servers:
            threading.Thread(target=server.serve_forever,daemon=True).start()
            started.append(server)
        stop = threading.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        print(f'Whitecube workspace: http://127.0.0.1:{args.port}/ (use whitecube-web open)',flush=True)
        stop.wait()
    finally:
        for server in servers:
            if server in started:
                server.shutdown()
            server.server_close()
        work.close()
        info_file.unlink(missing_ok=True)
        lock.close()


def running_info():
    try:
        path = runtime_dir()/'server.json'
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            return None
        info = json.loads(path.read_text())
        port = int(info['port'])
        url = f'http://127.0.0.1:{port}/api/catalog'
        request = urllib.request.Request(url,headers={'X-Whitecube-Token':info['token']})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=1) as response:
            if response.status == 200:
                return info
    except (OSError, ValueError, KeyError):
        pass
    return None


def open_workspace(args):
    info = running_info()
    if not info:
        log = runtime_dir()/'server.log'
        with log.open('ab') as output:
            os.chmod(log,0o600)
            argv=[sys.executable,str(BASE/'server.py'),'serve','--port',str(args.port),'--apps-port',str(args.apps_port)]
            if args.projects:
                argv += ['--projects',args.projects]
            process=subprocess.Popen(argv,stdout=output,stderr=output,start_new_session=True)
        for _ in range(40):
            time.sleep(0.1)
            info=running_info()
            if info:
                break
            if process.poll() is not None:
                break
        if not info:
            raise RuntimeError(f'Could not start workspace. See {log}')
    url=f"http://127.0.0.1:{info['port']}/#token={info['token']}"
    if args.print_url:
        print(url)
        return
    import shutil
    for browser in ('firefox','google-chrome','chromium','chromium-browser'):
        binary=shutil.which(browser)
        if binary:
            env=dict(os.environ,MOZ_ENABLE_WAYLAND='1')
            argv=[binary,'--new-window',url] if browser=='firefox' else [binary,'--ozone-platform=wayland',f'--app={url}']
            subprocess.Popen(argv,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            return
    raise RuntimeError('No browser installed; run whitecube-web open --print-url')


def run_shell(args):
    fcntl.ioctl(0,termios.TIOCSCTTY,0)
    if args.job or args.script:
        # Bash remains the interactive parent so its job control puts the game
        # in the foreground and an interrupt returns to a usable shell prompt.
        # The path is data, never interpolated into shell source.
        env = dict(os.environ)
        if args.job:
            env.update(WHITECUBE_JOB=str(args.job), WHITECUBE_JOB_RUNNER=str(BASE/'run_job.py'))
        else:
            env['WHITECUBE_START_SCRIPT'] = str(args.script)
        os.execve('/bin/bash',['bash','--noprofile','--rcfile',str(BASE/'shell-init.bash'),'-i'],env)
    os.execv('/bin/bash',['bash','--noprofile','--norc','-i'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('serve','open'):
        p=sub.add_parser(command)
        p.add_argument('--projects')
        p.add_argument('--port',type=int,default=8787)
        p.add_argument('--apps-port',type=int,default=8788)
        if command=='open':
            p.add_argument('--print-url',action='store_true')
    sub.add_parser('check')
    p=sub.add_parser('_shell');p.add_argument('--script');p.add_argument('--job')
    args=parser.parse_args()
    if args.command=='_shell':
        run_shell(args)
    if args.command=='check':
        print(json.dumps(Catalog(discover_projects()).public(8788),indent=2));return
    if os.getuid()==0:
        raise RuntimeError('Run the browser terminal as your desktop user, not root')
    if args.command=='serve':
        serve(args)
    else:
        open_workspace(args)


if __name__=='__main__':
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print(f'whitecube-web: {exc}',file=sys.stderr)
        sys.exit(1)
