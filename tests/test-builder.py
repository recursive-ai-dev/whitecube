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
        key = subprocess.check_output(['python3', '-I', str(ROOT / 'tools/build-inputs.py'), 'builder-key'], text=True).strip()
        (self.rootfs / '.whitecube-builder-ready').write_text(key + '\n')
        (self.rootfs / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\n')
        (self.rootfs / 'etc/group').write_text('root:x:0:\n')
        # A local version-probe fixture skips the real, already separately
        # integrated Node download; prepare-only does not execute build tools.
        node = self.work / 'node/bin/node'
        node.parent.mkdir(parents=True)
        node.write_text('#!/bin/sh\nprintf "v22.23.4\\n"\n')
        node.chmod(0o755)
        # A cached --prepare-only must never enter a namespace. Keep the
        # dependency probe independent of whether this test host has bwrap.
        commands = Path(self.tmp.name) / 'commands'
        commands.mkdir()
        bwrap = commands / 'bwrap'
        bwrap.write_text('#!/bin/sh\necho "unexpected namespace launch" >&2\nexit 97\n')
        bwrap.chmod(0o755)
        self.env = dict(os.environ, WHITECUBE_BUILDER_DIR=str(self.work),
                        PATH=str(commands) + os.pathsep + os.environ['PATH'])
        self.env['WHITECUBE_TEST_BOOTSTRAP_ARGS'] = str(Path(self.tmp.name) / 'bootstrap-args')
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

    def bootstrap_probe(self):
        # No network: a stale recipe or explicit refresh reaches this local
        # bootstrap fixture, while a current cache must never execute it.
        bootstrap = self.work / 'host/usr/bin/mmdebstrap'
        bootstrap.parent.mkdir(parents=True)
        bootstrap.write_text('#!/bin/sh\nprintf "%s\\n" "$WHITECUBE_BUILDER_CACHE" > "$WHITECUBE_TEST_BOOTSTRAP_ARGS.cache"\nprintf "%s\\n" "$@" > "$WHITECUBE_TEST_BOOTSTRAP_ARGS"\necho "bootstrap requested" >&2\nexit 97\n')
        bootstrap.chmod(0o755)
        keyring = self.work / 'host/usr/share/keyrings/debian-archive-keyring.pgp'
        keyring.parent.mkdir(parents=True)
        keyring.touch()

    def test_changed_recipe_invalidates_ready_root(self):
        self.bootstrap_probe()
        (self.rootfs / '.whitecube-builder-ready').write_text('obsolete recipe\n')
        result = self.run_builder('--prepare-only')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('bootstrap requested', result.stderr)
        self.assertTrue((self.rootfs / 'etc/passwd').is_file())

    def test_refresh_rebuilds_even_a_current_cache(self):
        self.bootstrap_probe()
        result = self.run_builder('--refresh', '--prepare-only')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('bootstrap requested', result.stderr)
        self.assertTrue((self.rootfs / 'etc/passwd').is_file())

    def test_bootstrap_separates_apt_pattern_from_named_packages(self):
        self.bootstrap_probe()
        result = self.run_builder('--refresh', '--prepare-only')
        self.assertEqual(result.returncode, 1, result.stderr)
        arguments = Path(self.env['WHITECUBE_TEST_BOOTSTRAP_ARGS']).read_text().splitlines()
        includes = [arg.removeprefix('--include=') for arg in arguments if arg.startswith('--include=')]
        self.assertEqual(includes[0], '?essential')
        self.assertIn('apt', includes[1].split(','))
        self.assertIn('build-essential', includes[1].split(','))
        self.assertFalse(any(value.startswith('?essential,') for value in includes))

    def test_refresh_reuses_ready_root_archive_cache_as_download_input(self):
        self.bootstrap_probe()
        cache = self.rootfs / 'var/cache/apt/archives'
        cache.mkdir(parents=True)
        result = self.run_builder('--refresh', '--prepare-only')
        self.assertEqual(result.returncode, 1, result.stderr)
        cache_record = Path(self.env['WHITECUBE_TEST_BOOTSTRAP_ARGS'] + '.cache')
        self.assertEqual(cache_record.read_text().strip(), str(cache))
        arguments = Path(self.env['WHITECUBE_TEST_BOOTSTRAP_ARGS']).read_text().splitlines()
        extract_hooks = [argument for argument in arguments if argument.startswith('--extract-hook=')]
        self.assertEqual(len(extract_hooks), 1)
        self.assertIn('MMDEBSTRAP_ESSENTIAL', extract_hooks[0])
        self.assertIn('.whitecube-selected-archives', extract_hooks[0])
        # Exercise the real hook with host-prefixed and root-relative inputs.
        # Every selected archive must survive, while namespace paths must not
        # retain a build-host prefix.
        selected_root = Path(self.tmp.name) / 'selected root'
        selected_root.mkdir()
        hook = extract_hooks[0].removeprefix('--extract-hook=')
        subprocess.run(['sh', '-ec', hook, 'extract-hook', str(selected_root)], check=True,
                       env=dict(os.environ, MMDEBSTRAP_ESSENTIAL='/host/rootfs/var/cache/apt/archives/first.deb /var/cache/apt/archives/second.deb'))
        self.assertEqual((selected_root / '.whitecube-selected-archives').read_text().splitlines(),
                         ['/var/cache/apt/archives/first.deb', '/var/cache/apt/archives/second.deb'])

    def test_unknown_flag_is_reported(self):
        result = self.run_builder('--unknown-flag')
        self.assertEqual(result.returncode, 2)
        self.assertIn('unknown option', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
