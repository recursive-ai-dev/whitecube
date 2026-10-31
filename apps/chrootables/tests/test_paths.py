"""Store-boundary validation with no privileges or filesystem mutations."""
from pathlib import Path
import tempfile
import unittest

from chrootables.paths import store_path
from chrootables.backend import Store


class StorePathTests(unittest.TestCase):
    def test_root_aliases_and_relative_paths_are_rejected(self):
        for path in ("/", "//", "/tmp/..", "/tmp/.././", "relative", "", None, b"/tmp/store"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                store_path(path)

    def test_control_characters_are_rejected_at_every_position(self):
        for character in ("\0", "\n", "\r", "\t", "\x1b", "\x7f"):
            with self.subTest(character=repr(character)), self.assertRaises(ValueError):
                Store("/tmp/work" + character + "space")

    def test_missing_store_is_canonicalized_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unused/../workspace with spaces"
            resolved = store_path(path)
            self.assertEqual(resolved, Path(directory).resolve() / "workspace with spaces")
            self.assertEqual(Store(path).path, resolved)
            self.assertFalse(resolved.exists())
            self.assertFalse((Path(directory) / "unused").exists())

    def test_symlinked_store_and_alias_to_root_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "store"
            alias.symlink_to("/", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                store_path(alias)
            with self.assertRaises(ValueError):
                store_path(alias / "tmp/..")


if __name__ == "__main__":
    unittest.main()
