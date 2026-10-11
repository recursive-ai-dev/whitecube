"""0.9.0: notebook export, checkpoints, cleanup, field probe and runtime hints (system Python, no torch)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import engine
import export_notebook

try:
    import nbformat
except ImportError:
    nbformat = None


def make_run(root, name='run-a', *, best=True, latest=True, snapshots=(), prepared=True, failed=False):
    run = root / name
    run.mkdir(parents=True)
    if failed:
        engine.atomic_json(run / 'status.json', {'state': 'failed', 'detail': 'No recognized text column'})
        return run
    engine.atomic_json(run / 'run.json', {'display_name': 'Story Model', 'model_size': '1m', 'parameters': 1_005_768,
                                          'steps': 10, 'batch_size': 2, 'grad_accum_steps': 3, 'learning_rate': 3e-4,
                                          'schedule': 'cosine', 'seed': 7, 'notes': 'Bedtime corpus'})
    engine.atomic_json(run / 'result.json', {'completed_steps': 8, 'best_val_loss': 1.3862943611198906})
    (run / 'train_log.jsonl').write_text('\n'.join(json.dumps(item) for item in (
        {'step': 1, 'total_loss': 4.0}, {'step': 3, 'val_loss': 2.0}, {'step': 6, 'val_loss': 1.4}, {'step': 7, 'total_loss': 1.2})))
    if prepared:
        (run / 'data').mkdir()
        for split in ('train', 'val'):
            (run / 'data' / f'{split}.bin').write_bytes(b'\0\0' * 600)
        engine.atomic_json(run / 'data/corpus.json', {'train_tokens': 600, 'val_tokens': 600, 'total_tokens': 1200})
    for kind, present in (('best', best), ('latest', latest)):
        if present:
            (run / f'ckpt_{kind}.pt').write_bytes(kind.encode() * 10)
    for step in snapshots:
        (run / f'ckpt_step{step:08d}.pt').write_bytes(b'snap')
    return run


class NotebookExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whitecube-notebook-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = make_run(self.root / 'runs', snapshots=(4,))

    def test_bundle_is_self_contained_valid_and_names_the_checkpoint(self):
        path = export_notebook.export(self.run, 'step00000004', self.root / 'out', corpus=True)
        out = self.root / 'out'
        self.assertEqual(path, out / 'story-model.ipynb')
        self.assertEqual((out / 'checkpoint/ckpt_step00000004.pt').read_bytes(), b'snap')
        for module in ('tensegrity_lm', 'tensegrity_serve', 'tensegrity_nb_lib', 'train_local', 'files', 'prepare_folder'):
            self.assertTrue((out / 'whitecube_pipeline' / f'{module}.py').is_file(), module)
        self.assertTrue((out / 'data/train.bin').is_file() and (out / 'run/train_log.jsonl').is_file())
        document = json.loads(path.read_text())
        source = '\n'.join(''.join(cell['source']) for cell in document['cells'])
        self.assertIn("CHECKPOINT = HERE / 'checkpoint' / 'ckpt_step00000004.pt'", source)
        self.assertIn("DATA_DIR = HERE / 'data'", source)
        self.assertIn('2.000 bits/byte', source)  # 1.386 nats = 2 bits per byte
        self.assertIn('Bedtime corpus', source)
        self.assertIn('batch_size=2', source)
        self.assertIn('grad_accum_steps=3', source)
        self.assertEqual(len({cell['id'] for cell in document['cells']}), len(document['cells']))
        for cell in document['cells']:
            if cell['cell_type'] == 'code':
                compile(''.join(cell['source']), cell['id'], 'exec')
        script = path.with_suffix('.py').read_text()
        self.assertIn('# %% [markdown]', script)
        compile(script, 'script', 'exec')
        self.assertEqual(oct(out.stat().st_mode & 0o777), '0o755')
        self.assertFalse(list(self.root.glob('.out-*')))
        if nbformat:
            nbformat.validate(nbformat.reads(path.read_text(), as_version=4))

    def test_reference_mode_writes_only_the_notebook_with_absolute_paths(self):
        path = export_notebook.export(self.run, 'best', self.root / 'ref', bundle=False, script=False)
        self.assertEqual(sorted(item.name for item in (self.root / 'ref').iterdir()), ['story-model.ipynb'])
        source = path.read_text()
        self.assertIn(json.dumps(f"RUN_DIR = Path({str(self.run.resolve())!r})\n")[1:-1], source)
        self.assertIn(json.dumps(str(export_notebook.PIPELINE))[1:-1], source)

    def test_bundle_without_corpus_points_at_the_original_prepared_data(self):
        path = export_notebook.export(self.run, 'latest', self.root / 'lean')
        self.assertFalse((self.root / 'lean/data').exists())
        self.assertIn(json.dumps(str(self.run.resolve() / 'data'))[1:-1], path.read_text())
        self.assertIn('prepared corpus at', (self.root / 'lean/README.md').read_text())

    def test_rejects_bad_kinds_missing_checkpoints_and_used_destinations(self):
        for kind in ('../ckpt_best', 'step12', 'nope', None):
            with self.assertRaises(ValueError):
                export_notebook.export(self.run, kind, self.root / 'x')
        (self.run / 'ckpt_latest.pt').unlink()
        with self.assertRaises(ValueError):
            export_notebook.export(self.run, 'latest', self.root / 'x')
        used = self.root / 'used'; used.mkdir(); (used / 'file').write_text('keep')
        with self.assertRaises(ValueError):
            export_notebook.export(self.run, 'best', used)
        self.assertEqual((used / 'file').read_text(), 'keep')
        bare = make_run(self.root / 'runs', 'bare', prepared=False)
        with self.assertRaises(ValueError):
            export_notebook.export(bare, 'best', self.root / 'y', corpus=True)

    def test_cancellation_and_failures_leave_no_partial_folder(self):
        with self.assertRaises(InterruptedError):
            export_notebook.export(self.run, 'best', self.root / 'cancelled', cancelled=lambda: True)
        with patch.object(export_notebook.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                export_notebook.export(self.run, 'best', self.root / 'failed')
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ['runs'])

    def test_bundle_size_counts_optional_corpus(self):
        lean = export_notebook.bundle_size(self.run, 'best')
        full = export_notebook.bundle_size(self.run, 'best', corpus=True)
        self.assertEqual(full - lean, 2400 + (self.run / 'data/corpus.json').stat().st_size)
        self.assertLess(export_notebook.bundle_size(self.run, 'best', bundle=False), lean)

    def test_command_line_and_whitecube_train_wrapper(self):
        result = subprocess.run([sys.executable, '-I', str(HERE / 'pipeline/export_notebook.py'), str(self.run),
                                 '--output', str(self.root / 'cli'), '--no-script'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).name, 'story-model.ipynb')
        self.assertFalse((self.root / 'cli/story-model.py').exists())
        again = subprocess.run([sys.executable, '-I', str(HERE / 'pipeline/export_notebook.py'), str(self.run),
                                '--output', str(self.root / 'cli')], capture_output=True, text=True)
        self.assertEqual(again.returncode, 1)
        self.assertIn('new or empty folder', again.stderr)


class EngineNotebookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whitecube-engine-nb-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'WHITECUBE_TRAIN_OUT': str(self.root / 'runs'), 'XDG_DATA_HOME': str(self.root / 'share'),
                                           'JUPYTER_DATA_DIR': str(self.root / 'jupyter')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.trainer = engine.Trainer(home=self.root / 'home', state=self.root / 'state', config=self.root / 'config')
        self.run = make_run(self.root / 'runs', snapshots=(2, 5))
        make_run(self.root / 'runs', 'failed-1', failed=True)
        make_run(self.root / 'runs', 'failed-2', failed=True)

    def test_checkpoints_list_best_latest_and_snapshots_with_steps(self):
        items = self.trainer.checkpoints('run-a')
        self.assertEqual([item['kind'] for item in items], ['best', 'latest', 'step00000005', 'step00000002'])
        best, latest, five, two = items
        self.assertEqual((best['step'], best['val_loss']), (6, 1.3862943611198906))
        self.assertEqual(latest['step'], 8)
        self.assertEqual((five['step'], five['val_loss'], five['label']), (5, 2.0, 'Step 5'))
        self.assertEqual((two['step'], two['val_loss']), (2, None))
        self.assertEqual(self.trainer.checkpoint_path('run-a', 'step00000002').name, 'ckpt_step00000002.pt')
        with self.assertRaises(ValueError):
            self.trainer.checkpoint_path('run-a', '../../etc/passwd')

    def test_snapshots_can_be_previewed_exported_and_trashed(self):
        command = self.trainer.preview_command('run-a', 'step00000005', 'Hi', sys.executable)
        self.assertIn(str(self.run / 'ckpt_step00000005.pt'), command)
        export = self.trainer.export_command('run-a', 'step00000002', self.root / 'w', sys.executable)
        self.assertIn(str(self.run / 'ckpt_step00000002.pt'), export)
        self.trainer.trash('run-a', 'step00000002')
        self.assertFalse((self.run / 'ckpt_step00000002.pt').exists())
        self.assertEqual(len(list((self.root / 'share/Trash/files').iterdir())), 1)
        with self.assertRaises(ValueError):
            self.trainer.trash('run-a', 'ckpt_best.pt')

    def test_cleanup_moves_only_failed_runs_without_data_or_weights(self):
        self.assertEqual(sorted(run['id'] for run in self.trainer.cleanup_candidates()), ['failed-1', 'failed-2'])
        engine.atomic_json(self.root / 'runs/failed-2/run.json', {'pinned': True})
        self.assertEqual([run['id'] for run in self.trainer.cleanup_candidates()], ['failed-1'])
        moved, errors = self.trainer.clean_up_runs(['failed-1', 'missing'])
        self.assertEqual((moved, len(errors)), (['failed-1'], 1))
        self.assertTrue(self.run.is_dir() and not (self.root / 'runs/failed-1').exists())

    def test_notebook_export_uses_registered_kernel_and_refuses_latest_while_training(self):
        kernel = self.root / 'jupyter/kernels' / engine.KERNEL_NAME
        kernel.mkdir(parents=True)
        engine.atomic_json(kernel / 'kernel.json', {'argv': ['python', '-m', 'ipykernel_launcher'], 'display_name': 'WC'})
        path = self.trainer.notebook_export('run-a', 'best', self.root / 'nb', bundle=False)
        self.assertEqual(json.loads(path.read_text())['metadata']['kernelspec']['name'], engine.KERNEL_NAME)
        running = {**self.trainer.find_run('run-a'), 'job': {'running': True}}
        with patch.object(self.trainer, 'find_run', return_value=running):
            with self.assertRaisesRegex(ValueError, 'Latest changes'):
                self.trainer.notebook_export('run-a', 'latest', self.root / 'nb2')
        other = self.root / 'other.ipynb'
        other.write_text(json.dumps({'nbformat': 4, 'metadata': {}, 'cells': []}))
        engine.set_notebook_kernel(other, 'Display')
        self.assertEqual(json.loads(other.read_text())['metadata']['kernelspec']['display_name'], 'Display')
        self.assertEqual(self.trainer.kernel_command('/usr/bin/python3')[-4:], ['--name', engine.KERNEL_NAME, '--display-name', 'Whitecube Trainer (PyTorch)'])
        with self.assertRaises(ValueError):
            self.trainer.kernel_command('relative/python')

    def test_jupyter_command_prefers_the_training_environment(self):
        bin_dir = self.root / 'venv/bin'; bin_dir.mkdir(parents=True)
        lab = bin_dir / 'jupyter-lab'; lab.write_text('#!/bin/sh\n'); lab.chmod(0o755)
        with patch.object(engine.os, 'access', return_value=True):  # /tmp may be mounted noexec
            self.assertEqual(engine.jupyter_command(bin_dir / 'python'), [str(lab)])
        with patch.object(engine.shutil, 'which', return_value=None):
            self.assertIsNone(engine.jupyter_command(self.root / 'none/python'))

    def test_recent_corpora_are_validated_and_remembered(self):
        self.trainer.save_preferences({'recent_datasets': ['/a', '/b']})
        self.assertEqual(self.trainer.preferences()['recent_datasets'], ['/a', '/b'])
        for bad in (['x'] * 9, [3], ['bad\0path'], 'not a list'):
            with self.assertRaises(ValueError):
                self.trainer.save_preferences({'recent_datasets': bad})
        engine.atomic_json(self.trainer.config, {'recent_datasets': [1, 2]})
        self.assertEqual(self.trainer.preferences()['recent_datasets'], [])
        self.assertNotIn('recent_datasets', engine.Trainer.defaults())

    def test_bits_per_byte_perplexity_and_repair_hint(self):
        self.assertAlmostEqual(engine.bits_per_byte(1.3862943611198906), 2.0)
        self.assertAlmostEqual(engine.perplexity(0), 1.0)
        self.assertIsNone(engine.bits_per_byte(float('nan')))
        self.assertIsNotNone(engine.perplexity(10_000))
        self.assertEqual(engine.repair_hint("ModuleNotFoundError: No module named 'sympy.core.cache'", '/env/bin/python'),
                         '/env/bin/python -m pip install --force-reinstall sympy')
        self.assertEqual(engine.repair_hint('RuntimeError: CUDA error', '/p'), '')

    def test_runtime_probe_reports_a_failing_optimizer_step(self):
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            'torch': '2', 'cuda': False, 'gpu': None, 'parquet': True, 'memory': None,
            'training_error': "ModuleNotFoundError: No module named 'sympy.core.cache'"}), stderr='')
        with patch.object(engine.subprocess, 'run', return_value=fake) as run:
            info = engine.probe_runtime(sys.executable)
        self.assertIn('o.step()', run.call_args.args[0][3])
        self.assertIn('sympy', info['training_error'])
        fake.stdout = json.dumps({'torch': '2', 'cuda': False, 'gpu': None, 'parquet': True, 'training_error': 5})
        with patch.object(engine.subprocess, 'run', return_value=fake):
            self.assertIsNone(engine.probe_runtime(sys.executable)['training_error'])

    def test_field_probe_reads_jsonl_fields_and_flags_files_that_would_fail(self):
        corpus = self.root / 'corpus'; corpus.mkdir()
        (corpus / 'a.jsonl').write_text(json.dumps({'story': 'x', 'meta': {'html': 'y'}, 'n': 1}) + '\n')
        (corpus / 'b.jsonl').write_text('not json\n' + json.dumps({'question': 'q', 'answer': 'a'}) + '\n')
        (corpus / 'c.txt').write_text('plain')
        probe = engine.probe_fields(sorted(str(path) for path in corpus.iterdir()), sys.executable)
        self.assertEqual(sorted(Path(name).name for name in probe), ['a.jsonl', 'b.jsonl'])
        self.assertEqual(probe[str(corpus / 'a.jsonl')], {'fields': ['story', 'meta.html'], 'auto': 'story', 'error': None})
        message, fields, failing = engine.field_summary(probe)
        self.assertEqual((failing, set(fields)), (['b.jsonl'], {'story', 'meta.html', 'question', 'answer'}))
        self.assertIn('Automatic text field: story (1 file)', message)
        message, _, failing = engine.field_summary(probe, 'question')
        self.assertEqual(failing, ['a.jsonl'])
        self.assertIn('found in 1 of 2 files', message)
        self.assertEqual(engine.field_summary({}), ('', [], []))
        self.assertEqual(engine.probe_fields([str(corpus / 'c.txt')], sys.executable), {})


if __name__ == '__main__':
    unittest.main()
