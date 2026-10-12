"""Execute every cell of an exported notebook against a real tiny run (training Python)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        cls.run_dir = cls.root / 'run'
        with contextlib.redirect_stdout(io.StringIO()):
            prepare_folder(corpus, cls.run_dir / 'data', val_fraction=.1)
            train(cls.run_dir / 'data', cls.run_dir, steps=3, batch_size=2, grad_accum_steps=1, device='cpu', model_size='1m', eval_interval=1)
        atomic_json(cls.run_dir / 'run.json', {'display_name': 'Robot stories', 'model_size': '1m', 'batch_size': 2, 'grad_accum_steps': 1})

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
        notebook = export_notebook.export(self.run_dir, 'best', self.root / 'bundle', corpus=True,
                                          preview={'prompt': 'The small robot', 'tokens': 24, 'temperature': 0})
        namespace, outputs = self.execute(notebook, [('CONTINUE_TRAINING = False', 'CONTINUE_TRAINING = True'),
                                                     ('EXTRA_STEPS = 200', 'EXTRA_STEPS = 2')])
        text = '\n'.join(outputs)
        self.assertIn('1,005,768 parameters', text)
        self.assertIn('bits/byte', text)
        self.assertIsInstance(namespace['generate']('The small robot'), str)
        self.assertEqual(namespace['generate']('The', max_new_tokens=8, temperature=0), namespace['generate']('The', max_new_tokens=8, temperature=0))
        self.assertRegex(text, r'Validation loss \d+\.\d{4} nats/byte')
        self.assertIn('Continued to step', text)
        continued = torch.load(self.root / 'bundle/continued/ckpt_latest.pt', map_location='cpu', weights_only=False)
        original = torch.load(self.run_dir / 'ckpt_best.pt', map_location='cpu', weights_only=False)
        self.assertEqual(continued['step'], original['step'] + 2)
        self.assertIn(f'Continued to step {continued["step"] + 1:,} ·', text)
        self.assertIn('Round trip matches', text)
        self.assertTrue((self.root / 'bundle/safetensors/model.safetensors').is_file())
        self.assertEqual(torch.load(self.run_dir / 'ckpt_best.pt', map_location='cpu', weights_only=False)['step'], original['step'])

    def test_reference_notebook_reads_the_run_in_place(self):
        notebook = export_notebook.export(self.run_dir, 'latest', self.root / 'reference', bundle=False, script=False)
        namespace, outputs = self.execute(notebook)
        self.assertEqual(Path(namespace['CHECKPOINT']), (self.run_dir / 'ckpt_latest.pt').resolve())
        self.assertIn('Set CONTINUE_TRAINING = True', '\n'.join(outputs))

    def test_moved_bundle_runs_in_a_fresh_interpreter_without_the_repository(self):
        notebook = export_notebook.export(self.run_dir, 'best', self.root / 'before-move', corpus=True,
                                          preview={'tokens': 8, 'temperature': 0})
        moved = self.root / 'after-move'
        notebook.parent.rename(moved)
        nested = moved / 'analysis'; nested.mkdir()
        result = subprocess.run([sys.executable, '-I', '-c', '''
import json, os, sys
from pathlib import Path
import torch
torch.set_num_threads(2)
os.environ['MPLBACKEND'] = 'Agg'
notebook = Path(sys.argv[1])
namespace = {'__name__': '__main__'}
for cell in json.loads(notebook.read_text())['cells']:
    if cell['cell_type'] == 'code':
        source = ''.join(cell['source']).replace("DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'", "DEVICE = 'cpu'")
        exec(compile(source, cell['id'], 'exec'), namespace)
import tensegrity_lm
assert Path(tensegrity_lm.__file__).parent == notebook.parent / 'whitecube_pipeline'
assert namespace['HERE'] == notebook.parent
print('Loaded moved bundle using only its own pipeline')
''', str(moved / notebook.name)], cwd=nested, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Loaded moved bundle using only its own pipeline', result.stdout)
        self.assertIn('Round trip matches', result.stdout)

    def test_weights_export_failure_restores_the_model_device(self):
        notebook = export_notebook.export(self.run_dir, 'best', self.root / 'export-failure',
                                          preview={'tokens': 8, 'temperature': 0})
        namespace, _ = self.execute(notebook)
        source = ''.join(json.loads(notebook.read_text())['cells'][-1]['source'])
        import tensegrity_nb_lib
        with patch.object(tensegrity_nb_lib, 'export_to_safetensors', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                exec(source, namespace)
        self.assertEqual(next(namespace['model'].parameters()).device.type, namespace['DEVICE'])


if __name__ == '__main__':
    unittest.main()
