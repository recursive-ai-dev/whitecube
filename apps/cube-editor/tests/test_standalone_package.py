"""Build and extract the standalone Debian package without touching the host."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]
PACKAGING = APP / 'packaging'


@unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb is unavailable')
class StandalonePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='cube-package-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.output = Path(cls.temporary.name) / 'output with spaces'
        result = subprocess.run(['bash', str(PACKAGING / 'build-deb.sh'), '--no-tests',
                                 '--output-directory', str(cls.output)], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.deb = next(cls.output.glob('cube-editor_*_all.deb'))
        cls.stage = Path(cls.temporary.name) / 'extracted root'
        subprocess.run(['dpkg-deb', '--raw-extract', str(cls.deb), str(cls.stage)], check=True,
                       capture_output=True)

    def run_launcher(self, name, *arguments):
        return subprocess.run([str(self.stage / 'usr/bin' / name), *arguments],
                              text=True, capture_output=True, timeout=15)

    def test_installed_launchers_work_without_a_display(self):
        version = (PACKAGING / 'VERSION').read_text().strip()
        for name in ('cube-editor-studio', 'cube-editor-cli'):
            result = self.run_launcher(name, '--version')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f'cube-editor {version}')
        for name, arguments in (('cube-editor-cli', ('dependencies',)),
                                ('cube-editor-studio', ('--cli', 'dependencies'))):
            result = self.run_launcher(name, *arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(any(item['name'] == 'xorriso' for item in json.loads(result.stdout)))

    @unittest.skipUnless(os.environ.get('GDK_BACKEND') == 'broadway', 'run tools/check.py for packaged GUI smoke test')
    def test_installed_gui_starts_and_renders(self):
        screenshot = Path(self.temporary.name) / 'installed-studio.png'
        result = self.run_launcher('cube-editor-studio', '--screenshot', str(screenshot))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(screenshot.is_file(), result.stdout + result.stderr)
        self.assertEqual(screenshot.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')

    def test_metadata_and_runtime_are_complete(self):
        fields = subprocess.check_output(['dpkg-deb', '--field', str(self.deb)], text=True)
        self.assertIn('Package: cube-editor\n', fields)
        self.assertIn('Architecture: all\n', fields)
        self.assertIn('gir1.2-adw-1 (>= 1.5)', fields)
        self.assertIn('qterminal | gnome-terminal | konsole | xfce4-terminal | xterm | foot', fields)
        self.assertNotIn('Conflicts:', fields)
        expected = ['usr/lib/cube-editor/app.py', 'usr/lib/cube-editor/engine.py',
                    'usr/lib/cube-editor/worker.py', 'usr/lib/cube-editor/kernel.py',
                    'usr/lib/cube-editor/diagnostics.py', 'usr/lib/cube-editor/style.css',
                    'usr/share/doc/cube-editor/COPYING', 'usr/share/doc/cube-editor/NOTICE',
                    'usr/share/doc/cube-editor/README.md.gz',
                    'usr/share/applications/org.whitecube.CubeEditor.Standalone.desktop',
                    'usr/share/icons/hicolor/scalable/apps/org.whitecube.CubeEditor.Standalone.svg',
                    'usr/share/metainfo/org.whitecube.CubeEditor.Standalone.metainfo.xml',
                    'usr/share/mime/packages/cube-editor-project.xml']
        for name in expected:
            self.assertTrue((self.stage / name).is_file(), name)
        self.assertFalse((self.stage / 'usr/bin/cube-editor').exists(), 'must coexist with Whitecube launcher')
        self.assertFalse((self.stage / 'usr/lib/cube-editor/tests').exists())
        self.assertFalse((self.stage / 'usr/lib/cube-editor/upstream').exists())
        self.assertFalse((self.stage / 'usr/lib/cube-editor/packaging').exists())

    def test_checksum_permissions_and_payload_manifest(self):
        self.assertEqual(self.deb.stat().st_mode & 0o777, 0o644)
        sidecar = self.deb.with_suffix('.deb.sha256').read_text().split()
        self.assertEqual(sidecar[0], hashlib.sha256(self.deb.read_bytes()).hexdigest())
        self.assertEqual(sidecar[1], self.deb.name)
        listing = subprocess.check_output(['dpkg-deb', '--contents', str(self.deb)], text=True)
        for line in listing.splitlines():
            self.assertIn('root/root', line)
        launcher = self.stage / 'usr/bin/cube-editor-studio'
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertEqual(self.stage.stat().st_mode & 0o777, 0o755)
        for path in self.stage.rglob('*'):
            if not path.is_symlink():
                self.assertEqual(path.stat().st_mode & 0o022, 0, str(path))
        result = subprocess.run(['md5sum', '--check', 'DEBIAN/md5sums'], cwd=self.stage,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_builder_rejects_unknown_arguments(self):
        result = subprocess.run(['bash', str(PACKAGING / 'build-deb.sh'), '--unknown'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unknown option', result.stderr)


if __name__ == '__main__':
    unittest.main()
