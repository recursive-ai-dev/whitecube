"""Regression tests for malformed state, launch races and worker lifecycle."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import engine


class EngineReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trainer = engine.Trainer(home=self.root, state=self.root / 'state', config=self.root / 'config')
        self.trainer.runs_dir = self.root / 'runs'
        self.corpus = self.root / 'source.jsonl'
        self.corpus.write_text('{"text":"hello"}\n', encoding='utf-8')
        self.values = {**self.trainer.preferences(), 'dataset': str(self.corpus), 'python': sys.executable, 'device': 'cpu'}

    def run_dir(self):
        run = self.trainer.runs_dir / 'example'
        run.mkdir(parents=True)
        return run

    def test_json_reader_handles_invalid_encoding_depth_and_nonfinite_values(self):
        path = self.root / 'state.json'
        for data in (b'\xff', b'[1]', b'{"loss": NaN}', b'{"loss": 1e999}', b'[' * 1500, b' ' * (1024 * 1024 + 1)):
            with self.subTest(data=data[:30]):
                path.write_bytes(data)
                self.assertEqual(engine.read_json(path), {})

    def test_preferences_restore_valid_keys_and_recover_invalid_types(self):
        engine.atomic_json(self.trainer.config, {'steps': 'bad', 'batch_size': True, 'model_size': [],
                                               'device': 'tpu', 'python': None, 'draft_name': 'Restored draft',
                                               'cooldown_seconds': 99999, 'low_priority': 'false'})
        prefs = self.trainer.preferences()
        self.assertEqual(prefs['steps'], 1000)
        self.assertEqual(prefs['batch_size'], 4)
        self.assertEqual(prefs['model_size'], '2m')
        self.assertEqual(prefs['device'], 'cuda')
        self.assertIsInstance(prefs['python'], str)
        self.assertFalse(prefs['low_priority'])
        self.assertEqual(prefs['draft_name'], 'Restored draft')
        self.trainer.save_preferences({'unknown': 1, 'steps': 2000})
        self.assertNotIn('unknown', engine.read_json(self.trainer.config))

    def test_missing_or_invalid_process_identity_is_never_running(self):
        for pid in (None, 0, -1, True, '100', 99999999, []):
            self.assertIsNone(engine.process_identity(pid))
            self.assertFalse(engine.job_running({'pid': pid, 'identity': None}))
        self.assertFalse(engine.job_running({'pid': os.getpid(), 'identity': {}}))
        self.assertTrue(engine.job_running({'pid': os.getpid(), 'identity': engine.process_identity(os.getpid())}))

    def test_invalid_preferences_never_replace_the_last_valid_configuration(self):
        self.trainer.save_preferences({'steps':2000, 'device':'cpu'})
        before = self.trainer.config.read_bytes()
        for value in ({'steps':True}, {'steps':1.5}, {'steps':float('nan')}, {'device':'tpu'}, {'model_size':'100m'}, {'low_priority':'false'}, {'draft_name':'bad\nname'}, {'column':'x' * 4097}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.trainer.save_preferences(value)
            self.assertEqual(self.trainer.config.read_bytes(), before)

    def test_invalid_job_paths_do_not_break_run_listing(self):
        self.run_dir()
        for index, value in enumerate(('\0', '', 'relative/path')):
            engine.atomic_json(self.trainer.state / 'jobs' / f'{index}.json',
                               {'run': value, 'log': str(self.root / 'job.log')})
        self.assertEqual(self.trainer.jobs(), [])
        self.assertEqual(len(self.trainer.list_runs()), 1)
        self.assertEqual(engine.tail_text('\0'), '')
        self.assertEqual(engine.tail_text(None), '')

    def test_numeric_validation_does_not_truncate_or_accept_booleans(self):
        for value in (True, None, [], 1.5, float('inf'), '1.5'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'whole number'):
                self.trainer.validate({**self.values, 'steps': value})
        self.assertEqual(self.trainer.validate({**self.values, 'steps': '120'})['steps'], 120)

    def test_text_field_and_toggle_validation(self):
        for field in ('document..text', '.text', 'text.', 'te\nxt', None):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'text field'):
                self.trainer.validate({**self.values, 'column': field})
        for key in ('low_priority', 'stagger_training'):
            with self.assertRaisesRegex(ValueError, 'enabled or disabled'):
                self.trainer.validate({**self.values, key: 'false'})
        with self.assertRaises(ValueError):
            self.trainer.validate({**self.values, 'dataset': ''})
        with self.assertRaises(ValueError):
            engine.friendly_name('Name\n')

    def test_runtime_probe_handles_timeout_and_wrong_schema(self):
        with patch('engine.subprocess.run', side_effect=subprocess.TimeoutExpired('python', 30)):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                engine.probe_runtime(sys.executable)
        for output in ('[]', '{"torch":"2", "cuda":"yes", "parquet":true}', 'junk'):
            with patch('engine.subprocess.run', return_value=subprocess.CompletedProcess([], 0, output, '')):
                with self.assertRaisesRegex(ValueError, 'usable runtime'):
                    engine.probe_runtime(sys.executable)

    def test_metrics_skip_scalar_corruption_and_invalid_losses(self):
        run = self.run_dir()
        (run / 'train_log.jsonl').write_text('\n'.join([
            'null', '[]', '7', '{"step":NaN}', '{"step":true}', '{"step":-1}',
            '{"step":2, "total_loss":"broken", "val_loss":2.5}',
            '{"step":3, "total_loss":2.1}', '{"step":']))
        metrics = engine.read_metrics(run)
        self.assertEqual([item['step'] for item in metrics], [2, 3])
        self.assertNotIn('total_loss', metrics[0])
        self.assertEqual(metrics[0]['val_loss'], 2.5)

    def test_telemetry_skips_invalid_records_and_fields(self):
        run = self.run_dir()
        (run / 'gpu.jsonl').write_text('{"temperature_c":"bad", "memory_used_mib":12}\nnull\n[]\n{')
        self.assertEqual(engine.latest_telemetry(run), {'memory_used_mib': 12})

    def test_run_listing_survives_invalid_metadata_and_reconciles_stale_status(self):
        run = self.run_dir()
        engine.atomic_json(run / 'run.json', {'steps': [], 'parameters': {}, 'model_size': [], 'display_name': []})
        engine.atomic_json(run / 'progress.json', {'completed_steps': 'bad', 'loss': 'bad'})
        engine.atomic_json(run / 'status.json', {'state': 'training'})
        item = self.trainer.find_run('example')
        self.assertEqual(item['phase'], 'interrupted')
        self.assertEqual(item['steps'], 0)
        self.assertEqual(item['completed_steps'], 0)
        self.assertEqual(item['name'], 'example')
        self.assertIsNone(item['loss'])

    def test_stale_status_respects_live_cli_training_lock(self):
        run = self.run_dir()
        engine.atomic_json(run / 'status.json', {'state': 'training'})
        lockpath = self.root / '.local/state/tensegrity/train.lock'
        lockpath.parent.mkdir(parents=True)
        with lockpath.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.trainer.find_run('example')['phase'], 'training')

    def test_launch_lock_prevents_cross_window_race(self):
        self.trainer.state.mkdir(parents=True)
        with (self.trainer.state / 'launch.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(self.trainer, '_start') as start:
                with self.assertRaisesRegex(ValueError, 'being started'):
                    self.trainer.start(self.values)
                start.assert_not_called()

    def test_failed_worker_launch_is_persisted(self):
        with patch('engine.subprocess.Popen', side_effect=OSError('Worker unavailable')):
            with self.assertRaisesRegex(ValueError, 'Unable to start'):
                self.trainer.start(self.values)
        job = self.trainer.jobs()[0]
        self.assertEqual(job['state'], 'failed')
        self.assertIn('Worker unavailable', job['detail'])
        self.assertFalse(job['running'])
        self.assertIn('finished_at', job)

    def test_stop_handles_process_disappearing_after_identity_check(self):
        path = self.root / 'job.json'
        engine.atomic_json(path, {'pid': os.getpid(), 'identity': engine.process_identity(os.getpid())})
        with patch('engine.os.kill', side_effect=ProcessLookupError):
            with self.assertRaisesRegex(ValueError, 'already stopped'):
                self.trainer.stop({'path': str(path)})

    def test_report_includes_failure_and_progress_context(self):
        run = self.run_dir()
        engine.atomic_json(run / 'failure.json', {'reason': 'Temperature limit'})
        engine.atomic_json(run / 'progress.json', {'completed_steps': 7})
        engine.atomic_json(run / 'status.json', {'state': 'failed'})
        report = self.trainer.report('example')
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['failure']['reason'], 'Temperature limit')
        self.assertEqual(report['progress']['completed_steps'], 7)

    def test_report_preserves_large_corpus_metadata_and_explains_omissions(self):
        run = self.run_dir()
        path = run / 'data/corpus.json'
        engine.atomic_json(path, {'source_details': 'x' * (1024 * 1024 + 1)})
        report = self.trainer.report('example')
        self.assertEqual(len(report['corpus']['source_details']), 1024 * 1024 + 1)
        self.assertEqual(report['report_warnings'], [])
        path.write_text('broken')
        report = self.trainer.report('example')
        self.assertEqual(report['corpus'], {})
        self.assertIn('Corpus metadata', report['report_warnings'][0])

    def test_dataset_inspection_is_sorted_and_reports_formats(self):
        (self.root / 'z.TXT').write_text('hello')
        (self.root / 'a.md').write_text('')
        info = engine.dataset_info(self.root)
        self.assertEqual(info['files'], sorted(info['files']))
        self.assertEqual(info['formats'], {'.md': 1, '.jsonl': 1, '.txt': 1})
        self.assertEqual(info['empty_files'], ['a.md'])
        self.assertEqual(info['errors'], [])

    def test_worker_escalates_unresponsive_process_group(self):
        ready = self.root / 'ready'
        jobpath = self.root / 'worker.json'
        script = ('import signal,time,pathlib,sys; '
                  'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                  'pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(30)')
        engine.atomic_json(jobpath, {'command': [sys.executable, '-c', script, str(ready)],
                                   'run': str(self.root), 'state': 'queued', 'prepare_only': False})
        child = subprocess.Popen([sys.executable, '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); import worker; raise SystemExit(worker.main(sys.argv[2], stop_grace=.2))',
            str(HERE), str(jobpath)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            job = engine.read_json(jobpath)
            child.terminate()
            child.communicate(timeout=5)
            saved = engine.read_json(jobpath)
            self.assertTrue(saved['forced_stop'])
            self.assertEqual(saved['state'], 'failed')
            self.assertFalse(engine.job_running({'pid': job['child_pid'], 'identity': job['child_identity']}))
        finally:
            if child.poll() is None:
                child.kill(); child.communicate()


if __name__ == '__main__':
    unittest.main()
