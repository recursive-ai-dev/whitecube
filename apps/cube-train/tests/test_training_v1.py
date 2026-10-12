"""Precision, evaluation budget and resumable early stopping regression coverage."""
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
from train_local import train
from test_training_improvements import TinyModel


class TrainingPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whitecube-training-v1-')
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
        settings = dict(max_steps=2, warmup_steps=1, batch_size=2, grad_accum_steps=1,
                        eval_iters=1, eval_interval=1, log_interval=1)
        settings.update(options)
        return training.run_training(self.model, self.optimizer, self.cfg, str(self.root / 'run'),
                                     'cpu', self.data, **settings)

    def checkpoint(self, name='ckpt_latest.pt'):
        return lm.load_checkpoint(self.root / 'run' / name, 'cpu')

    def test_cpu_precision_modes_train_and_report_actual_validation_budget(self):
        for requested, actual in (('auto', 'float32'), ('float32', 'float32'), ('bfloat16', 'bfloat16')):
            with self.subTest(dtype=requested):
                outcome = {}
                self.run_training(dtype=requested, outcome=outcome, eval_iters=2)
                self.assertEqual(outcome['dtype'], actual)
                self.assertEqual(outcome['validation_tokens_per_evaluation'], 128)
                saved = self.checkpoint()['training_state']
                self.assertEqual(saved['precision'], {'requested': requested, 'resolved': actual})
                self.assertEqual(saved['evaluation']['iters'], 2)
                progress = json.loads((self.root / 'run/progress.json').read_text())
                self.assertEqual(progress['dtype'], actual)
                self.assertTrue(np.isfinite(outcome['final_loss']))

    def test_invalid_precision_and_evaluation_controls_fail_before_writes(self):
        for invalid in ({'dtype': 'float16'}, {'dtype': 'bad'}, {'eval_iters': 0}, {'log_interval': 0},
                        {'early_stop_min_delta': -1}, {'early_stop_min_delta': float('nan')},
                        {'early_stop_min_delta': float('inf')}, {'early_stop_min_delta': True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.run_training(**invalid)
        self.assertFalse((self.root / 'run').exists())

    def test_cuda_precision_capability_checks(self):
        with patch.object(torch.cuda, 'is_available', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'CUDA is unavailable'):
                training.training_precision('auto', 'cuda')
        with patch.object(torch.cuda, 'is_available', return_value=True), \
             patch.object(torch.cuda, 'is_bf16_supported', return_value=False):
            self.assertEqual(training.training_precision('auto', 'cuda')[1], torch.float16)
            with self.assertRaisesRegex(ValueError, 'bfloat16 is unavailable'):
                training.training_precision('bfloat16', 'cuda')
        with patch.object(torch.cuda, 'is_available', return_value=True), \
             patch.object(torch.cuda, 'is_bf16_supported', return_value=True):
            self.assertEqual(training.training_precision('auto', 'cuda')[1], torch.bfloat16)
            saved = {'requested': 'auto', 'resolved': 'float32'}
            self.assertEqual(training.training_precision('auto', 'cuda', saved)[1], torch.float32)

    def test_resume_reuses_actual_precision_and_evaluation_plan_when_omitted(self):
        self.run_training(dtype='bfloat16', eval_iters=2, eval_interval=2)
        outcome = {}
        self.run_training(init_from='resume', max_steps=3, dtype=None,
                          eval_iters=None, eval_interval=None, outcome=outcome)
        self.assertEqual(outcome['dtype'], 'bfloat16')
        self.assertEqual(outcome['eval_iters'], 2)
        self.assertEqual(self.checkpoint()['training_state']['evaluation']['interval'], 2)

    def test_resume_conflicts_do_not_replace_checkpoint_or_load_weights(self):
        self.run_training(dtype='float32')
        path = self.root / 'run/ckpt_latest.pt'
        before = path.read_bytes()
        for options in ({'dtype': 'bfloat16'}, {'eval_iters': 2}, {'eval_interval': 2}):
            with self.subTest(options=options), \
                 patch.object(self.model, 'load_state_dict', side_effect=AssertionError('Loaded before validating')), \
                 self.assertRaisesRegex(ValueError, 'saved .* plan'):
                self.run_training(init_from='resume', max_steps=3, **options)
            self.assertEqual(path.read_bytes(), before)

    def test_small_improvements_keep_true_best_but_use_patience_reference(self):
        outcome = {}
        with patch.object(training, 'evaluate', side_effect=[3.0, 2.9, 2.85]):
            result = self.run_training(max_steps=6, early_stop_patience=2, early_stop_min_delta=.2, outcome=outcome)
        self.assertEqual(result, (3, 2.85))
        self.assertEqual(outcome['stop_reason'], 'early_stop')
        latest, best = self.checkpoint(), self.checkpoint('ckpt_best.pt')
        self.assertEqual(best['step'], 2)
        state = latest['training_state']['early_stopping']
        self.assertEqual(state['reference_loss'], 3.0)
        self.assertEqual(state['evaluations_without_improvement'], 2)
        self.assertEqual(state['evaluations'], 3)

    def test_patience_counters_and_reference_survive_resume(self):
        with patch.object(training, 'evaluate', side_effect=[3.0, 2.9]):
            self.run_training(early_stop_patience=2, early_stop_min_delta=.2)
        outcome = {}
        with patch.object(training, 'evaluate', return_value=2.85):
            result = self.run_training(init_from='resume', max_steps=6, early_stop_patience=2,
                                       early_stop_min_delta=.2, outcome=outcome)
        self.assertEqual(result, (3, 2.85))
        self.assertEqual(outcome['evaluations'], 3)
        self.assertEqual(outcome['evaluations_without_improvement'], 2)

    def test_cumulative_loss_improvement_resets_patience(self):
        outcome = {}
        with patch.object(training, 'evaluate', side_effect=[3.0, 2.9, 2.75, 2.7]):
            self.run_training(max_steps=4, early_stop_patience=2, early_stop_min_delta=.2, outcome=outcome)
        self.assertEqual(outcome['stop_reason'], 'completed')
        self.assertEqual(outcome['evaluations_without_improvement'], 1)
        self.assertEqual(self.checkpoint()['training_state']['early_stopping']['reference_loss'], 2.75)

    def test_changed_threshold_resets_history_and_final_eval_counts_once(self):
        with patch.object(training, 'evaluate', side_effect=[3.0, 2.9]):
            self.run_training(early_stop_min_delta=.2)
        outcome = {}
        with patch.object(training, 'evaluate', return_value=2.8):
            self.run_training(init_from='resume', max_steps=3, early_stop_min_delta=0, outcome=outcome)
        self.assertEqual((outcome['evaluations'], outcome['evaluations_without_improvement']), (1, 0))
        before = self.checkpoint()['training_state']['early_stopping']
        with patch.object(training, 'evaluate', return_value=2.8):
            self.run_training(init_from='resume', max_steps=3, early_stop_min_delta=0)
        self.assertEqual(self.checkpoint()['training_state']['early_stopping'], before)

    def test_final_evaluation_between_intervals_is_in_saved_counter(self):
        outcome = {}
        self.run_training(max_steps=3, eval_interval=2, outcome=outcome)
        self.assertEqual(outcome['evaluations'], 2)
        self.assertEqual(self.checkpoint()['training_state']['early_stopping']['evaluations'], 2)

    def test_legacy_training_state_remains_resumable(self):
        self.run_training()
        saved = self.checkpoint()
        for key in ('precision', 'early_stopping'):
            saved['training_state'].pop(key)
        saved['training_state']['evaluation'].pop('interval')
        saved['training_state']['evaluation'].pop('batch_size')
        torch.save(saved, self.root / 'run/ckpt_latest.pt')
        result = self.run_training(init_from='resume', max_steps=3)
        self.assertEqual(result[0], 3)
        self.assertEqual(self.checkpoint()['training_state']['precision']['resolved'], 'float32')

    def test_local_rejects_invalid_precision_before_allocating_model(self):
        with patch('train_local.TensegrityLM', side_effect=AssertionError('Invalid plan allocated model')):
            for options in ({'dtype': 'float16'}, {'dtype': 'bad'}, {'eval_iters': -1},
                            {'log_interval': False}, {'early_stop_min_delta': float('inf')}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    train(self.data, self.root / 'run', device='cpu', **options)


class TrainingPlanCliTests(unittest.TestCase):
    def test_precision_budget_and_min_delta_cli_persist_and_resume(self):
        with tempfile.TemporaryDirectory(prefix='whitecube-training-v1-cli-') as temp:
            root = Path(temp)
            corpus = root / 'corpus.jsonl'
            corpus.write_text('\n'.join(json.dumps({'text': f'Story {i}. ' + 'A cube learns to balance. ' * 40}) for i in range(30)))
            run = root / 'run'
            env = {**os.environ, 'HOME': str(root), 'XDG_STATE_HOME': str(root / 'state')}
            code = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
                    'import train_folder; train_folder.cpu_temperature = lambda: 40; train_folder.main()')
            def cli(*args):
                return subprocess.run([sys.executable, '-u', '-c', code, str(PIPELINE), *args],
                                      env=env, capture_output=True, text=True, timeout=120)
            result = cli(str(corpus), '--run-dir', str(run), '--device', 'cpu', '--steps', '2',
                         '--batch-size', '1', '--grad-accum-steps', '1', '--max-tokens', '30000',
                         '--dtype', 'float32', '--eval-iters', '1', '--eval-interval', '1',
                         '--log-interval', '2', '--early-stop-patience', '2', '--early-stop-min-delta', '.01')
            self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
            meta = json.loads((run / 'run.json').read_text())
            self.assertEqual((meta['dtype'], meta['eval_iters'], meta['log_interval'], meta['early_stop_min_delta']),
                             ('float32', 1, 2, .01))
            result = json.loads((run / 'result.json').read_text())
            self.assertEqual((result['dtype'], result['validation_tokens_per_evaluation']), ('float32', 2048))
            prior_checkpoint = (run / 'ckpt_latest.pt').read_bytes()
            for option, value in (('--eval-iters', '2'), ('--eval-interval', '2'), ('--dtype', 'bfloat16')):
                result = cli('--resume', str(run), '--device', 'cpu', '--steps', '3', option, value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(option, result.stderr)
                self.assertEqual((run / 'ckpt_latest.pt').read_bytes(), prior_checkpoint)
            result = cli('--resume', str(run), '--device', 'cpu', '--steps', '3', '--log-interval', '1')
            self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
            meta = json.loads((run / 'run.json').read_text())
            self.assertEqual((meta['dtype'], meta['eval_iters'], meta['log_interval']), ('float32', 1, 1))
            state = lm.load_checkpoint(run / 'ckpt_latest.pt', 'cpu')['training_state']
            self.assertEqual(state['early_stopping']['evaluations'], 3)


if __name__ == '__main__':
    unittest.main()
