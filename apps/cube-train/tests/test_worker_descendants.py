"""Workers must not leave training descendants behind when a supervisor exits."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import engine
import worker as worker_module


class WorkerDescendantTests(unittest.TestCase):
    def run_supervisor(self, *, exit_code, ignore_term):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / 'ready'
            pidfile = root / 'child.pid'
            child_script = ('import pathlib,signal,sys,time; '
                            + ('signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if ignore_term else '')
                            + 'pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(60)')
            supervisor = ('import pathlib,subprocess,sys,time; '
                          'child=subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]); '
                          'pathlib.Path(sys.argv[3]).write_text(str(child.pid)); '
                          '\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n'
                          'sys.exit(int(sys.argv[4]))')
            jobpath = root / 'job.json'
            engine.atomic_json(jobpath, {
                'command': [sys.executable, '-c', supervisor, child_script, str(ready), str(pidfile), str(exit_code)],
                'run': str(root), 'prepare_only': False,
            })
            worker = subprocess.Popen([sys.executable, '-c',
                'import sys; sys.path.insert(0,sys.argv[1]); import worker; raise SystemExit(worker.main(sys.argv[2],stop_grace=.2))',
                str(HERE), str(jobpath)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            descendant = None
            try:
                worker.wait(timeout=5)
                self.assertTrue(ready.exists(), 'The descendant installed its signal handler before leader exit')
                descendant = int(pidfile.read_text())
                deadline = time.monotonic() + 2
                while engine.process_identity(descendant) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIsNone(engine.process_identity(descendant), 'An exited supervisor must not leave live training')
                saved = engine.read_json(jobpath)
                self.assertEqual(saved['exit_code'], exit_code)
                self.assertEqual(saved['state'], 'failed' if exit_code else 'completed')
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=5)
                if descendant is None and pidfile.exists():
                    descendant = int(pidfile.read_text())
                if descendant and engine.process_identity(descendant):
                    try:
                        os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_abrupt_supervisor_exit_terminates_descendants(self):
        self.run_supervisor(exit_code=3, ignore_term=False)

    def test_term_ignoring_descendant_is_killed_after_leader_exit(self):
        self.run_supervisor(exit_code=3, ignore_term=True)

    def test_successful_supervisor_also_cleans_background_descendants(self):
        self.run_supervisor(exit_code=0, ignore_term=False)

    def test_reused_leader_pid_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobpath = root / 'job.json'
            engine.atomic_json(jobpath, {'command': ['fake-command'], 'run': str(root)})
            child = Mock(pid=99999999)
            child.wait.return_value = child.poll.return_value = 0
            calls = 0
            def identity(pid):
                nonlocal calls
                if pid == os.getpid():
                    return {'boot_id': 'boot', 'start_ticks': 'worker'}
                calls += 1
                return {'boot_id': 'boot', 'start_ticks': 'original' if calls == 1 else 'reused'}
            with patch('worker.subprocess.Popen', return_value=child), \
                    patch('worker.process_identity', side_effect=identity), patch('worker.os.killpg') as kill:
                self.assertEqual(worker_module.main(jobpath), 0)
                kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
