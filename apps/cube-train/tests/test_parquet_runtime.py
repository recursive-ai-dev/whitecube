"""Real Parquet fallback and decoder process regressions; no Torch required."""
import array
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
PIPELINE = APP / 'pipeline'
sys.path.insert(0, str(APP))
sys.path.insert(0, str(PIPELINE))
import engine
import parquet_io


class DecoderLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        worker = self.root / 'decoder.py'
        worker.write_text('''import json,sys,time
mode,path,*args=sys.argv[1:]
if mode=='schema':
 print(json.dumps({'fields':[{'name':'text','type':{'kind':'string'}}],'rows':10}))
elif path=='malformed':
 print('not JSON',flush=True);time.sleep(30)
elif path=='stalled':
 time.sleep(30)
else:
 print(json.dumps({'names':['text'],'columns':[['first','second']]}),flush=True)
 time.sleep(30)
''')
        decoder = patch.object(parquet_io, 'decoder', return_value=([sys.executable, '-I', str(worker)], 'fixture', '0'))
        decoder.start(); self.addCleanup(decoder.stop)

    def test_early_close_reaps_decoder_after_a_bounded_batch(self):
        source = parquet_io._RemoteParquetFile('early-close')
        batches = source.iter_batches(columns=['text'], batch_size=2)
        self.assertEqual(next(batches).to_pydict(), {'text': ['first', 'second']})
        process = next(iter(source.children))
        batches.close(); source.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(source.children)

    def test_malformed_decoder_output_is_rejected_and_child_reaped(self):
        with parquet_io._RemoteParquetFile('malformed') as source:
            with self.assertRaises(json.JSONDecodeError):
                next(source.iter_batches(columns=['text']))
            self.assertFalse(source.children)

    def test_stop_is_observed_while_waiting_for_a_decoder_batch(self):
        with parquet_io._RemoteParquetFile('stalled') as source:
            started = time.monotonic()
            with self.assertRaises(InterruptedError):
                next(source.iter_batches(columns=['text'], stop_requested=lambda: time.monotonic() - started > .15))
            self.assertLess(time.monotonic() - started, 3)
            self.assertFalse(source.children)

    def test_private_worker_rejects_wrong_abi_before_importing_extension(self):
        manifest = {'format': parquet_io.RUNTIME_FORMAT, 'version': 1, 'python': sys.executable,
                    'python_version': '0.0', 'pyarrow_version': '0'}
        (self.root / 'runtime.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'requires Python 0.0'):
            parquet_io._checked([*parquet_io.runtime_command(self.root), 'check'])


class PackagedParquetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        status = parquet_io.support()
        if not status['available']:
            raise unittest.SkipTest(status['error'])
        cls.temporary = tempfile.TemporaryDirectory(prefix='trainer-parquet-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.docs = [f'Row {index}: cubes café 雪🙂. ' * 20 for index in range(20)]
        if parquet_io.NATIVE_ERROR is None:
            python, private = sys.executable, ''
        else:
            command, provider, _ = parquet_io.decoder()
            python, private = command[0], str(parquet_io.RUNTIME) if provider == 'packaged decoder' else ''
        make = '''import json,sys
from pathlib import Path
if sys.argv[1]:sys.path.insert(0,sys.argv[1])
import pyarrow as pa,pyarrow.parquet as pq
root=Path(sys.argv[2]);docs=json.loads(sys.argv[3])
pq.write_table(pa.table({'text':range(len(docs)),'content':docs}),root/'mixed.parquet')
pq.write_table(pa.table({'document':pa.array([None if i==3 else {'html':text} for i,text in enumerate(docs)],type=pa.struct([('html',pa.string())]))}),root/'nested.parquet')
pq.write_table(pa.table({'text':pa.array(docs).dictionary_encode()}),root/'dictionary.parquet')
pq.write_table(pa.table({'document.html':docs}),root/'dotted.parquet')
'''
        made = subprocess.run([python, '-I', '-c', make, private, str(cls.root), json.dumps(cls.docs)],
                              text=True, capture_output=True, timeout=30)
        if made.returncode:
            raise AssertionError(made.stdout + made.stderr)
        # CPython honors pyvenv.cfg even in isolated mode. This environment has
        # no site packages, pip, NumPy, PyArrow or Torch.
        environment = cls.root / 'isolated'
        (environment / 'bin').mkdir(parents=True)
        cls.python = environment / 'bin/python'
        cls.python.symlink_to(sys.executable)
        (environment / 'pyvenv.cfg').write_text(f'home = {Path(sys.executable).parent}\ninclude-system-site-packages = false\n')

    def selected(self, code, *args):
        result = subprocess.run([str(self.python), '-I', '-c', f'import sys;sys.path.insert(0,{str(PIPELINE)!r});' + code, *map(str, args)],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_isolated_selected_python_inspects_parquet_without_training_packages(self):
        status = self.selected("import importlib.util,json;from parquet_io import support;print(json.dumps({'arrow':importlib.util.find_spec('pyarrow') is not None,'torch':importlib.util.find_spec('torch') is not None,'numpy':importlib.util.find_spec('numpy') is not None,'support':support()}))")
        self.assertFalse(status['arrow']); self.assertFalse(status['torch']); self.assertFalse(status['numpy'])
        self.assertTrue(status['support']['available'])
        self.assertIn(status['support']['provider'], ('system decoder', 'packaged decoder'))
        probe = engine.probe_fields([self.root / 'mixed.parquet'], self.python)
        self.assertEqual(probe[str(self.root / 'mixed.parquet')]['auto'], 'content')
        self.assertIsNone(probe[str(self.root / 'mixed.parquet')]['error'])

    def test_fallback_preserves_unicode_dictionary_dotted_columns_and_parent_nulls(self):
        code = """
import json
from parquet_io import pq,pick_column
with pq.ParquetFile(sys.argv[1]) as source:
 column=pick_column(source.schema_arrow)
 values=[value.as_py() for batch in source.iter_batches(columns=[column],batch_size=3) for value in batch.column(0)]
 print(json.dumps({'column':column,'rows':source.metadata.num_rows,'values':values}))
"""
        for name in ('mixed', 'dictionary', 'dotted', 'nested'):
            with self.subTest(name=name):
                result = self.selected(code, self.root / (name + '.parquet'))
                self.assertEqual(result['rows'], 20)
                expected = [None if index == 3 else {'html': text} for index, text in enumerate(self.docs)] if name == 'nested' else self.docs
                self.assertEqual(result['values'], expected)

    def test_prepare_only_uses_system_numpy_without_torch_or_selected_pyarrow(self):
        output = self.root / 'prepared-job'
        trainer = engine.Trainer(home=self.root, state=self.root / 'state', config=self.root / 'config')
        values = {**trainer.preferences(), 'python': str(self.python), 'dataset': str(self.root / 'mixed.parquet'),
                  'name': 'Parquet without training dependencies', 'device': 'cpu', 'max_tokens': 20000}
        command = trainer.command(values, output, prepare_only=True)
        self.assertEqual(command[0], '/usr/bin/python3')
        environment = dict(os.environ, HOME=str(self.root / 'preparation-home'))
        result = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((output / 'status.json').read_text())['state'], 'prepared')
        self.assertFalse(list(output.glob('ckpt_*.pt')))
        recovered = []
        for split in ('train', 'val'):
            tokens = array.array('H'); tokens.frombytes((output / 'data' / (split + '.bin')).read_bytes())
            current = bytearray()
            for token in tokens:
                if token == 257:
                    recovered.append(current.decode('utf-8')); current.clear()
                else:
                    current.append(token)
            self.assertFalse(current)
        self.assertEqual(sorted(recovered), sorted(self.docs))


if __name__ == '__main__':
    unittest.main()
