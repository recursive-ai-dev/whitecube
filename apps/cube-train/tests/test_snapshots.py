"""Background run snapshots remain accurate without repeatedly parsing libraries."""
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from snapshots import RunSnapshots


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        with patch.dict('os.environ', {'WHITECUBE_TRAIN_OUT': str(self.root / 'runs')}):
            self.trainer = engine.Trainer(home=self.root, state=self.root / 'state', config=self.root / 'config')
        self.trainer.runs_dir.mkdir()
        self.snapshots = RunSnapshots(self.trainer, lambda metrics: [(item['step'], item['total_loss']) for item in metrics if 'total_loss' in item])

    def add_run(self, name, step=10):
        directory = self.trainer.runs_dir / name
        directory.mkdir()
        engine.atomic_json(directory / 'run.json', {'steps': 100, 'model_size': '2m', 'device': 'cpu', 'display_name': name})
        engine.atomic_json(directory / 'progress.json', {'completed_steps': step, 'tokens_per_second': 123})
        return directory

    def test_large_library_uses_one_job_scan_and_reuses_unchanged_json(self):
        for index in range(400):
            self.add_run(f'run-{index:04d}')
        with patch.object(self.trainer, 'jobs', wraps=self.trainer.jobs) as jobs, patch.object(engine, 'read_json', wraps=engine.read_json) as read:
            first = self.snapshots.collect()
            self.assertEqual(len(first['runs']), 400)
            self.assertEqual(jobs.call_count, 1)
            self.assertGreater(read.call_count, 400)
        with patch.object(engine, 'read_json', side_effect=AssertionError('Unchanged JSON was reparsed')):
            second = self.snapshots.collect()
        self.assertEqual(first['runs'], second['runs'])
        second['runs'][0]['meta']['display_name'] = 'A GTK callback changed its copy'
        self.assertNotEqual(self.snapshots.collect()['runs'][0]['name'], 'A GTK callback changed its copy')

    def test_changed_deleted_new_runs_and_partial_logs_refresh_correctly(self):
        first = self.add_run('first')
        second = self.add_run('second')
        log = first / 'train_log.jsonl'
        log.write_text(json.dumps({'step': 10, 'total_loss': 3}) + '\n{partial')
        (first / 'ckpt_latest.pt').write_bytes(b'fixture, never loaded')
        snapshot = self.snapshots.collect('first', 'second')
        self.assertEqual(snapshot['details']['metrics'][0]['total_loss'], 3)
        self.assertEqual(snapshot['details']['checkpoints'][0]['step'], 10)
        engine.atomic_json(first / 'progress.json', {'completed_steps': 20})
        log.write_text(json.dumps({'step': 20, 'total_loss': 2}) + '\n')
        shutil.rmtree(second)
        self.add_run('third')
        snapshot = self.snapshots.collect('first', 'second')
        self.assertEqual({run['id'] for run in snapshot['runs']}, {'first', 'third'})
        self.assertEqual(snapshot['details']['metrics'][0]['step'], 20)
        self.assertEqual(snapshot['details']['checkpoints'][0]['step'], 20)
        self.assertIsNone(snapshot['comparison'])
        self.assertNotIn('second', self.snapshots.sparks)
        fallback = self.snapshots.collect('removed-selection')
        self.assertIn(fallback['details']['id'], {'first', 'third'})

    def test_simultaneous_job_scans_do_not_remove_an_exited_child_twice(self):
        entered, second_poll, release = threading.Event(), threading.Event(), threading.Event()
        count_lock = threading.Lock()
        count = 0
        class ExitedChild:
            def poll(self):
                nonlocal count
                with count_lock:
                    count += 1
                    call = count
                if call == 1:
                    entered.set()
                    release.wait(3)
                else:
                    second_poll.set()
                return 0
        self.trainer.children = [ExitedChild()]
        errors = []
        def scan():
            try:
                self.trainer.jobs()
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=scan) for _ in range(2)]
        workers[0].start()
        self.assertTrue(entered.wait(2))
        workers[1].start()
        # Without synchronization the second scan removes the child while the
        # first is paused in poll(), then the first raises list.remove(...).
        second_poll.wait(.1)
        release.set()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.trainer.children, [])

    def test_job_cleanup_cannot_lose_a_concurrent_child_registration(self):
        entered, release = threading.Event(), threading.Event()
        class ExitedChild:
            def poll(self):
                entered.set(); release.wait(3); return 0
        class RunningChild:
            def poll(self):
                return None
        self.trainer.pending_jobs['old'] = {'child': ExitedChild(), 'identity': 'old'}
        scanner = threading.Thread(target=self.trainer.jobs)
        child = RunningChild()
        registrar = threading.Thread(target=self.trainer._register_child, args=(child, 'new', 'new identity'))
        scanner.start(); self.assertTrue(entered.wait(2)); registrar.start()
        release.set()
        scanner.join(3); registrar.join(3)
        self.assertFalse(scanner.is_alive() or registrar.is_alive())
        self.assertEqual(self.trainer.pending_jobs, {'new': {'child': child, 'identity': 'new identity'}})
        self.assertEqual(self.trainer.children, [child])


