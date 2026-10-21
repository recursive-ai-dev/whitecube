"""Focused stabilization regressions; no privilege or guest execution required."""
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
from unittest import mock

import engine
from test_cube_editor import ProjectFixture
from worker import Worker


class WorkerProcessTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.worker = Worker(self.project, os.getuid(), os.getgid(), 'customize')
        self.worker.emit = mock.Mock()
        self.processes = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        self.launch = launch
        self.addCleanup(self.reap_children)

    def reap_children(self):
        for process in self.processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()

    def test_output_failure_reaps_child_before_caller_cleanup(self):
        def emit(kind, message, **fields):
            if message == 'ready':
                raise OSError('event log is full')

        self.worker.emit.side_effect = emit
        command = [sys.executable, '-c', 'import time; print("ready", flush=True); time.sleep(60)']
        with mock.patch('worker.subprocess.Popen', side_effect=self.launch):
            with self.assertRaisesRegex(OSError, 'event log is full'):
                self.worker.run(command)
        # Callers may now restore DNS, remove staging or roll back tree copies.
        self.assertIsNotNone(self.processes[0].poll())
        self.assertTrue(self.processes[0].stdout.closed)
        self.assertIsNone(self.worker.child)

    def test_cancellation_during_launch_stops_a_silent_child(self):
        marker = self.path / 'unexpected-write'

        def cancel_then_launch(*args, **kwargs):
            self.worker.request_cancel()
            return self.launch(*args, **kwargs)

        script = f'import time; from pathlib import Path; time.sleep(.3); Path({str(marker)!r}).touch()'
        with mock.patch('worker.subprocess.Popen', side_effect=cancel_then_launch):
            with self.assertRaises(engine.Cancelled):
                self.worker.run([sys.executable, '-c', script])
        self.assertFalse(marker.exists())
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertIsNone(self.worker.child)

    def test_non_utf8_tool_output_is_logged_without_aborting_the_tool(self):
        with mock.patch('worker.subprocess.Popen', side_effect=self.launch), mock.patch('builtins.print'):
            output = self.worker.run([sys.executable, '-c', 'import os; os.write(1, b"tool: \\xff\\n")'], capture=True)
        self.assertEqual(output, 'tool: \ufffd\n')
        self.assertIsNone(self.worker.child)

    def test_failed_unmount_is_reported_and_retained_for_retry(self):
        first, second = str(self.path / 'proc'), str(self.path / 'sys')
        self.worker.mounts = [first, second]
        results = [subprocess.CompletedProcess([], 1, 'target is busy'),
                   subprocess.CompletedProcess([], 0, '')]
        with mock.patch('worker.subprocess.run', side_effect=results) as run:
            with self.assertRaisesRegex(engine.CubeError, 'target is busy'):
                self.worker.cleanup_mounts()
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], [second, first])
        self.assertEqual(self.worker.mounts, [second])
        with mock.patch('worker.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '')):
            self.worker.cleanup_mounts()
        self.assertEqual(self.worker.mounts, [])


class InspectionTransactionTests(ProjectFixture):
    def test_inspection_keeps_selection_locked_until_results_are_saved(self):
        self.project.save(squashfs_path='/casper/original.squashfs')
        other = engine.Project.load(self.project.directory)

        def inspect(source, selected):
            self.assertEqual(selected, '/casper/original.squashfs')
            with self.assertRaisesRegex(engine.CubeError, 'Another operation'):
                other.save(squashfs_path='/casper/other.squashfs')
            return {'squashfs_path': selected}

        with mock.patch('engine.inspect_iso', side_effect=inspect):
            result = engine.Engine(self.project).inspect_source()
        self.assertEqual(result['squashfs_path'], '/casper/original.squashfs')
        other.save(name='Lock released')

    def test_inspection_failure_preserves_metadata_and_releases_lock(self):
        before = self.project.data.copy()
        with mock.patch('engine.inspect_iso', side_effect=engine.CubeError('inspection failed')):
            with self.assertRaisesRegex(engine.CubeError, 'inspection failed'):
                engine.Engine(self.project).inspect_source()
        self.assertEqual(engine.Project.load(self.project.directory).data, before)
        engine.Project.load(self.project.directory).save(name='Lock released')
