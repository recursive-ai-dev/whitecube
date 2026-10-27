"""Read-only diagnostics and actual-content release report regression tests."""
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
import engine
from test_cube_editor import ProjectFixture


class DiagnosticsTests(ProjectFixture):
    def mock_bindings(self, version=(1, 5, 0)):
        gi, gtk, adwaita = mock.Mock(), mock.Mock(), mock.Mock()
        for library, numbers in ((gtk, (4, 14, 0)), (adwaita, version)):
            library.get_major_version.return_value = numbers[0]
            library.get_minor_version.return_value = numbers[1]
            library.get_micro_version.return_value = numbers[2]
        bindings = {'gi': gi, 'gi.repository.Gtk': gtk, 'gi.repository.Adw': adwaita}
        return bindings

    def test_runtime_checks_versions_without_initializing_gtk_or_a_display(self):
        bindings = self.mock_bindings()
        with mock.patch('diagnostics.importlib.import_module', side_effect=bindings.__getitem__):
            versions = diagnostics._gtk_runtime()
        self.assertEqual(versions['adwaita'], [1, 5, 0])
        self.assertEqual(versions['gtk'], [4, 14, 0])
        bindings['gi'].require_version.assert_any_call('Gtk', '4.0')
        bindings['gi'].require_version.assert_any_call('Adw', '1')
        bindings['gi.repository.Gtk'].init.assert_not_called()
        bindings['gi.repository.Adw'].init.assert_not_called()
        bindings['gi.repository.Gtk'].Window.assert_not_called()

    def test_diagnostics_rejects_old_adwaita_even_when_gi_package_exists(self):
        bindings = self.mock_bindings((1, 4, 4))
        with mock.patch('diagnostics.importlib.util.find_spec', return_value=object()), \
                mock.patch('engine.dependencies', return_value=[]), \
                mock.patch('diagnostics.importlib.import_module', side_effect=bindings.__getitem__):
            result = diagnostics.diagnose()
        runtime = next(check for check in result['checks'] if check['id'] == 'gtk-runtime')
        self.assertFalse(result['ready'])
        self.assertEqual(runtime['status'], 'fail')
        self.assertIn('found 1.4.4', runtime['detail'])

    def test_diagnostics_rejects_missing_typelib_even_when_gi_package_exists(self):
        gi = mock.Mock()
        gi.require_version.side_effect = ValueError('Namespace Adw not available')
        with mock.patch('diagnostics.importlib.util.find_spec', return_value=object()), \
                mock.patch('engine.dependencies', return_value=[]), \
                mock.patch('diagnostics.importlib.import_module', return_value=gi):
            result = diagnostics.diagnose()
        runtime = next(check for check in result['checks'] if check['id'] == 'gtk-runtime')
        self.assertFalse(result['ready'])
        self.assertEqual(runtime['status'], 'fail')
        self.assertIn('Namespace Adw not available', runtime['detail'])

    def test_broadway_is_a_graphical_session_for_private_checks(self):
        with mock.patch.dict(os.environ, {'BROADWAY_DISPLAY': ':7'}, clear=True), \
                mock.patch('engine.dependencies', return_value=[]), \
                mock.patch('diagnostics._gtk_runtime', return_value={'gtk': [4, 14, 0], 'adwaita': [1, 5, 0]}):
            result = diagnostics.diagnose()
        self.assertEqual(result['system']['session'], 'broadway')
        self.assertEqual(next(check for check in result['checks'] if check['id'] == 'display')['status'], 'pass')

    def make_release(self):
        iso = self.project.output_path
        iso.parent.mkdir(exist_ok=True)
        iso.write_bytes(b'current release')
        digest = hashlib.sha256(iso.read_bytes()).hexdigest()
        Path(str(iso) + '.sha256').write_text(digest + '  ' + iso.name + '\n')
        release = {'path': str(iso), 'sha256': digest, 'size_bytes': iso.stat().st_size,
                   'completed_at': engine.utcnow(), 'source_iso': str(self.source),
                   'source_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                   'recipe': {'install_packages': ['curl']}}
        self.project.save(last_build=release, releases=[release])
        return iso, digest

    def test_diagnose_without_project_needs_no_gtk_or_process(self):
        with mock.patch('engine.dependencies', return_value=[{'name': 'required', 'purpose': 'Needed', 'available': False, 'required': True},
                                                              {'name': 'optional', 'purpose': 'Helpful', 'available': False, 'required': False}]), \
                mock.patch('diagnostics.importlib.util.find_spec', return_value=object()), \
                mock.patch('subprocess.run', side_effect=AssertionError('No subprocess needed')):
            result = diagnostics.diagnose()
        self.assertFalse(result['ready'])
        checks = {item['id']: item for item in result['checks']}
        self.assertEqual(checks['tool-required']['status'], 'fail')
        self.assertEqual(checks['tool-optional']['status'], 'warning')
        self.assertIsNone(result['project'])
        json.dumps(result)

    def test_diagnose_project_does_not_create_lock_or_save_metadata(self):
        (self.project.directory / '.operation.lock').unlink()
        before = (self.project.directory / 'cube-editor.json').read_bytes()
        with mock.patch('engine.dependencies', return_value=[]), \
                mock.patch('engine.storage_preflight', return_value={'sufficient': True, 'checks': []}):
            result = diagnostics.diagnose(self.project)
        checks = {item['id']: item for item in result['checks']}
        self.assertEqual(checks['source']['status'], 'pass')
        self.assertEqual(checks['extracted']['status'], 'warning')
        self.assertEqual(before, (self.project.directory / 'cube-editor.json').read_bytes())
        self.assertFalse((self.project.directory / '.operation.lock').exists())

    def test_diagnose_missing_source_and_pending_environment(self):
        self.source.unlink()
        (self.project.directory / '.guest-environment.json').write_text('{}')
        with mock.patch('engine.dependencies', return_value=[]), \
                mock.patch('engine.storage_preflight', side_effect=PermissionError('Cannot inspect tree')):
            result = diagnostics.diagnose(self.project)
        checks = {item['id']: item for item in result['checks']}
        self.assertFalse(result['ready'])
        self.assertEqual(checks['source']['status'], 'fail')
        self.assertEqual(checks['recovery']['status'], 'warning')
        self.assertIn('Cannot inspect tree', checks['storage']['detail'])

    def test_diagnose_active_operation_is_reported_without_taking_lock(self):
        import fcntl
        with (self.project.directory / '.operation.lock').open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch('engine.storage_preflight', return_value={'sufficient': True, 'checks': []}):
                result = diagnostics.diagnose(self.project)
            self.assertEqual(next(check for check in result['checks'] if check['id'] == 'operation')['status'], 'warning')

    def test_report_hashes_release_and_uses_saved_build_recipe(self):
        iso, digest = self.make_release()
        self.project.save(recipe={'install_packages': ['nano']})
        before = (self.project.directory / 'cube-editor.json').read_bytes()
        target = self.path / 'release.html'
        report = diagnostics.export_release_report(self.project, target)
        self.assertTrue(report['verification']['success'])
        self.assertEqual(report['verification']['actual_sha256'], digest)
        self.assertEqual(report['release']['recipe']['install_packages'], ['curl'])
        self.assertEqual((self.project.directory / 'cube-editor.json').read_bytes(), before)
        self.assertIn('Checksum verified', target.read_text())
        self.assertIn('does not rehash or authenticate the source', target.read_text())
        self.assertEqual(set(report['boot_summary'].values()), {'not tested'})
        json.dumps(report)

    def test_changed_iso_invalidates_saved_checks_and_checksum(self):
        iso, digest = self.make_release()
        engine.Engine(self.project).record_check('BIOS live', 'passed', path=iso)
        iso.write_bytes(b'changed release contents')
        report = diagnostics.release_report(self.project)
        self.assertFalse(report['verification']['success'])
        self.assertEqual(report['verification']['sidecar_status'], 'failed')
        self.assertEqual(report['verification']['recorded_status'], 'failed')
        self.assertFalse(report['checks'][0]['applies_to_current_contents'])
        self.assertEqual(report['boot_summary']['BIOS live'], 'not tested')
        self.assertNotIn('BIOS live', report['latest_checks'])

    def test_latest_matching_check_wins_and_old_hash_is_historical(self):
        iso, digest = self.make_release()
        worker = engine.Engine(self.project)
        worker.record_check('BIOS live', 'passed', 'first try', path=iso)
        worker.record_check('BIOS live', 'failed', 'second try', path=iso)
        report = diagnostics.release_report(self.project)
        self.assertEqual(report['boot_summary']['BIOS live'], 'failed')
        self.assertEqual(len(report['checks']), 2)
        self.assertEqual(report['latest_checks']['BIOS live']['note'], 'second try')

    def test_invalid_or_missing_sidecar_is_not_verified(self):
        iso, digest = self.make_release()
        sidecar = Path(str(iso) + '.sha256')
        for value in ('', 'bad checksum'):
            sidecar.write_text(value)
            report = diagnostics.release_report(self.project)
            self.assertFalse(report['verification']['success'])
            self.assertEqual(report['verification']['sidecar_status'], 'failed')
        sidecar.unlink()
        report = diagnostics.release_report(self.project)
        self.assertEqual(report['verification']['sidecar_status'], 'missing')
        self.assertFalse(report['verification']['success'])

    def test_explicit_historical_release_uses_its_metadata(self):
        iso, digest = self.make_release()
        old_record = self.project.data['last_build']
        self.project.save(last_build={})
        report = diagnostics.release_report(self.project, str(iso))
        self.assertEqual(report['release']['sha256'], digest)
        self.assertEqual(diagnostics.release_report(self.project, old_record)['verification']['actual_sha256'], digest)

    def test_report_missing_iso_fails_without_creating_target(self):
        target = self.path / 'report.html'
        with self.assertRaises(engine.CubeError):
            diagnostics.export_release_report(self.project, target)
        self.assertFalse(target.exists())

    def test_report_escapes_title_script_recipe_and_notes(self):
        iso, digest = self.make_release()
        script = '<script>alert("root")</script>'
        record = {**self.project.data['last_build'], 'recipe': {'script': script}}
        self.project.save(name='<script>name</script>', last_build=record)
        engine.Engine(self.project).record_check('BIOS live', 'passed', script, path=iso)
        target = self.path / 'report.html'
        diagnostics.export_release_report(self.project, target)
        content = target.read_text()
        self.assertNotIn('<script>', content)
        self.assertIn('&lt;script&gt;', content)
        self.assertIn('Content-Security-Policy', content)
        self.assertIn('Current ISO contents', content)

    def test_report_never_overwrites_existing_or_dangling_symlink(self):
        iso, digest = self.make_release()
        target = self.path / 'report.html'
        target.write_text('keep report')
        with self.assertRaises(engine.CubeError): diagnostics.export_release_report(self.project, target)
        self.assertEqual(target.read_text(), 'keep report')
        target.unlink()
        missing = self.path / 'missing'
        target.symlink_to(missing)
        with self.assertRaises(engine.CubeError): diagnostics.export_release_report(self.project, target)
        self.assertFalse(missing.exists())

    def test_report_publication_does_not_clobber_racing_writer(self):
        self.make_release()
        target = self.path / 'report.html'
        actual_link = os.link
        def race(source, destination):
            target.write_text('another writer')
            actual_link(source, destination)
        with mock.patch('diagnostics.os.link', side_effect=race):
            with self.assertRaises(engine.CubeError): diagnostics.export_release_report(self.project, target)
        self.assertEqual(target.read_text(), 'another writer')
        self.assertFalse(list(self.path.glob('.cube-release-report-*')))

    def test_report_rejects_metadata_iso_and_editable_paths(self):
        self.make_release()
        (self.project.rootfs / 'etc').mkdir(parents=True)
        for target in (self.source, self.project.output_path, self.project.directory / 'cube-editor.json', self.project.rootfs / 'etc/report.html'):
            with self.subTest(target=target), self.assertRaises(engine.CubeError):
                diagnostics.export_release_report(self.project, target)
        self.assertFalse((self.project.rootfs / 'etc/report.html').exists())

    def test_report_cannot_enter_symlinked_editable_directory(self):
        self.make_release()
        other = self.path / 'real-root'
        other.mkdir()
        self.project.rootfs.symlink_to(other)
        with self.assertRaises(engine.CubeError):
            diagnostics.export_release_report(self.project, other / 'report.html')
        self.assertFalse((other / 'report.html').exists())

    def test_report_cannot_create_missing_recipe_input_or_modify_payload_directory(self):
        self.make_release()
        payload = self.path / 'missing.html'
        directory = self.path / 'payload'
        directory.mkdir()
        self.project.save(recipe={'payloads': [{'source': str(payload), 'destination': '/etc/missing'},
                                              {'source': str(directory), 'destination': '/etc/config'}]})
        for target in (payload, directory / 'report.html'):
            with self.subTest(target=target), self.assertRaises(engine.CubeError):
                diagnostics.export_release_report(self.project, target)
            self.assertFalse(target.exists())

    def test_report_of_previous_release_survives_invalid_current_output_choice(self):
        iso, digest = self.make_release()
        self.project.save(output_dir=str(self.source.parent), output_name=self.source.name)
        result = diagnostics.export_release_report(self.project, self.path / 'report.html', release=iso)
        self.assertEqual(result['verification']['actual_sha256'], digest)

    def test_mutation_during_report_hash_fails(self):
        iso, digest = self.make_release()
        original = engine.sha256_file
        def mutate(path):
            result = original(path)
            path.write_bytes(b'replaced while reporting')
            return result
        with mock.patch('engine.sha256_file', side_effect=mutate), self.assertRaises(engine.CubeError):
            diagnostics.release_report(self.project)
