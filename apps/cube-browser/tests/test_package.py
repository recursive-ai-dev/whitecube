#!/usr/bin/env python3
"""Verify an actual .deb and exercise it after extraction, without installing it."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else None


class DebianPackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ARCHIVE is None or not ARCHIVE.is_file():
            raise RuntimeError("Usage: python3 tests/test_package.py PATH.deb")
        cls.temp = tempfile.TemporaryDirectory(prefix="cube-browser-installed-")
        cls.base = Path(cls.temp.name)
        cls.tree = cls.base / "extracted app"
        subprocess.run(["dpkg-deb", "--extract", ARCHIVE, cls.tree], check=True)
        cls.control = cls.base / "control"
        subprocess.run(["dpkg-deb", "--control", ARCHIVE, cls.control], check=True)
        # Exercise the archive in a path unrelated to the source checkout.
        cls.tree.rename(cls.base / "moved app")
        cls.tree = cls.base / "moved app"
        cls.launcher = cls.tree / "usr/bin/cube-browser"
        cls.libdir = cls.tree / "usr/lib/cube-browser"
        cls.env = os.environ.copy()
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "LD_LIBRARY_PATH", "LD_PRELOAD", "CUBE_BROWSER_BUILD"):
            cls.env.pop(key, None)
        for key, value in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
            cls.env[key] = str(cls.base / value)
            (cls.base / value).mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def launch(self, *args):
        result = subprocess.run([str(self.launcher), *args], cwd=self.base, env=self.env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_metadata_and_payload(self):
        fields = subprocess.check_output(["dpkg-deb", "-f", str(ARCHIVE)], text=True)
        self.assertIn("Package: cube-browser\n", fields)
        self.assertIn("Version: " + (ROOT / "VERSION").read_text().strip() + "\n", fields)
        self.assertRegex(fields, r"Depends: .*libc6")
        self.assertIn("ca-certificates", fields)
        for name in ("cube-browser", "cube-renderd", "cube-netd"):
            binary = self.libdir / name
            self.assertTrue(binary.is_file())
            self.assertTrue(os.access(binary, os.X_OK))
        self.assertTrue((self.libdir / "lib/libcairo.so.2").is_file())
        docs = self.tree / "usr/share/doc/cube-browser"
        for name in ("COPYING", "NOTICE", "copyright", "README.md", "README.Debian",
                     "licenses/quickjs-LICENSE", "licenses/mbedtls-LICENSE",
                     "licenses/libjpeg-turbo-copyright", "licenses/nanosvg-LICENSE",
                     "licenses/COPYING-LGPL-2.1", "licenses/sqlite-PUBLIC-DOMAIN"):
            self.assertTrue((docs / name).is_file(), name)
        # No maintainer scripts or machine-specific profile changes.
        self.assertFalse(any((self.control / script).exists() for script in ("postinst", "postrm", "preinst", "prerm")))
        self.assertFalse((self.tree / "home").exists())
        self.assertFalse((self.tree / "etc").exists())
        desktop = self.tree / "usr/share/applications/cube-browser.desktop"
        subprocess.run(["desktop-file-validate", desktop], check=True)
        self.assertIn("Exec=cube-browser -- %u", desktop.read_text())
        self.assertIn("[Desktop Action Private]", desktop.read_text())
        self.assertTrue((self.tree / "usr/share/icons/hicolor/scalable/apps/cube-browser.svg").is_file())
        for line in (self.control / "md5sums").read_text().splitlines():
            digest, relative = line.split("  ", 1)
            self.assertEqual(hashlib.md5((self.tree / relative).read_bytes()).hexdigest(), digest, relative)

    def test_private_library_and_no_checkout_runtime_paths(self):
        for name in ("cube-browser", "cube-renderd"):
            dynamic = subprocess.check_output(["readelf", "-d", self.libdir / name], text=True)
            paths = [line for line in dynamic.splitlines() if "RUNPATH" in line or "RPATH" in line]
            self.assertEqual(len(paths), 1)
            self.assertIn("[$ORIGIN/lib]", paths[0])
            self.assertNotIn(str(ROOT), dynamic)
            closure = subprocess.check_output(["ldd", self.libdir / name], text=True, env=self.env)
            self.assertIn(str(self.libdir / "lib/libcairo.so.2"), closure)
            self.assertNotIn(".deps", closure)
            self.assertNotRegex(closure, r"lib(?:X11|xcb|SDL)|not found")

    def test_relocated_launcher_and_headless_workflows(self):
        self.assertIn("cube-browser " + (ROOT / "VERSION").read_text().strip(), self.launch("--version"))
        self.assertIn("Usage:", self.launch("--help"))
        self.assertIn("cube-renderd: OK", self.launch("--check"))
        self.assertIn("PRIVATE WINDOW", self.launch("--dump", "cube:home"))
        document = self.base / "local document.html"
        document.write_text("<!doctype html><title>Installed package test</title><h1>Relocated engine works</h1>")
        self.assertIn("Relocated engine works", self.launch("--dump", document.as_uri()))
        image = self.base / "export.png"
        self.launch("--render", "cube:home", str(image), "800x600")
        self.assertEqual(image.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreater(image.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
