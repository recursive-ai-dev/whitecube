#!/usr/bin/python3 -I
"""Offline tests for Whitecube GRUB and Plymouth boot theme assets and installer."""
import configparser
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import contextlib

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BOOT = REPO / "boot"


def load_gen():
    loader = importlib.machinery.SourceFileLoader("gen_boot_theme", str(BOOT / "gen-boot-theme.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


gen = load_gen()


class BootThemeAssetsTests(unittest.TestCase):
    def test_plymouth_and_grub_definitions_stay_consistent_with_script(self):
        cfg = configparser.ConfigParser()
        cfg.read(BOOT / "whitecube.plymouth")
        self.assertEqual(cfg["Plymouth Theme"]["Name"], "whitecube")
        self.assertEqual(cfg["Plymouth Theme"]["ModuleName"], "script")
        self.assertTrue(cfg["script"]["ScriptFile"].endswith("/whitecube.script"))

        script = (BOOT / "whitecube.script").read_text()
        self.assertIn("max_bullets", script)
        self.assertIn("dialog.prompt", script)
        self.assertIn("progress < 0", script)
        referenced = set(re.findall(r'Image\(\s*"([^"]+\.png)"\s*\)', script))
        self.assertTrue(referenced <= set(gen.REQUIRED_PLYMOUTH), referenced - set(gen.REQUIRED_PLYMOUTH))

        theme = (BOOT / "theme.txt").read_text()
        self.assertIn('desktop-image: "background.png"', theme)
        self.assertIn('selected_item_pixmap_style = "select_*.png"', theme)
        self.assertIn('terminal-box: "inbox_*.png"', theme)

    def test_render_verify_scale_seed_and_dimension_bounds(self):
        try:
            gen._require_pillow()
        except RuntimeError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "theme"
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                code = gen.main([str(out), "800", "600", "--seed", "11", "--scale", "2", "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["assets"]["grub"]["background.png"], [800, 600])
            self.assertEqual(payload["assets"]["plymouth"]["logo.png"], [320, 320])
            self.assertEqual(payload["assets"]["plymouth"]["entry.png"], [760, 88])

            with contextlib.redirect_stdout(io.StringIO()) as vbuf:
                self.assertEqual(gen.main([str(out), "--verify", "--json"]), 0)
            vdata = json.loads(vbuf.getvalue())
            self.assertEqual(vdata["mode"], "verify")
            self.assertEqual((vdata["width"], vdata["height"]), (800, 600))

            (out / "plymouth/lock.png").unlink()
            with self.assertRaises(FileNotFoundError):
                gen.verify_output(out)

        with self.assertRaises(ValueError):
            gen.render_theme("/tmp/unused", width=100, height=100)
        with self.assertRaises(ValueError):
            gen.render_theme("/tmp/unused", width=800, height=600, scale=9)

    def test_missing_pillow_reports_clear_error_and_version_works_without_pil(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
            gen.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(gen.VERSION, out.getvalue())

        with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None, "PIL.ImageDraw": None}):
            with self.assertRaisesRegex(RuntimeError, "Pillow"):
                gen._require_pillow()


class InstallBootScriptTests(unittest.TestCase):
    def test_posix_shell_syntax_help_version_and_dry_run(self):
        syntax = subprocess.run(["sh", "-n", str(BOOT / "install-boot.sh")], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        ver = subprocess.run([str(BOOT / "install-boot.sh"), "--version"], capture_output=True, text=True)
        self.assertEqual(ver.returncode, 0)
        self.assertIn("whitecube-install-boot", ver.stdout)

        dry = subprocess.run([str(BOOT / "install-boot.sh"), "--dry-run", "--resolution", "1280x720", "--scale", "2", "--no-issue", "--timeout", "3"],
                             capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("1280x720 (scale=2)", dry.stdout)
        self.assertIn("timeout=3", dry.stdout)
        self.assertIn("issue=0", dry.stdout)

        bad_res = subprocess.run([str(BOOT / "install-boot.sh"), "--dry-run", "--resolution", "100x100"], capture_output=True, text=True)
        self.assertNotEqual(bad_res.returncode, 0)

    def test_preflight_checks_and_sandboxed_install_status_and_revert(self):
        try:
            gen._require_pillow()
        except RuntimeError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            bin_dir = Path(tmp) / "fakebin"
            bin_dir.mkdir()
            (root / "etc/default").mkdir(parents=True)
            (root / "etc/default/grub").write_text('GRUB_DEFAULT=0\nGRUB_TIMEOUT=9\nGRUB_CMDLINE_LINUX="quiet & fast"\n')
            (root / "etc/issue").write_text("Debian GNU/Linux \\n \\l\n")
            fonts = root / "usr/share/fonts/truetype/dejavu"
            fonts.mkdir(parents=True)
            (fonts / "DejaVuSansMono.ttf").write_text("ttf")
            (fonts / "DejaVuSansMono-Bold.ttf").write_text("ttf")

            for name in ("grub-mkfont", "update-grub", "plymouth-set-default-theme"):
                script = bin_dir / name
                if name == "grub-mkfont":
                    script.write_text('#!/bin/sh\nout=""; while [ "$#" -gt 0 ]; do case "$1" in -o) out=$2; shift ;; esac; shift; done; : > "$out"\n')
                elif name == "plymouth-set-default-theme":
                    script.write_text('#!/bin/sh\nif [ "$#" -eq 0 ]; then echo "spinner"; fi\n')
                else:
                    script.write_text('#!/bin/sh\nexit 0\n')
                script.chmod(0o755)

            env = dict(os.environ, WC_BOOT_ROOT=str(root), PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
            res = subprocess.run([str(BOOT / "install-boot.sh"), "--resolution", "800x600", "--no-issue"], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

            grub_text = (root / "etc/default/grub").read_text()
            self.assertIn('GRUB_THEME="/boot/grub/themes/whitecube/theme.txt"', grub_text)
            self.assertIn("GRUB_TIMEOUT=9", grub_text)
            self.assertIn('GRUB_CMDLINE_LINUX="quiet & fast"', grub_text)
            # --no-issue preserved /etc/issue untouched
            self.assertEqual((root / "etc/issue").read_text(), "Debian GNU/Linux \\n \\l\n")
            self.assertTrue((root / "boot/grub/themes/whitecube/dejavu-mono-14.pf2").is_file())
            self.assertTrue((root / "usr/share/plymouth/themes/whitecube/whitecube.plymouth").is_file())

            st = subprocess.run([str(BOOT / "install-boot.sh"), "--status", "--json"], env=env, capture_output=True, text=True)
            self.assertEqual(st.returncode, 0, st.stderr)
            info = json.loads(st.stdout)
            self.assertTrue(info["plymouth_theme_installed"])
            self.assertTrue(info["grub_theme_installed"])
            self.assertTrue(info["backup_present"])
            self.assertEqual(info["plymouth_active"], "spinner")

            rev = subprocess.run([str(BOOT / "install-boot.sh"), "--revert", "--purge"], env=env, capture_output=True, text=True)
            self.assertEqual(rev.returncode, 0, rev.stderr)
            self.assertIn("GRUB_TIMEOUT=9", (root / "etc/default/grub").read_text())
            self.assertNotIn("GRUB_THEME=", (root / "etc/default/grub").read_text())
            self.assertFalse((root / "boot/grub/themes/whitecube").exists())
            self.assertFalse((root / "usr/share/plymouth/themes/whitecube").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
