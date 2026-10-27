"""Exercise an actual standalone Debian archive without installing it."""
import ast
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


APP = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which('dpkg-deb') and shutil.which('dpkg'), 'Debian packaging tools required')
class StandalonePackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix='cube-prompts-package-test-')
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.directory = Path(cls.scratch.name)
        cls.source = cls.directory / 'source snapshot'
        cls.source.mkdir()
        for name in ('app.py', 'style.css', 'README.md', 'NOTICE'):
            shutil.copy2(APP / name, cls.source / name)
        shutil.copytree(APP / 'prompt_manager', cls.source / 'prompt_manager',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(APP / 'packaging', cls.source / 'packaging',
                        ignore=shutil.ignore_patterns('out', '__pycache__'))
        cls.output = cls.directory / 'output with spaces'
        subprocess.run(
            [str(cls.source / 'packaging/build-deb.sh'), '--no-tests', '--output-directory', str(cls.output)],
            check=True, capture_output=True, text=True,
            env={**os.environ, 'SOURCE_DATE_EPOCH': '1700000000'},
        )
        cls.deb = next(cls.output.glob('*.deb'))
        cls.root = cls.directory / 'extracted'
        subprocess.run(['dpkg-deb', '--raw-extract', str(cls.deb), str(cls.root)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def test_metadata_and_dependencies(self):
        fields = subprocess.check_output(
            ['dpkg-deb', '-f', str(self.deb), 'Package', 'Version', 'Architecture', 'Depends'], text=True
        )
        self.assertIn('Package: cube-prompts', fields)
        self.assertIn('Architecture: all', fields)
        self.assertIn('gir1.2-gtk-4.0 (>= 4.10)', fields)
        self.assertIn('gir1.2-adw-1 (>= 1.5)', fields)
        self.assertNotIn('whitecube-desktop', fields)
        module = ast.parse((self.source / 'prompt_manager/__init__.py').read_text())
        version = next(ast.literal_eval(node.value) for node in module.body
                       if isinstance(node, ast.Assign) and any(isinstance(name, ast.Name)
                       and name.id == '__version__' for name in node.targets))
        self.assertIn(f'Version: {version}', fields)

    def test_runtime_is_complete_and_does_not_overlap_whitecube(self):
        runtime = self.root / 'usr/lib/cube-prompts'
        self.assertTrue((runtime / 'app.py').is_file())
        self.assertTrue((runtime / 'style.css').is_file())
        for source in (self.source / 'prompt_manager').rglob('*.py'):
            self.assertTrue((runtime / source.relative_to(self.source)).is_file(), str(source))
        self.assertFalse((self.root / 'usr/bin/cube-prompts').exists())
        self.assertFalse((self.root / 'usr/share/whitecube').exists())
        names = [path.name for path in self.root.rglob('*')]
        for excluded in ('__pycache__', 'settings.json', 'credentials.json', 'prompts.db', '.git', 'tests'):
            self.assertNotIn(excluded, names)
        for path in self.root.rglob('*.py'):
            compile(path.read_bytes(), str(path), 'exec')

    def test_desktop_and_appstream_integration(self):
        desktop = self.root / 'usr/share/applications/org.whitecube.CubePrompts.Standalone.desktop'
        text = desktop.read_text()
        self.assertIn('Exec=/usr/bin/cube-prompts-studio', text)
        self.assertIn('StartupWMClass=org.whitecube.CubePrompts', text)
        if shutil.which('desktop-file-validate'):
            subprocess.run(['desktop-file-validate', str(desktop)], check=True)
        metadata = ET.parse(self.root / 'usr/share/metainfo/org.whitecube.CubePrompts.Standalone.metainfo.xml')
        self.assertEqual(metadata.findtext('launchable'), desktop.name)
        self.assertTrue((self.root / 'usr/share/icons/hicolor/scalable/apps/org.whitecube.CubePrompts.Standalone.svg').is_file())
        self.assertTrue((self.root / 'usr/share/doc/cube-prompts/copyright').is_file())
        self.assertFalse((self.root / 'DEBIAN/postinst').exists())
        self.assertFalse((self.root / 'DEBIAN/postrm').exists())

    def test_checksums_and_root_ownership(self):
        checksums = (self.root / 'DEBIAN/md5sums').read_text().splitlines()
        self.assertGreater(len(checksums), 20)
        for line in checksums:
            expected, relative = line.split('  ', 1)
            self.assertEqual(hashlib.md5((self.root / relative).read_bytes()).hexdigest(), expected)
        digest = hashlib.sha256(self.deb.read_bytes()).hexdigest()
        self.assertEqual(self.deb.with_suffix('.deb.sha256').read_text(), f'{digest}  {self.deb.name}\n')
        listing = subprocess.check_output(['dpkg-deb', '--contents', str(self.deb)], text=True)
        for line in listing.splitlines():
            self.assertEqual(line.split()[1], 'root/root')
            if line.startswith('d'):
                self.assertEqual(line.split()[0], 'drwxr-xr-x')

    def test_extracted_launcher_isolated_from_python_environment(self):
        available = subprocess.run(
            ['/usr/bin/python3', '-I', '-c', 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")'],
            capture_output=True,
        )
        if available.returncode:
            self.skipTest('GTK runtime is not installed on builder')
        poison = self.directory / 'poison'
        poison.mkdir(exist_ok=True)
        (poison / 'gi.py').write_text('raise RuntimeError("untrusted PYTHONPATH loaded")\n')
        env = {**os.environ, 'PYTHONPATH': str(poison), 'XDG_CONFIG_HOME': str(self.directory / 'config'),
               'XDG_DATA_HOME': str(self.directory / 'data')}
        result = subprocess.run([str(self.root / 'usr/bin/cube-prompts-studio'), '--version'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cube-prompts ', result.stdout)
        self.assertFalse((self.directory / 'config').exists())
        self.assertFalse((self.directory / 'data').exists())

    def test_reproducible_archive(self):
        repeated = self.directory / 'repeated'
        subprocess.run(
            [str(self.source / 'packaging/build-deb.sh'), '--no-tests', '--output-directory', str(repeated)],
            check=True, capture_output=True, text=True,
            env={**os.environ, 'SOURCE_DATE_EPOCH': '1700000000'},
        )
        self.assertEqual(next(repeated.glob('*.deb')).read_bytes(), self.deb.read_bytes())

    def test_invalid_build_option_fails(self):
        result = subprocess.run([str(self.source / 'packaging/build-deb.sh'), '--unknown'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unknown option', result.stderr)


if __name__ == '__main__':
    unittest.main()
