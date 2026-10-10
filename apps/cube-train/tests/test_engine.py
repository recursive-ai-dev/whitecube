import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import engine
from pacing import TrainingPacer


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trainer = engine.Trainer(home=self.root, state=self.root / 'state', config=self.root / 'config')
        self.trainer.runs_dir = self.root / 'train-out'
        self.corpus = self.root / 'corpus.jsonl'
        self.corpus.write_text('\n'.join(json.dumps({'text': 'Document ' + str(i) + ' A' * 400}) for i in range(30)))
        self.values = {**self.trainer.preferences(), 'python': sys.executable, 'dataset': str(self.corpus), 'name': 'New model',
                       'device': 'cpu', 'model_size': '5m'}

    def make_run(self):
        directory = self.trainer.runs_dir / 'stable-run'
        (directory / 'data').mkdir(parents=True)
        engine.atomic_json(directory / 'run.json', {'steps': 100, 'parameters': 4_987_448, 'batch_size': 2, 'grad_accum_steps': 3})
        engine.atomic_json(directory / 'result.json', {'completed_steps': 20, 'best_val_loss': 2.1, 'stopped_early': True})
        engine.atomic_json(directory / 'progress.json', {'completed_steps': 25})
        for split in ('train', 'val'):
            (directory / 'data' / f'{split}.bin').write_bytes(b'fake corpus')
        (directory / 'ckpt_best.pt').write_bytes(b'fixture weights, never unpickled')
        (directory / 'ckpt_latest.pt').write_bytes(b'latest fixture')
        return directory

    def test_names_selection_and_reports_preserve_files(self):
        directory = self.make_run()
        original = (directory / 'ckpt_best.pt').read_bytes()
        self.trainer.rename_run('stable-run', 'My 5M model')
        run = self.trainer.find_run('stable-run')
        self.assertEqual(run['name'], 'My 5M model')
        self.assertEqual(run['size'], '5m'); self.assertEqual(run['completed_steps'], 25)
        self.assertEqual((directory / 'ckpt_best.pt').read_bytes(), original)
        self.assertEqual(self.trainer.chat_command('stable-run', 'best')[-1], 'stable-run/ckpt_best.pt')
        self.assertEqual(self.trainer.chat_command('stable-run', 'latest')[-1], 'stable-run/ckpt_latest.pt')
        with self.assertRaises(ValueError): self.trainer.find_run('../../elsewhere')
        report = self.trainer.report('stable-run')
        self.assertEqual(report['configuration']['display_name'], 'My 5M model')
        self.assertEqual(report['result']['best_val_loss'], 2.1)

    def test_preferences_are_private_and_atomic(self):
        self.trainer.save_preferences({'model_size': '10m', 'steps': 1500, 'python': '/python path with spaces'})
        self.assertEqual(self.trainer.preferences()['model_size'], '10m')
        self.assertEqual(self.trainer.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.trainer.config.parent.glob('.trainer.json-*')), [])

    def test_validation_corpus_limits_disk_and_resume(self):
        self.assertEqual(self.trainer.validate(self.values)['model_size'], '5m')
        for key, value in [('steps', 0), ('max_tokens', 5000), ('max_temp', 100), ('name', 'bad\nname'), ('model_size', '20m'),
                           ('burst_seconds', 0), ('cooldown_seconds', 601)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.trainer.validate({**self.values, key: value})
        with patch('engine.shutil.disk_usage') as disk:
            disk.return_value.free = 1
            with self.assertRaisesRegex(ValueError, 'disk space'): self.trainer.validate(self.values)
        self.make_run()
        with self.assertRaisesRegex(ValueError, 'above 25'):
            self.trainer.validate({**self.values, 'steps': 25}, 'stable-run')
        self.assertEqual(self.trainer.validate({**self.values, 'steps': 101}, 'stable-run')['steps'], 101)

    def test_empty_and_unsupported_corpus_detected(self):
        empty = self.root / 'empty.md'; empty.touch()
        with self.assertRaisesRegex(ValueError, 'empty'): self.trainer.validate({**self.values, 'dataset': str(empty)})
        bad = self.root / 'bad.bin'; bad.write_bytes(b'data')
        with self.assertRaisesRegex(ValueError, 'TXT'): self.trainer.validate({**self.values, 'dataset': str(bad)})
        self.assertEqual(len(engine.dataset_info(self.corpus)['files']), 1)

    def test_legacy_training_lock_prevents_new_job(self):
        lock = self.root / '.local/state/tensegrity/train.lock'; lock.parent.mkdir(parents=True)
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'Another train process'):
                self.trainer.start(self.values)

    def test_process_identity_prevents_signalling_reused_pid(self):
        self.assertTrue(engine.process_identity(os.getpid()))
        job_path = self.root / 'job.json'
        engine.atomic_json(job_path, {'pid': os.getpid(), 'identity': {'start_ticks': 'wrong', 'boot_id': 'wrong'}})
        with patch('engine.os.kill') as kill, self.assertRaisesRegex(ValueError, 'already stopped'):
            self.trainer.stop({'path': str(job_path)})
        kill.assert_not_called()

    def test_worker_records_exit_and_graceful_stop(self):
        run = self.root / 'fake-run'; run.mkdir()
        job_path = self.root / 'fake-job.json'
        command = [sys.executable, '-u', '-c', 'import signal,time; signal.signal(signal.SIGTERM,lambda *_:exit(0)); print("ready",flush=True); time.sleep(30)']
        log = self.root / 'worker.log'
        engine.atomic_json(job_path, {'run': str(run), 'command': command, 'log': str(log), 'state': 'queued', 'prepare_only': False})
        with log.open('w') as output:
            child = subprocess.Popen([sys.executable, '-I', str(HERE / 'worker.py'), str(job_path)], stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 5
            while 'ready' not in log.read_text() and time.monotonic() < deadline: time.sleep(.02)
            job = engine.read_json(job_path)
            self.assertTrue(engine.job_running(job))
            self.trainer.stop({**job, 'path': str(job_path)})
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual(engine.read_json(job_path)['state'], 'stopped')
            self.assertFalse(engine.job_running(engine.read_json(job_path)))
        finally:
            if child.poll() is None: child.kill(); child.wait()

    def test_log_tail_and_partial_metrics_are_bounded(self):
        run = self.root / 'logs'; run.mkdir()
        (run / 'train_log.jsonl').write_text('broken\n' + json.dumps({'step': 0, 'total_loss': 3.0}) + '\n{"unfinished":')
        self.assertEqual(len(engine.read_metrics(run)), 1)
        large = run / 'console.log'; large.write_text('x' * 200_000)
        self.assertEqual(len(engine.tail_text(large)), 48_000)

    def test_trash_is_recoverable_and_missing_latest_cannot_restart_training(self):
        directory = self.make_run()
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'data-home')}):
            removed = self.trainer.trash('stable-run', 'latest')
            self.assertTrue(removed.is_file()); self.assertFalse((directory / 'ckpt_latest.pt').exists())
            self.assertTrue((directory / 'ckpt_best.pt').exists())
            info = next((self.root / 'data-home/Trash/info').glob('*.trashinfo')).read_text()
            self.assertIn('ckpt_latest.pt', info)
            with self.assertRaisesRegex(ValueError, 'Latest checkpoint'):
                self.trainer.validate({**self.values, 'steps': 200}, 'stable-run')
            removed.rename(directory / 'ckpt_latest.pt')
            self.assertEqual(self.trainer.validate({**self.values, 'steps': 200}, 'stable-run')['steps'], 200)
            removed_run = self.trainer.trash('stable-run')
            self.assertTrue(removed_run.is_dir()); self.assertFalse(directory.exists())

    def test_pacing_waits_only_between_bursts_and_stops_during_cooling(self):
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        events = []
        pacer = TrainingPacer(2, 1, clock=lambda: clock[0], sleep=sleep)
        clock[0] = 1
        self.assertFalse(pacer.after_step(lambda: False, events.append))
        self.assertEqual(events, [])
        clock[0] = 2
        self.assertFalse(pacer.after_step(lambda: False, events.append))
        self.assertAlmostEqual(clock[0], 3)
        self.assertEqual(events, [True, False])
        clock[0] = 5
        self.assertTrue(pacer.after_step(lambda: clock[0] >= 5.3, events.append))
        self.assertLess(clock[0], 6)
        self.assertEqual(events[-2:], [True, False])
        disabled = TrainingPacer(0, 10, clock=lambda: clock[0], sleep=sleep)
        self.assertFalse(disabled.after_step(lambda: False, lambda _: self.fail('Disabled pacing paused')))


if __name__ == '__main__': unittest.main()
