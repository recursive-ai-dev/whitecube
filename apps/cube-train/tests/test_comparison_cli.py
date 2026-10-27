"""Run comparisons and headless entry points must work without a model runtime."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import cli
import engine


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trainer = engine.Trainer(home=self.root, state=self.root / 'state', config=self.root / 'config')
        self.trainer.runs_dir = self.root / 'runs'
        self.corpus = {'tokenizer': 'UTF-8 bytes + EOS=257', 'block_size': 256,
                       'artifacts': {'train.bin': {'sha256': 'a' * 64, 'bytes': 2048},
                                     'val.bin': {'sha256': 'b' * 64, 'bytes': 1024}}}

    def run_fixture(self, name, loss=3.0, **settings):
        directory = self.trainer.runs_dir / name
        directory.mkdir(parents=True)
        engine.atomic_json(directory / 'run.json', {'model_size': '2m', 'steps': 10, 'eval_iters': 20,
                                                  'device': 'cpu', **settings})
        engine.atomic_json(directory / 'result.json', {'best_val_loss': loss, 'completed_steps': 10,
                                                     'dtype': 'float32', 'total_training_seconds': 8})
        engine.atomic_json(directory / 'progress.json', {'tokens_per_second': 250, 'dtype': 'float32'})
        engine.atomic_json(directory / 'data/corpus.json', self.corpus)
        return directory

    def test_identical_corpora_rank_finite_losses(self):
        self.run_fixture('first', 3.0)
        self.run_fixture('second', 2.5)
        report = self.trainer.compare_runs(['first', 'second'])
        self.assertTrue(report['comparable'])
        self.assertEqual(report['best_run_id'], 'second')
        self.assertEqual([row['id'] for row in report['runs']], ['first', 'second'])
        self.assertAlmostEqual(report['runs'][1]['bits_per_byte'], 2.5 / engine.math.log(2))
        self.assertEqual(report['runs'][0]['training_seconds'], 8)
        json.dumps(report, allow_nan=False)

    def test_reused_corpus_location_does_not_affect_comparability(self):
        self.run_fixture('a')
        b = self.run_fixture('b')
        engine.atomic_json(b / 'data/corpus.json', {**self.corpus, 'source': '/another/place', 'reused_from': '/original/data'})
        self.assertTrue(self.trainer.compare_runs()['comparable'])

    def test_different_corpora_never_get_a_winner(self):
        self.run_fixture('a')
        b = self.run_fixture('b', 2)
        other = {**self.corpus, 'artifacts': {**self.corpus['artifacts'], 'val.bin': {'sha256': 'c' * 64, 'bytes': 1024}}}
        engine.atomic_json(b / 'data/corpus.json', other)
        report = self.trainer.compare_runs()
        self.assertFalse(report['comparable'])
        self.assertIsNone(report['best_run_id'])
        self.assertIn('different prepared corpora', ' '.join(report['warnings']))

    def test_legacy_or_corrupt_metadata_cannot_claim_matching_data(self):
        self.run_fixture('a')
        b = self.run_fixture('b')
        for value in ({}, {'artifacts': []}, {'artifacts': {'train.bin': None}},
                      {**self.corpus, 'artifacts': {**self.corpus['artifacts'], 'val.bin': {'sha256': 'x', 'bytes': 1024}}}):
            engine.atomic_json(b / 'data/corpus.json', value)
            self.assertFalse(self.trainer.compare_runs()['comparable'])
        (b / 'data/corpus.json').write_text('{broken')
        self.assertFalse(self.trainer.compare_runs()['comparable'])

    def test_conflicting_or_missing_evaluation_plans_cannot_rank(self):
        self.run_fixture('a')
        b = self.run_fixture('b', eval_iters=4)
        self.assertFalse(self.trainer.compare_runs()['comparable'])
        engine.atomic_json(b / 'run.json', {'model_size': '2m'})
        self.assertFalse(self.trainer.compare_runs()['comparable'])

    def test_precision_is_part_of_comparison_plan(self):
        self.run_fixture('a')
        b = self.run_fixture('b')
        engine.atomic_json(b / 'progress.json', {'dtype': 'bfloat16'})
        self.assertFalse(self.trainer.compare_runs()['comparable'])

    def test_running_best_available_without_finished_result(self):
        a = self.run_fixture('a')
        self.run_fixture('b')
        (a / 'result.json').unlink()
        engine.atomic_json(a / 'progress.json', {'best_val_loss': 2.1, 'dtype': 'float32', 'completed_steps': 4})
        report = self.trainer.compare_runs(['a', 'b'])
        self.assertEqual(report['best_run_id'], 'a')
        self.assertEqual(report['runs'][0]['step'], 4)

    def test_invalid_numbers_are_missing_and_json_remains_strict(self):
        a = self.run_fixture('a', loss=-1)
        self.run_fixture('b')
        engine.atomic_json(a / 'progress.json', {'tokens_per_second': -2, 'best_val_loss': 'bad', 'dtype': 'float32'})
        report = self.trainer.compare_runs(['a', 'b'])
        self.assertIsNone(report['runs'][0]['val_loss'])
        self.assertIsNone(report['runs'][0]['tokens_per_second'])
        self.assertIsNone(report['best_run_id'])
        json.dumps(report, allow_nan=False)

    def test_overflowing_derived_metrics_are_unrankable_and_json_safe(self):
        self.run_fixture('a', loss=1.7e308)
        self.run_fixture('b')
        report = self.trainer.compare_runs(['a', 'b'])
        self.assertFalse(report['comparable'])
        self.assertIsNone(report['runs'][0]['val_loss'])
        self.assertIsNone(report['runs'][0]['bits_per_byte'])
        json.dumps(report, allow_nan=False)

    def test_empty_single_duplicates_and_selection_errors(self):
        self.assertFalse(self.trainer.compare_runs()['comparable'])
        self.run_fixture('a')
        self.assertEqual(len(self.trainer.compare_runs(['a', 'a'])['runs']), 1)
        for ids in ('a', [None], ['missing'], ['a'] * 21):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.trainer.compare_runs(ids)
        self.assertEqual(self.trainer.compare_runs([])['runs'], [])

    def test_default_selection_uses_creation_time_instead_of_directory_name(self):
        for name, date in (('zzz-old', '2025-01-01T00:00:00+00:00'), ('aaa-new', '2026-01-01T00:00:00+00:00')):
            self.run_fixture(name, created_at=date)
        self.assertEqual([row['id'] for row in self.trainer.compare_runs()['runs']], ['aaa-new', 'zzz-old'])

    def test_csv_formula_safety_and_comparability_warnings(self):
        self.run_fixture('a', display_name='  =2+2', eval_iters=10)
        self.run_fixture('b')
        report = self.trainer.compare_runs(['a', 'b'])
        rows = list(csv.DictReader(io.StringIO(cli.comparison_csv(report))))
        self.assertEqual(rows[0]['name'], "'  =2+2")
        self.assertEqual(rows[0]['comparable'], 'False')
        self.assertIn('budgets', rows[0]['warnings'])
        self.assertIn('Warning:', cli.comparison_text(report))

    def test_packaged_resume_commands_use_installed_cli(self):
        self.run_fixture('a')
        with patch.dict(os.environ, {'WHITECUBE_TRAIN_COMMAND': 'whitecube-trainer-cli'}):
            self.assertTrue(self.trainer.resume_command('a', 20).startswith('whitecube-trainer-cli --resume '))

    def test_compare_cli_outputs_parseable_json_without_modifying_runs(self):
        self.run_fixture('a')
        self.run_fixture('b', 2)
        with patch.object(cli.engine, 'Trainer', return_value=self.trainer), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['--compare-runs', 'a', 'b', '--json']), 0)
        self.assertEqual(json.loads(output.getvalue())['best_run_id'], 'b')


class HeadlessCLITests(unittest.TestCase):
    def test_documented_help_and_summary_modes_run_in_system_python(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, HOME=temp, XDG_CONFIG_HOME=temp + '/config',
                       XDG_STATE_HOME=temp + '/state', WHITECUBE_TRAIN_OUT=temp + '/runs')
            for args in (['--version'], ['--help'], ['--benchmark', '--help'], ['--export-notebook', '--help'],
                         ['--list-runs'], ['--status'], ['--compare-runs', '--json']):
                with self.subTest(args=args):
                    result = subprocess.run(['/usr/bin/python3', '-I', str(HERE / 'cli.py'), *args],
                                            env=env, text=True, capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.strip())
            self.assertFalse((Path(temp) / 'config').exists())
            self.assertFalse((Path(temp) / 'runs').exists())

    def test_mode_import_does_not_load_torch(self):
        script = f"import sys; sys.path.insert(0,{str(HERE)!r}); import cli; cli.main(['--help']); assert 'torch' not in sys.modules"
        result = subprocess.run(['/usr/bin/python3', '-I', '-c', script], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_comparison_id_is_a_concise_error(self):
        result = subprocess.run(['/usr/bin/python3', '-I', str(HERE / 'cli.py'), '--compare-runs', 'missing-id'],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Unavailable run IDs', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
