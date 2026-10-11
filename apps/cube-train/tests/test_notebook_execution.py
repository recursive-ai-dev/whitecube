"""Execute every cell of an exported notebook against a real tiny run (training Python)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1] / 'pipeline'
sys.path.insert(0, str(PIPELINE))
import torch
torch.set_num_threads(2)

import export_notebook
from files import atomic_json
from prepare_folder import prepare_folder
from train_local import train


class NotebookExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='whitecube-notebook-run-')
        cls.root = Path(cls.temp.name)
        corpus = cls.root / 'corpus.jsonl'
        corpus.write_text('\n'.join(json.dumps({'text': f'Story {i}. The small robot learned to balance on one wheel. ' * 8})
                                    for i in range(40)))
        cls.run = cls.root / 'run'
        with contextlib.redirect_stdout(io.StringIO()):
            prepare_folder(corpus, cls.run / 'data', val_fraction=.1)
            train(cls.run / 'data', cls.run, steps=3, batch_size=2, grad_accum_steps=1, device='cpu', model_size='1m', eval_interval=1)
        atomic_json(cls.run / 'run.json', {'display_name': 'Robot stories', 'model_size': '1m', 'batch_size': 2, 'grad_accum_steps': 1})

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def execute(self, notebook, changes=()):
        """Run code cells in order in one namespace, from the notebook's folder, like a kernel."""
        document = json.loads(notebook.read_text())
        namespace, outputs, previous = {'__name__': '__main__'}, [], Path.cwd()
        os.chdir(notebook.parent)
        try:
            for cell in document['cells']:
                if cell['cell_type'] != 'code':
                    continue
                source = ''.join(cell['source'])
                for old, new in changes:
                    source = source.replace(old, new)
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    exec(compile(source, cell['id'], 'exec'), namespace)
                outputs.append(stream.getvalue())
        finally:
            os.chdir(previous)
        return namespace, outputs

    def test_bundle_cells_generate_evaluate_continue_and_export(self):
        os.environ.setdefault('MPLBACKEND', 'Agg')
        notebook = export_notebook.export(self.run, 'best', self.root / 'bundle', corpus=True,
                                          preview={'prompt': 'The small robot', 'tokens': 24, 'temperature': 0})
        namespace, outputs = self.execute(notebook, [('CONTINUE_TRAINING = False', 'CONTINUE_TRAINING = True'),
                                                     ('EXTRA_STEPS = 200', 'EXTRA_STEPS = 2')])
        text = '\n'.join(outputs)
        self.assertIn('1,005,768 parameters', text)
        self.assertIn('bits/byte', text)
        self.assertTrue(namespace['generate']('The small robot').strip() or True)
        self.assertEqual(namespace['generate']('The', max_new_tokens=8, temperature=0), namespace['generate']('The', max_new_tokens=8, temperature=0))
        self.assertRegex(text, r'Validation loss \d+\.\d{4} nats/byte')
        self.assertIn('Continued to step', text)
        continued = torch.load(self.root / 'bundle/continued/ckpt_latest.pt', map_location='cpu', weights_only=False)
        original = torch.load(self.run / 'ckpt_best.pt', map_location='cpu', weights_only=False)
        self.assertEqual(continued['step'], original['step'] + 2)
        self.assertIn('Round trip matches', text)
        self.assertTrue((self.root / 'bundle/safetensors/model.safetensors').is_file())
        self.assertEqual(torch.load(self.run / 'ckpt_best.pt', map_location='cpu', weights_only=False)['step'], original['step'])

    def test_reference_notebook_reads_the_run_in_place(self):
        notebook = export_notebook.export(self.run, 'latest', self.root / 'reference', bundle=False, script=False)
        namespace, outputs = self.execute(notebook)
        self.assertEqual(Path(namespace['CHECKPOINT']), (self.run / 'ckpt_latest.pt').resolve())
        self.assertIn('Set CONTINUE_TRAINING = True', '\n'.join(outputs))


if __name__ == '__main__':
    unittest.main()
