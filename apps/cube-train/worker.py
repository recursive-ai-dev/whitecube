#!/usr/bin/python3 -I
"""Keep a supervised training job running independently of the GTK window."""
import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import atomic_json, now, process_identity, read_json


def main(path):
    path = Path(path)
    job = read_json(path)
    if not job or not isinstance(job.get('command'), list):
        raise ValueError('Invalid training job.')
    job.update(pid=os.getpid(), identity=process_identity(os.getpid()), state='starting')
    atomic_json(path, job)
    child = None
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        job.update(state='stopping', stop_requested=True)
        atomic_json(path, job)
        if child and child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        if stopping:
            code = 130
        else:
            child = subprocess.Popen(job['command'])
            if stopping:
                child.terminate()
            job['state'] = 'stopping' if stopping else 'running'
            atomic_json(path, job)
            code = child.wait()
        result = read_json(Path(job['run']) / 'result.json')
        state = 'stopped' if stopping else 'failed' if code else 'stopped' if result.get('stopped_early') else 'prepared' if job['prepare_only'] else 'completed'
        job.update(state=state, exit_code=code, finished_at=now())
    except Exception as error:
        job.update(state='failed', detail=str(error), finished_at=now(), exit_code=1)
        print(error, file=sys.stderr)
    finally:
        atomic_json(path, job)
    return job['exit_code']


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
