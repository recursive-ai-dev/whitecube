"""Named script workflows and bounded, cancellable queues. Standard library only."""
import base64
import collections
import json
import os
from pathlib import Path
import secrets
import re
import threading
import time
from workspace_data import arguments, atomic_json, now, RunStore, IDENT


def validate_workflow(data):
    if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not IDENT.fullmatch(data['id']):
        raise ValueError('Workflow requires a valid id')
    name = data.get('name', data['id'])
    if not isinstance(name, str) or len(name) > 120:
        raise ValueError('Invalid workflow name')
    steps = data.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise ValueError('Workflow requires 1 to 64 steps')
    result = {'id':data['id'], 'name':name, 'steps':[], 'stop_on_failure':data.get('stop_on_failure', True), 'notify':data.get('notify', False)}
    if type(result['stop_on_failure']) is not bool or type(result['notify']) is not bool:
        raise ValueError('Workflow options must be booleans')
    ids = set()
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get('id'), str) or not IDENT.fullmatch(step['id']) or step['id'] in ids:
            raise ValueError('Workflow step ids must be unique')
        ids.add(step['id'])
        if not isinstance(step.get('script'), str) or not IDENT.fullmatch(step['script']):
            raise ValueError('Step requires a script id')
        dependencies = step.get('depends_on', [])
        if not isinstance(dependencies, list) or len(dependencies) > 64 or any(not isinstance(d,str) for d in dependencies):
            raise ValueError('Invalid step dependencies')
        clean = {'id':step['id'], 'script':step['script'], 'depends_on':list(dict.fromkeys(dependencies))}
        if 'args' in step:
            clean['args'] = arguments(step['args'])
        result['steps'].append(clean)
    remaining = {s['id']:set(s['depends_on']) for s in result['steps']}
    if any(not deps <= ids or ident in deps for ident,deps in remaining.items()):
        raise ValueError('Unknown or self dependency')
    ordered = []
    while remaining:
        ready = [ident for ident,deps in remaining.items() if not deps]
        if not ready:
            raise ValueError('Cyclic workflow dependencies')
        for ident in ready[:1]:
            ordered.append(next(s for s in result['steps'] if s['id'] == ident))
            remaining.pop(ident)
        for deps in remaining.values():
            deps.difference_update(ready[:1])
    result['steps'] = ordered
    return result

class WorkflowStore:
    def __init__(self, state):
        self.path = Path(state)/'workflows.json'
    def list(self):
        if not self.path.exists():
            return []
        if self.path.is_symlink() or self.path.stat().st_size > 1048576 or self.path.stat().st_uid != os.getuid():
            raise ValueError('Unsafe workflow definitions')
        data = json.loads(self.path.read_text())
        if not isinstance(data, list) or len(data) > 100:
            raise ValueError('Invalid workflow store')
        return [validate_workflow(item) for item in data]
    def save(self, definition):
        definition = validate_workflow(definition)
        values = {item['id']:item for item in self.list()}
        values[definition['id']] = definition
        if len(values) > 100 or len(json.dumps(list(values.values()))) > 1048576:
            raise ValueError('Workflow store is full')
        atomic_json(self.path, list(values.values()))
        return definition