try:
    import app
except (ImportError, ValueError):
    app = None


@unittest.skipUnless(app, 'GTK bindings are required for the callback boundary')
class RefreshBoundaryTests(unittest.TestCase):
    def window(self):
        callbacks = []
        window = SimpleNamespace(closed=False, refresh_running=False, refresh_pending=False,
            refresh_force=False, selected_id='first', selected_compare=lambda: None, last_job=None,
            snapshots=MagicMock(), apply_refresh=MagicMock(), status=MagicMock(),
            background=lambda work, done: callbacks.append((work, done)))
        window.refresh = lambda force=False: app.TrainerWindow.refresh(window, force)
        return window, callbacks

    def test_refresh_coalesces_ticks_and_never_applies_an_old_selection(self):
        window, callbacks = self.window()
        window.refresh(); window.refresh(); window.refresh()
        self.assertEqual(len(callbacks), 1)
        self.assertTrue(window.refresh_running)
        window.snapshots.collect.assert_not_called()
        callbacks[0][0]()
        window.snapshots.collect.assert_called_once_with('first', None, None, False)
        window.selected_id = 'second'
        queued = []
        with patch.object(app.GLib, 'idle_add', side_effect=lambda callback: queued.append(callback)):
            callbacks[0][1]({'obsolete': True}, None)
        window.apply_refresh.assert_not_called()
        self.assertFalse(window.refresh_running)
        self.assertEqual(len(queued), 1)
        queued[0]()
        callbacks[1][0]()
        self.assertEqual(window.snapshots.collect.call_args.args[0], 'second')

    def test_forced_refresh_invalidates_inflight_data_and_errors_release_the_gate(self):
        window, callbacks = self.window()
        window.refresh(); window.refresh(True)
        queued = []
        with patch.object(app.GLib, 'idle_add', side_effect=lambda callback: queued.append(callback)):
            callbacks[0][1]({'obsolete': True}, None)
        window.apply_refresh.assert_not_called()
        queued[0]()
        callbacks[1][0]()
        self.assertTrue(window.snapshots.collect.call_args.args[-1])
        callbacks[1][1](None, OSError('disk unavailable'))
        window.status.set_text.assert_called_with('disk unavailable')
        self.assertFalse(window.refresh_running)
        window.closed = True
        window.refresh()
        self.assertEqual(len(callbacks), 2)

    def test_removed_selection_uses_the_run_whose_details_the_worker_collected(self):
        runs = [{'id': 'newest', 'directory': '/runs/newest'},
                {'id': 'active', 'directory': '/runs/active'}]
        snapshot = {'runs': runs, 'active': {'run': '/runs/active'},
                    'details': {'id': 'active', 'metrics': [{'step': 10, 'total_loss': 2}]}}
        class StopAtDetailRendering(Exception):
            pass
        window = SimpleNamespace(selected_id='removed', displayed_id='removed',
            launching=False, observed_active=None, last_job=None,
            last_log=None, metrics_signature=None, preview_request=0,
            clear_preview=MagicMock(), load_notes=MagicMock(side_effect=StopAtDetailRendering))
        with self.assertRaises(StopAtDetailRendering):
            app.TrainerWindow.apply_refresh(window, snapshot)
        self.assertEqual(window.selected_id, snapshot['details']['id'])
        window.load_notes.assert_called_once_with(runs[1])


if __name__ == '__main__':
    unittest.main()
