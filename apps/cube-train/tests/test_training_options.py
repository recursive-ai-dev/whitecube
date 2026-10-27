"""Command help and argument parsing work before a training runtime is installed."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

PIPELINE = Path(__file__).resolve().parents[1] / 'pipeline'
sys.path.insert(0, str(PIPELINE))
import training_options
from model_presets import MODEL_PRESETS


class TrainingOptionsTests(unittest.TestCase):
    def test_output_override_and_packaged_resume_hint_are_consistent(self):
        with patch.dict(os.environ, {'WHITECUBE_TRAIN_OUT': '/data/training output',
                                    'WHITECUBE_TRAIN_COMMAND': 'whitecube-trainer-cli'}):
            self.assertEqual(training_options.default_output_root('/home/fixture'), Path('/data/training output'))
            self.assertEqual(training_options.resume_hint('/data/a run', 12),
                             "whitecube-trainer-cli --resume '/data/a run' --steps 12")
        with patch.dict(os.environ, {'WHITECUBE_TRAIN_OUT': '', 'WHITECUBE_TRAIN_COMMAND': ''}):
            self.assertEqual(training_options.default_output_root('/home/fixture'), Path('/home/fixture/train-out'))
            self.assertEqual(training_options.resume_hint('/data/run'), 'whitecube-train --resume /data/run')

    def test_help_imports_without_training_dependencies(self):
        code = f'''
import importlib.abc
import sys
sys.path.insert(0, {str(PIPELINE)!r})
class NoTraining(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('torch', 'numpy', 'gi', 'pyarrow'):
            raise AssertionError('Help must not import ' + fullname)
sys.meta_path.insert(0, NoTraining())
from training_options import build_parser
parser = build_parser(prog='whitecube-train')
parser.parse_args(['--help'])
'''
        result = subprocess.run([sys.executable, '-I', '-c', code], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage: whitecube-train', result.stdout)
        self.assertIn('--learning-rate', result.stdout)
        self.assertIn('--thermal-pause', result.stdout)
        self.assertIn('--no-thermal-pause', result.stdout)

    def test_defaults_preserve_implicit_plan_and_runtime_controls(self):
        args = training_options.build_parser().parse_args([])
        self.assertEqual(args.dataset, Path.home() / 'train-in')
        self.assertEqual(args.max_tokens, 20_000_000)
        self.assertEqual(args.device, 'cuda')
        self.assertEqual(args.batch_size, 4)
        self.assertEqual(args.grad_accum_steps, 4)
        self.assertEqual(args.cpu_threads, 2)
        self.assertEqual(args.max_temp, 70)
        self.assertEqual(args.max_cpu_temp, 85)
        self.assertEqual(args.burst_seconds, 0)
        self.assertEqual(args.cooldown_seconds, 10)
        self.assertEqual(args.batch_rows, 32)
        self.assertFalse(args.prepare_only)
        self.assertFalse(args.deduplicate)
        for name in ('model_size', 'steps', 'schedule_steps', 'learning_rate', 'min_lr_ratio',
                     'warmup_steps', 'weight_decay', 'grad_clip', 'eval_interval', 'eval_iters',
                     'dtype', 'seed', 'schedule', 'val_fraction', 'early_stop_patience',
                     'early_stop_min_delta', 'log_interval', 'keep_snapshots', 'max_minutes', 'thermal_pause'):
            with self.subTest(name=name):
                self.assertIsNone(getattr(args, name))

    def test_all_model_flags_and_thermal_override_preserve_values(self):
        parser = training_options.build_parser()
        for size in MODEL_PRESETS:
            self.assertEqual(parser.parse_args([f'--{size}']).model_size, size)
        self.assertTrue(parser.parse_args(['--thermal-pause']).thermal_pause)
        self.assertFalse(parser.parse_args(['--no-thermal-pause']).thermal_pause)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            parser.parse_args(['--1m', '--2m'])
        self.assertEqual(error.exception.code, 2)

    def test_realistic_resume_controls_remain_typed(self):
        args = training_options.build_parser().parse_args([
            '--resume', '/tmp/run', '--steps', '2000', '--device', 'cpu',
            '--max-minutes', '45.5', '--keep-snapshots', '3', '--early-stop-min-delta', '0.005',
            '--eval-iters', '40', '--dtype', 'bfloat16', '--no-thermal-pause'])
        self.assertEqual(args.resume, Path('/tmp/run'))
        self.assertEqual(args.steps, 2000)
        self.assertEqual(args.max_minutes, 45.5)
        self.assertEqual(args.early_stop_min_delta, .005)
        self.assertEqual(args.dtype, 'bfloat16')
        self.assertEqual(args.eval_iters, 40)
        self.assertFalse(args.thermal_pause)


if __name__ == '__main__':
    unittest.main()
