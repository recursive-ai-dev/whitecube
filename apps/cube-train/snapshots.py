"""Disposable filesystem caches for the trainer's background UI refresh.

Only the refresh worker owns this cache. Returned records are independent, so
GTK callbacks cannot mutate an index being used by a later refresh.
"""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

import engine


def signature(path):
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


class RunSnapshots:
    def __init__(self, trainer, summarize):
        self.trainer = trainer
        self.summarize = summarize
        self.json_cache = OrderedDict()
        self.json_bytes = 0
        self.sparks = {}
        self.metrics = {}

    def read_json(self, path):
        fingerprint = signature(path)
        old = self.json_cache.pop(path, None)
        if old:
            self.json_bytes -= old[2]
        if old and old[0] == fingerprint:
            entry = old
        else:
            entry = fingerprint, engine.read_json(path), min(fingerprint[1], 1024 * 1024) if fingerprint else 0
        self.json_cache[path] = entry
        self.json_bytes += entry[2]
        while self.json_bytes > 32 * 1024 * 1024 or len(self.json_cache) > 8192:
            _, evicted = self.json_cache.popitem(last=False)
            self.json_bytes -= evicted[2]
        return deepcopy(entry[1])

    def read_metrics(self, run):
        key = run['id']
        fingerprint = run['_log_signature']
        previous = self.metrics.get(key)
        if previous is None or previous[0] != fingerprint:
            previous = fingerprint, engine.read_metrics(run['directory'])
            self.metrics[key] = previous
        return previous[1]

    def collect(self, selected_id=None, compare_id=None, last_job=None, force=False):
        jobs = self.trainer.jobs()
        runs = self.trainer.list_runs(jobs=jobs, reader=self.read_json, include_progress=True)
        active = next((job for job in jobs if job['running']), None)
        present = {run['id'] for run in runs}
        self.sparks = {key: value for key, value in self.sparks.items() if key in present}
        selected = next((run for run in runs if run['id'] == selected_id), None)
        if selected is None:
            selected = next((run for run in runs if active and run['directory'] == active['run']), None) or next(iter(runs), None)
        detail_ids = {(selected or {}).get('id'), compare_id}
        for run in runs:
            logfile = Path(run['directory']) / 'train_log.jsonl'
            fingerprint = signature(logfile)
            run['_log_signature'] = fingerprint
            old = self.sparks.get(run['id'])
            if force or old is None or old[0] != fingerprint:
                # read_metrics already bounds its input and ignores incomplete JSONL.
                metrics = engine.read_metrics(run['directory']) if fingerprint else []
                self.sparks[run['id']] = fingerprint, self.summarize(metrics)
                if run['id'] in detail_ids:
                    self.metrics[run['id']] = fingerprint, metrics
            run['_spark_points'] = self.sparks[run['id']][1]
        details = {}
        if selected:
            metrics = self.read_metrics(selected)
            details = {'id': selected['id'], 'metrics': metrics,
                       'telemetry': engine.latest_telemetry(selected['directory']),
                       'checkpoints': self.trainer.checkpoints(selected['id'], run=selected, metrics=metrics),
                       'benchmarks': self.trainer.benchmarks(selected['id'], run=selected),
                       'previews': self.trainer.previews(selected['id'], run=selected)}
        compare = next((run for run in runs if run['id'] == compare_id), None)
        comparison = {'id': compare_id, 'metrics': self.read_metrics(compare)} if compare else None
        self.metrics = {key: value for key, value in self.metrics.items()
                        if key in {details.get('id'), compare_id}}
        job = active or (selected or {}).get('job') or last_job
        if job and not selected and job.get('path'):
            job = {**job, **self.read_json(Path(job['path']))}
        logfile = (selected.get('job') or {}).get('log', str(Path(selected['directory']) / 'console.log')) if selected else (job or {}).get('log')
        return deepcopy({'runs': runs, 'active': active, 'details': details, 'comparison': comparison,
                'job': job, 'log': engine.tail_text(logfile) if logfile else '',
                'chat_available': engine.chat_launcher() is not None})
