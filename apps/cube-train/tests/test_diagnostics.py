"""Readiness checks run independently of GTK displays and training packages."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
import engine


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trainer = engine.Trainer(home=self.root, state=self.root / 'state', config=self.root / 'config')
        self.trainer.runs_dir = self.root / 'runs/deep'
        corpus = self.root / 'corpus.txt'
        corpus.write_text('Corpus contents\n')
        self.values = {'python': sys.executable, 'dataset': str(corpus), 'device': 'cpu'}
        self.runtime = {'torch': '2.0', 'numpy': '2.0', 'cuda': False, 'gpu': None,
                        'bf16': False, 'parquet': False, 'training_error': None}
        desktop = patch.object(diagnostics, '_desktop_check', return_value=diagnostics._check('desktop', 'Desktop libraries', 'ok', 'fixture'))
        desktop.start()
        self.addCleanup(desktop.stop)
        self.before = sorted(str(item.relative_to(self.root)) for item in self.root.rglob('*'))

    @staticmethod
    def by_id(report, identifier):
        return next(item for item in report['checks'] if item['id'] == identifier)

    def test_fast_checks_never_import_training_or_create_directories(self):
        with patch.object(engine, 'probe_runtime', side_effect=AssertionError('Runtime not requested')):
            report = diagnostics.collect_diagnostics(self.trainer, values=self.values)
        self.assertTrue(report['ok'])
        self.assertFalse(report['runtime_checked'])
        self.assertEqual(self.by_id(report, 'runtime')['status'], 'warning')
        self.assertEqual(self.by_id(report, 'output')['filesystem_path'], str(self.root))
        self.assertEqual(self.before, sorted(str(item.relative_to(self.root)) for item in self.root.rglob('*')))
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)
        self.assertIn('not been verified', diagnostics.format_diagnostics(report))

    def test_readiness_remedy_uses_package_command(self):
        with patch.dict(os.environ, {'WHITECUBE_TRAIN_COMMAND': 'whitecube-trainer-cli'}):
            report = diagnostics.collect_diagnostics(self.trainer, values=self.values)
        self.assertIn('whitecube-trainer-cli --doctor', self.by_id(report, 'runtime')['remedy'])

    def test_missing_parquet_decoder_is_a_required_runtime_failure(self):
        with patch.object(engine, 'probe_runtime', return_value=self.runtime) as probe:
            report = diagnostics.collect_diagnostics(self.trainer, check_runtime=True, values=self.values)
        probe.assert_called_once_with(sys.executable)
        self.assertFalse(report['ok'])
        self.assertTrue(report['runtime_checked'])
        self.assertEqual(self.by_id(report, 'runtime')['status'], 'ok')
        self.assertEqual(self.by_id(report, 'device')['detail'], 'CPU')
        self.assertEqual(self.by_id(report, 'parquet')['status'], 'error')
        self.assertIn('PyArrow', self.by_id(report, 'parquet')['detail'])

    def test_cuda_unavailable_and_incompatible_precision_have_actionable_errors(self):
        with patch.object(engine, 'probe_runtime', return_value=self.runtime):
            report = diagnostics.collect_diagnostics(self.trainer, check_runtime=True,
                                                     values={**self.values, 'device': 'cuda'})
            self.assertFalse(report['ok'])
            self.assertEqual(self.by_id(report, 'device')['status'], 'error')
            self.assertIn('Select CPU', self.by_id(report, 'device')['remedy'])
            report = diagnostics.collect_diagnostics(self.trainer, check_runtime=True,
                                                     values={**self.values, 'dtype': 'float16'})
            self.assertEqual(self.by_id(report, 'precision')['status'], 'error')

    def test_missing_or_broken_runtime_has_environment_repair_instructions(self):
        for failure in (ValueError('Missing PyTorch'), None):
            with self.subTest(failure=failure):
                runtime = {**self.runtime, 'training_error': 'Missing sympy'}
                with patch.object(engine, 'probe_runtime', side_effect=failure, return_value=runtime):
                    report = diagnostics.collect_diagnostics(self.trainer, check_runtime=True, values=self.values)
                check = self.by_id(report, 'runtime')
                self.assertFalse(report['ok'])
                self.assertEqual(check['status'], 'error')
                self.assertIn('python3 -m venv', check['remedy'])
                self.assertNotIn('sudo pip', check['remedy'])

    def test_invalid_interpreter_does_not_launch_probe(self):
        for executable in ('python3', '/does/not/exist', '\x00bad', None):
            with self.subTest(executable=executable), patch.object(engine, 'probe_runtime') as probe:
                report = diagnostics.collect_diagnostics(self.trainer, check_runtime=True,
                                                         values={**self.values, 'python': executable})
                self.assertFalse(report['ok'])
                self.assertEqual(self.by_id(report, 'python')['status'], 'error')
                probe.assert_not_called()

    def test_storage_file_ancestor_and_low_disk(self):
        blocked = self.root / 'regular-file'
        blocked.write_text('file')
        self.trainer.runs_dir = blocked / 'runs'
        report = diagnostics.collect_diagnostics(self.trainer, values=self.values)
        self.assertEqual(self.by_id(report, 'output')['status'], 'error')
        self.assertFalse(report['ok'])
        self.trainer.runs_dir = self.root / 'runs'
        with patch.object(diagnostics.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 512})()):
            report = diagnostics.collect_diagnostics(self.trainer, values=self.values)
        self.assertEqual(self.by_id(report, 'output')['status'], 'warning')
        self.assertIn('Reduce', self.by_id(report, 'output')['remedy'].replace('reduce', 'Reduce'))

    def test_corpus_accessibility_does_not_claim_content_validation(self):
        folder = self.root / 'dataset'
        folder.mkdir()
        report = diagnostics.collect_diagnostics(self.trainer, values={**self.values, 'dataset': str(folder)})
        self.assertIn('Inspect', self.by_id(report, 'corpus')['detail'])
        for value in (str(folder / 'missing.txt'), str(self.root / 'empty.txt')):
            if value.endswith('empty.txt'):
                Path(value).touch()
            report = diagnostics.collect_diagnostics(self.trainer, values={**self.values, 'dataset': value})
            self.assertEqual(self.by_id(report, 'corpus')['status'], 'error')
        report = diagnostics.collect_diagnostics(self.trainer, values={**self.values, 'dataset': ''})
        self.assertEqual(self.by_id(report, 'corpus')['status'], 'warning')

    def test_desktop_check_is_isolated_and_reports_missing_libraries(self):
        # Access the original function while the per-test fixture is patched.
        import importlib.util
        specification = importlib.util.spec_from_file_location('diagnostics_desktop_fixture', diagnostics.__file__)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        result = subprocess.CompletedProcess([], 0, '{"gtk":"4.10","adwaita":"1.3"}\n', '')
        with patch.object(module.subprocess, 'run', return_value=result) as launch:
            check = module._desktop_check()
        self.assertEqual(check['status'], 'ok')
        self.assertEqual(launch.call_args.args[0][:2], ['/usr/bin/python3', '-I'])
        self.assertLessEqual(launch.call_args.kwargs['timeout'], 10)
        with patch.object(module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('python', 10)):
            self.assertEqual(module._desktop_check()['status'], 'error')

    def test_cli_json_respects_overrides_without_saving(self):
        output = io.StringIO()
        with patch.object(engine, 'Trainer', return_value=self.trainer), contextlib.redirect_stdout(output):
            status = diagnostics.main(['--json', '--python', sys.executable, '--device', 'cpu',
                                       '--output-root', str(self.root / 'elsewhere')])
        report = json.loads(output.getvalue())
        self.assertEqual(report['system']['training_python'], sys.executable)
        self.assertEqual(self.by_id(report, 'output')['path'], str(self.root / 'elsewhere'))
        self.assertEqual(status, 1)  # the trainer's unsaved default corpus does not exist
        self.assertFalse(self.trainer.config.exists())


if __name__ == '__main__':
    unittest.main()
