#!/usr/bin/python3 -I
"""Keep a supervised training job running independently of the GTK window."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import atomic_json, now, process_identity, read_json


def main(path, *, stop_grace=60):
    path = Path(path)
    job = read_json(path)
    command = job.get('command')
    if (not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or '\0' in arg for arg in command)
            or not isinstance(job.get('run'), str)):
        raise ValueError('Invalid training job.')
    child = None
    stopping = False
    stopped_at = None
    requested_signal = signal.SIGTERM

    def stop(signum, frame):
        nonlocal stopping, requested_signal
        stopping = True
        requested_signal = signum

    old_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.signal(signum, stop)

    def group_owned():
        if not child:
            return False
        # Once our leader has been reaped, its PID can be reused. A different
        # live leader must never receive signals intended for this job.
        identity = process_identity(child.pid)
        return child.poll() is None or identity is None or identity == job.get('child_identity')

    def signal_group(signum):
        if group_owned():
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    def group_alive():
        if not group_owned():
            return False
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            return False
        # Orphaned zombies may wait for their new parent to reap them. They
        # cannot train or respond to signals, and must not delay completion.
        for path in Path('/proc').glob('[0-9]*/stat'):
            try:
                fields = path.read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == child.pid and fields[0] not in ('Z', 'X'):
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    def cleanup_group():
        if not child:
            return
        if child.poll() is None or group_alive():
            signal_group(signal.SIGTERM)
            deadline = time.monotonic() + min(stop_grace, 5)
            while time.monotonic() < deadline:
                if child.poll() is not None and not group_alive():
                    break
                time.sleep(.05)
            if child.poll() is None or group_alive():
                signal_group(signal.SIGKILL)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            job.update(state='failed', detail='Training did not exit after process-group cleanup.', exit_code=1)

    try:
        job.update(pid=os.getpid(), identity=process_identity(os.getpid()), state='starting')
        atomic_json(path, job)
        if stopping:
            code = 130
        else:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
            job.update(state='running', child_pid=child.pid, child_identity=process_identity(child.pid))
            atomic_json(path, job)
            while True:
                if stopping and stopped_at is None:
                    stopped_at = time.monotonic()
                    job.update(state='stopping', stop_requested=True)
                    atomic_json(path, job)
                    signal_group(requested_signal)
                try:
                    code = child.wait(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    if stopped_at is not None and time.monotonic() - stopped_at >= stop_grace:
                        job['detail'] = f'Training did not stop within {stop_grace:g} seconds; remaining processes were terminated.'
                        job['forced_stop'] = True
                        signal_group(signal.SIGKILL)
                        try:
                            code = child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            raise RuntimeError('The training process did not exit after termination.')
                        break
        result = read_json(Path(job['run']) / 'result.json')
        state = 'failed' if job.get('forced_stop') else 'stopped' if stopping else 'failed' if code else 'stopped' if result.get('stopped_early') else 'prepared' if job.get('prepare_only') else 'completed'
        if state == 'failed' and not job.get('detail'):
            failure = read_json(Path(job['run']) / 'failure.json')
            job['detail'] = failure.get('reason') or failure.get('error') or (f'Training ended with signal {-code}.' if code < 0 else f'Training exited with code {code}. See the job log for details.')
        job.update(state=state, exit_code=code, finished_at=now())
    except Exception as error:
        job.update(state='failed', detail=str(error), finished_at=now(), exit_code=1)
        print(error, file=sys.stderr)
    finally:
        try:
            # A supervisor can fail or finish before its descendants. Always
            # clean the owned group, including exceptions during state writes.
            cleanup_group()
        except OSError as error:
            job.update(state='failed', detail=f'Unable to clean training processes: {error}', exit_code=1)
        finally:
            try:
                atomic_json(path, job)
            finally:
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
    return job['exit_code']


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
