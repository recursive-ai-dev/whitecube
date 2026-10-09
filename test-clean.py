"""tools/clean.sh must never delete rollback material. Run: python3 -I test-clean.py"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


class CleanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = Path(self.tmp.name) / "whitecube"
        (self.tree / "tools").mkdir(parents=True)
        shutil.copy(HERE / "tools/clean.sh", self.tree / "tools/clean.sh")
        build = self.tree / "build"
        (build / "privacy-install-before-abc").mkdir(parents=True)
        (build / "privacy-install-before-abc/manifest.json").write_text("{}")
        for name in ("whitecube", "cube-status", "bar.png", "privacy-before-1.tar.gz", "privacy-upgrade.patch", "notes.txt"):
            (build / name).write_text("x")
        (build / "toolchain").mkdir()
        (self.tree / "src.c").write_text("source")
        (self.tree / "__pycache__").mkdir()
        (self.tree / "build-asan").mkdir()

    def run_clean(self, *args):
        return subprocess.run(["bash", str(self.tree / "tools/clean.sh"), *args], capture_output=True, text=True)

    def test_dry_run_deletes_nothing(self):
        before = sorted(p for p in self.tree.rglob("*"))
        out = self.run_clean().stdout
        self.assertIn("would remove build/whitecube", out)
        self.assertEqual(sorted(self.tree.rglob("*")), before)

    def test_apply_removes_only_regenerable_files_and_keeps_every_rollback_archive(self):
        self.run_clean("--apply")
        build = self.tree / "build"
        for gone in ("whitecube", "cube-status", "bar.png", "toolchain"):
            self.assertFalse((build / gone).exists(), gone)
        for kept in ("privacy-install-before-abc/manifest.json", "privacy-before-1.tar.gz", "privacy-upgrade.patch", "notes.txt"):
            self.assertTrue((build / kept).exists(), kept)
        self.assertTrue((self.tree / "src.c").exists())
        self.assertFalse((self.tree / "__pycache__").exists())
        self.assertFalse((self.tree / "build-asan").exists())

    def test_it_succeeds_on_an_already_clean_tree(self):
        self.run_clean("--apply")
        self.assertEqual(self.run_clean("--apply").returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
