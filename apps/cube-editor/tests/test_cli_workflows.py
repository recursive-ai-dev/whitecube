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
        self.assertEqual(result.stdout.strip(), 'cube-editor 0.5.0')

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
