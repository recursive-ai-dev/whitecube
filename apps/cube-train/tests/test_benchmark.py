"""Real tiny-model benchmarks; all corpus and checkpoint files are temporary."""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PIPELINE = Path(__file__).resolve().parents[1] / 'pipeline'
sys.path.insert(0, str(PIPELINE))

import numpy as np
import torch

import benchmark as assessment
from files import atomic_json, file_sha256
import tensegrity_lm as lm

torch.set_num_threads(2)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='whitecube-benchmark-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / 'run'
        self.data = self.run / 'data'
        self.data.mkdir(parents=True)
        for split, offset in (('train', 0), ('val', 19)):
            ((np.arange(1024, dtype=np.uint16) + offset) % 259).tofile(self.data / f'{split}.bin')
        self.manifest = {'train_tokens': 1024, 'val_tokens': 1024, 'total_tokens': 2048,
                         'split_method': 'document_hash',
                         'artifacts': {name: {'bytes': (self.data / name).stat().st_size,
                                              'sha256': file_sha256(self.data / name)}
                                       for name in ('train.bin', 'val.bin')}}
        atomic_json(self.data / 'corpus.json', self.manifest)
        torch.manual_seed(17)
        self.cfg = lm.TensegrityConfig(block_size=8, n_layer=1, n_embd=16, n_head=4, n_kv_head=2,
                                       sliding_window=8, ffn_align=8, dropout=.2, layerdrop_max=.2,
                                       grad_checkpoint=False)
        self.model = lm.TensegrityLM(self.cfg)
        optimizer = torch.optim.AdamW(self.model.parameters(), lr=.001)
        x, y = lm.get_batch(str(self.data), 'train', 2, self.cfg.block_size, 'cpu', rng=np.random.RandomState(1))
        _, losses, _ = self.model(x, y)
        losses['total_loss'].backward()
        optimizer.step()
        self.model.eval()
        for name in ('ckpt_best.pt', 'ckpt_latest.pt', 'ckpt_step00000005.pt'):
            lm.save_checkpoint(str(self.run / name), self.model, optimizer, self.cfg, 4, 1.25, {})
        (self.run / 'run.json').write_text('{"name":"test run"}\n', encoding='utf-8')

    def run_benchmark(self, **kwargs):
        options = {'eval_iters': 3, 'batch_size': 2, 'seed': 27, 'cpu_threads': 2,
                   'prompts': ['A story', 'An answer'], 'max_new_tokens': 6}
        options.update(kwargs)
        return assessment.benchmark(self.run, **options)

    def test_measures_loss_and_byte_metrics_on_real_validation_batches(self):
        report = self.run_benchmark()
        self.assertEqual(report['checkpoint']['completed_steps'], 5)
        self.assertEqual(report['checkpoint']['best_val_loss'], 1.25)
        self.assertEqual(report['checkpoint']['sha256'], file_sha256(self.run / 'ckpt_best.pt'))
        self.assertEqual(report['model']['parameters'], sum(p.numel() for p in self.model.parameters()))
        self.assertEqual(report['runtime']['device'], 'cpu')
        self.assertTrue(report['corpus']['integrity_verified'])
        self.assertEqual(report['corpus']['artifacts'], self.manifest['artifacts'])
        validation = report['validation']
        self.assertEqual(validation['tokens'], 3 * 2 * 8)
        self.assertGreater(validation['tokens_per_second'], 0)
        self.assertGreater(report['generation']['tokens_per_second'], 0)
        expected_nll = expected_byte_nll = 0.0
        byte_tokens = 0
        rng = np.random.RandomState(27)
        with torch.no_grad():
            for _ in range(3):
                x, y = lm.get_batch(str(self.data), 'val', 2, 8, 'cpu', rng=rng)
                logits, _, _ = self.model(x)
                losses = torch.nn.functional.cross_entropy(logits.flatten(0, 1), y.flatten(), reduction='none').double()
                mask = y.flatten() < 256
                expected_nll += losses.sum().item()
                expected_byte_nll += losses[mask].sum().item()
                byte_tokens += mask.sum().item()
        self.assertAlmostEqual(validation['loss'], expected_nll / 48, places=6)
        self.assertAlmostEqual(validation['bits_per_byte'], expected_byte_nll / byte_tokens / math.log(2), places=6)
        self.assertAlmostEqual(validation['perplexity'], math.exp(validation['loss']), places=6)
        self.assertEqual(validation['byte_tokens'], byte_tokens)
        self.assertNotEqual(validation['loss'], report['checkpoint']['best_val_loss'])

    def test_repeatable_seeded_validation_and_stochastic_generation(self):
        first = self.run_benchmark(temperature=.8)
        second = self.run_benchmark(temperature=.8)
        for name in ('loss', 'perplexity', 'bits_per_byte', 'batch_sha256'):
            self.assertEqual(first['validation'][name], second['validation'][name])
        self.assertEqual([sample['token_ids'] for sample in first['generation']['samples']],
                         [sample['token_ids'] for sample in second['generation']['samples']])
        third = self.run_benchmark(seed=28)
        self.assertNotEqual(first['validation']['batch_sha256'], third['validation']['batch_sha256'])
        self.assertEqual(first['generation']['samples'][0]['seed'], 27)
        self.assertEqual(first['generation']['samples'][1]['seed'], 28)

    def test_report_is_only_mutation_and_caller_rng_threads_are_preserved(self):
        before = {path: (path.stat().st_mtime_ns, file_sha256(path)) for path in self.run.rglob('*') if path.is_file()}
        torch.manual_seed(77)
        rng_before = torch.get_rng_state().clone()
        threads_before = torch.get_num_threads()
        output = self.run / 'benchmarks' / 'best.json'
        report = self.run_benchmark(output=output, cpu_threads=1)
        self.assertTrue(torch.equal(torch.get_rng_state(), rng_before))
        self.assertEqual(torch.get_num_threads(), threads_before)
        self.assertEqual(json.loads(output.read_text()), report)
        self.assertEqual(set(path for path in self.run.rglob('*') if path.is_file()), set(before) | {output})
        for path, signature in before.items():
            self.assertEqual((path.stat().st_mtime_ns, file_sha256(path)), signature)
        with self.assertRaises(FileExistsError):
            self.run_benchmark(output=output)
        self.assertEqual(json.loads(output.read_text()), report)

    def test_rejects_corrupt_data_without_publishing_report(self):
        output = self.run / 'benchmarks' / 'bad.json'
        with (self.data / 'val.bin').open('r+b') as handle:
            handle.write(b'\x00\x00')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.run_benchmark(output=output)
        self.assertFalse(output.parent.exists())

    def test_rejects_out_of_vocabulary_legacy_data_and_reports_missing_manifest(self):
        (self.data / 'corpus.json').unlink()
        report = self.run_benchmark(prompts=[], checkpoint='latest')
        self.assertFalse(report['corpus']['integrity_verified'])
        self.assertIsNone(report['corpus']['metadata_sha256'])
        self.assertIn('sha256', report['corpus']['artifacts']['val.bin'])
        self.assertEqual(report['generation']['tokens'], 0)
        with (self.data / 'val.bin').open('r+b') as handle:
            handle.write(np.array([259], dtype=np.uint16).tobytes())
        with self.assertRaisesRegex(ValueError, 'outside the vocabulary'):
            self.run_benchmark()

    def test_protects_data_checkpoints_metadata_and_symlinks(self):
        paths = [self.data / 'report.json', self.run / 'ckpt_latest.pt', self.run / 'run.json',
                 self.run / 'status.json', self.root / 'corpus.json']
        alias = self.root / 'alias'
        alias.symlink_to(self.data, target_is_directory=True)
        paths.append(alias / 'report.json')
        symlink = self.root / 'output.json'
        symlink.symlink_to(self.run / 'run.json')
        paths.append(symlink)
        for output in paths:
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.run_benchmark(output=output)
        self.assertEqual((self.run / 'run.json').read_text(), '{"name":"test run"}\n')

    def test_invalid_options_and_snapshot_selection(self):
        for options in ({'eval_iters': 0}, {'eval_iters': 10001}, {'batch_size': True},
                        {'cpu_threads': 0}, {'seed': -1}, {'seed': 2**32},
                        {'temperature': float('nan')}, {'max_new_tokens': -1},
                        {'prompts': 'bad'}, {'checkpoint': '../../outside'}, {'checkpoint': 'step0'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_benchmark(**options)
        report = self.run_benchmark(checkpoint='step5', max_new_tokens=0)
        self.assertEqual(report['checkpoint']['name'], 'ckpt_step00000005.pt')
        self.assertEqual(report['generation']['tokens'], 0)
        self.assertEqual(report['generation']['samples'][0]['continuation'], '')

    def test_rejects_inputs_changed_during_evaluation(self):
        original_hash = assessment.file_sha256
        output = self.root / 'changed.json'
        changed = False

        def mutate_after_hash(path):
            nonlocal changed
            result = original_hash(path)
            if Path(path).name == 'val.bin' and not changed:
                changed = True
                with Path(path).open('ab') as handle:
                    handle.write(b'\x00\x00')
            return result

        with patch.object(assessment, 'file_sha256', side_effect=mutate_after_hash):
            with self.assertRaisesRegex(ValueError, 'changed during evaluation'):
                self.run_benchmark(output=output)
        self.assertFalse(output.exists())

    def test_cli_writes_machine_readable_report(self):
        output = self.run / 'benchmarks' / 'cli.json'
        command = [sys.executable, '-I', str(PIPELINE / 'benchmark.py'), '--run-dir', str(self.run),
                   '--checkpoint', 'latest', '--output', str(output), '--eval-iters', '1',
                   '--batch-size', '1', '--prompt', 'Hello', '--prompt', 'World', '--max-new-tokens', '2']
        process = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report, json.loads(output.read_text()))
        self.assertEqual([sample['prompt'] for sample in report['generation']['samples']], ['Hello', 'World'])
        self.assertEqual(report['validation']['tokens'], 8)
        self.assertEqual(report['checkpoint']['name'], 'ckpt_latest.pt')


if __name__ == '__main__':
    unittest.main()
