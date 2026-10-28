#!/usr/bin/python3 -I
"""Check cached builder repair and early failures without downloading packages."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / 'tools/build-debian.sh'


class CachedBuilder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / 'builder with spaces'
        self.rootfs = self.work / 'rootfs'
        (self.rootfs / 'etc').mkdir(parents=True)
        (self.rootfs / '.whitecube-builder-ready').touch()
        (self.rootfs / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\n')
        (self.rootfs / 'etc/group').write_text('root:x:0:\n')
        # A local version-probe fixture skips the real, already separately
        # integrated Node download; prepare-only does not execute build tools.
        node = self.work / 'node/bin/node'
        node.parent.mkdir(parents=True)
        node.write_text('#!/bin/sh\nprintf "v22.23.4\\n"\n')
        node.chmod(0o755)
        self.env = dict(os.environ, WHITECUBE_BUILDER_DIR=str(self.work))
        self.env.pop('ELECTRON_DIST', None)

    def run_builder(self, *args, **extra_env):
        return subprocess.run(['bash', str(RUNNER), *args], capture_output=True,
                              text=True, env=dict(self.env, **extra_env))

    def test_cached_root_repairs_account_private_home_and_dns(self):
        first = self.run_builder('--prepare-only')
        self.assertEqual(first.returncode, 0, first.stderr)
        passwd = (self.rootfs / 'etc/passwd').read_text()
        group = (self.rootfs / 'etc/group').read_text()
        if os.getuid() != 0:
            self.assertIn(f'whitecube-builder:x:{os.getuid()}:{os.getgid()}:', passwd)
        self.assertTrue(any(line.split(':')[2:3] == [str(os.getgid())] for line in group.splitlines()))
        self.assertEqual((self.rootfs / 'tmp/whitecube-build-home').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.rootfs / 'etc/resolv.conf').read_bytes(), Path('/etc/resolv.conf').read_bytes())
        again = self.run_builder('--prepare-only')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.rootfs / 'etc/passwd').read_text(), passwd)
        self.assertEqual((self.rootfs / 'etc/group').read_text(), group)

    def test_cached_absolute_dns_link_cannot_write_outside_root(self):
        sentinel = Path(self.tmp.name) / 'outside-resolver'
        sentinel.write_text('do not modify\n')
        resolver = self.rootfs / 'etc/resolv.conf'
        resolver.symlink_to(sentinel)
        result = self.run_builder('--prepare-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(resolver.is_symlink())
        self.assertEqual(sentinel.read_text(), 'do not modify\n')

    def test_command_is_required_before_any_bootstrap(self):
        result = self.run_builder('--')
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires a command', result.stderr)
        self.assertFalse((self.work / '.prepare.lock').exists())

    def test_invalid_explicit_electron_fails_before_any_bootstrap(self):
        result = self.run_builder('--', 'true', ELECTRON_DIST='/no/such/electron-runtime')
        self.assertEqual(result.returncode, 1)
        self.assertIn('ELECTRON_DIST must contain an executable electron', result.stderr)
        self.assertFalse((self.work / '.prepare.lock').exists())

    def test_unknown_flag_is_reported(self):
        result = self.run_builder('--unknown-flag')
        self.assertEqual(result.returncode, 2)
        self.assertIn('unknown option', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
