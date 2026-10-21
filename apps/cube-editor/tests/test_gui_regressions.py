"""GTK regressions for project changes, recipes and release provenance.

Run under a private GTK display for full coverage. Ordinary unittest discovery
skips the widget tests when no display or GTK bindings are available. All data
is temporary; these tests never start a privileged worker.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import app
except (ImportError, ValueError) as exc:
    app = None
    GTK_UNAVAILABLE = str(exc)


@unittest.skipIf(app is None, 'GTK/libadwaita bindings unavailable')
class RuntimeTests(unittest.TestCase):
    def test_old_runtime_exits_with_an_actionable_error(self):
        stderr = io.StringIO()
        with mock.patch.object(app.Adw, 'get_major_version', return_value=1), \
             mock.patch.object(app.Adw, 'get_minor_version', return_value=4), \
             mock.patch.object(sys, 'argv', ['cube-editor']), contextlib.redirect_stderr(stderr):
            self.assertEqual(app.main(), 2)
        self.assertIn('requires libadwaita 1.5', stderr.getvalue())
        self.assertIn('gir1.2-adw-1', stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())

    def test_minimum_runtime_is_accepted(self):
        with mock.patch.object(app.Adw, 'get_major_version', return_value=1), \
             mock.patch.object(app.Adw, 'get_minor_version', return_value=5):
            app.check_runtime()


@unittest.skipIf(app is None, 'GTK/libadwaita bindings unavailable')
class GuiRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not any(os.environ.get(key) for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'BROADWAY_DISPLAY')):
            raise unittest.SkipTest('No GTK display; run on a private Broadway or Wayland display')
        if not app.Gtk.init_check():
            raise unittest.SkipTest('GTK could not open the display')
        try:
            app.check_runtime()
        except app.engine.CubeError as exc:
            raise unittest.SkipTest(str(exc))
        cls.application = app.EditorApp(argparse.Namespace(project=None, page=None, screenshot=None))
        cls.application.register(None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cube-gui-regression-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        home = mock.patch('engine.Path.home', return_value=self.path / 'home')
        home.start(); self.addCleanup(home.stop)
        self.source = self.path / 'source.iso'
        self.source.write_bytes(b'GUI fixture, never executed')
        self.project = app.engine.Project.create(self.source, 'Original', self.path / 'projects', self.path / 'releases')
        self.window = app.EditorWindow(self.application, str(self.project.directory))
        self.assertIsNotNone(self.window.project)
        self.project = self.window.project

    def tearDown(self):
        if self.window.save_timer:
            app.GLib.source_remove(self.window.save_timer)
            self.window.save_timer = None
        if self.window.release_monitor:
            self.window.release_monitor.cancel()
        self.window.destroy()
        self.pump()

    @staticmethod
    def pump():
        context = app.GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.pump()
            time.sleep(.01)
        self.assertTrue(predicate())

    def recipe_file(self, recipe):
        path = self.path / 'recipe.json'
        path.write_text(json.dumps({'format': 'cube-editor-recipe', 'version': 1, 'recipe': recipe}))
        return path

    def test_import_preserves_pending_unrelated_edits_without_waiting_for_autosave(self):
        w = self.window
        w.entries['name'].set_text('Pending name')
        w.entries['volume_id'].set_text('PENDING_LABEL')
        w.entries['payload_destination'].set_text('/etc/pending-overlay')
        w.import_recipe(self.recipe_file({'install_packages': 'curl git'}))
        self.assertEqual(w.entries['name'].get_text(), 'Pending name')
        self.assertEqual(w.entries['volume_id'].get_text(), 'PENDING_LABEL')
        self.assertEqual(w.entries['payload_destination'].get_text(), '/etc/pending-overlay')
        saved = app.engine.Project.load(self.project.directory).data
        self.assertEqual(saved['name'], 'Pending name')
        self.assertEqual(saved['volume_id'], 'PENDING_LABEL')
        self.assertEqual(w.recipe()['install_packages'], ['curl', 'git'])

    def test_invalid_import_leaves_pending_edits_and_recipe_untouched(self):
        w = self.window
        w.entries['name'].set_text('Pending name')
        w.entries['install_packages'].set_text('git')
        w.import_recipe(self.recipe_file({'install_packages': '--unsafe'}))
        self.assertEqual(w.entries['name'].get_text(), 'Pending name')
        self.assertEqual(w.recipe()['install_packages'], ['git'])

    def test_recipe_hydration_preserves_text_and_array_package_meaning(self):
        for packages in ('curl git', ['curl', 'git']):
            with self.subTest(packages=packages):
                self.window.hydrate_recipe({'install_packages': packages, 'remove_packages': 'nano'})
                self.assertEqual(self.window.entries['install_packages'].get_text(), 'curl git')
                self.assertEqual(self.window.recipe()['install_packages'], ['curl', 'git'])
                self.assertEqual(self.window.recipe()['remove_packages'], ['nano'])

    def show_old_root(self):
        inspection = {'squashfs_paths': ['/casper/old.squashfs'], 'squashfs_path': '/casper/old.squashfs',
                      'layer_chain': ['/casper/old.squashfs']}
        self.project.save(inspection=inspection)
        self.window.show_inspection(inspection)

    def test_empty_inspection_clears_previous_choices(self):
        self.show_old_root()
        self.window.show_inspection({'squashfs_paths': [], 'layer_chain': []})
        self.assertFalse(self.window.root_selector_box.get_visible())
        self.assertEqual(self.window.root_selector.get_model().get_n_items(), 0)
        self.assertIsNone(self.window.root_selector.get_selected_item())

    def test_uninspected_project_clears_previous_inspection(self):
        self.show_old_root()
        other = app.engine.Project.create(self.source, 'Other', self.path / 'projects', self.path / 'releases')
        self.window.load_project(other.directory)
        self.assertEqual(self.window.project.directory, other.directory)
        self.assertFalse(self.window.root_selector_box.get_visible())
        self.assertEqual(self.window.root_selector.get_model().get_n_items(), 0)
        self.assertNotIn('old.squashfs', self.window.inspect_label.get_text())

    def test_stale_root_selection_cannot_be_saved(self):
        self.show_old_root()
        self.project.save(inspection={})
        with mock.patch.object(self.window, 'inspect_project') as inspect:
            self.window.select_root()
        inspect.assert_not_called()
        self.assertEqual(self.project.data['squashfs_path'], '')
        self.assertFalse(self.window.root_selector_box.get_visible())

    def test_live_root_selector_respects_busy_and_extracted_state(self):
        self.show_old_root()
        self.assertTrue(self.window.root_selector.get_sensitive())
        self.window.set_busy(True)
        self.assertFalse(self.window.root_selector.get_sensitive())
        self.window.show_inspection(self.project.data['inspection'])
        self.assertFalse(self.window.root_selector.get_sensitive())
        self.window.set_busy(False)
        self.assertTrue(self.window.root_selector.get_sensitive())
        (self.project.directory / '.extracted').touch()
        self.window.set_busy(True)
        self.window.set_busy(False)
        self.assertFalse(self.window.root_selector.get_sensitive())

    def test_failed_candidate_validation_keeps_open_project(self):
        invalid = self.path / 'invalid'
        invalid.mkdir()
        (invalid / 'cube-editor.json').write_text('{"schema": 1}')
        original_worker = self.window.worker
        self.window.load_project(invalid)
        self.assertIs(self.window.project, self.project)
        self.assertIs(self.window.worker, original_worker)
        self.assertEqual(self.window.entries['name'].get_text(), 'Original')
        self.assertFalse(self.window.hydrating)

    def test_lzo_project_opens_and_preserves_compression(self):
        other = app.engine.Project.create(self.source, 'LZO', self.path / 'projects', self.path / 'releases')
        other.save(compression='lzo')
        self.window.load_project(other.directory)
        self.assertEqual(self.window.project.directory, other.directory)
        self.assertEqual(self.window.resource_settings()['compression'], 'lzo')
        self.assertTrue(self.window.save_project())
        self.assertEqual(app.engine.Project.load(other.directory).data['compression'], 'lzo')

    def test_failed_candidate_read_keeps_open_project(self):
        other = app.engine.Project.create(self.source, 'Other', self.path / 'projects', self.path / 'releases')
        with mock.patch('engine.recovery_report', side_effect=OSError('cannot inspect candidate')):
            self.window.load_project(other.directory)
        self.assertIs(self.window.project, self.project)
        self.assertEqual(self.window.entries['name'].get_text(), 'Original')
        self.assertFalse(self.window.hydrating)

    def test_failed_candidate_widget_hydration_restores_previous_project(self):
        other = app.engine.Project.create(self.source, 'Other', self.path / 'projects', self.path / 'releases')
        hydrate_recipe = self.window.hydrate_recipe
        def fail_candidate(recipe):
            if self.window.project.directory == other.directory:
                raise RuntimeError('simulated widget failure')
            hydrate_recipe(recipe)
        with mock.patch.object(self.window, 'hydrate_recipe', side_effect=fail_candidate):
            self.window.load_project(other.directory)
        self.assertIs(self.window.project, self.project)
        self.assertEqual(self.window.entries['name'].get_text(), 'Original')
        self.assertFalse(self.window.hydrating)

    def test_hydration_flags_survive_widget_failure_and_nested_inspection(self):
        with mock.patch.object(self.window, 'refresh_payloads', side_effect=RuntimeError('widget failure')):
            with self.assertRaises(RuntimeError):
                self.window.hydrate()
        self.assertFalse(self.window.hydrating)
        self.window.hydrating = True
        self.window.show_inspection({})
        self.assertTrue(self.window.hydrating)
        self.window.hydrating = False

    def release_fixture(self):
        release = self.project.output_path
        release.parent.mkdir(exist_ok=True)
        release.write_bytes(b'first image')
        digest = hashlib.sha256(release.read_bytes()).hexdigest()
        self.project.save(release_checks=[{'path': str(release), 'kind': 'BIOS live', 'outcome': 'passed',
                                         'sha256': digest, 'checked_at': '2026-10-15T12:00:00Z', 'note': ''}])
        self.window.update_release_display()
        return release, digest

    def test_recorded_checks_start_historical_until_current_digest_is_known(self):
        release, digest = self.release_fixture()
        self.assertIn('Historical · BIOS live: passed', self.window.history_summary.get_text())
        self.assertNotIn('Current · BIOS live: passed', self.window.history_summary.get_text())
        self.window.remember_release_digest(release, digest, self.window.release_identity(release))
        self.assertIn('Current · BIOS live: passed', self.window.history_summary.get_text())

    def test_replacing_release_preserving_size_and_mtime_invalidates_current_checks(self):
        release, digest = self.release_fixture()
        identity = self.window.release_identity(release)
        self.window.remember_release_digest(release, digest, identity)
        replacement = self.path / 'replacement.iso'
        replacement.write_bytes(b'other image')
        os.utime(replacement, ns=(identity[3], identity[3]))
        os.replace(replacement, release)
        self.window.update_release_display()
        self.assertIsNone(self.window.verified_release)
        self.assertIn('Historical · BIOS live: passed', self.window.history_summary.get_text())
        self.assertNotIn('Current · BIOS live: passed', self.window.history_summary.get_text())

    def test_monitor_invalidates_checks_when_selected_release_changes_on_disk(self):
        release, digest = self.release_fixture()
        self.window.remember_release_digest(release, digest, self.window.release_identity(release))
        release.write_bytes(b'changed image')
        self.wait_until(lambda: self.window.verified_release is None)
        self.assertNotIn('Current · BIOS live: passed', self.window.history_summary.get_text())

    def test_selection_change_discards_cached_digest(self):
        release, digest = self.release_fixture()
        self.window.remember_release_digest(release, digest, self.window.release_identity(release))
        self.window.selected_release = self.path / 'another.iso'
        self.window.update_release_display()
        self.window.selected_release = None
        self.window.update_release_display()
        self.assertIsNone(self.window.verified_release)
        self.assertNotIn('Current · BIOS live: passed', self.window.history_summary.get_text())

    def test_mutation_between_hash_and_completion_does_not_promote_checks(self):
        release, digest = self.release_fixture()
        identity = self.window.release_identity(release)
        release.write_bytes(b'changed image')
        self.window.remember_release_digest(release, digest, identity)
        self.assertIsNone(self.window.verified_release)

    def test_checksum_verification_promotes_only_matching_hash_checks(self):
        release, digest = self.release_fixture()
        Path(str(release) + '.sha256').write_text(f'{digest}  {release.name}\n')
        self.window.verify_release()
        self.wait_until(lambda: not self.window.busy)
        self.assertIn('Current · BIOS live: passed', self.window.history_summary.get_text())
        self.assertIn('Current · checksum: passed', self.window.history_summary.get_text())


if __name__ == '__main__':
    unittest.main()
