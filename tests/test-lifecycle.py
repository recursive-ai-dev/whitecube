#!/usr/bin/python3 -I
"""Check lifecycle harness failure evidence and destination safety without networking."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('package_lifecycle', ROOT / 'packaging/smoke-lifecycle.py')
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


class LifecycleFailures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_existing_reports_and_runtime_are_never_overwritten(self):
        output = self.directory / 'output'
        output.mkdir()
        sentinel = output / 'report.json'
        sentinel.write_text('retained report')
        with self.assertRaisesRegex(RuntimeError, 'absent or empty'):
            lifecycle.outside(argparse.Namespace(output=output))
        self.assertEqual(sentinel.read_text(), 'retained report')

    def test_bootstrap_failure_retains_archive_identity_log_and_failed_report(self):
        if not all(shutil.which(name) for name in ('dpkg-deb', 'dpkg-scanpackages', 'bwrap')):
            self.skipTest('Debian bootstrap host tools unavailable')
        stage = self.directory / 'fixture'
        (stage / 'DEBIAN').mkdir(parents=True)
        stage.chmod(0o755)
        (stage / 'DEBIAN').chmod(0o755)
        (stage / 'DEBIAN/control').write_text('Package: whitecube-desktop\nVersion: 0.0.1\nArchitecture: all\nMaintainer: Fixture <fixture@example.invalid>\nDescription: Lifecycle fixture\n')
        package = self.directory / 'fixture.deb'
        subprocess.run(['dpkg-deb', '--build', str(stage), str(package)], capture_output=True, check=True)
        bootstrap = self.directory / 'mmdebstrap'
        bootstrap.write_text('#!/bin/sh\nprintf \"%s\\n\" \"$@\"\necho bootstrap-fixture-failure >&2\nexit 19\n')
        bootstrap.chmod(0o755)
        keyring = self.directory / 'fixture-keyring'
        keyring.write_text('not read by failing fixture')
        output = self.directory / 'report'
        args = argparse.Namespace(output=output, deb=package, previous_deb=None,
                                  mmdebstrap=bootstrap, keyring=keyring)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lifecycle.outside(args), 1)
        report = json.loads((output / 'report.json').read_text())
        self.assertEqual(report['verification'], 'failed')
        self.assertEqual(report['source']['sha256'], hashlib.sha256(package.read_bytes()).hexdigest())
        self.assertIn('19', report['error'])
        self.assertIn('bootstrap-fixture-failure', (output / 'bootstrap.log').read_text())
        self.assertIn('--include=?essential', (output / 'bootstrap.log').read_text())
        self.assertIn('--include=apt,fakeroot', (output / 'bootstrap.log').read_text())
        self.assertFalse((output / 'namespace.log').exists())

    def test_inventory_detects_multiarch_development_packages(self):
        inventory = 'libc6:amd64\t2.41\nlibcairo2-dev:amd64\t1.18\npython3-dev\t3.13\n'
        self.assertEqual(lifecycle.development_packages(inventory), ['libcairo2-dev:amd64', 'python3-dev'])

    def test_numpy_headers_are_allowed_only_when_required_by_numpy_runtime(self):
        inventory = 'python3-numpy\t2.2.4\npython3-numpy-dev:amd64\t2.2.4\npython3-dev\t3.13\n'
        self.assertEqual(lifecycle.development_packages(inventory), ['python3-numpy-dev:amd64', 'python3-dev'])
        required = 'python3-numpy-dev (= 1:2.2.4+ds-1), python3 (>= 3.13~), libc6 (>= 2.38)'
        self.assertEqual(lifecycle.development_packages(inventory, required), ['python3-dev'])
        for dependencies in ('python3-numpy-dev-tools', 'python3-numpy-dev | other', 'libnumpy-dev'):
            with self.subTest(dependencies=dependencies):
                self.assertEqual(lifecycle.development_packages(inventory, dependencies),
                                 ['python3-numpy-dev:amd64', 'python3-dev'])

    def test_zero_exit_with_modified_regular_file_is_failure(self):
        result = subprocess.CompletedProcess(['dpkg'], 0, stdout='??5??????   /usr/bin/cube-status\n', stderr='')
        with patch.object(lifecycle.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'payload verification failed'):
                lifecycle.verify_package_payload(self.directory / 'verify.log')
        self.assertIn('/usr/bin/cube-status', (self.directory / 'verify.log').read_text())

    def test_modified_conffile_is_accepted_only_during_upgrade(self):
        result = subprocess.CompletedProcess(['dpkg'], 0, stdout='??5?????? c /etc/whitecube/config\n', stderr='')
        with patch.object(lifecycle.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                lifecycle.verify_package_payload(self.directory / 'strict.log')
            lifecycle.verify_package_payload(self.directory / 'upgrade.log', allow_modified_conffiles=True)

    def test_conffile_exception_does_not_hide_regular_file_damage(self):
        result = subprocess.CompletedProcess(['dpkg'], 0, stdout='??5?????? c /etc/whitecube/config\nmissing     /usr/bin/cube-status\n', stderr='')
        with patch.object(lifecycle.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                lifecycle.verify_package_payload(self.directory / 'upgrade.log', allow_modified_conffiles=True)

    def test_failed_subprocess_output_is_retained_with_its_command(self):
        log = self.directory / 'failure.log'
        with self.assertRaisesRegex(RuntimeError, 'failed \\(7\\)'):
            lifecycle.run(['/bin/sh', '-c', 'printf failure-evidence; exit 7'], log)
        self.assertIn('failure-evidence', log.read_text())
        self.assertIn('/bin/sh', log.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
