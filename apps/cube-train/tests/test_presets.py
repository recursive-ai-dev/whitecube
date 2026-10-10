"""Train tiny temporary runs to verify real architectures, saved states and resume."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import torch
torch.set_num_threads(2)
from model_presets import MODEL_PRESETS
from prepare_folder import prepare_folder
from train_local import local_config, train
from tensegrity_lm import TensegrityLM, load_checkpoint


class PresetTests(unittest.TestCase):
    def test_declared_counts_are_real_model_counts(self):
        for name, preset in MODEL_PRESETS.items():
            with self.subTest(name=name):
                model = TensegrityLM(local_config(name))
                self.assertEqual(sum(parameter.numel() for parameter in model.parameters()), preset['parameters'])

    def test_new_sizes_train_save_and_resume_on_cpu(self):
        with tempfile.TemporaryDirectory(prefix='whitecube-preset-test-') as temp:
            root = Path(temp)
            corpus = root / 'corpus.jsonl'
            corpus.write_text('\n'.join(json.dumps({'text': f'Document {i}. ' + 'A small cube learned from stories. ' * 30}) for i in range(40)))
            prepare_folder(corpus, root / 'data')
            for size in ('2m', '5m', '10m'):
                with self.subTest(size=size):
                    directory = root / size
                    result = train(root / 'data', directory, steps=2, batch_size=1, grad_accum_steps=1,
                                   device='cpu', model_size=size, stop_requested=lambda step: step >= 1)
                    self.assertEqual(result[0], 1)
                    first = load_checkpoint(str(directory / 'ckpt_latest.pt'), 'cpu')
                    self.assertEqual(first['config']['n_embd'], MODEL_PRESETS[size]['width'])
                    self.assertTrue((directory / 'ckpt_best.pt').is_file())
                    result = train(root / 'data', directory, steps=2, batch_size=1, grad_accum_steps=1,
                                   device='cpu', resume=True, model_size=None)
                    self.assertEqual(result[0], 2)
                    resumed = load_checkpoint(str(directory / 'ckpt_latest.pt'), 'cpu')
                    self.assertEqual(resumed['step'], 1)
                    self.assertEqual(resumed['training_state']['schedule']['schedule_steps'], 2)
                    with self.assertRaisesRegex(ValueError, 'conflicts'):
                        train(root / 'data', directory, steps=3, device='cpu', resume=True, model_size='1m')


if __name__ == '__main__': unittest.main()