class JobQueue:
    def __init__(self, state, supervisor):
        self.state, self.supervisor = Path(state), supervisor
        self.path = self.state/'jobs'
        self.path.mkdir(mode=0o700, exist_ok=True)
        if self.path.is_symlink() or self.path.stat().st_uid != os.getuid():
            raise ValueError('Unsafe job queue directory')
        self.path.chmod(0o700)
        self.records = {}
        self.pending = collections.deque()
        self.condition = threading.Condition(threading.RLock())
        self.stopping = False
        self.runs = RunStore(state)
        for path in self.path.glob('*.json'):
            if path.is_symlink() or path.stat().st_size > 2097152 or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                continue
            try:
                record = json.loads(path.read_text())
                statuses = ('queued','running','finished','failed','skipped','cancelled','interrupted')
                if not isinstance(record, dict) or not re.fullmatch('[a-f0-9]{32}', str(record.get('id',''))) or record['id'] != path.stem or record.get('status') not in statuses:
                    continue
                if not isinstance(record.get('created_at'),str) or len(record['created_at']) > 80 or not isinstance(record.get('name'),str) or len(record['name']) > 120:
                    continue
                steps = record.get('steps')
                if not isinstance(steps,list) or len(steps) > 64 or any(not isinstance(s,dict) or not isinstance(s.get('id'),str) or not IDENT.fullmatch(s['id']) or s.get('status') not in statuses or not isinstance(s.get('log',''),str) or len(s.get('log','')) > 16384 for s in steps):
                    continue
                if record.get('status') in ('queued','running'):
                    record['status'] = 'interrupted'
                    for step in record['steps']:
                        if step['status'] in ('queued','running'):
                            step['status'] = 'interrupted'
                    atomic_json(path, record)
                self.records[record['id']] = record
            except (ValueError, KeyError, OSError, TypeError, AttributeError):
                continue
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()
    def _persist(self, record):
        atomic_json(self.path/(record['id']+'.json'), record)
    def list(self):
        with self.condition:
            records = sorted(self.records.values(), key=lambda r:r['created_at'], reverse=True)
            active = [r for r in records if r['status'] in ('queued','running')]
            completed = [r for r in records if r['status'] not in ('queued','running')][:20]
            result, budget = [], 65536
            for record in sorted(active+completed, key=lambda r:r['created_at'], reverse=True):
                clean = {k:v for k,v in record.items() if k != 'steps'}
                clean['steps'] = []
                for step in record['steps']:
                    item = {k:v for k,v in step.items() if k not in ('args','depends_on','log')}
                    log = step.get('log','')
                    excerpt = log[-min(budget,4096):] if budget else ''
                    budget -= len(excerpt)
                    item.update(log=excerpt, log_truncated=len(excerpt)<len(log))
                    clean['steps'].append(item)
                result.append(clean)
            return json.loads(json.dumps(result))
    def submit(self, workflow, specifications):
        original_steps = workflow.get('steps', []) if isinstance(workflow, dict) else []
        workflow = validate_workflow(workflow)
        if not isinstance(specifications, list) or len(specifications) != len(workflow['steps']):
            raise ValueError('Invalid resolved workflow steps')
        specification_map = {step['id']:spec for step,spec in zip(original_steps,specifications)}
        specifications = [specification_map[step['id']] for step in workflow['steps']]
        with self.condition:
            if len(self.pending) >= 32:
                raise ValueError('Queue is full')
            ident = secrets.token_hex(16)
            record = {'id':ident, 'name':workflow['name'], 'workflow_id':workflow['id'], 'status':'queued', 'created_at':now(),
                      'notify':workflow['notify'], 'stop_on_failure':workflow['stop_on_failure'], 'steps':[]}
            for step, spec in zip(workflow['steps'], specifications):
                argv = arguments(spec['argv'], limit=128)
                if not argv:
                    raise ValueError('Empty workflow command')
                record['steps'].append({**step, 'args':arguments(spec['args']), 'status':'queued', 'session_id':None, 'run_id':None, 'exit_code':None, 'log':''})
            self.records[ident] = record
            self.pending.append((record, specifications))
            self._persist(record)
            # Bound finished records without deleting pending/live work.
            finished = sorted((r for r in self.records.values() if r['status'] not in ('queued','running')), key=lambda r:r['created_at'], reverse=True)
            for old in finished[100:]:
                self.records.pop(old['id'], None)
                (self.path/(old['id']+'.json')).unlink(missing_ok=True)
            self.condition.notify()
            return {'id':ident, 'status':'queued'}
    def cancel(self, ident):
        with self.condition:
            record = self.records.get(ident)
            if not record:
                raise ValueError('Unknown queued job')
            if record['status'] not in ('queued','running'):
                return {'id':ident, 'status':record['status']}
            record['status'] = 'cancelled'
            running = [s.get('session_id') for s in record['steps'] if s['status'] == 'running']
            for step in record['steps']:
                if step['status'] in ('queued','running'):
                    step['status'] = 'cancelled'
            self._persist(record)
        for session in running:
            if session:
                self.supervisor.close(session)
        return {'id':ident, 'status':'cancelled'}
    def stop(self):
        with self.condition:
            self.stopping = True
            running = [r['id'] for r in self.records.values() if r['status'] in ('queued','running')]
            self.condition.notify_all()
        for ident in running:
            self.cancel(ident)
        self.worker.join(timeout=3)
    def _work(self):
        while True:
            with self.condition:
                while not self.pending and not self.stopping:
                    self.condition.wait()
                if self.stopping:
                    return
                record, specs = self.pending.popleft()
                if record['status'] == 'cancelled':
                    continue
                record['status'] = 'running'
                self._persist(record)
            try:
                self._run(record, specs)
            except Exception as exc:
                with self.condition:
                    if record['status'] != 'cancelled':
                        record['status'], record['error'] = 'failed', str(exc)
                        for step in record['steps']:
                            if step['status'] == 'queued':
                                step['status'] = 'skipped'
                            elif step['status'] == 'running':
                                step['status'] = 'interrupted'
                    self._persist(record)
    def _run(self, record, specs):
        failed = False
        for step, spec in zip(record['steps'], specs):
            with self.condition:
                if record['status'] == 'cancelled':
                    return
                statuses = {s['id']:s['status'] for s in record['steps']}
                if (failed and record['stop_on_failure']) or any(statuses[d] != 'finished' for d in step['depends_on']):
                    step['status'] = 'skipped'
                    self._persist(record)
                    continue
            ident = secrets.token_hex(16)
            run = self.runs.start(step['script'], spec['name'], spec['args'], ident, spec['argv'], spec['cwd'])
            try:
                # Create + record under the cancellation lock so cancelling
                # cannot miss a newly launched process.
                with self.condition:
                    if record['status'] == 'cancelled':
                        atomic_json(run.with_suffix('.result'), {'finished_at':now(), 'status':'cancelled', 'exit_code':None})
                        return
                    session = self.supervisor.create(cwd=spec['cwd'], job=str(run), ident=ident, name=spec['name'])
                    step.update(status='running', session_id=session['id'], run_id=run.stem)
                    self._persist(record)
                offset = 0
                while True:
                    with self.condition:
                        if record['status'] == 'cancelled' or self.stopping:
                            return
                    terminal = self.supervisor.terminals[ident]
                    chunk = terminal.output(offset)
                    offset = chunk['offset']
                    text = base64.b64decode(chunk['output']).decode('utf-8', 'replace')
                    with self.condition:
                        step['log'] = (step['log']+text)[-16384:]
                    result = RunStore.public(run)
                    if result['status'] != 'running':
                        # Drain bounded retained PTY output so a fast multi-MiB
                        # build preserves its final diagnostic rather than its middle.
                        for _ in range(17):
                            chunk = terminal.output(offset)
                            offset = chunk['offset']
                            raw = base64.b64decode(chunk['output'])
                            with self.condition:
                                step['log'] = (step['log']+raw.decode('utf-8','replace'))[-16384:]
                            if not raw:
                                break
                        with self.condition:
                            step['exit_code'] = result['exit_code']
                            step['status'] = 'finished' if result['exit_code'] == 0 else 'failed'
                            failed = failed or step['status'] == 'failed'
                            self._persist(record)
                        break
                    time.sleep(.1)
            except Exception as exc:
                if not run.with_suffix('.result').exists():
                    atomic_json(run.with_suffix('.result'), {'finished_at':now(), 'status':'launch-failed', 'exit_code':None})
                with self.condition:
                    if record['status'] != 'cancelled':
                        step['status'], step['error'] = 'failed', str(exc)
                        failed = True
                        self._persist(record)
            finally:
                self.supervisor.close(ident)
        with self.condition:
            if record['status'] != 'cancelled':
                record['status'] = 'failed' if failed else 'finished'
                record['finished_at'] = now()
                self._persist(record)
