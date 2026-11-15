"""Exercise installed-style isolated Python CLI imports and exit statuses."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from test_cube_editor import ProjectFixture


class CliWorkflowTests(ProjectFixture):
    def cli(self, *arguments, environment=None):
        return subprocess.run([sys.executable, '-I', engine.__file__, *map(str, arguments)],
                              capture_output=True, text=True, timeout=15,
                              env=environment)

    def test_version_does_not_need_a_display_or_gtk(self):
        result = self.cli('--version')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'cube-editor {engine.VERSION}')

    def test_doctor_reports_failure_in_json_without_launching_any_worker(self):
        environment = dict(os.environ, CUBE_EDITOR_TERMINAL='no-such-cube-terminal')
        result = self.cli('doctor', self.project.directory, environment=environment)
        self.assertEqual(result.returncode, 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value['ready'])
        self.assertTrue(any(check['id'] == 'tool-terminal' for check in value['checks']))
        self.assertNotIn('Traceback', result.stderr)

    def test_report_failures_use_friendly_errors_from_shared_engine_module(self):
        for filename in ('report.html', 'invalid.json'):
            with self.subTest(filename=filename):
                result = self.cli('report', self.project.directory, self.path / filename)
                self.assertEqual(result.returncode, 1)
                self.assertIn('cube-editor:', result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_report_verification_controls_exit_status_and_exports_evidence(self):
        output = self.project.output_path
        output.parent.mkdir()
        output.write_bytes(b'CLI synthetic release')
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        Path(str(output) + '.sha256').write_text(digest + '  ' + output.name + '\n')
        for index, expected_status in enumerate((0, 1)):
            with self.subTest(expected_status=expected_status):
                report = self.path / f'report-{index}.html'
                result = self.cli('report', self.project.directory, report)
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual(json.loads(result.stdout)['verification']['success'], not expected_status)
                self.assertTrue(report.is_file())
                output.write_bytes(b'changed release')

    def test_presets_recent_and_strict_doctor_cli(self):
        presets = self.cli('presets')
        self.assertEqual(presets.returncode, 0, presets.stderr)
        preset_list = json.loads(presets.stdout)
        self.assertTrue(any(item['name'] == 'Developer kit' and 'git' in item['install_packages'] for item in preset_list))

        recent_env = {**os.environ, 'XDG_CONFIG_HOME': str(self.path / 'home/.config')}
        recent = self.cli('recent', environment=recent_env)
        self.assertEqual(recent.returncode, 0, recent.stderr)
        recent_items = json.loads(recent.stdout)
        self.assertIsInstance(recent_items, list)

        bad_doctor = self.cli('doctor', '--strict', self.project.directory,
                              environment={**os.environ, 'CUBE_EDITOR_TERMINAL': 'foot', 'WAYLAND_DISPLAY': 'wayland-0'})
        # On a headless CI/project without extracted rootfs/restore/etc., --strict exits 1 on warnings or failures.
        parsed = json.loads(bad_doctor.stdout)
        self.assertEqual(bad_doctor.returncode, 1 if (not parsed['ready'] or any(c['status'] == 'warning' for c in parsed['checks'])) else 0)

    def test_qemu_command_cli_and_input_hardening(self):
        from unittest import mock
        output = self.project.output_path
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b'synthetic release')
        result = self.cli('qemu-command', self.project.directory, '--memory', '2048')
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            self.assertIn('qemu-system-x86_64', payload['command'])
            self.assertIn('2048', payload['command'])
            self.assertIn(str(output), payload['shell'])
        else:
            self.assertIn('qemu-system-x86', result.stderr)

        api = engine.Engine(self.project)
        with mock.patch('engine.shutil.which', return_value='/usr/bin/qemu-system-x86_64'):
            with self.assertRaisesRegex(engine.CubeError, 'integer'):
                api.qemu_command(memory=True)
            bad_iso = self.path / 'bad,iso.iso'
            bad_iso.write_bytes(b'iso')
            with self.assertRaisesRegex(engine.CubeError, 'commas'):
                api.qemu_command(iso=bad_iso)

        fifo = self.path / 'recipe.fifo'
        os.mkfifo(fifo)
        with self.assertRaisesRegex(engine.CubeError, 'regular file'):
            engine.import_recipe(fifo)
        with self.assertRaisesRegex(engine.CubeError, 'regular file'):
            engine.Project._read(fifo)

        oversized = self.path / 'huge.json'
        oversized.write_bytes(b'{}' + b' ' * (engine.MAX_JSON_BYTES + 16))
        with self.assertRaisesRegex(engine.CubeError, 'byte limit'):
            engine.import_recipe(oversized)

    def test_whitecube_launcher_supports_headless_version_and_cli(self):
        launcher = Path(engine.__file__).resolve().parents[2] / 'bin/cube-editor'
        if not launcher.is_file():
            self.skipTest('repo launcher unavailable in standalone package')
        env = {k: v for k, v in os.environ.items() if k not in ('WAYLAND_DISPLAY', 'DISPLAY')}
        ver = subprocess.run([str(launcher), '--version'], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(ver.returncode, 0, ver.stderr)
        self.assertEqual(ver.stdout.strip(), f'cube-editor {engine.VERSION}')
        presets = subprocess.run([str(launcher), '--cli', 'presets'], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(presets.returncode, 0, presets.stderr)
        self.assertTrue(json.loads(presets.stdout))
