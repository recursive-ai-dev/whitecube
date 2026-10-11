"""0.8.0 training upgrades; run with the configured PyTorch Python environment."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PIPELINE = Path(__file__).resolve().parents[1] / 'pipeline'
sys.path.insert(0, str(PIPELINE))
import numpy as np
import torch
torch.set_num_threads(2)

import tensegrity_lm as lm
import tensegrity_nb_lib as training
import train_folder
from train_local import train, wsd_plan
from test_training_improvements import TinyModel


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RunTrainingUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whitecube-upgrades-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        for split in ('train', 'val'):
            np.arange(128, dtype=np.uint16).tofile(self.data / f'{split}.bin')
        self.cfg = lm.TensegrityConfig(block_size=8)
        self.model = TinyModel()
        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=3e-4)

    def run_training(self, **options):
        kwargs = dict(max_steps=6, warmup_steps=1, batch_size=2, grad_accum_steps=1,
                      eval_iters=1, eval_interval=1, log_interval=1)
        kwargs.update(options)
        return training.run_training(self.model, self.optimizer, self.cfg, str(self.root / 'run'),
                                     'cpu', self.data, **kwargs)

    def test_early_stop_after_patience_keeps_best_and_reports_reason(self):
        outcome = {}
        losses = iter([3.0, 2.0, 2.5, 2.6, 9.0, 9.0])
        with patch.object(training, 'evaluate', side_effect=lambda *args: next(losses)):
            step, best = self.run_training(early_stop_patience=2, outcome=outcome)
        self.assertEqual((step, best), (4, 2.0))
        self.assertEqual(outcome['stop_reason'], 'early_stop')
        self.assertEqual(outcome['evaluations_without_improvement'], 2)
        self.assertEqual(lm.load_checkpoint(str(self.root / 'run/ckpt_latest.pt'), 'cpu')['step'], 3)
        self.assertEqual(lm.load_checkpoint(str(self.root / 'run/ckpt_best.pt'), 'cpu')['step'], 1)
        self.assertTrue(np.isfinite(outcome['final_loss']))

    def test_snapshots_keep_only_the_newest_copies(self):
        outcome = {}
        self.run_training(keep_snapshots=2, outcome=outcome)
        names = sorted(path.name for path in (self.root / 'run').glob('ckpt_step*.pt'))
        self.assertEqual(names, ['ckpt_step00000005.pt', 'ckpt_step00000006.pt'])
        self.assertEqual(lm.load_checkpoint(str(self.root / 'run' / names[-1]), 'cpu')['step'], 5)
        self.assertEqual(outcome['stop_reason'], 'completed')
        self.assertFalse(list((self.root / 'run').glob('*.tmp')))

    def test_stop_request_reason_and_invalid_options(self):
        outcome = {}
        step, _ = self.run_training(stop_requested=lambda completed: completed >= 2, outcome=outcome)
        self.assertEqual((step, outcome['stop_reason']), (2, 'stop_requested'))
        for bad in ({'early_stop_patience': -1}, {'keep_snapshots': True}, {'outcome': []}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.run_training(**bad)

    def test_train_validates_hyperparameters_and_wsd_plan(self):
        self.assertEqual(wsd_plan(100, 10), (70, 20))
        self.assertEqual(wsd_plan(3, 3), (0, 1))
        for bad in ({'learning_rate': 0}, {'min_lr_ratio': 1.5}, {'weight_decay': -.1}, {'grad_clip': True},
                    {'eval_interval': 0}, {'seed': -1}, {'schedule': 'linear'}, {'warmup_steps': -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                train(self.data, self.root / 'never', device='cpu', **bad)
        self.assertFalse((self.root / 'never').exists())


class MonitorUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whitecube-monitor-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'gpu.jsonl'
        self.clock = FakeClock()

    def monitor(self, **options):
        return train_folder.Monitor(self.path, 70, 'cpu', 85, clock=self.clock, sleep=self.clock.sleep, **options)

    def test_time_limit_stops_with_reason(self):
        monitor = self.monitor(max_minutes=1)
        with patch.object(train_folder, 'cpu_temperature', return_value=40):
            self.assertFalse(monitor(1))
            self.clock.now = 61
            self.assertTrue(monitor(2))
        self.assertEqual(monitor.stop_reason, 'time_limit')

    def test_thermal_pause_waits_for_cooling_then_continues(self):
        readings = iter([81, 80, 78, 74])
        monitor = self.monitor(thermal_pause=True)
        with patch.object(train_folder, 'cpu_temperature', side_effect=lambda: next(readings)):
            self.assertFalse(monitor(1))
        self.assertFalse(monitor.stopped)
        self.assertGreaterEqual(self.clock.now, 3)
        status = json.loads((self.path.parent / 'status.json').read_text())
        self.assertEqual(status['state'], 'training')

    def test_thermal_pause_stops_at_the_limit_or_after_fifteen_minutes(self):
        readings = iter([82, 86])
        monitor = self.monitor(thermal_pause=True)
        with patch.object(train_folder, 'cpu_temperature', side_effect=lambda: next(readings)):
            self.assertTrue(monitor(1))
        self.assertEqual(monitor.stop_reason, 'thermal')
        monitor = self.monitor(thermal_pause=True)
        with patch.object(train_folder, 'cpu_temperature', return_value=81):
            self.assertTrue(monitor(1))
        self.assertEqual(monitor.stop_reason, 'thermal_timeout')
        self.assertGreaterEqual(self.clock.now, train_folder.THERMAL_PAUSE_LIMIT)

    def test_without_thermal_pause_warm_readings_do_not_pause(self):
        monitor = self.monitor()
        with patch.object(train_folder, 'cpu_temperature', return_value=84):
            self.assertFalse(monitor(1))
        self.assertEqual(self.clock.now, 0)


class CommandLineUpgradeTests(unittest.TestCase):
    def test_new_flags_are_saved_reused_on_resume_and_exportable(self):
        with tempfile.TemporaryDirectory(prefix='whitecube-cli-upgrades-') as temp:
            root = Path(temp)
            corpus = root / 'corpus.jsonl'
            corpus.write_text('\n'.join(json.dumps({'text': f'Document {i}. ' + 'Cubes keep notes of local stories. ' * 30}) for i in range(40)))
            env = {**os.environ, 'HOME': str(root), 'XDG_STATE_HOME': str(root / 'state')}
            run = root / 'train-out' / 'run'
            def cli(*args):
                # Exercise the real CLI/parser/training with a controlled healthy
                # probe. Thermal behavior has separate Monitor/watchdog coverage.
                code = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
                        'import train_folder; train_folder.cpu_temperature = lambda: 40; train_folder.main()')
                return subprocess.run([sys.executable, '-u', '-c', code, str(PIPELINE), *args], env=env,
                                      capture_output=True, text=True, timeout=600)
            result = cli(str(corpus), '--run-dir', str(run), '--1m', '--device', 'cpu', '--cpu-threads', '2',
                         '--max-tokens', '40000', '--steps', '3', '--batch-size', '1', '--grad-accum-steps', '1',
                         '--learning-rate', '0.001', '--min-lr-ratio', '0.2', '--schedule', 'wsd', '--warmup-steps', '1',
                         '--weight-decay', '0.05', '--grad-clip', '0.5', '--eval-interval', '1', '--seed', '7',
                         '--val-fraction', '0.1', '--keep-snapshots', '1', '--max-minutes', '30', '--max-cpu-temp', '95',
                         '--deduplicate', '--shuffle-files', '--min-document-bytes', '200', '--max-document-bytes', '800', '--batch-rows', '8')
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
            meta = json.loads((run / 'run.json').read_text())
            self.assertEqual((meta['learning_rate'], meta['schedule'], meta['seed'], meta['val_fraction']), (.001, 'wsd', 7, .1))
            self.assertEqual((meta['keep_snapshots'], meta['max_minutes'], meta['warmup_steps']), (1, 30, 1))
            self.assertTrue(meta['deduplicate'] and meta['shuffle_files'])
            self.assertEqual((meta['min_document_bytes'], meta['max_document_bytes'], meta['batch_rows']), (200, 800, 8))
            self.assertIn('created_at', meta)
            outcome = json.loads((run / 'result.json').read_text())
            self.assertEqual((outcome['completed_steps'], outcome['stop_reason']), (3, 'completed'))
            self.assertGreater(outcome['total_training_seconds'], 0)
            self.assertTrue(np.isfinite(outcome['final_loss']))
            self.assertEqual([path.name for path in run.glob('ckpt_step*.pt')], ['ckpt_step00000003.pt'])
            corpus_meta = json.loads((run / 'data/corpus.json').read_text())
            self.assertEqual((corpus_meta['split_seed'], corpus_meta['val_fraction']), (7, .1))
            self.assertEqual(corpus_meta['document_limit_truncations'], 40)
            self.assertIn('documents.jsonl', corpus_meta['artifacts'])
            saved = lm.load_checkpoint(str(run / 'ckpt_latest.pt'), 'cpu')
            self.assertEqual(saved['training_state']['schedule']['kind'], 'wsd')
            self.assertAlmostEqual(saved['training_state']['schedule']['learning_rate'], .001)
            self.assertAlmostEqual(saved['optimizer_state_dict']['param_groups'][0]['weight_decay'], .05)

            rejected = cli('--resume', str(run), '--steps', '5', '--device', 'cpu', '--learning-rate', '0.01')
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('--learning-rate', rejected.stderr)
            resumed = cli('--resume', str(run), '--steps', '5', '--device', 'cpu', '--max-cpu-temp', '95')
            self.assertEqual(resumed.returncode, 0, resumed.stdout[-3000:] + resumed.stderr[-3000:])
            later = json.loads((run / 'result.json').read_text())
            self.assertEqual(later['completed_steps'], 5)
            self.assertGreater(later['total_training_seconds'], outcome['total_training_seconds'])
            self.assertEqual(json.loads((run / 'run.json').read_text())['learning_rate'], .001)

            corpus.unlink()
            reused_run = root / 'reused-run'
            reused = cli(str(run / 'data'), '--run-dir', str(reused_run), '--10m', '--device', 'cpu', '--prepare-only',
                         '--seed', '99', '--val-fraction', '0.3')
            self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
            reused_meta = json.loads((reused_run / 'run.json').read_text())
            self.assertEqual(reused_meta['model_size'], '10m')
            self.assertEqual((reused_meta['corpus_split_seed'], reused_meta['corpus_val_fraction']), (7, .1))
            self.assertEqual(reused_meta['max_tokens'], corpus_meta['max_tokens'])
            for split in ('train.bin', 'val.bin', 'documents.jsonl'):
                self.assertEqual((run / 'data' / split).read_bytes(), (reused_run / 'data' / split).read_bytes())

            output = root / 'export'
            exported = subprocess.run([sys.executable, str(PIPELINE / 'export_model.py'), '--checkpoint', str(run / 'ckpt_latest.pt'),
                                       '--output', str(output), '--name', 'CLI model'], capture_output=True, text=True, timeout=300)
            self.assertEqual(exported.returncode, 0, exported.stderr[-3000:])
            self.assertEqual(json.loads(exported.stdout)['files'], ['README.md', 'model.config.json', 'model.safetensors'])
            card = (output / 'README.md').read_text()
            self.assertIn('# CLI model', card)
            self.assertIn('| Completed steps | 5 |', card)
            model, _ = training.load_from_safetensors(str(output / 'model.safetensors'), str(output / 'model.config.json'))
            reference = lm.TensegrityLM(lm.TensegrityConfig(**saved['config']))
            reference.load_state_dict(lm.load_checkpoint(str(run / 'ckpt_latest.pt'), 'cpu')['model_state_dict'])
            for name, tensor in reference.state_dict().items():
                self.assertTrue(torch.equal(tensor, model.state_dict()[name]), name)
            again = subprocess.run([sys.executable, str(PIPELINE / 'export_model.py'), '--checkpoint', str(run / 'ckpt_latest.pt'),
                                    '--output', str(output)], capture_output=True, text=True, timeout=300)
            self.assertNotEqual(again.returncode, 0)

            preview = [sys.executable, str(PIPELINE / 'preview.py'), '--checkpoint', str(run / 'ckpt_latest.pt'),
                       '--prompt', 'Cubes', '--max-new-tokens', '24', '--top-p', '0.5', '--seed', '3']
            first = subprocess.run(preview, capture_output=True, text=True, timeout=120)
            second = subprocess.run(preview, capture_output=True, text=True, timeout=120)
            self.assertEqual(first.returncode, 0, first.stderr[-2000:])
            self.assertEqual(first.stdout, second.stdout)
            invalid = subprocess.run([*preview[:-4], '--top-p', '0'], capture_output=True, text=True, timeout=120)
            self.assertNotEqual(invalid.returncode, 0)


if __name__ == '__main__':
    unittest.main()
