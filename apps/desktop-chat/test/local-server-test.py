"""Checkpoint discovery and hot-reload keys need no torch installation."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('local_server', Path(__file__).resolve().parents[1] / 'local-model-server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class DiscoveryTests(unittest.TestCase):
    def test_exact_files_symlinks_and_replaced_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'run'
            run.mkdir()
            best = run / 'ckpt_best.pt'
            best.write_bytes(b'first')
            (run / 'ckpt_latest.pt').write_bytes(b'latest')
            (root / 'linked-run').symlink_to(run, target_is_directory=True)
            (run / 'ckpt_link.pt').symlink_to(best)
            first = server.checkpoint_entries(root)
            self.assertEqual({item['id'] for item in first}, {'run/ckpt_best.pt', 'run/ckpt_latest.pt'})
            original = next(item for item in first if item['id'].endswith('best.pt'))
            replacement = run / 'replacement'
            replacement.write_bytes(b'replaced weights')
            replacement.replace(best)
            changed = next(item for item in server.checkpoint_entries(root) if item['id'].endswith('best.pt'))
            self.assertNotEqual(original['signature'], changed['signature'])

    def test_missing_folder(self):
        self.assertEqual(server.checkpoint_entries(Path('/nonexistent/checkpoint-test')), [])


if __name__ == '__main__':
    unittest.main()
