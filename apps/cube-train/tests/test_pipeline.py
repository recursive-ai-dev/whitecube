import json
import tempfile
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))

import numpy as np
import torch
torch.set_num_threads(2)
import pyarrow as pa
import pyarrow.parquet as pq

from prepare_folder import prepare_folder
from prepare_parquet import prepare
from tensegrity_lm import TensegrityConfig, evaluate, load_checkpoint, prepare_data
from tensegrity_nb_lib import run_training


class TinyModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = torch.nn.Embedding(258, 8)
        self.output = torch.nn.Linear(8, 258)

    def forward(self, x, y):
        logits = self.output(self.embedding(x))
        loss = torch.nn.functional.cross_entropy(logits.flatten(0, 1), y.flatten())
        return logits, {'ce_loss': loss, 'total_loss': loss, 'homeo_loss': loss * 0}, None


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = [f'Document {i}: ' + (chr(65 + i % 26) * 600) for i in range(40)]
        source = self.root / 'docs.jsonl'
        source.write_text('\n'.join(json.dumps({'text': d}) for d in self.docs + self.docs[:5]))
        self.source = source

    def documents(self, path):
        ids = np.fromfile(path, dtype=np.uint16)
        result, start = [], 0
        for end in np.where(ids == 257)[0]:
            result.append(bytes(ids[start:end].astype(np.uint8)).decode())
            start = end + 1
        return result

    def test_whole_documents_duplicates_repeatability_and_parquet(self):
        a, b = self.root / 'a', self.root / 'b'
        prepare_folder(self.source, a)
        prepare_folder(self.source, b)
        train, val = self.documents(a / 'train.bin'), self.documents(a / 'val.bin')
        self.assertFalse(set(train) & set(val))
        self.assertCountEqual(train + val, self.docs + self.docs[:5])
        for split in ('train', 'val'):
            self.assertEqual((a / f'{split}.bin').read_bytes(), (b / f'{split}.bin').read_bytes())
        parquet = self.root / 'docs.parquet'
        pq.write_table(pa.table({'text': self.docs + self.docs[:5]}), parquet)
        prepare(parquet, self.root / 'p')
        for split in ('train', 'val'):
            self.assertEqual((a / f'{split}.bin').read_bytes(), (self.root / 'p' / f'{split}.bin').read_bytes())

    def test_legacy_preparation_entry_and_token_cap(self):
        output = self.root / 'legacy'
        prepare_data(str(self.source), str(output))
        self.assertTrue((output / 'meta.json').exists())
        self.assertFalse(set(self.documents(output / 'train.bin')) &
                         set(self.documents(output / 'val.bin')))
        report = prepare_folder(self.source, self.root / 'capped', max_tokens=6500)
        self.assertLessEqual(report['total_tokens'], 6500)
        self.assertGreater(report['val_tokens'], 257)

    def test_single_file_chunks_cannot_leak(self):
        source = self.root / 'long.txt'
        source.write_text('A' * 150000)
        with self.assertRaisesRegex(ValueError, 'two distinct documents'):
            prepare_folder(source, self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())

    def test_cancelled_preparation_does_not_leave_partial_corpus(self):
        calls = 0
        def stop():
            nonlocal calls
            calls += 1
            return calls >= 7
        with self.assertRaises(InterruptedError):
            prepare_folder(self.source, self.root / 'cancelled', stop_requested=stop)
        self.assertFalse((self.root / 'cancelled').exists())
        self.assertFalse(list(self.root.glob('.prepare-*')))

    def test_evaluation_preserves_rng_and_mode(self):
        data = self.root / 'data'
        prepare_folder(self.source, data)
        model = TinyModel().train()
        cfg = TensegrityConfig(block_size=16)
        before_np, before_torch = np.random.get_state(), torch.get_rng_state().clone()
        args = (model, str(data), cfg, 'cpu', 'cpu', torch.float32, False, 4)
        first = evaluate(*args)
        self.assertEqual(first, evaluate(*args))
        self.assertTrue(model.training)
        self.assertTrue(torch.equal(before_torch, torch.get_rng_state()))
        after_np = np.random.get_state()
        self.assertTrue(np.array_equal(before_np[1], after_np[1]))
        self.assertEqual(before_np[2:], after_np[2:])

    def test_resume_matches_uninterrupted_and_extension_keeps_plan(self):
        data = self.root / 'data'
        prepare_folder(self.source, data)
        cfg = TensegrityConfig(block_size=16)
        def run(name, stop=None, resume=False, steps=4, horizon=None):
            torch.manual_seed(13)
            np.random.seed(13)
            model = TinyModel()
            optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4)
            run_training(model, optimizer, cfg, str(self.root / name), 'cpu', str(data),
                         max_steps=steps, warmup_steps=1, batch_size=2, eval_iters=3,
                         log_interval=1, eval_interval=2, stop_requested=stop,
                         init_from='resume' if resume else 'scratch', schedule_steps=horizon)
            return load_checkpoint(str(self.root / name / 'ckpt_latest.pt'), 'cpu')
        full = run('full')
        run('split', stop=lambda step: step == 2)
        resumed = run('split', resume=True)
        for key, value in full['model_state_dict'].items():
            self.assertTrue(torch.equal(value, resumed['model_state_dict'][key]), key)
        extended = run('split', resume=True, steps=5)
        self.assertEqual(extended['training_state']['schedule']['schedule_steps'], 4)
        self.assertAlmostEqual(extended['optimizer_state_dict']['param_groups'][0]['lr'], 3e-5)
        legacy = dict(extended)
        legacy.pop('training_state')
        torch.save(legacy, self.root / 'split' / 'ckpt_latest.pt')
        migrated = run('split', resume=True, steps=6)
        self.assertEqual(migrated['training_state']['schedule']['schedule_steps'], 5)
        self.assertTrue(np.isfinite(migrated['best_val_loss']))
        changed = run('split', resume=True, steps=7, horizon=10)
        self.assertEqual(changed['training_state']['schedule']['schedule_steps'], 10)


if __name__ == '__main__':
    torch.set_num_threads(1)
    unittest.main()
