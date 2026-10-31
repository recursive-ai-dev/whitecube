#!/usr/bin/python3 -I
"""Studio package ownership, actual artifact, reproducibility and publication checks."""
from pathlib import Path
import importlib.util
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "apps/chrootables/packaging"
sys.path.insert(0, str(PACKAGING))
import package_support as support
spec = importlib.util.spec_from_file_location("cube_studio_build", PACKAGING / "build-packages.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class StudioPayloadTests(unittest.TestCase):
    def test_allowlist_identity_modes_and_private_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = support.stage_payload(Path(temporary) / "payload", support.DEFAULT_EPOCH)
            app = stage / support.PRIVATE
            for module in support.MODULES:
                self.assertTrue((app / "chrootables" / module).is_file(), module)
            for name in ("backend.py", "broker.py", "tests", "requirements.txt", "packaging", "__pycache__"):
                self.assertFalse((app / name).exists(), name)
            for command in (support.PACKAGE, support.MANAGER_ALIAS):
                path = stage / "usr/bin" / command
                self.assertTrue(path.is_symlink())
                self.assertTrue(path.resolve().is_relative_to(app))
            for command in ("cube-chrootables", "whitecube-chrootable-manager", "chrootables", "chrootables-desktop", "chrootable-manager"):
                self.assertFalse((stage / "usr/bin" / command).exists(), command)
            desktop = stage / f"usr/share/applications/{support.APPLICATION_ID}.desktop"
            self.assertIn("Exec=cube-chrootables-studio\n", desktop.read_text())
            self.assertIn("StartupWMClass=cube-chrootables-studio", desktop.read_text())
            for path in stage.rglob("*"):
                if path.is_symlink():
                    continue
                relative = path.relative_to(stage).as_posix()
                expected = 0o755 if path.is_dir() or relative in support.EXECUTABLES else 0o644
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, relative)
                self.assertEqual(path.stat().st_mtime, support.DEFAULT_EPOCH, relative)
            provenance = json.loads((stage / f"usr/share/doc/{support.PACKAGE}/build-provenance.json").read_text())
            self.assertEqual(provenance["application_version"], support.upstream_version())
            self.assertEqual(provenance["engine_version"], support.engine_version())
            self.assertNotEqual(provenance["application_version"], provenance["engine_version"])

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop validator is unavailable")
    def test_desktop_file_is_valid(self):
        subprocess.run(["desktop-file-validate", str(PACKAGING / (support.APPLICATION_ID + ".desktop"))], check=True)

    def test_complete_publication_preflights_all_collisions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, staged = root / "out", root / "stage"
            output.mkdir()
            staged.mkdir()
            artifact = staged / "application.deb"
            artifact.write_bytes(b"new package")
            (output / "release-manifest.json").write_bytes(b"existing manifest")
            with self.assertRaisesRegex(ValueError, "--force"):
                builder.publish([("deb", artifact)], output, "1.3.0", "1", support.DEFAULT_EPOCH)
            self.assertFalse((output / artifact.name).exists())
            self.assertFalse((output / "SHA256SUMS").exists())
            self.assertEqual((output / "release-manifest.json").read_bytes(), b"existing manifest")
            destination = output / artifact.name
            outside = root / "untouched"
            outside.write_bytes(b"other file")
            destination.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "nonregular"):
                builder.publish([("deb", artifact)], output, "1.3.0", "1", support.DEFAULT_EPOCH, force=True)
            self.assertEqual(outside.read_bytes(), b"other file")

    def test_publication_syncs_files_before_renaming_and_directory_afterwards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            output.mkdir()
            artifact = root / "application.deb"
            artifact.write_bytes(b"package")
            events = []
            real_sync, real_replace = os.fsync, os.replace
            def sync(fd):
                events.append("sync")
                real_sync(fd)
            def replace(source, target):
                events.append("rename")
                real_replace(source, target)
            with patch.object(os, "fsync", side_effect=sync), patch.object(os, "replace", side_effect=replace):
                builder.publish([("deb", artifact)], output, "1.3.0", "1", support.DEFAULT_EPOCH)
            self.assertEqual(events[:4], ["sync"] * 4)
            self.assertEqual(events[-1], "sync")
            self.assertEqual(stat.S_IMODE((output / "SHA256SUMS").stat().st_mode), 0o644)
            manifest = json.loads((output / "release-manifest.json").read_text())
            self.assertEqual(manifest["artifacts"][0]["sha256"], support.file_digest(output / artifact.name))


class StudioArtifactTests(unittest.TestCase):
    def test_real_formats_reproduce_across_paths_and_umasks(self):
        formats = [kind for kind, tools in (("deb", ("dpkg-deb",)), ("rpm", ("rpmbuild", "rpm2cpio", "cpio")),
                                             ("arch", ("zstd",))) if all(shutil.which(tool) for tool in tools)]
        if os.environ.get("APPIMAGETOOL") and os.environ.get("APPIMAGE_RUNTIME_FILE"):
            formats.append("appimage")
        if not formats:
            self.skipTest("No native packaging tools are installed")
        # Scratch must permit executing extracted helpers, including on noexec /tmp hosts.
        scratch = ROOT / "build"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cube-studio-artifacts-", dir=scratch) as temporary:
            root = Path(temporary)
            for kind in formats:
                with self.subTest(format=kind):
                    built, sources = [], []
                    for label, mask in (("first output", 0o022), ("second output", 0o077)):
                        work = root / kind / label
                        work.mkdir(parents=True)
                        previous = os.umask(mask)
                        try:
                            artifact = getattr(builder, "build_" + kind)(work, support.upstream_version(), "1", support.DEFAULT_EPOCH)
                            extracted = builder.verify_artifact(kind, artifact, work / "extracted")
                            self.assertFalse((extracted / "usr/bin/cube-chrootables").exists())
                            self.assertFalse((extracted / "var/lib/chrootable-manager").exists())
                            sources.append(json.loads((extracted / f"usr/share/doc/{support.PACKAGE}/build-provenance.json").read_text()))
                            built.append(hashlib.sha256(artifact.read_bytes()).hexdigest())
                        finally:
                            os.umask(previous)
                    self.assertEqual(sources[0], sources[1], "Sources changed during repeated artifact builds")
                    self.assertEqual(built[0], built[1], f"{kind} is not reproducible")


if __name__ == "__main__":
    unittest.main()
