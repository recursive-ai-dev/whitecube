"""CPU-only regression coverage for training, checkpoint and watchdog failures."""
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import numpy as np
import torch
torch.set_num_threads(2)

import tensegrity_lm as lm
import tensegrity_nb_lib as training
import train_supervisor as watchdog
from pacing import TrainingPacer
from tensegrity_serve import decode, sample
from train_local import train
from train_folder import PreparationProgress
from files import file_sha256


class TinyModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = torch.nn.Embedding(259, 4)
        self.output = torch.nn.Linear(4, 259)

    def forward(self, x, y):
        logits = self.output(self.embedding(x))
        loss = torch.nn.functional.cross_entropy(logits.flatten(0, 1), y.flatten())
        return logits, {'ce_loss': loss, 'total_loss': loss, 'homeo_loss': loss * 0}, None


class TrainingImprovementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whitecube-training-unit-')
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
        kwargs = dict(max_steps=2, warmup_steps=1, batch_size=2, grad_accum_steps=1,
                      eval_iters=1, eval_interval=2, log_interval=1)
        kwargs.update(options)
        return training.run_training(self.model, self.optimizer, self.cfg, str(self.root / 'run'),
                                     'cpu', self.data, **kwargs)

    def test_exact_context_and_last_mixed_window_are_sampleable(self):
        np.arange(9, dtype=np.uint16).tofile(self.data / 'train.bin')
        x, y = lm.get_batch(self.data, 'train', 2, 8, 'cpu')
        self.assertEqual(x[0].tolist(), list(range(8)))
        self.assertEqual(y[0].tolist(), list(range(1, 9)))
        x, y = training.get_batch_mixed([self.data], [1], 'train', 1, 8, 'cpu')
        self.assertEqual(y[0, -1], 8)
        np.arange(10, dtype=np.uint16).tofile(self.data / 'train.bin')
        with patch('tensegrity_nb_lib.np.random.randint', return_value=1) as draw:
            _, y = training.get_batch_mixed([self.data], [1], 'train', 1, 8, 'cpu')
        draw.assert_called_once_with(0, 2)
        self.assertEqual(y[0, -1], 9)

    def test_bad_binary_and_batch_arguments_fail_clearly(self):
        for contents in (b'', b'abc'):
            (self.data / 'train.bin').write_bytes(contents)
            with self.assertRaisesRegex(ValueError, 'uint16'):
                lm.get_batch(self.data, 'train', 1, 8, 'cpu')
        for split, batch, block in (('other', 1, 8), ('train', 0, 8), ('train', 1, False)):
            with self.assertRaises(ValueError):
                lm.get_batch(self.data, split, batch, block, 'cpu')

    def test_memmap_cache_reloads_replaced_files_and_is_bounded(self):
        path = self.data / 'train.bin'
        before = lm._get_memmap(path)
        replacement = self.data / 'replacement.bin'
        np.full(128, 42, dtype=np.uint16).tofile(replacement)
        replacement.replace(path)
        self.assertEqual(lm._get_memmap(path)[0], 42)
        self.assertEqual(before[0], 0)
        for index in range(lm._MEMMAP_CACHE_LIMIT + 2):
            path = self.data / f'{index}.bin'
            np.arange(9, dtype=np.uint16).tofile(path)
            lm._get_memmap(path)
        self.assertLessEqual(len(lm._MEMMAP_CACHE), lm._MEMMAP_CACHE_LIMIT)

    def test_sampling_weight_validation(self):
        for weights in ([], [-1], [0], [float('nan')], [float('inf')], [[1]]):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                training.get_batch_mixed([self.data], weights, 'train', 1, 8, 'cpu')

    def test_failed_checkpoint_write_preserves_prior_and_cleans_temporary(self):
        path = self.root / 'checkpoint.pt'
        path.write_bytes(b'previous checkpoint')
        def fail(_checkpoint, handle):
            handle.write(b'incomplete')
            raise OSError('disk full')
        with patch.object(lm.torch, 'save', side_effect=fail), self.assertRaisesRegex(OSError, 'disk full'):
            lm.save_checkpoint(path, self.model, self.optimizer, self.cfg, 0, 1.0, {})
        self.assertEqual(path.read_bytes(), b'previous checkpoint')
        self.assertEqual(list(self.root.glob('.checkpoint.pt-*')), [])

    def test_checkpoint_schema_validation(self):
        path = self.root / 'checkpoint.pt'
        lm.save_checkpoint(path, self.model, self.optimizer, self.cfg, 0, 1.0, {})
        good = lm.load_checkpoint(path, 'cpu')
        for value in ([], {**good, 'step': True}, {**good, 'step': -2},
                      {**good, 'config': []}, {**good, 'best_val_loss': float('nan')}):
            torch.save(value, path)
            with self.assertRaises(ValueError):
                lm.load_checkpoint(path, 'cpu')

    def test_python_numpy_and_torch_rng_roundtrip_without_cuda_initialization(self):
        with patch.object(torch.cuda, 'is_initialized', return_value=False), \
             patch.object(torch.cuda, 'get_rng_state_all', side_effect=AssertionError('CPU initialized CUDA')):
            state = lm.capture_rng_state()
        expected = (random.random(), np.random.random(), torch.rand(2))
        lm.restore_rng_state(state)
        actual = (random.random(), np.random.random(), torch.rand(2))
        self.assertEqual(expected[:2], actual[:2])
        self.assertTrue(torch.equal(expected[2], actual[2]))

    def test_training_settings_rejected_before_creating_run(self):
        invalid = ({'max_steps': 0}, {'eval_iters': 0}, {'log_interval': 0}, {'eval_interval': -1},
                   {'batch_size': True}, {'grad_clip': float('nan')}, {'learning_rate': 0},
                   {'min_lr': 1}, {'ewc_lambda': 1}, {'data_weights': [0]},
                   {'schedule': 'wsd', 'stable_steps': -1, 'decay_steps': 2})
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.run_training(**settings)
        self.assertFalse((self.root / 'run').exists())

    def test_nonfinite_gradients_never_update_weights_or_create_checkpoint(self):
        parameter = next(self.model.parameters())
        before = parameter.detach().clone()
        handle = parameter.register_hook(lambda gradient: torch.full_like(gradient, float('inf')))
        self.addCleanup(handle.remove)
        with self.assertRaisesRegex(RuntimeError, 'non-finite'):
            self.run_training(max_steps=1)
        self.assertTrue(torch.equal(before, parameter))
        self.assertFalse((self.root / 'run' / 'ckpt_latest.pt').exists())

    def test_nonfinite_validation_never_publishes_checkpoint(self):
        with patch.object(training, 'evaluate', return_value=float('nan')):
            with self.assertRaisesRegex(RuntimeError, 'Nonfinite validation'):
                self.run_training(max_steps=1)
        self.assertFalse((self.root / 'run' / 'ckpt_latest.pt').exists())

    def test_recoverable_fp16_overflow_does_not_count_as_an_optimizer_step(self):
        scaler = torch.amp.GradScaler('cpu', init_scale=128)
        calls = [0]
        def one_overflow(gradient):
            calls[0] += 1
            return torch.full_like(gradient, float('inf')) if calls[0] == 1 else gradient
        handle = next(self.model.parameters()).register_hook(one_overflow)
        self.addCleanup(handle.remove)
        with patch.object(training.torch.amp, 'GradScaler', return_value=scaler):
            result = self.run_training(max_steps=1)
        self.assertEqual(result[0], 1)
        self.assertEqual(calls[0], 2)
        self.assertEqual(scaler.get_scale(), 64)
        self.assertEqual(lm.load_checkpoint(self.root / 'run' / 'ckpt_latest.pt', 'cpu')['step'], 0)

    def test_finite_gradients_with_overflowed_norm_never_step_optimizer(self):
        scaler = torch.amp.GradScaler('cpu', init_scale=128)
        calls = [0]
        def huge_once(gradient):
            calls[0] += 1
            return torch.full_like(gradient, 1e30) if calls[0] == 1 else gradient
        handle = next(self.model.parameters()).register_hook(huge_once)
        self.addCleanup(handle.remove)
        with patch.object(training.torch.amp, 'GradScaler', return_value=scaler), \
             patch.object(self.optimizer, 'step', wraps=self.optimizer.step) as step:
            result = self.run_training(max_steps=1)
        self.assertEqual(result[0], 1)
        self.assertEqual(calls[0], 2)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(scaler.get_scale(), 64)

    def test_resume_from_disabled_scaler_keeps_new_enabled_scaler(self):
        self.run_training(max_steps=1)
        saved = lm.load_checkpoint(self.root / 'run' / 'ckpt_latest.pt', 'cpu')
        self.assertEqual(saved['scaler_state_dict'], {})
        scaler = torch.amp.GradScaler('cpu', init_scale=128)
        with patch.object(training.torch.amp, 'GradScaler', return_value=scaler):
            result = self.run_training(init_from='resume', max_steps=2)
        self.assertEqual(result[0], 2)
        self.assertEqual(scaler.get_scale(), 128)

    def test_preparation_progress_reports_phases_counts_and_throttles_repeated_writes(self):
        now = [0]
        path = self.root / 'status.json'
        progress = PreparationProgress(path, 'cpu', clock=lambda: now[0])
        progress({'phase': 'reading', 'tokens': 20, 'documents': 2})
        progress({'phase': 'reading', 'tokens': 30, 'documents': 3})
        self.assertEqual(json.loads(path.read_text())['prepared_tokens'], 20)
        now[0] = .3
        progress({'phase': 'reading', 'tokens': 40, 'documents': 4})
        self.assertEqual(json.loads(path.read_text())['prepared_tokens'], 40)
        progress({'phase': 'publishing', 'tokens': 40, 'documents': 4})
        saved = json.loads(path.read_text())
        self.assertEqual(saved['state'], 'preparing')
        self.assertEqual(saved['preparation_phase'], 'publishing')
        self.assertIn('4 documents', saved['detail'])

    def test_progress_retains_final_loss_and_reports_tokens_speed_and_gradient(self):
        with patch.object(training, 'evaluate', wraps=training.evaluate) as evaluate:
            self.run_training()
        self.assertEqual(evaluate.call_count, 1, 'Final periodic evaluation must not run twice')
        progress = json.loads((self.root / 'run' / 'progress.json').read_text())
        self.assertEqual(progress['completed_steps'], 2)
        self.assertEqual(progress['tokens_processed'], 32)
        self.assertGreater(progress['tokens_per_second'], 0)
        self.assertGreater(progress['loss'], 0)
        self.assertGreater(progress['gradient_norm'], 0)
        self.assertEqual(progress['eta_seconds'], 0)
        events = [json.loads(line) for line in (self.root / 'run' / 'train_log.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'finished')
        self.assertEqual(events[-1]['completed_steps'], 2)

    def test_resume_rejects_batch_change_and_backwards_step_target(self):
        self.run_training()
        with self.assertRaisesRegex(ValueError, 'batch_size'):
            self.run_training(init_from='resume', max_steps=3, batch_size=1)
        with self.assertRaisesRegex(ValueError, 'completed steps'):
            self.run_training(init_from='resume', max_steps=1)

    def test_local_input_validation_precedes_model_allocation(self):
        for split in ('train', 'val'):
            np.full(300, 600, dtype=np.uint16).tofile(self.data / f'{split}.bin')
        with patch('train_local.TensegrityLM', side_effect=AssertionError('Allocated invalid corpus')):
            with self.assertRaisesRegex(ValueError, 'vocabulary'):
                train(self.data, self.root / 'run', steps=1, device='cpu')
            with self.assertRaisesRegex(ValueError, 'positive integer'):
                train(self.data, self.root / 'run', steps=0, device='cpu')

    def test_local_training_verifies_manifest_checksums_before_model_allocation(self):
        artifacts = {}
        for split in ('train.bin', 'val.bin'):
            path = self.data / split
            np.full(300, 42, dtype=np.uint16).tofile(path)
            artifacts[split] = {'bytes': path.stat().st_size, 'sha256': file_sha256(path)}
        (self.data / 'corpus.json').write_text(json.dumps({'schema_version': 2, 'artifacts': artifacts}))
        np.full(300, 43, dtype=np.uint16).tofile(self.data / 'train.bin')
        with patch('train_local.TensegrityLM', side_effect=AssertionError('Allocated modified corpus')):
            with self.assertRaisesRegex(ValueError, 'integrity verification'):
                train(self.data, self.root / 'run', steps=1, device='cpu')

    def test_sampler_rejects_invalid_settings_and_corrupt_logits(self):
        logits = torch.tensor([[1., 3., 2.]])
        self.assertEqual(sample(logits, 0, None, None), 1)
        for temperature, k, p in ((float('nan'), None, None), (-1, None, None),
                                  (1, -2, None), (1, 2.5, None), (1, None, 2)):
            with self.assertRaises(ValueError):
                sample(logits, temperature, k, p)
        for values in ([float('nan'), 1], [float('inf'), 1], [-float('inf'), -float('inf')]):
            with self.assertRaisesRegex(RuntimeError, 'logits'):
                sample(torch.tensor([values]), 1, None, None)

    def test_decoder_seeds_empty_prompt_and_validates_token_budget(self):
        model = TinyModel()
        model.cfg = self.cfg
        def forward(window, past, position):
            self.assertEqual(window.tolist(), [[lm.ByteTokenizer.BOS]])
            logits = torch.zeros(1, 1, 259)
            logits[0, 0, lm.ByteTokenizer.EOS] = 10
            return logits, None, None
        model.forward_incremental = forward
        options = dict(temperature=0, top_k=None, top_p=None, max_new_tokens=1)
        self.assertEqual(list(decode(model, [], **options)), [lm.ByteTokenizer.EOS])
        with self.assertRaises(ValueError):
            list(decode(model, [], **{**options, 'max_new_tokens': -1}))
        with self.assertRaises(ValueError):
            list(decode(model, [900], **options))


class WatchdogAndPacingTests(unittest.TestCase):
    def test_pacer_rejects_invalid_durations_and_obeys_stop_when_disabled(self):
        for burst, cooldown in ((-1, 1), (float('inf'), 1), (1, 0), (1, float('nan'))):
            with self.assertRaises(ValueError):
                TrainingPacer(burst, cooldown)
        self.assertTrue(TrainingPacer(0, 10).after_step(lambda: True))

    def test_pacer_resets_cooling_state_when_callback_fails(self):
        events = []
        pacer = TrainingPacer(1, 1, clock=lambda: 0)
        pacer.next_break = 0
        def callback(active):
            events.append(active)
            if active:
                raise RuntimeError('status write failed')
        with self.assertRaises(RuntimeError):
            pacer.after_step(lambda: False, callback)
        self.assertEqual(events, [True, False])
        self.assertEqual(pacer.next_break, 1)

    def test_gpu_probe_uses_hottest_device_and_rejects_unavailable_reading(self):
        with patch.object(watchdog.subprocess, 'run', return_value=SimpleNamespace(stdout='41\n83\n', stderr='')):
            self.assertEqual(watchdog.gpu_temperature(), 83)
        with patch.object(watchdog.subprocess, 'run', return_value=SimpleNamespace(stdout='N/A\n', stderr='')):
            with self.assertRaisesRegex(RuntimeError, 'GPU inaccessible'):
                watchdog.gpu_temperature()

    def test_watchdog_declines_nonfinite_probe_without_starting_child(self):
        with patch.object(watchdog.subprocess, 'Popen') as popen:
            result = watchdog.supervise(['unused'], probe=lambda: float('nan'), cpu_probe=lambda: None)
        self.assertEqual(result, 2)
        popen.assert_not_called()

    def test_corrupt_child_report_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / 'report'
            for value in ('', 'relative', '\x00', 'a' * 9000):
                report.write_text(value)
                self.assertIsNone(watchdog.reported_run(report))
            report.write_text(temp)
            self.assertEqual(watchdog.reported_run(report), Path(temp))

    def test_watchdog_stops_even_when_failure_report_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / 'run'
            run.mkdir()
            child = Mock()
            child.poll.return_value = None
            child.wait.side_effect = [subprocess.TimeoutExpired('train', .01), 0]
            def launch(command, env):
                Path(env['TENSEGRITY_RUN_REPORT']).write_text(str(run))
                return child
            prior = signal.getsignal(signal.SIGTERM)
            with patch.dict(os.environ, {'XDG_STATE_HOME': temp}), \
                 patch.object(watchdog.subprocess, 'Popen', side_effect=launch), \
                 patch.object(watchdog, 'atomic_json', side_effect=OSError('disk full')):
                result = watchdog.supervise(['unused'], probe=Mock(side_effect=[40, 90]),
                                            cpu_probe=lambda: None, interval=.01, grace=.01)
            self.assertEqual(result, 2)
            child.terminate.assert_called_once()
            self.assertEqual(signal.getsignal(signal.SIGTERM), prior)

    def test_stop_child_escalates_when_grace_expires_and_tolerates_exit_race(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('train', 1), 0]
        watchdog.stop_child(child, 1)
        child.kill.assert_called_once()
        child.terminate.side_effect = ProcessLookupError()
        watchdog.stop_child(child, 1)


if __name__ == '__main__':
    unittest.main()
