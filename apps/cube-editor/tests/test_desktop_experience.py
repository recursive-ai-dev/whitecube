"""Desktop preferences and keyboard workflow checks; never launch a worker."""
import argparse
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
except (ImportError, ValueError):
    app = None


@unittest.skipIf(app is None, 'GTK bindings unavailable')
class PreferencesTests(unittest.TestCase):
    def test_corrupt_or_out_of_range_preferences_keep_usable_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'preferences.json'
            with mock.patch.object(app, 'preferences_path', return_value=path):
                path.write_text('{broken')
                self.assertEqual(app.load_preferences()['theme'], 'system')
                path.write_text(json.dumps({'width': True, 'height': 1, 'stage': 'unknown', 'theme': 'unknown'}))
                self.assertEqual(app.load_preferences()['width'], 1180)
                self.assertEqual(app.load_preferences()['height'], 820)
                self.assertEqual(app.load_preferences()['stage'], 'source')

    def test_xdg_config_home_is_respected(self):
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': '/tmp/cube-config-fixture'}):
            self.assertEqual(app.preferences_path(), Path('/tmp/cube-config-fixture/cube-editor/preferences.json'))


@unittest.skipIf(app is None, 'GTK bindings unavailable')
class DesktopWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not any(os.environ.get(key) for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'BROADWAY_DISPLAY')) or not app.Gtk.init_check():
            raise unittest.SkipTest('A GTK display is required')
        app.check_runtime()
        cls.application = app.EditorApp(argparse.Namespace(project=None, page=None, screenshot=None))
        cls.application.set_application_id('org.whitecube.CubeEditor.DesktopTests')
        cls.application.register(None)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        config = mock.patch.object(app, 'preferences_path', return_value=self.path / 'preferences.json')
        config.start(); self.addCleanup(config.stop)
        home = mock.patch('engine.Path.home', return_value=self.path / 'home')
        home.start(); self.addCleanup(home.stop)
        self.window = app.EditorWindow(self.application)
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        if self.window.save_timer:
            app.GLib.source_remove(self.window.save_timer)
        if self.window.release_monitor:
            self.window.release_monitor.cancel()
        self.window.destroy()

    def fixture_project(self):
        source = self.path / 'source.iso'; source.write_bytes(b'UI-only fixture')
        project = app.engine.Project.create(source, 'UI project', self.path / 'work', self.path / 'out')
        self.window.load_project(project.directory)
        return self.window.project

    def test_palette_filters_words_and_runs_selected_command_with_enter(self):
        self.window.palette()
        search = self.window.palette_search
        search.set_text('release build'); search.emit('search-changed')
        row = self.window.palette_list.get_first_child()
        self.assertEqual(row.get_child().get_label(), 'Build release image')
        self.assertFalse(row.get_child().get_sensitive())
        search.set_text('unfindable command'); search.emit('search-changed')
        self.assertTrue(self.window.palette_empty.get_visible())
        self.assertIsNone(self.window.palette_list.get_first_child())
        search.set_text('go activity'); search.emit('search-changed')
        search.emit('activate')
        self.assertEqual(self.window.current_stage, 'activity')

    def test_preferences_survive_a_new_window(self):
        self.window.set_default_size(940, 740)
        self.window.navigate('files')
        self.window.preferences['theme'] = 'dark'
        self.window.persist_preferences()
        other = app.EditorWindow(self.application)
        try:
            self.assertEqual(other.get_default_size(), (940, 740))
            self.assertEqual(other.current_stage, 'files')
            self.assertEqual(other.preferences['theme'], 'dark')
        finally:
            other.destroy()

    def test_project_overview_tracks_readiness_and_recovery(self):
        project = self.fixture_project()
        self.assertIn('Inspect the source', self.window.overview_next.get_text())
        project.save(inspection={'can_rebuild': True})
        self.window.refresh_buttons()
        self.assertIn('Extract', self.window.overview_next.get_text())
        (project.directory / '.extracted').touch()
        self.window.refresh_buttons()
        self.assertIn('Apply your recipe', self.window.overview_next.get_text())
        project.save(state='error')
        self.window.refresh_buttons()
        self.assertIn('recovery', self.window.overview_next.get_text())
        self.window.set_busy(True)
        self.assertIn('Operation in progress', self.window.overview_next.get_text())

    def test_appearance_dialog_and_shortcuts_open_and_change_theme(self):
        self.window.show_preferences()
        dialog = self.window.get_visible_dialog()
        self.assertEqual(dialog.get_title(), 'Appearance & preferences')
        content = dialog.get_child().get_content()
        child = content.get_first_child()
        while child and not isinstance(child, app.Gtk.DropDown):
            child = child.get_next_sibling()
        child.set_selected(2)
        self.assertTrue(app.Adw.StyleManager.get_default().get_dark())
        self.assertEqual(app.load_preferences()['theme'], 'dark')
        dialog.close()
        self.window.show_shortcuts()
        self.assertEqual(self.window.get_visible_dialog().get_title(), 'Keyboard shortcuts')
        self.window.get_visible_dialog().close()
        self.window.show_about()
        self.assertIsInstance(self.window.get_visible_dialog(), app.Adw.AboutDialog)
        self.application.apply_theme('light')

    def test_system_diagnostics_work_before_a_project_is_opened(self):
        import diagnostics
        report = {'ready': True, 'checks': [{'status': 'pass', 'label': 'Fixture', 'detail': 'Ready'}]}
        with mock.patch.object(diagnostics, 'diagnose', return_value=report):
            self.window.show_diagnostics()
            deadline = time.monotonic() + 3
            context = app.GLib.MainContext.default()
            while self.window.busy and time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                time.sleep(.01)
            self.assertFalse(self.window.busy)
            self.assertEqual(self.window.get_visible_dialog().get_title(), 'System diagnostics')


if __name__ == '__main__':
    unittest.main()
