"""Training plan, benchmark workflow and presentation regressions for 1.0."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine


class TrainerV1Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.trainer = engine.Trainer(home=self.root, state=self.root / 'state', config=self.root / 'config')
        self.trainer.runs_dir = self.root / 'runs'
        self.run = self.trainer.runs_dir / 'fixture'
        (self.run / 'data').mkdir(parents=True)
        for split in ('train', 'val'):
            (self.run / 'data' / f'{split}.bin').write_bytes(b'\0\0' * 600)
        engine.atomic_json(self.run / 'data/corpus.json', {'train_tokens': 600, 'val_tokens': 600, 'total_tokens': 1200})
        engine.atomic_json(self.run / 'run.json', {'steps': 10, 'model_size': '2m', 'batch_size': 4, 'grad_accum_steps': 4})
        (self.run / 'ckpt_best.pt').write_bytes(b'fixture never loaded')
        (self.run / 'ckpt_latest.pt').write_bytes(b'fixture never loaded')
        self.corpus = self.root / 'corpus.txt'
        self.corpus.write_text('A corpus for command validation. ' * 100)
        self.values = {**self.trainer.preferences(), 'python': sys.executable, 'dataset': str(self.corpus),
                       'name': 'V1 test', 'device': 'cpu'}

    def test_training_precision_and_evaluation_settings_round_trip(self):
        options = {'dtype': 'bfloat16', 'eval_iters': 17, 'log_interval': 3, 'early_stop_min_delta': .025}
        self.trainer.save_preferences(options)
        self.assertEqual({key: self.trainer.preferences()[key] for key in options}, options)
        for invalid in ({'dtype': 'int8'}, {'eval_iters': 0}, {'eval_iters': True}, {'log_interval': 1.5},
                        {'early_stop_min_delta': -1}, {'early_stop_min_delta': float('nan')}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.trainer.save_preferences(invalid)

    def test_plan_flags_saved_for_new_runs_and_controls_carried_on_resume(self):
        values = self.trainer.validate({**self.values, 'dtype': 'float32', 'eval_iters': 7,
                                        'log_interval': 2, 'early_stop_min_delta': .001})
        command = self.trainer.command(values, self.run)
        for flag, value in (('--dtype', 'float32'), ('--eval-iters', '7'), ('--log-interval', '2'),
                            ('--early-stop-min-delta', '0.001')):
            self.assertEqual(command[command.index(flag) + 1], value)
        resume = self.trainer.command(values, self.run, resume_id='fixture')
        self.assertNotIn('--dtype', resume)
        self.assertNotIn('--eval-iters', resume)
        self.assertIn('--early-stop-min-delta', resume)
        self.assertIn('--log-interval', resume)
        with self.assertRaisesRegex(ValueError, 'Float16'):
            self.trainer.validate({**self.values, 'dtype': 'float16'})

    def test_benchmark_command_has_unique_outputs_and_validates_plan(self):
        with patch.object(self.trainer, 'pipeline_busy', return_value=False):
            first = self.trainer.benchmark_command('fixture', 'best', sys.executable, eval_iters=2,
                                                   batch_size=1, seed=4, tokens=10, prompts=['A cube'])
            second = self.trainer.benchmark_command('fixture', 'best', sys.executable)
            self.assertEqual(first[first.index('--prompt') + 1], 'A cube')
            self.assertEqual(first[first.index('--seed') + 1], '4')
            output = Path(first[first.index('--output') + 1])
            self.assertEqual(output.parent, self.run / 'benchmarks')
            self.assertNotEqual(str(output), second[second.index('--output') + 1])
            self.assertFalse(output.exists())
            for options in ({'eval_iters': 0}, {'seed': -1}, {'tokens': 5000}, {'prompts': ['x'] * 9},
                            {'prompts': ['é' * 3000]}, {'batch_size': 65}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.trainer.benchmark_command('fixture', 'best', sys.executable, **options)
        with patch.object(self.trainer, 'pipeline_busy', return_value=True), self.assertRaisesRegex(ValueError, 'finish'):
            self.trainer.benchmark_command('fixture', 'best', sys.executable)

    def test_benchmark_history_ignores_invalid_reports_and_is_in_run_report(self):
        reports = self.run / 'benchmarks'; reports.mkdir()
        good = {'kind': 'whitecube_checkpoint_benchmark', 'checkpoint': {'name': 'ckpt_best.pt'},
                'validation': {'loss': 1.25}}
        engine.atomic_json(reports / '002.json', good)
        (reports / '001.json').write_text('not json')
        engine.atomic_json(reports / '003.json', {'kind': 'unrelated'})
        engine.atomic_json(reports / '004.json', {**good, 'generation': []})
        engine.atomic_json(reports / '005.json', {**good, 'generation': {'samples': [None]}})
        self.assertEqual(self.trainer.benchmarks('fixture'), [good])
        self.assertEqual(self.trainer.report('fixture')['benchmarks'], [good])


if __name__ == '__main__':
    unittest.main()
