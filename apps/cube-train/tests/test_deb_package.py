"""Build and exercise the standalone Debian package without installing it."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

APP = Path(__file__).resolve().parent.parent
BUILDER = APP / 'packaging/build-deb.sh'


@unittest.skipUnless(shutil.which('dpkg-deb') and shutil.which('dpkg'), 'Debian packaging tools required')
class StandalonePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix='trainer-package-test-')
        cls.directory = Path(cls.sandbox.name)
        cls.environment = dict(os.environ, HOME=str(cls.directory / 'home'),
                               XDG_CONFIG_HOME=str(cls.directory / 'config'),
                               XDG_STATE_HOME=str(cls.directory / 'state'),
                               WHITECUBE_TRAIN_OUT=str(cls.directory / 'runs'),
                               DISPLAY='', WAYLAND_DISPLAY='')
        command = [str(BUILDER), '--output-dir', str(cls.directory / 'first'), '--epoch', '1700000000']
        subprocess.run(command, check=True, capture_output=True, text=True)
        cls.deb = next((cls.directory / 'first').glob('*.deb'))
        cls.release = subprocess.check_output(['dpkg-deb', '--field', str(cls.deb), 'Version'], text=True).strip()
        cls.stage = cls.directory / 'extracted'
        subprocess.run(['dpkg-deb', '--extract', str(cls.deb), str(cls.stage)], check=True)
        cls.control = cls.directory / 'control'
        subprocess.run(['dpkg-deb', '--control', str(cls.deb), str(cls.control)], check=True)
        cls.binaries = cls.stage / 'usr/bin'

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.cleanup()

    def run_launcher(self, command, *arguments):
        return subprocess.run([str(self.binaries / command), *arguments], env=self.environment,
                              cwd=self.directory, capture_output=True, text=True, timeout=30)

    def test_reproducible_archive_and_checksum(self):
        subprocess.run([str(BUILDER), '--output-dir', str(self.directory / 'second'), '--epoch', '1700000000'],
                       check=True, capture_output=True, text=True)
        rebuilt = next((self.directory / 'second').glob('*.deb'))
        self.assertEqual(hashlib.sha256(self.deb.read_bytes()).digest(),
                         hashlib.sha256(rebuilt.read_bytes()).digest())
        sidecar = self.deb.with_name(self.deb.name + '.sha256').read_text().split()
        self.assertEqual(sidecar, [hashlib.sha256(self.deb.read_bytes()).hexdigest(), self.deb.name])

    def test_all_installed_files_match_md5sums(self):
        sums = (self.control / 'md5sums').read_text().splitlines()
        self.assertGreater(len(sums), 50)
        for line in sums:
            expected, filename = line.split('  ', 1)
            with self.subTest(filename=filename):
                self.assertEqual(hashlib.md5((self.stage / filename).read_bytes()).hexdigest(), expected)

    def test_standalone_package_dependencies_and_no_desktop_collision(self):
        fields = subprocess.check_output(['dpkg-deb', '--field', str(self.deb)], text=True)
        self.assertIn('Package: whitecube-trainer\n', fields)
        self.assertIn('Architecture: all\n', fields)
        self.assertIn('gir1.2-adw-1 (>= 1.5)', fields)
        self.assertNotIn('whitecube-desktop', fields)
        self.assertEqual(sorted(p.name for p in self.binaries.iterdir()), ['whitecube-trainer', 'whitecube-trainer-cli'])
        self.assertFalse((self.stage / 'usr/share/applications/cube-train.desktop').exists())
        self.assertFalse((self.stage / 'usr/share/icons/hicolor/scalable/apps/cube-train.svg').exists())
        self.assertFalse((self.control / 'postinst').exists())
        self.assertFalse((self.control / 'postrm').exists())

    def test_private_files_include_pipeline_without_build_or_user_data(self):
        application = self.stage / 'usr/lib/whitecube-trainer/cube-train'
        for filename in ('app.py', 'engine.py', 'worker.py', 'cli.py', 'diagnostics.py',
                         'pipeline/train_local.py', 'pipeline/export_notebook.py',
                         'pipeline/tensegrity/train.py'):
            self.assertTrue((application / filename).is_file(), filename)
        self.assertTrue((application.parent / 'cube-apps/cube_theme.py').is_file())
        names = {p.name for p in self.stage.rglob('*')}
        self.assertFalse(names.intersection({'__pycache__', '.pytest_cache', 'tests', '.git', 'train-out'}))

    def test_cli_headless_version_help_and_empty_run_list(self):
        for arguments, expected in ((('--version',), self.release), (('--help',), '--doctor'),
                                    (('--list-runs',), 'No runs')):
            with self.subTest(arguments=arguments):
                result = self.run_launcher('whitecube-trainer-cli', *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertNotIn('Traceback', result.stderr)

    @unittest.skipUnless(importlib.util.find_spec('gi'), 'GTK Python bindings required for desktop launch')
    def test_gui_version_and_help_without_display(self):
        for option in ('--version', '--help'):
            result = self.run_launcher('whitecube-trainer', option)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Whitecube Trainer', result.stdout)

    @unittest.skipUnless(shutil.which('gtk4-broadwayd') and importlib.util.find_spec('gi'),
                         'GTK Broadway server and bindings required')
    def test_packaged_gtk_window_and_upgraded_controls(self):
        runtime = self.directory / 'broadway-runtime'
        runtime.mkdir(mode=0o700)
        environment = dict(self.environment, XDG_RUNTIME_DIR=str(runtime), GDK_BACKEND='broadway',
                           BROADWAY_DISPLAY=':177', GIO_USE_VFS='local')
        application = self.stage / 'usr/lib/whitecube-trainer/cube-train'
        driver = self.directory / 'gtk-smoke.py'
        driver.write_text('''import argparse, sys, traceback
sys.path.insert(0, sys.argv[1])
import app
class Smoke(app.TrainerApp):
    failure = None
    def do_activate(self):
        super().do_activate()
        app.GLib.timeout_add(100, self.exercise)
    def exercise(self):
        try:
            window = self.window
            assert window.stack.get_visible_child_name() == 'train'
            assert window.start_button is not None
            assert window.detail_tabs.get_child_by_name('compare') is not None
            assert window.readiness_button is not None
            window.stack.set_visible_child_name('runs')
            assert window.stack.get_visible_child_name() == 'runs'
            print('Packaged GTK controls constructed successfully.')
        except BaseException:
            self.failure = traceback.format_exc()
            print(self.failure, file=sys.stderr)
        self.quit()
        return False
smoke=Smoke(argparse.Namespace(page=None,screenshot=None,no_probe=True))
result=smoke.run([sys.argv[0]])
raise SystemExit(1 if smoke.failure else result)
''')
        server = subprocess.Popen(['gtk4-broadwayd', '--address=127.0.0.1', '--port=0', ':177'],
                                  env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(30):
                if list(runtime.glob('broadway*.socket')):
                    break
                time.sleep(.05)
            result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(driver), str(application)],
                                    env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Packaged GTK controls constructed successfully.', result.stdout)
        finally:
            server.terminate()
            server.communicate(timeout=5)

    def test_desktop_and_appstream_entrypoints(self):
        desktop = self.stage / 'usr/share/applications/whitecube-trainer.desktop'
        if shutil.which('desktop-file-validate'):
            result = subprocess.run(['desktop-file-validate', str(desktop)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        contents = desktop.read_text()
        self.assertIn('Exec=whitecube-trainer\n', contents)
        self.assertIn('Icon=whitecube-trainer\n', contents)
        metadata = ET.parse(self.stage / 'usr/share/metainfo/org.whitecube.Trainer.metainfo.xml').getroot()
        self.assertEqual(metadata.find('launchable').text, desktop.name)
        self.assertTrue((self.stage / 'usr/share/icons/hicolor/scalable/apps/whitecube-trainer.svg').exists())


if __name__ == '__main__':
    unittest.main()
