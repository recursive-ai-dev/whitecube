#!/usr/bin/python3 -I
"""The install tree and the path checker: layout facts that must stay true as the project changes."""
from pathlib import Path
import importlib.util
import importlib.machinery
import json
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent


class Tree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / 'build/whitecube').is_file():
            raise unittest.SkipTest('build first (./BUILD.sh)')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.stage = Path(cls.tmp.name) / 'stage'
        cls.stage.mkdir()
        done = subprocess.run(['bash', str(ROOT / 'packaging/install-tree.sh'), str(cls.stage)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_nothing_is_installed_under_usr_local_or_home(self):
        for top in ('usr/local', 'home', 'root'):
            self.assertFalse((self.stage / top).exists(), top)

    def test_system_shared_files_are_readable_by_every_account(self):
        for path in (self.stage / 'usr').rglob('*'):
            if path.is_file():
                self.assertTrue(path.stat().st_mode & 0o004, str(path.relative_to(self.stage)))
            elif path.is_dir():
                self.assertEqual(path.stat().st_mode & 0o005, 0o005, str(path.relative_to(self.stage)))

    def test_every_command_is_executable_and_in_usr_bin(self):
        for name in ('whitecube-compositor', 'cube-status', 'cube-settings', 'cube-ca', 'cube-net', 'whitecube-session', 'whitecube-terminal',
                     'whitecube-web', 'cube-scripts', 'cube-lab', 'cube-preferences', 'cube-privacy', 'desktop-chat', 'cube-lock', 'whitecube-migrate', 'cube-editor',
                     'chrootables', 'whitecube-chrootable-manager', 'whitecube-browser', 'cube-strap', 'net-off', 'net-on'):
            path = self.stage / 'usr/bin' / name
            self.assertTrue(path.exists(), name)
            self.assertTrue(os.access(path, os.X_OK), name)

    def test_native_trainer_and_complete_pipeline_are_shipped(self):
        trainer = self.stage / 'usr/share/whitecube/cube-train'
        for name in ('app.py', 'engine.py', 'worker.py', 'pipeline/model_presets.py', 'pipeline/train_folder.py',
                     'pipeline/train_local.py', 'pipeline/train_supervisor.py', 'pipeline/prepare_folder.py',
                     'pipeline/prepare_parquet.py', 'pipeline/corpus_split.py', 'pipeline/tensegrity_lm.py',
                     'pipeline/tensegrity_nb_lib.py', 'pipeline/tensegrity_serve.py', 'pipeline/preview.py',
                     'pipeline/pacing.py', 'pipeline/files.py', 'pipeline/export_model.py'):
            self.assertTrue((trainer / name).is_file(), name)
        for name in ('cube-train', 'whitecube-train'):
            self.assertTrue(os.access(self.stage / 'usr/bin' / name, os.X_OK))
        self.assertTrue((self.stage / 'usr/share/icons/hicolor/scalable/apps/cube-train.svg').is_file())
        desktop = (self.stage / 'usr/share/applications/cube-train.desktop').read_text()
        self.assertIn('Terminal=false', desktop)
        self.assertIn('X-Wayland-Native=true', desktop)
        self.assertFalse((trainer / 'tests').exists())
        self.assertFalse(any(trainer.rglob('*.pt')))
        for module in (trainer / 'pipeline').rglob('*.py'):
            if not module.read_bytes().startswith(b'#!'):
                self.assertFalse(module.stat().st_mode & 0o111, str(module.relative_to(trainer)))

    def test_the_image_tools_command_name_is_left_alone(self):
        self.assertFalse((self.stage / 'usr/bin/whitecube').exists(), 'the separate "whitecube" package owns /usr/bin/whitecube')

    def test_helpers_live_in_libexec_and_not_on_the_path(self):
        self.assertTrue((self.stage / 'usr/libexec/whitecube/cube-settings-helper').is_file())
        self.assertTrue((self.stage / 'usr/libexec/whitecube/cube-net-dns').is_file())
        self.assertFalse((self.stage / 'usr/bin/cube-settings-helper').exists())

    def test_the_login_session_and_unit_are_present(self):
        session = (self.stage / 'usr/share/wayland-sessions/whitecube.desktop').read_text()
        self.assertIn('Exec=/usr/bin/whitecube-session', session)
        unit = (self.stage / 'usr/lib/systemd/system/whitecube-network.service').read_text()
        self.assertIn('ExecStart=/usr/bin/cube-net run', unit)

    def test_network_lockdown_is_packaged_with_boot_ordering_and_conffile(self):
        self.assertTrue(os.access(self.stage / 'usr/libexec/whitecube/whitecube-net-switch', os.X_OK))
        conf = 'etc/whitecube/net-switch.conf'
        self.assertTrue((self.stage / conf).is_file())
        self.assertIn('/' + conf, (ROOT / 'packaging/debian/conffiles').read_text().splitlines())
        units = self.stage / 'usr/lib/systemd/system'
        restore = units / 'whitecube-net-lockdown.service'
        self.assertTrue(restore.is_file())
        self.assertEqual((units / 'multi-user.target.wants/whitecube-net-lockdown.service').resolve(), restore.resolve())
        dependency = (units / 'whitecube-network.service.d/net-lockdown.conf').read_text()
        self.assertIn('Requires=whitecube-net-lockdown.service', dependency)
        self.assertIn('After=whitecube-net-lockdown.service', dependency)
        self.assertIn('nftables.service ufw.service', restore.read_text())
        self.assertTrue((self.stage / 'usr/share/doc/whitecube-desktop/NET-SWITCH.txt').is_file())
        self.assertFalse((self.stage / 'var/lib/whitecube/net-lockdown.json').exists())

    def test_cube_editor_runtime_and_desktop_integration_are_shipped(self):
        editor = self.stage / 'usr/share/whitecube/cube-editor'
        for name in ('app.py', 'engine.py', 'worker.py', 'kernel.py', 'COPYING', 'NOTICE'):
            self.assertTrue((editor / name).is_file(), name)
            self.assertTrue(os.access(editor / name, os.R_OK), name)
        entry = (self.stage / 'usr/share/applications/cube-editor.desktop').read_text()
        self.assertIn('Exec=/usr/bin/cube-editor', entry)
        self.assertIn('Icon=cube-editor', entry)
        self.assertTrue((self.stage / 'usr/share/icons/hicolor/scalable/apps/cube-editor.svg').is_file())
        self.assertFalse((editor / 'upstream').exists())

    def test_chrootables_window_manager_and_desktop_integration_are_shipped(self):
        app = self.stage / 'usr/share/whitecube/chrootables'
        for name in ('app.py', 'backend.py', 'style.css'):
            self.assertTrue((app / name).is_file(), name)
            self.assertTrue(os.access(app / name, os.R_OK), name)
        # The window must run the root-owned command, never a copy beside it.
        self.assertFalse((app / 'chrootable-manager').exists())
        helper = self.stage / 'usr/libexec/whitecube/chrootable-manager'
        self.assertTrue(helper.read_text().startswith('#!/usr/bin/env bash'))
        self.assertEqual((self.stage / 'usr/bin/whitecube-chrootable-manager').resolve(), helper.resolve())
        self.assertIn("Path('/usr/libexec/whitecube/chrootable-manager')", (app / 'backend.py').read_text())
        self.assertFalse((self.stage / 'usr/bin/chrootable-manager').exists())
        entry = (self.stage / 'usr/share/applications/chrootables.desktop').read_text()
        self.assertIn('Exec=/usr/bin/chrootables', entry)
        self.assertIn('Icon=chrootables', entry)
        self.assertTrue((self.stage / 'usr/share/icons/hicolor/scalable/apps/chrootables.svg').is_file())

    def test_bundled_browser_leaves_standalone_package_files_unowned(self):
        for name in ('usr/bin/cube-browser', 'usr/share/applications/cube-browser.desktop',
                     'usr/share/icons/hicolor/scalable/apps/cube-browser.svg'):
            self.assertFalse((self.stage / name).exists(), name)
        launcher = self.stage / 'usr/bin/whitecube-browser'
        self.assertEqual(launcher.resolve(), (self.stage / 'usr/libexec/whitecube/cube-browser/cube-browser').resolve())
        entry = (self.stage / 'usr/share/applications/whitecube-browser.desktop').read_text()
        self.assertIn('Exec=/usr/bin/whitecube-browser -- %u', entry)
        self.assertIn('Icon=whitecube-browser', entry)
        self.assertTrue((self.stage / 'usr/share/icons/hicolor/scalable/apps/whitecube-browser.svg').is_file())

    def test_wc_command_suite_is_shipped_system_wide_and_runs_from_the_tree(self):
        sys.path.insert(0, str(ROOT / 'apps/wc-suite'))
        import wc_suite
        names = sorted(wc_suite.COMMANDS)
        self.assertGreaterEqual(len(names), 40)
        for name in names:
            wrapper = self.stage / f'usr/bin/wc-{name}'
            self.assertTrue(os.access(wrapper, os.X_OK), wrapper.name)
            self.assertNotIn('/usr/local', wrapper.read_text())
            self.assertTrue((self.stage / f'usr/share/bash-completion/completions/wc-{name}').is_file(), name)
        self.assertTrue((self.stage / 'usr/share/zsh/vendor-completions/_wc').is_file())
        self.assertFalse((self.stage / 'usr/lib/wc-suite/test_suite.py').exists())
        # Run through sh: the staging directory may sit on a noexec /tmp.
        run = lambda *argv: subprocess.run(['sh', str(self.stage / 'usr/bin' / argv[0]), *argv[1:]],
                                           capture_output=True, text=True, env=dict(os.environ, HOME=self.tmp.name))
        listing = run('wc-ls', '--json')
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertLessEqual({f'wc-{name}' for name in names}, set(json.loads(listing.stdout)))
        doctor = json.loads(run('wc-doctor', '--json').stdout)
        self.assertEqual(doctor['bin'], str(self.stage / 'usr/bin'))
        self.assertEqual(doctor['missing_wrappers'], [])

    def test_system_defaults_are_conffiles_and_users_get_dotfiles_from_skel(self):
        conffiles = (ROOT / 'packaging/debian/conffiles').read_text().split()
        for path in conffiles:
            self.assertTrue((self.stage / path.lstrip('/')).is_file(), path)
        self.assertTrue((self.stage / 'etc/skel/.config/foot/foot.ini').is_file())
        self.assertNotIn('/home/', (self.stage / 'etc/whitecube/config').read_text())

    def test_chat_runtime_is_shipped_and_missing_runtime_is_rejected(self):
        spec = importlib.util.spec_from_file_location('chat_runtime_check', ROOT / 'packaging/check-chat-runtime.py')
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        self.assertEqual(checker.check(self.stage), [])
        version = self.stage / 'usr/lib/whitecube/desktop-chat-runtime/version'
        original = version.read_bytes()
        original_mode = version.stat().st_mode & 0o777
        try:
            version.unlink()
            self.assertTrue(any('version' in p for p in checker.check(self.stage)))
        finally:
            version.write_bytes(original)
            version.chmod(original_mode)

    def test_chat_checkpoint_server_and_picker_are_packaged_without_training_data(self):
        chat = self.stage / 'usr/share/whitecube/desktop-chat'
        self.assertTrue((chat / 'local-model-server.py').is_file())
        self.assertTrue((chat / 'src/local-models.js').is_file())
        self.assertIn('model-picker', (chat / 'renderer/index.html').read_text())
        self.assertFalse(any(self.stage.rglob('ckpt_*.pt')))
        self.assertTrue((self.stage / 'usr/share/doc/whitecube-desktop/LOCAL-CHAT-CHECKPOINTS.txt').is_file())

    def test_sudoers_is_documented_but_never_dropped_into_etc(self):
        self.assertFalse((self.stage / 'etc/sudoers.d').exists())
        self.assertTrue((self.stage / 'usr/share/doc/whitecube-desktop/sudoers-whitecube-settings.hardened.example').is_file())

    def test_no_tests_caches_or_build_tools_are_shipped(self):
        found = [p for p in self.stage.rglob('*') if p.name in ('__pycache__', '.git', 'node_modules', 'tests', 'os-build')]
        self.assertEqual(found, [])

    def test_native_library_checker_rejects_host_fallback_and_self_dependencies(self):
        spec = importlib.util.spec_from_file_location('whitecube_library_check', ROOT / 'packaging/check-libraries.py')
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        private = self.stage / 'usr/lib/whitecube'
        private.mkdir(parents=True, exist_ok=True)
        check = [sys.executable, '-I', str(ROOT / 'packaging/check-libraries.py'), str(self.stage)]
        control = self.stage / 'DEBIAN/control'
        try:
            done = subprocess.run(check, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('private library is missing', done.stdout)
            output = subprocess.run(['ldd', str(ROOT / 'build/whitecube')], capture_output=True, text=True, check=True).stdout
            resolved = dict(checker.RESOLVED.findall(output))
            for name in checker.PRIVATE:
                source = Path(resolved[name]).resolve()
                if name == 'libcairo.so.2':
                    candidate = ROOT / 'build-cairo/stage/usr/local/lib/x86_64-linux-gnu/libcairo.so.2'
                    if candidate.is_file():
                        source = candidate.resolve()
                shutil.copyfile(source, private / name)
            done = subprocess.run(check, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertTrue(checker.resolution_problems('libexample.so => not found\n', private.resolve()))
            control.parent.mkdir()
            control.write_text('Package: whitecube-desktop\nDepends: libc6, whitecube-desktop (>= 0.3.0)\n')
            done = subprocess.run(check, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('self dependency', done.stdout)
            control.write_text('Package: whitecube-desktop\nDepends: libc6, libgles2\n')
            done = subprocess.run(check, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('missing from Depends: libegl1', done.stdout)
            control.unlink()
            control.parent.rmdir()
            (private / 'libwlroots-0.18.so').unlink()
            done = subprocess.run(check, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('libwlroots-0.18.so', done.stdout)
        finally:
            if control.exists():
                control.unlink()
                control.parent.rmdir()
            for name in checker.PRIVATE:
                (private / name).unlink(missing_ok=True)
    def test_the_path_checker_passes_and_catches_a_stale_path(self):
        check = [sys.executable, '-I', str(ROOT / 'packaging/check-tree.py'), str(self.stage), '--sources', str(ROOT / 'src')]
        done = subprocess.run(check, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout)
        script = self.stage / 'usr/bin/cube-check'
        original = script.read_text()
        worker = self.stage / 'usr/share/whitecube/cube-editor/worker.py'
        original_worker = worker.read_text()
        helper = self.stage / 'usr/libexec/whitecube/cube-settings-helper'
        original_helper = helper.read_text()
        try:
            script.write_text(original + '\n# /usr/share/whitecube/no-such-file\n# /usr/local/bin/old-copy\n# /usr/bin/amixer\n')
            worker.write_text(original_worker + '\n# Host path regression: /usr/local/bin/old-worker\n')
            helper.write_text(original_helper + '\n# /usr/bin/amixer-typo\n')
            done = subprocess.run(check, capture_output=True, text=True)
            self.assertEqual(done.returncode, 1)
            self.assertIn('no-such-file', done.stdout)
            self.assertIn('/usr/local/', done.stdout)
            self.assertIn('cube-editor/worker.py', done.stdout)
            self.assertIn('amixer-typo', done.stdout)
            if not Path('/usr/bin/amixer').exists():
                self.assertIn('cube-check: refers to /usr/bin/amixer', done.stdout)
        finally:
            script.write_text(original)
            worker.write_text(original_worker)
            helper.write_text(original_helper)

    def test_comment_about_unrelated_whitecube_does_not_exempt_a_runtime_reference(self):
        session = self.stage / 'usr/bin/whitecube-session'
        original = session.read_text()
        host = Path(self.tmp.name) / 'empty-host'
        host.mkdir()
        try:
            session.write_text(original + '\nexec /usr/bin/whitecube\n')
            done = subprocess.run([sys.executable, '-I', str(ROOT / 'packaging/check-tree.py'), str(self.stage),
                                   '--host-root', str(host)], capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('whitecube-session: refers to /usr/bin/whitecube,', done.stdout)
        finally:
            session.write_text(original)


class DependencyOwnership(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('whitecube_library_dependencies', ROOT / 'packaging/library-dependencies.py')
        cls.scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.scanner)

    def test_usrmerge_alias_keeps_the_original_diverted_library_owner(self):
        seen = []
        original = '/usr/lib/x86_64-linux-gnu/libEGL.so.1'

        def query(path):
            seen.append(path)
            if path == original:
                return ('diversion by glx-diversions from: ' + original + '\n'
                        'diversion by glx-diversions to: /usr/lib/mesa-diverted/x86_64-linux-gnu/libEGL.so.1\n'
                        'libegl1:amd64: ' + original + '\n')
            return ''

        self.assertEqual(self.scanner.owner_for_library('/lib/x86_64-linux-gnu/libEGL.so.1', query), 'libegl1')
        self.assertEqual(seen[0], original)

    def test_diverted_physical_file_follows_dpkg_original_path_metadata(self):
        original = '/usr/lib/x86_64-linux-gnu/libGLESv2.so.2.1.0'
        diverted = '/usr/lib/mesa-diverted/x86_64-linux-gnu/libGLESv2.so.2.1.0'

        def query(path):
            if path == diverted:
                return f'diversion by glx-diversions from: {original}\ndiversion by glx-diversions to: {diverted}\n'
            if path == original:
                return f'libgles2:amd64: {original}\n'
            return ''

        self.assertEqual(self.scanner.owner_for_library(diverted, query), 'libgles2')

    def test_unowned_required_library_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, 'no Debian package owns'):
            self.scanner.owner_for_library('/opt/unowned/libexample.so', lambda _: '')

    def test_real_graphics_library_owners_are_found(self):
        self.assertEqual(self.scanner.owner_for_library('/lib/x86_64-linux-gnu/libEGL.so.1'), 'libegl1')
        self.assertEqual(self.scanner.owner_for_library('/lib/x86_64-linux-gnu/libGLESv2.so.2'), 'libgles2')

    def compile_dependency_fixture(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('a C compiler is required for ELF dependency fixtures')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        stage = root / 'stage'
        destination = stage / 'usr/bin'
        destination.mkdir(parents=True)
        distro = root / 'distribution-libraries'
        distro.mkdir()
        library = distro / 'libbridge.so.1'
        subprocess.run([compiler, '-shared', '-fPIC', '-Wl,-soname,libbridge.so.1',
                        '-x', 'c', '-o', str(library), '-', '-lm'],
                       input='#include <math.h>\ndouble bridge(double x) { return cos(x); }\n',
                       text=True, capture_output=True, check=True)
        subprocess.run([compiler, '-x', 'c', '-o', str(destination / 'example'), '-',
                        '-L' + str(distro), '-Wl,-rpath,' + str(distro), '-l:libbridge.so.1'],
                       input='extern double bridge(double); int main(void) { return (int)bridge(1.23); }\n',
                       text=True, capture_output=True, check=True)
        return stage, library

    @staticmethod
    def fixture_owner(path):
        name = Path(path).name
        return ('libbridge-provider' if name.startswith('libbridge') else
                'libm-transitive-provider' if name.startswith('libm.so') else 'libc6')

    def test_distribution_library_transitive_implementation_is_not_pinned(self):
        stage, _ = self.compile_dependency_fixture()
        with patch.object(self.scanner, 'owner_for_library', side_effect=self.fixture_owner):
            dependencies = self.scanner.discover(stage)
        self.assertEqual(dependencies, ['libbridge-provider', 'libc6'])

    def test_shipped_private_library_contributes_its_own_direct_dependencies(self):
        stage, library = self.compile_dependency_fixture()
        private = stage / 'usr/lib/whitecube'
        private.mkdir(parents=True)
        shutil.copyfile(library, private / library.name)
        with patch.object(self.scanner, 'owner_for_library', side_effect=self.fixture_owner):
            dependencies = self.scanner.discover(stage)
        self.assertEqual(dependencies, ['libc6', 'libm-transitive-provider'])

    def test_missing_transitive_distribution_library_still_fails_validation(self):
        stage, _ = self.compile_dependency_fixture()
        execute = subprocess.run
        def missing(command, **kwargs):
            result = execute(command, **kwargs)
            if command[0] == 'ldd':
                result.stdout += '\n libimplementation.so.1 => not found\n'
            return result
        with patch.object(self.scanner.subprocess, 'run', side_effect=missing):
            with self.assertRaisesRegex(RuntimeError, 'unresolved shared library'):
                self.scanner.discover(stage)

    def test_x11_in_transitive_closure_still_fails_native_validation(self):
        stage, _ = self.compile_dependency_fixture()
        execute = subprocess.run
        def x11(command, **kwargs):
            result = execute(command, **kwargs)
            if command[0] == 'ldd':
                result.stdout += '\n libX11.so.6 => /usr/lib/libX11.so.6 (0x1234)\n'
            return result
        with patch.object(self.scanner.subprocess, 'run', side_effect=x11):
            with self.assertRaisesRegex(RuntimeError, 'X11 library reaches a native executable'):
                self.scanner.discover(stage)

    def test_direct_dynamic_loader_dependency_resolves_without_an_arrow(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('a C compiler is required for ELF dependency fixtures')
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            binary = stage / 'usr/bin/loader-user'
            binary.parent.mkdir(parents=True)
            subprocess.run([compiler, '-x', 'c', '-o', str(binary), '-', '-Wl,--no-as-needed',
                            '-x', 'none', '/lib64/ld-linux-x86-64.so.2'], input='int main(void) { return 0; }\n',
                           text=True, capture_output=True, check=True)
            self.assertIn('ld-linux-x86-64.so.2', self.scanner.needed_libraries(binary))
            self.assertIn('libc6', self.scanner.discover(stage))


class ArchiveVerification(unittest.TestCase):
    """Exercise the final archive checker on real deb files, independent of a desktop build."""
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('whitecube_archive_check', ROOT / 'packaging/verify-deb.py')
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stage = Path(self.tmp.name) / 'stage'
        metadata = self.stage / 'DEBIAN'
        metadata.mkdir(parents=True)
        self.control = metadata / 'control'
        self.control.write_text(
            'Package: whitecube-desktop\nVersion: 0.8.1\nArchitecture: amd64\n'
            'Maintainer: Whitecube <whitecube@localhost>\nSection: x11\nPriority: optional\n'
            'Installed-Size: 1\nDepends: libc6 (>= 2.34), python3\nDescription: test fixture\n')
        for name in ('postinst', 'postrm'):
            script = metadata / name
            script.write_text('#!/bin/sh\nexit 0\n')
            script.chmod(0o755)
        self.config = self.stage / 'etc/whitecube/config'
        self.config.parent.mkdir(parents=True)
        self.config.write_text('setting=yes\n')
        self.config.chmod(0o644)
        self.sandbox = self.stage / self.checker.SANDBOX
        self.sandbox.parent.mkdir(parents=True)
        self.sandbox.write_bytes(b'fixture')
        self.sandbox.chmod(0o4755)
        (metadata / 'conffiles').write_text('/etc/whitecube/config\n')
        for path in self.stage.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
        self.write_checksums()
        self.package = Path(self.tmp.name) / 'whitecube-desktop_0.8.1_amd64.deb'

    def write_checksums(self):
        hashes = []
        for path in sorted(self.stage.rglob('*')):
            if path.is_file() and not path.is_relative_to(self.stage / 'DEBIAN'):
                hashes.append(hashlib.md5(path.read_bytes()).hexdigest() + '  ' + str(path.relative_to(self.stage)))
        (self.stage / 'DEBIAN/md5sums').write_text('\n'.join(hashes) + '\n')
        for path in (self.stage / 'DEBIAN').iterdir():
            if path.name not in ('postinst', 'postrm'):
                path.chmod(0o644)

    def verify(self):
        result = subprocess.run(['dpkg-deb', '--root-owner-group', '-Znone', '--build',
                                 str(self.stage), str(self.package)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.checker.check(self.package)

    def rewrite_archive(self, flag, mutate):
        """Alter a tar member to exercise checks dpkg-deb's builder precludes."""
        self.verify()
        parts = []
        for current, name in (('--ctrl-tarfile', 'control.tar'), ('--fsys-tarfile', 'data.tar')):
            raw = subprocess.check_output(['dpkg-deb', current, str(self.package)])
            if current == flag:
                output = io.BytesIO()
                with tarfile.open(fileobj=io.BytesIO(raw)) as source, tarfile.open(fileobj=output, mode='w') as destination:
                    for member in source:
                        content = source.extractfile(member).read() if member.isfile() else None
                        destination.addfile(member, io.BytesIO(content) if content is not None else None)
                    mutate(destination)
                raw = output.getvalue()
            parts.append((name, raw))
        archive = bytearray(b'!<arch>\n')
        for name, data in [('debian-binary', b'2.0\n'), *parts]:
            archive.extend(f'{name:<16}{0:<12}{0:<6}{0:<6}{0o100644:<8o}{len(data):<10}`\n'.encode())
            archive.extend(data)
            if len(data) % 2:
                archive.extend(b'\n')
        self.package.write_bytes(archive)

    def test_complete_archive_passes_and_reports_a_verifiable_manifest(self):
        problems, summary = self.verify()
        self.assertEqual(problems, [])
        self.assertEqual(summary['verification'], 'passed')
        self.assertEqual(summary['payload_files'], 2)
        self.assertEqual(summary['filename'], self.package.name)
        self.assertEqual(summary['sha256'], hashlib.sha256(self.package.read_bytes()).hexdigest())


    def test_new_release_requires_embedded_build_provenance(self):
        self.control.write_text(self.control.read_text().replace('Version: 0.8.1', 'Version: 0.9.0'))
        problems, _ = self.verify()
        self.assertIn('build provenance is missing from release payload', problems)

    def add_provenance(self, runtime='45.1.0'):
        payload = {'format': 'org.whitecube.build-provenance', 'version': 1,
                   'source': {'commit': 'a' * 40, 'dirty': True},
                   'inputs_sha256': {'BUILD.sh': 'b' * 64},
                   'runtime': {'electron': runtime}}
        path = self.stage / self.checker.PROVENANCE
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload))
        (self.sandbox.parent / 'version').write_text('45.1.0\n')
        self.write_checksums()
        return payload

    def test_manifest_copies_provenance_from_verified_payload(self):
        expected = self.add_provenance()
        self.control.write_text(self.control.read_text().replace('Version: 0.8.1', 'Version: 0.9.0'))
        problems, summary = self.verify()
        self.assertEqual(problems, [])
        self.assertEqual(summary['build_provenance'], expected)

    def test_provenance_runtime_must_match_shipped_electron(self):
        self.add_provenance(runtime='44.0.0')
        problems, _ = self.verify()
        self.assertTrue(any('Electron runtime do not match' in problem for problem in problems), problems)

    def test_modified_payload_is_rejected(self):
        self.config.write_text('changed=yes\n')
        problems, _ = self.verify()
        self.assertTrue(any('incorrect payload checksum' in problem for problem in problems), problems)

    def test_unregistered_configuration_would_not_survive_an_upgrade(self):
        extra = self.stage / 'etc/whitecube/extra.conf'
        extra.write_text('extra=yes\n')
        extra.chmod(0o644)
        self.write_checksums()
        problems, _ = self.verify()
        self.assertIn('configuration file is not registered as a conffile: etc/whitecube/extra.conf', problems)

    def test_sandbox_losing_setuid_is_rejected(self):
        self.sandbox.chmod(0o755)
        problems, _ = self.verify()
        self.assertTrue(any('sandbox helper' in problem for problem in problems), problems)

    def test_unexpected_privileged_file_is_rejected(self):
        self.config.chmod(0o4644)
        problems, _ = self.verify()
        self.assertIn('unexpected setuid/setgid file: etc/whitecube/config', problems)

    def test_world_writable_runtime_file_is_rejected(self):
        self.config.chmod(0o666)
        problems, _ = self.verify()
        self.assertIn('payload is writable by group or others: etc/whitecube/config', problems)

    def test_abi_minimum_cannot_be_omitted(self):
        self.control.write_text(self.control.read_text().replace('libc6 (>= 2.34)', 'libc6'))
        problems, _ = self.verify()
        self.assertIn('libc6 ABI minimum is missing from Depends', problems)

    def test_self_dependency_is_rejected(self):
        self.control.write_text(self.control.read_text().replace('python3', 'whitecube-desktop (>= 0.1)'))
        problems, _ = self.verify()
        self.assertIn('package depends on itself', problems)

    def test_unsafe_tar_paths_are_rejected(self):
        for name in ('../etc/passwd', '/usr/bin/escape', './usr/../../escape'):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'unsafe archive path'):
                self.checker.payload_path(name)

    def test_wrong_architecture_elf_is_rejected_even_with_amd64_control(self):
        binary = self.stage / 'usr/bin/example'
        binary.parent.mkdir(parents=True)
        header = bytearray(64)
        header[:7] = b'\x7fELF\x02\x01\x01'
        header[18:20] = (183).to_bytes(2, 'little')  # EM_AARCH64
        binary.write_bytes(header)
        self.write_checksums()
        problems, _ = self.verify()
        self.assertIn('ELF file is not amd64: usr/bin/example', problems)

    def test_duplicate_control_entries_are_rejected(self):
        def duplicate(archive):
            member = tarfile.TarInfo('./postinst')
            member.mode = 0o755
            script = b'#!/bin/sh\nexit 0\n'
            member.size = len(script)
            archive.addfile(member, io.BytesIO(script))
        self.rewrite_archive('--ctrl-tarfile', duplicate)
        problems, _ = self.checker.check(self.package)
        self.assertIn('duplicate control path: postinst', problems)

    def test_payload_cannot_traverse_a_symlink_parent(self):
        (self.stage / 'etc/whitecube/alias').symlink_to('config')
        def append_child(archive):
            member = tarfile.TarInfo('./etc/whitecube/alias/child')
            member.mode = 0o644
            archive.addfile(member, io.BytesIO())
        self.rewrite_archive('--fsys-tarfile', append_child)
        problems, _ = self.checker.check(self.package)
        self.assertIn('payload parent is not a directory: etc/whitecube/alias/child through etc/whitecube/alias', problems)

    def compile_core(self, flags):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('a C compiler is required for ELF hardening fixtures')
        binary = self.stage / 'usr/bin/whitecube-compositor'
        binary.parent.mkdir(parents=True)
        subprocess.run([compiler, '-x', 'c', '-o', str(binary), *flags, '-'],
                       input='int main(void) { return 0; }\n', text=True, capture_output=True, check=True)
        binary.chmod(0o755)
        self.write_checksums()

    def test_hardened_native_executable_passes_and_is_in_the_manifest(self):
        self.compile_core(['-fPIE', '-pie', '-Wl,-z,relro,-z,now,-z,noexecstack'])
        problems, summary = self.verify()
        self.assertEqual(problems, [])
        self.assertEqual(summary['hardened_executables'], ['usr/bin/whitecube-compositor'])

    def test_unhardened_native_executable_is_rejected(self):
        self.compile_core(['-fno-pie', '-no-pie', '-Wl,-z,norelro,-z,lazy,-z,execstack'])
        problems, _ = self.verify()
        self.assertIn('native executable is not PIE: usr/bin/whitecube-compositor', problems)
        self.assertIn('native executable lacks full RELRO: usr/bin/whitecube-compositor', problems)
        self.assertIn('native executable lacks a non-executable stack: usr/bin/whitecube-compositor', problems)

    def test_external_symlink_cannot_escape_installed_root(self):
        (self.config.parent / 'bad').symlink_to('../../../outside')
        problems, _ = self.verify()
        self.assertIn('symlink escapes installed root: etc/whitecube/bad', problems)

    def test_absolute_symlink_must_resolve_in_the_package_instead_of_the_host(self):
        (self.config.parent / 'bad').symlink_to('/etc/passwd')
        problems, _ = self.verify()
        self.assertIn('dangling symlink: etc/whitecube/bad -> etc/passwd', problems)
        (self.config.parent / 'bad').unlink()
        (self.config.parent / 'good').symlink_to('/etc/whitecube/config')
        problems, _ = self.verify()
        self.assertEqual(problems, [])

    def test_symlink_cycles_are_rejected(self):
        (self.config.parent / 'cycle-a').symlink_to('cycle-b')
        (self.config.parent / 'cycle-b').symlink_to('cycle-a')
        problems, _ = self.verify()
        self.assertIn('cyclic symlink: etc/whitecube/cycle-a', problems)

    def test_real_dpkg_unpack_preserves_standalone_packages_and_registers_own_payload(self):
        problems, _ = self.verify()
        self.assertEqual(problems, [])
        result = self.checker.smoke_unpack(self.package)
        self.assertEqual(result['unpack_smoke'], 'passed')
        self.assertEqual(result['coexistence_smoke'], {
            'verification': 'passed', 'packages': ['chrootables', 'cube-browser'],
            'preserved_paths': [
                '/usr/bin/chrootable-manager', '/usr/bin/cube-browser',
                '/usr/share/applications/cube-browser.desktop',
                '/usr/share/icons/hicolor/scalable/apps/cube-browser.svg',
            ],
        })

    def test_real_dpkg_rejects_each_legacy_shared_application_path(self):
        for relative in ('usr/bin/chrootable-manager', 'usr/bin/cube-browser',
                         'usr/share/applications/cube-browser.desktop',
                         'usr/share/icons/hicolor/scalable/apps/cube-browser.svg'):
            with self.subTest(path=relative):
                target = self.stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'conflicting Whitecube payload\n')
                target.chmod(0o644)
                self.write_checksums()
                problems, _ = self.verify()
                self.assertEqual(problems, [])
                with self.assertRaisesRegex(RuntimeError, 'trying to overwrite'):
                    self.checker.smoke_unpack(self.package)
                target.unlink()
        self.write_checksums()

    def test_coexistence_check_rejects_takeover_even_if_replaces_permits_unpack(self):
        target = self.stage / 'usr/bin/cube-browser'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'#!/bin/sh\nexit 0\n')
        target.chmod(0o755)
        self.control.write_text(self.control.read_text() + 'Replaces: cube-browser\n')
        self.write_checksums()
        problems, _ = self.verify()
        self.assertEqual(problems, [])
        with self.assertRaisesRegex(RuntimeError, 'replaced standalone fixture content: /usr/bin/cube-browser'):
            self.checker.smoke_unpack(self.package)

    def test_coexistence_check_verifies_dpkg_owner_even_when_original_bytes_are_identical(self):
        target = self.stage / 'usr/bin/cube-browser'
        target.parent.mkdir(parents=True)
        target.write_bytes(self.checker.STANDALONE_FILES['cube-browser']['/usr/bin/cube-browser'])
        target.chmod(0o755)
        self.control.write_text(self.control.read_text() + 'Replaces: cube-browser\n')
        self.write_checksums()
        problems, _ = self.verify()
        self.assertEqual(problems, [])
        with self.assertRaisesRegex(RuntimeError, 'changed standalone ownership of /usr/bin/cube-browser'):
            self.checker.smoke_unpack(self.package)

    def test_rootless_unpack_refuses_packages_that_would_execute_a_preinst(self):
        script = self.stage / 'DEBIAN/preinst'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o755)
        self.verify()
        with self.assertRaisesRegex(RuntimeError, 'without preinst'):
            self.checker.smoke_unpack(self.package)


class ABIDependencies(unittest.TestCase):
    def test_real_elf_gets_distribution_symbol_version_minima(self):
        spec = importlib.util.spec_from_file_location('whitecube_dependency_versions', ROOT / 'packaging/dependency-versions.py')
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            destination = stage / 'usr/bin'
            destination.mkdir(parents=True)
            shutil.copyfile('/usr/bin/true', destination / 'example')
            dependencies = scanner.discover(stage)
            self.assertTrue(any(value.startswith('libc6 (>= ') for value in dependencies), dependencies)
            (destination / 'example').unlink()
            with self.assertRaisesRegex(RuntimeError, 'no ELF'):
                scanner.discover(stage)

    def test_excluded_private_provider_is_removed_without_losing_libc_minimum(self):
        spec = importlib.util.spec_from_file_location('whitecube_excluded_dependency_versions', ROOT / 'packaging/dependency-versions.py')
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('a C compiler is required for the private-provider fixture')
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            binary = stage / 'usr/bin/example'
            binary.parent.mkdir(parents=True)
            subprocess.run([compiler, '-x', 'c', '-o', str(binary), '-', '-lz'],
                           input='extern const char *zlibVersion(void); int main(void) { return zlibVersion()[0] == 0; }\n',
                           text=True, capture_output=True, check=True)
            output = subprocess.check_output(['ldd', str(binary)], text=True)
            source = next(line.split()[2] for line in output.splitlines() if line.strip().startswith('libz.so.1 =>'))
            private = stage / 'usr/lib/whitecube'
            private.mkdir(parents=True)
            shutil.copyfile(source, private / 'libz.so.1')
            original = scanner.discover(stage)
            self.assertTrue(any(value.startswith('zlib1g') for value in original), original)
            excluded = scanner.discover(stage, excluded_packages=('zlib1g',))
            self.assertFalse(any(value.startswith('zlib1g') for value in excluded), excluded)
            self.assertTrue(any(value.startswith('libc6 (>= ') for value in excluded), excluded)


class OptionalSettingsTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader('whitecube_optional_settings_helper', str(ROOT / 'libexec/cube-settings-helper'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def test_missing_optional_read_tools_return_an_actionable_result(self):
        with patch.object(self.helper.os, 'access', return_value=False), patch.object(self.helper.subprocess, 'run') as execute:
            for command, package in self.helper.OPTIONAL_COMMANDS.items():
                result = self.helper.run(command, '--status')
                self.assertEqual(result.returncode, 127)
                self.assertEqual(result.stdout, '')
                self.assertIn('install ' + package, result.stderr)
            execute.assert_not_called()

    def test_missing_optional_write_tool_fails_before_execution(self):
        with patch.object(self.helper.os, 'access', return_value=False), patch.object(self.helper.subprocess, 'run') as execute:
            with self.assertRaises(SystemExit):
                self.helper.run('/usr/bin/wpctl', 'set-volume', 'sink', '0.5', check=True)
            execute.assert_not_called()

    def test_console_keyboard_needs_setupcon_before_changing_configuration(self):
        with patch.object(self.helper, 'require_root'), patch.object(self.helper.os, 'access', return_value=False), patch.object(self.helper, 'read') as read:
            with self.assertRaises(SystemExit):
                self.helper.console_keyboard('us', '')
            read.assert_not_called()

    def test_optional_packages_are_declared_and_checker_references_are_narrow(self):
        control = (ROOT / 'packaging/debian/control.in').read_text()
        optional = '\n'.join(line for line in control.splitlines() if line.startswith(('Suggests:', 'Recommends:')))
        for package in self.helper.OPTIONAL_COMMANDS.values():
            self.assertIn(package, optional)
        spec = importlib.util.spec_from_file_location('whitecube_path_checker_optional', ROOT / 'packaging/check-tree.py')
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        self.assertEqual(checker.EXTERNAL_REFERENCES['usr/libexec/whitecube/cube-settings-helper'], set(self.helper.OPTIONAL_COMMANDS))


if __name__ == '__main__':
    unittest.main(verbosity=2)
