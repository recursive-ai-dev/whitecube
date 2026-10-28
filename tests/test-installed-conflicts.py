#!/usr/bin/python3 -I
"""Check coexistence with standalone packages without changing the real host."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / 'packaging/check-installed-conflicts.py'
spec = importlib.util.spec_from_file_location('whitecube_installed_conflicts', CHECKER)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class InstalledConflicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = self.root / 'host'
        self.info = self.host / 'var/lib/dpkg/info'
        self.info.mkdir(parents=True)
        self.status = self.info.parent / 'status'
        self.status.write_text('')
        self.stage = self.root / 'stage'
        metadata = self.stage / 'DEBIAN'
        metadata.mkdir(parents=True)
        metadata.chmod(0o755)
        (metadata / 'control').write_text('Package: whitecube-desktop\nVersion: 0.8.0\nArchitecture: amd64\n'
                                         'Maintainer: Whitecube <whitecube@localhost>\nDescription: conflict fixture\n')
        (metadata / 'control').chmod(0o644)

    def payload(self, name, content='payload'):
        path = self.stage / name.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def installed(self, package, files, state='installed', architecture='amd64'):
        with self.status.open('a') as stream:
            stream.write(f'Package: {package}\nStatus: install ok {state}\nArchitecture: {architecture}\nVersion: 3.2.0-1\n\n')
        (self.info / (package + '.list')).write_text('\n'.join(files) + '\n')

    def test_standalone_manager_collision_is_detected(self):
        self.payload('/usr/bin/chrootable-manager')
        self.installed('chrootables', ['/usr', '/usr/bin', '/usr/bin/chrootable-manager'])
        report = checker.check(self.stage, self.host)
        self.assertEqual(report['result'], 'conflicts')
        self.assertEqual(report['conflicts'][0]['path'], '/usr/bin/chrootable-manager')
        self.assertEqual(report['conflicts'][0]['owners'], [{'package': 'chrootables', 'version': '3.2.0-1'}])

    def test_private_manager_coexists_with_standalone_package(self):
        self.payload('/usr/libexec/whitecube/chrootable-manager')
        self.installed('chrootables', ['/usr', '/usr/bin', '/usr/bin/chrootable-manager'])
        self.assertEqual(checker.check(self.stage, self.host)['conflicts'], [])

    def test_upgrade_ignores_the_packages_own_installed_files(self):
        self.payload('/usr/bin/cube-status')
        self.installed('whitecube-desktop', ['/usr/bin/cube-status'])
        self.assertEqual(checker.check(self.stage, self.host)['conflicts'], [])

    def test_shared_directories_and_removed_packages_are_not_conflicts(self):
        self.payload('/usr/share/whitecube/readme')
        self.installed('another-package', ['/usr', '/usr/share', '/usr/share/whitecube'])
        self.installed('removed-package', ['/usr/share/whitecube/readme'], state='config-files')
        self.assertEqual(checker.check(self.stage, self.host)['conflicts'], [])

    def test_unpacked_package_still_owns_its_files(self):
        self.payload('/usr/bin/tool')
        self.installed('unpacked-package', ['/usr/bin/tool'], state='unpacked')
        self.assertEqual(len(checker.check(self.stage, self.host)['conflicts']), 1)

    def test_multiarch_package_file_lists_are_recognized(self):
        self.payload('/usr/lib/x86_64-linux-gnu/libexample.so.1')
        self.installed('libexample', ['/usr/lib/x86_64-linux-gnu/libexample.so.1'])
        (self.info / 'libexample.list').rename(self.info / 'libexample:amd64.list')
        report = checker.check(self.stage, self.host)
        self.assertEqual(report['conflicts'][0]['owners'][0]['package'], 'libexample:amd64')

    def test_usrmerge_parent_aliases_are_checked_in_the_host_root(self):
        self.payload('/usr/bin/tool')
        (self.host / 'usr/bin').mkdir(parents=True)
        (self.host / 'bin').symlink_to('/usr/bin')
        self.installed('legacy-layout', ['/bin/tool'])
        report = checker.check(self.stage, self.host)
        self.assertEqual(report['conflicts'][0]['canonical_path'], '/usr/bin/tool')

    def test_final_symlink_is_preserved_rather_than_followed_to_another_owned_file(self):
        self.payload('/usr/bin/tool')
        (self.host / 'usr/bin').mkdir(parents=True)
        (self.host / 'usr/bin/link').symlink_to('tool')
        self.installed('link-owner', ['/usr/bin/link'])
        self.assertEqual(checker.check(self.stage, self.host)['conflicts'], [])

    def test_symlink_payload_is_a_file_collision(self):
        path = self.payload('/usr/bin/tool')
        path.unlink()
        path.symlink_to('../libexec/whitecube/tool')
        self.installed('tool-owner', ['/usr/bin/tool'])
        self.assertEqual(len(checker.check(self.stage, self.host)['conflicts']), 1)

    def test_real_deb_inventory_and_cli_exit_status(self):
        self.payload('/usr/bin/chrootable-manager')
        self.installed('chrootables', ['/usr/bin/chrootable-manager'])
        package = self.root / 'fixture.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Znone', '--build', str(self.stage), str(package)],
                       capture_output=True, text=True, check=True)
        result = subprocess.run([sys.executable, '-I', str(CHECKER), str(package), '--host-root', str(self.host), '--json'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)['conflicts'][0]['path'], '/usr/bin/chrootable-manager')

    def test_missing_package_database_fails_instead_of_reporting_success(self):
        self.payload('/usr/bin/tool')
        self.status.unlink()
        result = subprocess.run([sys.executable, '-I', str(CHECKER), str(self.stage), '--host-root', str(self.host)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('check-installed-conflicts:', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
