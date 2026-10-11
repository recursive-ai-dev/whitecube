"""Corpus controls and prepared-corpus inspection without NumPy or PyTorch."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from files import file_sha256


class EngineCorpusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trainer = engine.Trainer(home=self.root, config=self.root / 'config.json', state=self.root / 'state')
        self.source = self.root / 'source.jsonl'
        self.source.write_text('{"text":"hello"}')
        self.values = {**self.trainer.preferences(), 'python': sys.executable, 'dataset': str(self.source), 'name': 'Corpus run'}

    def prepared(self):
        directory = self.root / 'prepared'
        directory.mkdir()
        for split in ('train', 'val'):
            (directory / f'{split}.bin').write_bytes(struct.pack('H', 97) * 700)
        manifest = {'train_tokens': 700, 'val_tokens': 700, 'total_tokens': 1400,
                    'artifacts': {name: {'bytes': 1400, 'sha256': file_sha256(directory / name)}
                                  for name in ('train.bin', 'val.bin')}}
        (directory / 'corpus.json').write_text(json.dumps(manifest))
        return directory

    def test_document_controls_round_trip_and_reach_new_run_only(self):
        options = {'deduplicate': True, 'shuffle_files': True, 'min_document_bytes': 200,
                   'max_document_bytes': 8000, 'batch_rows': 8}
        self.trainer.save_preferences(options)
        self.assertEqual({key: self.trainer.preferences()[key] for key in options}, options)
        values = self.trainer.validate({**self.values, **options})
        command = self.trainer.command(values, self.root / 'run')
        self.assertIn('--deduplicate', command)
        self.assertIn('--shuffle-files', command)
        for key in ('min_document_bytes', 'max_document_bytes', 'batch_rows'):
            self.assertEqual(command[command.index('--' + key.replace('_', '-')) + 1], str(options[key]))
        resume = self.trainer.command(values, self.root / 'run', resume_id='run')
        self.assertNotIn('--deduplicate', resume)
        self.assertNotIn('--batch-rows', resume)

    def test_invalid_controls_and_length_bounds_fail_validation(self):
        for options in ({'deduplicate': 'true'}, {'shuffle_files': 1}, {'batch_rows': 0},
                        {'min_document_bytes': 20, 'max_document_bytes': 10}, {'max_document_bytes': -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.trainer.validate({**self.values, **options})

    def test_prepared_folder_is_inspected_without_training_dependencies(self):
        directory = self.prepared()
        info = engine.dataset_info(directory)
        self.assertTrue(info['prepared'])
        self.assertEqual(info['corpus']['train_tokens'], 700)
        self.assertEqual(info['errors'], [])
        self.assertEqual(len(info['files']), 3)
        # Import needs only enough space for the existing artifacts, even with a
        # very large cap selected in the draft (the saved corpus cap is reused).
        with patch('engine.shutil.disk_usage') as disk:
            disk.return_value.free = 200_000_000
            values = self.trainer.validate({**self.values, 'dataset': str(directory), 'max_tokens': 10_000_000_000})
        self.assertEqual(values['dataset'], str(directory))

    def test_malformed_or_missing_prepared_artifacts_report_an_error(self):
        directory = self.prepared()
        (directory / 'val.bin').unlink()
        self.assertTrue(engine.dataset_info(directory)['errors'])
        with self.assertRaisesRegex(ValueError, 'Unable to read corpus'):
            self.trainer.validate({**self.values, 'dataset': str(directory)})
        (directory / 'corpus.json').write_text('{"total_tokens":"broken"}')
        info = engine.dataset_info(directory)
        self.assertIn('nonnegative integer', info['errors'][0])
