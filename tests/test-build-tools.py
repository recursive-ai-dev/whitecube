#!/usr/bin/python3 -I
"""Exercise cache invalidation and release failure reporting without building."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


inputs = module('whitecube_inputs_test', 'tools/build-inputs.py')
release = module('whitecube_release_test', 'tools/verify-release.py')


class ElectronInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / 'apps/desktop-chat'
        self.app.mkdir(parents=True)
        (self.app / 'package.json').write_text(json.dumps({'devDependencies': {'electron': '45.1.0'}}))
        self.lockfile = self.app / 'package-lock.json'
        self.lockfile.write_text('{"lockfileVersion": 3}\n')
        self.dist = self.app / 'node_modules/electron/dist'
        self.dist.mkdir(parents=True)
        (self.dist / 'electron').write_text('#!/bin/sh\nexit 0\n')
        (self.dist / 'electron').chmod(0o755)
        (self.dist / 'version').write_text('45.1.0\n')
        (self.dist.parent / 'install.js').write_text('// pinned Electron installer fixture\n')

    def test_unchanged_cache_skips_npm_but_lockfile_change_refreshes(self):
        with patch.object(inputs.subprocess, 'run') as npm:
            inputs.ensure_electron(self.root, self.dist)
            self.assertEqual(npm.call_count, 2)
            inputs.ensure_electron(self.root, self.dist)
            self.assertEqual(npm.call_count, 2)
            self.lockfile.write_text('{"lockfileVersion": 3, "changed": true}\n')
            inputs.ensure_electron(self.root, self.dist)
            self.assertEqual(npm.call_count, 4)
            self.assertIn('ci', npm.call_args_list[2].args[0])
            self.assertEqual(npm.call_args_list[3].args[0][1], str(self.dist.parent / 'install.js'))

    def test_explicit_refresh_always_runs_npm(self):
        with patch.object(inputs.subprocess, 'run') as npm:
            inputs.ensure_electron(self.root, self.dist)
            inputs.ensure_electron(self.root, self.dist, refresh=True)
            self.assertEqual(npm.call_count, 4)

    def test_local_installer_uses_selected_node_and_must_finish_before_stamp(self):
        installer = str(self.dist.parent / 'install.js')
        stamp = self.app / 'node_modules/.whitecube-inputs.sha256'
        def install(command, **kwargs):
            self.assertFalse(stamp.exists())
            self.assertTrue(kwargs['check'])
            if command[0] != 'npm':
                self.assertEqual(command, ['/toolchain/node with spaces', installer])
        with patch.dict(os.environ, NODE='/toolchain/node with spaces'), patch.object(inputs.subprocess, 'run', side_effect=install) as run:
            inputs.ensure_electron(self.root, self.dist)
            self.assertEqual(run.call_count, 2)
        self.assertTrue(stamp.exists())

    def test_installer_failure_does_not_publish_a_successful_cache_key(self):
        outcomes = [subprocess.CompletedProcess(['npm'], 0), subprocess.CalledProcessError(7, ['node', 'install.js'])]
        with patch.object(inputs.subprocess, 'run', side_effect=outcomes):
            with self.assertRaises(subprocess.CalledProcessError):
                inputs.ensure_electron(self.root, self.dist)
        self.assertFalse((self.app / 'node_modules/.whitecube-inputs.sha256').exists())

    def test_missing_local_installer_is_reported_without_writing_stamp(self):
        (self.dist.parent / 'install.js').unlink()
        with patch.object(inputs.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'installer is missing'):
                inputs.ensure_electron(self.root, self.dist)
            self.assertEqual(run.call_count, 1)
        self.assertFalse((self.app / 'node_modules/.whitecube-inputs.sha256').exists())

    def test_external_runtime_must_match_without_installing_or_writing_stamp(self):
        with patch.object(inputs.subprocess, 'run') as npm:
            inputs.ensure_electron(self.root, self.dist, external=True)
            (self.dist / 'version').write_text('44.0.0\n')
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                inputs.ensure_electron(self.root, self.dist, external=True)
            npm.assert_not_called()
        self.assertFalse((self.app / 'node_modules/.whitecube-inputs.sha256').exists())

    def test_failed_refresh_does_not_publish_a_successful_cache_key(self):
        with patch.object(inputs.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['npm'])):
            with self.assertRaises(subprocess.CalledProcessError):
                inputs.ensure_electron(self.root, self.dist)
        self.assertFalse((self.app / 'node_modules/.whitecube-inputs.sha256').exists())


class ReleaseReport(unittest.TestCase):
    def test_release_rejects_dirty_or_mismatched_archive_sources(self):
        expected = {'commit': 'a' * 40, 'version': '0.9.0'}
        manifest = {'version': '0.9.0', 'build_provenance': {'source': {'commit': 'a' * 40, 'dirty': False}}}
        release.validate_release_source(manifest, expected)
        for source in ({'commit': 'b' * 40, 'dirty': False}, {'commit': 'a' * 40, 'dirty': True},
                       {'commit': 'a' * 40, 'dirty': None}, {}):
            with self.subTest(source=source), self.assertRaisesRegex(RuntimeError, 'clean release source'):
                release.validate_release_source({**manifest, 'build_provenance': {'source': source}}, expected)
        with self.assertRaises(RuntimeError):
            release.validate_release_source({**manifest, 'version': '0.8.1'}, expected)

    def test_failure_retains_completed_checks_log_and_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            commands = [('first', ['ok'], 1), ('broken', ['bad'], 1), ('later', ['later'], 1)]
            with patch.object(release.subprocess, 'run', side_effect=[subprocess.CompletedProcess(['ok'], 0), subprocess.CompletedProcess(['bad'], 9)]) as run:
                self.assertEqual(release.run_checks(commands, output, {}), 1)
            self.assertEqual(run.call_count, 2)
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['failed_check'], 'broken')
            self.assertEqual(report['verification'], 'failed')
            self.assertEqual(len(report['checks']), 2)
            self.assertTrue((output / 'first.log').is_file())
            self.assertTrue((output / 'broken.log').is_file())

    def test_timeout_is_a_retained_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(release.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['slow'], 1)):
                self.assertEqual(release.run_checks([('slow', ['slow'], 1)], output, {}), 1)
            self.assertIn('timed out', (output / 'slow.log').read_text())
            self.assertEqual(json.loads((output / 'report.json').read_text())['failed_check'], 'slow')


if __name__ == '__main__':
    unittest.main(verbosity=2)
