#!/usr/bin/python3 -I
"""Verify private Parquet runtime integrity and extraction rules without downloads."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('parquet_runtime_packaging', ROOT / 'apps/cube-train/packaging/parquet-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeIntegrity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.pin = runtime.configuration()
        metadata = 'pyarrow-' + self.pin['pyarrow_version'] + '.dist-info'
        self.license = self.folder / metadata / 'licenses/LICENSE.txt'
        entries = {'pyarrow/__init__.py': '# private decoder fixture\n',
                   metadata + '/METADATA': 'Name: pyarrow\nVersion: ' + self.pin['pyarrow_version'] + '\n',
                   metadata + '/licenses/LICENSE.txt': 'license fixture\n',
                   metadata + '/licenses/NOTICE.txt': 'notice fixture\n'}
        for name, data in entries.items():
            path = self.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        self.hashes = {name: hashlib.sha256(data.encode()).hexdigest() for name, data in entries.items()}
        (self.folder / 'runtime.json').write_text(json.dumps(self.pin))
        self.write_hashes()

    def write_hashes(self):
        (self.folder / 'files.sha256.json').write_text(json.dumps(self.hashes))

    def test_valid_runtime_and_corruption_detection(self):
        runtime.check(self.folder)
        (self.folder / 'pyarrow/__init__.py').write_text('corrupted\n')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            runtime.check(self.folder)

    def test_private_decoder_abi_cannot_be_changed_by_runtime_manifest(self):
        (self.folder / 'runtime.json').write_text(json.dumps({**self.pin, 'python_version': '3.14'}))
        with self.assertRaisesRegex(ValueError, 'pinned inputs'):
            runtime.check(self.folder)

    def test_unexpected_payload_and_symlinks_are_rejected(self):
        (self.folder / 'unexpected.py').write_text('unexpected')
        with self.assertRaisesRegex(ValueError, 'unexpected files'):
            runtime.check(self.folder)
        (self.folder / 'unexpected.py').unlink()
        path = self.folder / 'pyarrow/__init__.py'
        path.unlink()
        path.symlink_to(self.license)
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            runtime.check(self.folder)

    def test_license_is_required_even_with_updated_checksum_manifest(self):
        del self.hashes[str(self.license.relative_to(self.folder))]
        self.license.unlink()
        self.write_hashes()
        with self.assertRaisesRegex(ValueError, 'license or notice'):
            runtime.check(self.folder)

    def test_checksum_manifest_cannot_escape_runtime(self):
        self.hashes['../outside'] = 'a' * 64
        self.write_hashes()
        with self.assertRaises(ValueError):
            runtime.check(self.folder)

    def test_wheel_traversal_and_build_payload_are_not_accepted(self):
        for name in ('../escape', '/tmp/escape', 'pyarrow/../../escape', 'pyarrow\\escape'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                runtime.payload_member(name)
        self.assertFalse(runtime.payload_member('pyarrow/tests/corpus.parquet'))
        self.assertFalse(runtime.payload_member('pyarrow/include/arrow/array.h'))
        self.assertTrue(runtime.payload_member('pyarrow/libparquet.so.2600'))

    def test_probe_excludes_site_numpy_and_uses_manifest_interpreter(self):
        with patch.object(runtime.subprocess, 'run') as run:
            runtime.check(self.folder, probe=True)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [self.pin['python'], '-I', '-B', '-S'])
        self.assertIn("find_spec('numpy') is None", command[5])


if __name__ == '__main__':
    unittest.main(verbosity=2)
