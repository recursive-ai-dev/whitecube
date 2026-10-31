import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt, QSettings
from chrootables.app import Window, CreateDialog, NotesDialog
from chrootables.style import STYLE


class DesktopTests(unittest.TestCase):
    def test_console_caps_unterminated_output_and_preserves_retained_selection(self):
        from PySide6.QtGui import QTextCursor
        with patch('chrootables.app.MAX_CONSOLE_CHARACTERS', 128):
            self.window.append_output('a' * 90)
            self.window.follow_output.setChecked(False)
            cursor = self.window.task_output.textCursor()
            cursor.setPosition(80)
            cursor.setPosition(85, QTextCursor.MoveMode.KeepAnchor)
            self.window.task_output.setTextCursor(cursor)
            self.window.append_output('b' * 100)
            self.assertEqual(self.window.task_output.document().characterCount() - 1, 128)
            self.assertEqual(self.window.task_output.textCursor().selectedText(), 'aaaaa')
            self.window.append_output('界' * 10000)
            self.assertEqual(self.window.task_output.toPlainText(), '界' * 128)
            self.window.follow_output.setChecked(True)
            self.window.append_output('😀' * 10000)
            self.assertLessEqual(self.window.task_output.document().characterCount() - 1, 128)
            self.assertEqual(self.window.task_output.toPlainText(), '😀' * 64)
            self.assertEqual(self.window.task_output.textCursor().position(), 128)

    def test_one_authentication_attempt_preserves_action_and_can_be_cancelled(self):
        self.enable_shared_folder_controls()
        with patch.object(self.window.client, 'start') as start:
            first = {'name': 'python-playground', 'target': 'first-copy'}
            self.window.perform('clone', first)
            first['target'] = 'changed-by-caller'
            self.window.perform('delete', {'name': 'python-playground', 'confirmed': True})
            start.assert_called_once_with(True)
            self.assertEqual(self.window.pending_action,
                             ('clone', {'name': 'python-playground', 'target': 'first-copy'}))
            self.assertTrue(self.window.authenticating)
            self.assertFalse(self.window.new_button.isEnabled())
            self.assertFalse(self.window.terminal_button.isEnabled())
            self.assertEqual(self.window.unlock_button.text(), 'Cancel authentication')
            self.assertTrue(self.window.unlock_button.isEnabled())
            self.window.unlock_button.click()
            self.assertEqual([call.args for call in start.call_args_list], [(True,), (False,)])
            self.assertFalse(self.window.authenticating)
            self.assertIsNone(self.window.pending_action)
            self.assertTrue(self.window.new_button.isEnabled())
            self.assertIn('cancelled', self.window.notice_text.text())

    def test_authenticated_pending_action_runs_once_and_controls_recover_after_failure(self):
        self.enable_shared_folder_controls()
        with patch.object(self.window.client, 'start'), patch.object(self.window, 'refresh'), \
                patch.object(self.window, 'check_health'), patch.object(self.window, 'run_operation') as operation:
            self.window.perform('clone', {'name': 'python-playground', 'target': 'copy'})
            self.window.client.writable = True
            self.window.connected(True)
            self.window.connected(True)
            operation.assert_called_once_with('clone', {'name': 'python-playground', 'target': 'copy'})
            self.assertFalse(self.window.authenticating)
            self.assertIsNone(self.window.pending_action)
            self.window.client.writable = False
            self.window.unlock()
            self.window.disconnected('Authentication failed. Unlock store to retry.')
            self.assertFalse(self.window.authenticating)
            self.assertTrue(self.window.new_button.isEnabled())
            self.assertIn('retry', self.window.notice_text.text())

    def test_failed_authentication_fallback_cannot_replace_a_new_attempt(self):
        self.enable_shared_folder_controls()
        self.window.client.requested_writable = True
        with patch.object(self.window.client, 'start') as start:
            self.window.unlock()
            self.window.disconnected('cancelled')
            self.window.unlock()
            self.app.processEvents()
            self.assertEqual([call.args for call in start.call_args_list], [(True,), (True,)])

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.signal_errors = []
        self.original_hook = sys.excepthook
        sys.excepthook = lambda *args: self.signal_errors.append(args)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = QSettings(str(Path(self.directory.name) / "desktop.ini"), QSettings.Format.IniFormat)
        with patch("chrootables.app.QSettings", return_value=self.settings):
            self.window = Window(demo=True)
        self.window.timer.stop()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        sys.excepthook = self.original_hook
        self.assertEqual(self.signal_errors, [], "Unhandled exception from a Qt callback")

    def test_search_and_filters(self):
        self.assertEqual(self.window.workspaces.count(), 4)
        QTest.keyClicks(self.window.search, "python")
        self.assertEqual(self.window.workspaces.count(), 1)
        self.assertEqual(self.window.selected, "python-playground")
        self.window.search.clear()
        self.window.filter.setCurrentIndex(2)
        self.assertEqual(self.window.workspaces.count(), 2)
        self.window.search.setText("missing")
        self.assertEqual(self.window.library_stack.currentIndex(), 1)
        self.assertEqual(self.window.empty_title.text(), "No matching workspaces.")

    def workspace_order(self):
        return [self.window.workspaces.item(row).data(Qt.ItemDataRole.UserRole)["name"]
                for row in range(self.window.workspaces.count())]

    def test_sort_modes_keep_selection_and_put_unused_workspaces_last(self):
        self.window.favorites = {"network-sandbox"}
        self.window.render_library()
        self.assertEqual(self.workspace_order()[0], "network-sandbox")
        self.window.sort.setCurrentIndex(self.window.sort.findData("name"))
        self.assertEqual(self.workspace_order(), ["build-studio", "clean-slate", "network-sandbox", "python-playground"])
        self.window.sort.setCurrentIndex(self.window.sort.findData("recent"))
        self.assertEqual(self.workspace_order(), ["build-studio", "python-playground", "clean-slate", "network-sandbox"])
        self.window.sort.setCurrentIndex(self.window.sort.findData("created"))
        self.assertEqual(self.workspace_order()[0], "clean-slate")
        for key in ("capacity", "allocated"):
            self.window.sort.setCurrentIndex(self.window.sort.findData(key))
            self.assertEqual(self.workspace_order()[0], "build-studio")
        self.assertEqual(self.window.selected, "python-playground")
        self.window.filter.setCurrentIndex(2)
        self.assertEqual(self.window.library_count.text(), "2 of 4 workspaces")

    def test_session_preferences_survive_reopen_without_folder(self):
        self.enable_shared_folder_controls()
        self.window.offline.setChecked(True)
        self.window.restricted.setChecked(True)
        self.window.save_notes("python-playground", "Project launch instructions")
        self.window.save_shared_folder("python-playground", self.shared_directory(), True)
        self.window.clear_shared_folder()
        reopened = self.another_demo_window()
        self.enable_shared_folder_controls(reopened)
        reopened.network_choices.clear()
        reopened.restricted_choices.clear()
        reopened.notes_cache.clear()
        reopened.render_library()
        self.assertTrue(reopened.offline.isChecked())
        self.assertTrue(reopened.restricted.isChecked())
        self.assertEqual(reopened.workspace_notes("python-playground"), "Project launch instructions")
        self.assertEqual(reopened.shared_folder_choice("python-playground"), ("", False))
        other_store = self.another_demo_window(Path(self.directory.name) / "other-store")
        self.enable_shared_folder_controls(other_store)
        self.assertFalse(other_store.session_choice("python-playground", "isolated"))
        self.assertEqual(other_store.workspace_notes("python-playground"), "")

    def test_notes_are_searchable_and_modal_save_keeps_original_workspace(self):
        self.enable_shared_folder_controls()
        def refresh_selection():
            self.window.selected = "clean-slate"
            self.window.render_library()
            return QDialog.DialogCode.Accepted
        with patch("chrootables.app.NotesDialog") as dialog:
            dialog.return_value.exec.side_effect = refresh_selection
            dialog.return_value.value.return_value = "Run make galaxy"
            self.window.edit_notes()
        self.assertEqual(self.window.workspace_notes("python-playground"), "Run make galaxy")
        self.assertEqual(self.window.workspace_notes("clean-slate"), "")
        self.window.search.setText("GALAXY")
        self.assertEqual(self.workspace_order(), ["python-playground"])
        self.assertEqual(self.window.notes_preview.text(), "Run make galaxy")
        with self.assertRaises(ValueError):
            self.window.save_notes("python-playground", "x" * 4001)

    def test_notes_dialog_limits_and_demo_has_no_preference_writes(self):
        dialog = NotesDialog("lab", "hello", self.window)
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.editor.setPlainText("x" * 4001)
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.editor.setPlainText(" hello ")
        self.assertEqual(dialog.value(), "hello")
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertFalse(self.window.notes_button.isEnabled())
        with patch.object(self.settings, "setValue") as write:
            self.window.save_notes("python-playground", "ignored")
            self.window.sort.setCurrentIndex(1)
            self.window.filter.setCurrentIndex(1)
            self.window.save_session_choice("isolated", True)
            self.window.close()
        write.assert_not_called()

    def test_sort_and_filter_saved_per_store_and_bad_preferences_fall_back(self):
        self.enable_shared_folder_controls()
        self.window.sort.setCurrentIndex(self.window.sort.findData("capacity"))
        self.window.filter.setCurrentIndex(2)
        with patch("chrootables.app.QSettings", return_value=self.settings), \
                patch("chrootables.app.QTimer.singleShot"):
            reopened = Window(self.window.store)
            different = Window(Path(self.directory.name) / "different")
            self.addCleanup(reopened.close)
            self.addCleanup(different.close)
        self.assertEqual(reopened.sort.currentData(), "capacity")
        self.assertEqual(reopened.filter.currentIndex(), 2)
        self.assertEqual(different.sort.currentData(), "favorites")
        self.assertEqual(different.filter.currentIndex(), 0)
        self.settings.setValue(self.window.session_settings_prefix + "/sort", "obsolete-sort")
        self.settings.setValue(self.window.session_settings_prefix + "/filter", "obsolete-filter")
        with patch("chrootables.app.QSettings", return_value=self.settings), \
                patch("chrootables.app.QTimer.singleShot"):
            fallback = Window(self.window.store)
            self.addCleanup(fallback.close)
        self.assertEqual(fallback.sort.currentIndex(), 0)
        self.assertEqual(fallback.filter.currentIndex(), 0)

    def test_corrupt_saved_geometry_does_not_prevent_startup(self):
        self.settings.setValue("window/geometry", 123)
        self.settings.setValue("window/splitter", ["invalid", "sizes"])
        self.settings.setValue("appearance/text_scale", "invalid")
        with patch("chrootables.app.QSettings", return_value=self.settings), \
                patch("chrootables.app.QTimer.singleShot"):
            window = Window(self.window.store)
            self.addCleanup(window.close)
        self.assertGreater(window.width(), 0)
        self.assertEqual(window.text_scale.currentData(), 100)

    def test_favorites_are_scoped_to_store_and_preserve_empty_choices(self):
        first_store = Path(self.directory.name) / "first"
        second_store = Path(self.directory.name) / "second"
        with patch("chrootables.app.QSettings", return_value=self.settings), \
                patch("chrootables.app.QTimer.singleShot"):
            first, second = Window(first_store), Window(second_store)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            first.selected = "same-name"
            first.favorite()
            second.selected = "other-name"
            second.favorite()
            reopened = Window(first_store)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.favorites, {"same-name"})
            self.assertEqual(second.favorites, {"other-name"})
            reopened.selected = "same-name"
            reopened.favorite()
            empty = Window(first_store)
            self.addCleanup(empty.close)
            self.assertEqual(empty.favorites, set())
            self.assertEqual(self.settings.value(second.session_settings_prefix + "/favorites"), ["other-name"])

    def test_legacy_favorites_migrate_only_to_default_store_and_demo_is_isolated(self):
        self.settings.setValue("favorites", ["python-playground", ["invalid"]])
        with patch("chrootables.app.QSettings", return_value=self.settings), \
                patch("chrootables.app.QTimer.singleShot"):
            default = Window()
            custom = Window(Path(self.directory.name) / "custom")
            demo = Window(demo=True)
            for window in (default, custom, demo):
                self.addCleanup(window.close)
            self.assertEqual(default.favorites, {"python-playground"})
            self.assertEqual(custom.favorites, set())
            self.assertEqual(demo.favorites, set())
            self.assertEqual(self.settings.value(default.session_settings_prefix + "/favorites"), ["python-playground"])
            default.selected = "python-playground"
            default.favorite()
            reopened = Window()
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.favorites, set())
            with patch.object(self.settings, "setValue") as write:
                demo.favorite()
            write.assert_not_called()

    def test_health_distinguishes_store_configuration_from_dependencies(self):
        self.enable_shared_folder_controls()
        self.window.client.is_ready = True
        report = {"ready": False, "output": "Store ownership needs fixing", "errors": 1,
                  "warnings": 2, "checks": [{"id": "store-permissions", "status": "error"}]}
        with patch.object(self.window.client, "request", side_effect=lambda action, params, done: done(True, report)):
            self.window.check_health()
            self.assertFalse(self.window.setup_button.isEnabled())
            self.assertIn("blocking issues", self.window.notice_text.text())
            self.assertIn("2 warnings", self.window.health_summary.text())
            report["checks"] = [{"id": "command:debootstrap", "status": "error"}]
            self.window.check_health()
            self.assertTrue(self.window.setup_button.isEnabled())

    def test_recovery_and_activity_navigation(self):
        QTest.mouseClick(self.window.nav[1], Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertEqual(self.window.recovery_table.rowCount(), 3)
        self.window.recovery_table.selectRow(0)
        self.assertFalse(self.window.restore_button.isEnabled())  # Tour must never mutate.
        QTest.mouseClick(self.window.nav[2], Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.activity_table.rowCount(), 3)

    def test_network_choice_survives_background_refresh(self):
        self.window.offline.setChecked(True)
        self.window.render_library()
        self.assertEqual(self.window.selected, "python-playground")
        self.assertTrue(self.window.offline.isChecked())

    def test_workspace_details_keep_scroll_on_refresh_and_reset_on_selection(self):
        self.window.resize(1180, 780)
        self.app.processEvents()
        scroll = self.window.details.verticalScrollBar()
        self.assertGreater(scroll.maximum(), 0)
        scroll.setValue(scroll.maximum())
        position = scroll.value()
        self.window.render_library()
        self.app.processEvents()
        self.assertEqual(scroll.value(), position)
        for row in range(self.window.workspaces.count()):
            item = self.window.workspaces.item(row)
            if item.data(Qt.ItemDataRole.UserRole)["name"] == "clean-slate":
                self.window.workspaces.setCurrentItem(item)
                break
        self.app.processEvents()
        self.assertEqual(self.window.selected, "clean-slate")
        self.assertEqual(scroll.value(), 0)

    def enable_shared_folder_controls(self, window=None):
        window = window or self.window
        window.demo = False
        window.shared_folder_choices.clear()
        window.render_library()

    def shared_directory(self, name="host project"):
        folder = Path(self.directory.name) / name
        folder.mkdir()
        return str(folder)

    def another_demo_window(self, store=None):
        self.settings.sync()
        settings = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        with patch("chrootables.app.QSettings", return_value=settings):
            window = Window(self.window.store if store is None else store, demo=True)
        window.timer.stop()
        self.addCleanup(window.close)
        return window

    def test_shared_folder_selection_survives_refresh_and_can_be_cleared(self):
        self.enable_shared_folder_controls()
        folder = self.shared_directory()
        with patch("chrootables.app.QFileDialog.getExistingDirectory", return_value=folder):
            self.window.share_choose.click()
        self.assertEqual(self.window.share_path.text(), folder)
        self.assertTrue(self.window.share_clear.isEnabled())
        self.assertTrue(self.window.share_read_only.isEnabled())
        self.window.share_read_only.setChecked(True)
        self.window.render_library()
        self.assertEqual(self.window.share_path.text(), folder)
        self.assertTrue(self.window.share_read_only.isChecked())
        self.window.share_clear.click()
        self.assertEqual(self.window.share_path.text(), "")
        self.assertFalse(self.window.share_clear.isEnabled())
        self.assertFalse(self.window.share_read_only.isEnabled())
        self.window.shared_folder_choices.clear()
        self.assertEqual(self.window.shared_folder_choice("python-playground"), ("", False))

    def test_shared_folder_preferences_are_saved_per_workspace_and_store(self):
        self.enable_shared_folder_controls()
        folder = self.shared_directory()
        self.window.save_shared_folder("python-playground", folder, True)
        self.window.selected = "clean-slate"
        self.window.render_library()
        self.assertEqual(self.window.share_path.text(), "")
        other_folder = self.shared_directory("other project")
        with patch("chrootables.app.QFileDialog.getExistingDirectory", return_value=other_folder):
            self.window.share_choose.click()
        self.window.selected = "python-playground"
        self.window.render_library()
        self.assertEqual(self.window.share_path.text(), folder)
        self.assertTrue(self.window.share_read_only.isChecked())

        reopened = self.another_demo_window()
        self.enable_shared_folder_controls(reopened)
        self.assertEqual(reopened.shared_folder_choice("python-playground"), (folder, True))
        self.assertEqual(reopened.shared_folder_choice("clean-slate"), (other_folder, False))
        different_store = self.another_demo_window(Path(self.directory.name) / "other-store")
        self.enable_shared_folder_controls(different_store)
        self.assertEqual(different_store.shared_folder_choice("python-playground"), ("", False))
        different_store.save_shared_folder("python-playground", other_folder)
        reopened.shared_folder_choices.clear()
        self.assertEqual(reopened.shared_folder_choice("python-playground"), (folder, True))

    def test_cancelled_or_invalid_folder_chooser_keeps_previous_selection(self):
        self.enable_shared_folder_controls()
        folder = self.shared_directory()
        self.window.save_shared_folder("python-playground", folder, True)
        self.window.render_library()
        with patch("chrootables.app.QFileDialog.getExistingDirectory", return_value=""):
            self.window.share_choose.click()
        self.assertEqual(self.window.share_path.text(), folder)
        self.assertTrue(self.window.share_read_only.isChecked())
        with patch("chrootables.app.QFileDialog.getExistingDirectory", return_value=folder + "/missing"), \
                patch.object(self.window, "show_error") as show_error:
            self.window.share_choose.click()
        show_error.assert_called_once()
        self.assertEqual(self.window.shared_folder_choice("python-playground"), (folder, True))

    def test_terminal_and_disposable_session_receive_shared_folder_choices(self):
        self.enable_shared_folder_controls()
        folder = self.shared_directory()
        self.window.save_shared_folder("python-playground", folder, True)
        self.window.render_library()
        self.window.offline.setChecked(True)
        with patch("chrootables.app.session_command", return_value=["session", "command"]) as command, \
                patch("chrootables.app.TerminalWindow") as window:
            for disposable in (False, True):
                with self.subTest(disposable=disposable):
                    self.window.open_terminal(disposable=disposable)
                    command.assert_called_with("python-playground", self.window.store, disposable=disposable,
                                               isolated=True, restricted_dev=False,
                                               share=folder, read_only=True)
            self.window.clear_shared_folder()
            self.window.open_terminal()
            command.assert_called_with("python-playground", self.window.store, disposable=False,
                                       isolated=True, restricted_dev=False,
                                       share=None, read_only=False)
            self.window.restricted.setChecked(True)
            self.window.open_terminal()
            command.assert_called_with("python-playground", self.window.store, disposable=False,
                                       isolated=True, restricted_dev=True,
                                       share=None, read_only=False)
        self.assertEqual(window.call_count, 4)

    def test_external_terminal_keeps_original_workspace_after_selection_changes(self):
        self.enable_shared_folder_controls()
        with patch("chrootables.app.TerminalWindow") as window, \
                patch("chrootables.app.terminal_command", return_value=["terminal"]) as command, \
                patch("chrootables.app.QProcess.startDetached", return_value=(True, 123)):
            self.window.open_terminal()
            external = window.call_args.kwargs["external"]
            self.window.selected = "clean-slate"
            external()
            self.assertEqual(command.call_args.args[0], "python-playground")

    def test_terminal_launch_failure_is_reported_without_registering_window(self):
        self.enable_shared_folder_controls()
        with patch("chrootables.app.TerminalWindow", side_effect=OSError("missing sudo")), \
                patch.object(self.window, "show_error") as error:
            self.window.open_terminal()
        error.assert_called_once()
        self.assertIn("missing sudo", error.call_args.args[0])
        self.assertEqual(self.window.terminals, [])

    def test_unavailable_saved_folder_prevents_terminal_launch(self):
        self.enable_shared_folder_controls()
        folder = self.shared_directory()
        self.window.save_shared_folder("python-playground", folder)
        Path(folder).rmdir()
        with patch("chrootables.app.session_command", side_effect=ValueError("missing folder")) as command, \
                patch("chrootables.app.TerminalWindow") as window, \
                patch.object(self.window, "show_error") as show_error:
            self.window.open_terminal()
        command.assert_called_once()
        window.assert_not_called()
        show_error.assert_called_once()
        self.assertEqual(self.window.shared_folder_choice("python-playground"), (folder, False))

    def test_demo_ignores_saved_shared_folders_and_does_not_write_preferences(self):
        self.enable_shared_folder_controls()
        folder = self.shared_directory()
        self.window.save_shared_folder("python-playground", folder, True)
        demo = self.another_demo_window()
        self.assertEqual(demo.shared_folder_choice("python-playground"), ("", False))
        self.assertFalse(demo.share_choose.isEnabled())
        self.assertFalse(demo.share_clear.isEnabled())
        self.assertFalse(demo.share_read_only.isEnabled())
        with patch("chrootables.app.QFileDialog.getExistingDirectory") as choose:
            demo.choose_shared_folder()
            demo.clear_shared_folder()
        choose.assert_not_called()
        self.window.shared_folder_choices.clear()
        self.assertEqual(self.window.shared_folder_choice("python-playground"), (folder, True))

    def test_create_validation_and_profile_defaults(self):
        dialog = CreateDialog({"exists"}, self.window)
        dialog.name_input.setText("../invalid")
        dialog.validate()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(dialog.error.text())
        dialog.name_input.setText("exists")
        dialog.validate()
        self.assertIn("already", dialog.error.text())
        dialog.name_input.setText("new-python")
        dialog.profile.setCurrentIndex(2)
        self.assertEqual(dialog.size_input.value(), 4)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.parameters()["profile"], "python")

    def test_actual_async_helper_and_empty_store(self):
        with tempfile.TemporaryDirectory() as directory:
            other = Window(Path(directory) / "store")
            other.show()
            try:
                for _ in range(100):
                    QTest.qWait(30)
                    if other.client.is_ready and other.metrics[0].text() == "0":
                        break
                self.assertTrue(other.client.is_ready)
                self.assertEqual(other.metrics[0].text(), "0")
                self.assertFalse(other.client.writable)
                self.assertEqual(other.library_stack.currentIndex(), 1)
            finally:
                other.close()

    def test_old_helper_output_is_ignored_after_disconnect(self):
        from PySide6.QtCore import QProcess
        previous = QProcess(self.window)
        self.window.client.read(previous)
        self.assertEqual(self.window.client.buffer, b"")

    def test_locked_store_is_normal_onboarding_not_an_error(self):
        from chrootables.client import BackendError
        window = self.window
        window.demo = False
        window.client.is_ready = True
        window.health_ready = True
        def request(action, params, callback):
            callback(False, BackendError("[Errno 13] Permission denied", "permission_required"))
        window.client.request = request
        window.refresh()
        self.assertTrue(window.store_locked)
        self.assertEqual(window.library_stack.currentIndex(), 1)
        self.assertTrue(window.notice.isHidden())
        self.assertEqual(window.empty_create.text(), "Unlock workspace store")
        window.search.setText("something")
        self.assertEqual(window.empty_title.text(), "Unlock your workspace library.")

    def test_widget_reuse_and_accessibility_metadata(self):
        detail_widget = self.window.details.widget()
        first_item = self.window.workspaces.item(0)
        first_row = self.window.workspaces.itemWidget(first_item)
        self.window.render()
        self.assertIs(self.window.details.widget(), detail_widget)
        self.assertIs(self.window.workspaces.itemWidget(self.window.workspaces.item(0)), first_row)
        self.assertIn("build-studio", first_item.data(Qt.ItemDataRole.AccessibleTextRole))
        self.assertIn("python-playground", self.window.workspaces.currentItem().data(Qt.ItemDataRole.AccessibleTextRole))
        self.assertEqual(self.window.search.accessibleName(), "Search workspaces")
        self.assertEqual(self.window.filter.accessibleName(), "Filter workspaces")
        self.assertEqual(self.window.refresh_button.accessibleName(), "Refresh workspaces")
        self.assertTrue(self.window.favorite_button.accessibleName())

    def test_client_coalesces_batched_output_before_result(self):
        from unittest.mock import MagicMock
        events = []
        self.window.client.output.connect(lambda ident, text: events.append(("output", ident, text)))
        self.window.client.callbacks[7] = lambda ok, data: events.append(("result", ok, data))
        proc = MagicMock()
        proc.readAllStandardOutput.return_value = (
            b'{"id":7,"event":"output","text":"line 1\\n"}\n'
            b'{"id":7,"event":"output","text":"line 2\\n"}\n'
            b'{"id":7,"event":"result","ok":true,"data":{"exit_code":0}}\n'
        )
        self.window.client.process = proc
        self.window.client.read(proc)
        self.assertEqual(events, [("output", 7, "line 1\nline 2\n"), ("result", True, {"exit_code": 0})])

    def test_minimized_window_skips_timer_poll_and_refreshes_on_restore(self):
        from PySide6.QtGui import QWindowStateChangeEvent
        with patch.object(self.window, "refresh") as refresh:
            with patch.object(self.window, "isMinimized", return_value=True):
                self.window._poll_timer()
                self.assertEqual(refresh.call_count, 0)
            with patch.object(self.window, "isMinimized", return_value=False):
                self.window._poll_timer()
                self.assertEqual(refresh.call_count, 1)
                event = QWindowStateChangeEvent(Qt.WindowState.WindowMinimized)
                self.window.changeEvent(event)
                self.assertEqual(refresh.call_count, 2)

    def test_client_starts_broker_in_isolated_no_site_mode(self):
        from PySide6.QtCore import QProcess
        with patch.object(QProcess, "start") as mock_start:
            self.window.client.start(writable=False)
            prog, args = mock_start.call_args[0]
            self.assertEqual(prog, sys.executable)
            self.assertEqual(args[:2], ["-I", "-S"])
            self.window.client.start(writable=True)
            prog, args = mock_start.call_args[0]
            self.assertEqual(prog, sys.executable)
            self.assertEqual(args[:2], ["-I", "-S"])
            self.assertEqual(Path(args[2]).name, "authenticate.py")
            self.assertEqual(args[-2:], ["--store", self.window.client.store])

    def test_workspace_actions_keep_target_while_dialog_processes_refresh(self):
        images = list(self.window.data["images"])
        def refresh_selection(*args, **kwargs):
            self.window.data["images"] = [item for item in images if item["name"] != "python-playground"]
            self.window.render_library()
            return True

        for action, value, params in [
            ("snapshot", "checkpoint", {"target": "checkpoint"}),
            ("clone", "branch", {"target": "branch"}),
            ("resize_image", 8192, {"size_mib": 8192}),
        ]:
            with self.subTest(action=action):
                self.window.data["images"] = images
                self.window.selected = "python-playground"
                self.window.render_library()
                with patch("chrootables.app.ValueDialog") as dialog_class, \
                        patch.object(self.window, "perform") as perform:
                    dialog_class.return_value.exec.side_effect = refresh_selection
                    dialog_class.return_value.value.return_value = value
                    getattr(self.window, action)()
                perform.assert_called_once_with("resize" if action == "resize_image" else action,
                                                {"name": "python-playground", **params})

        self.window.data["images"] = images
        self.window.selected = "python-playground"
        with patch.object(self.window, "confirm", side_effect=refresh_selection), \
                patch.object(self.window, "perform") as perform:
            self.window.repair()
        perform.assert_called_once_with("repair", {"name": "python-playground", "confirmed": True})

        self.window.data["images"] = images
        self.window.selected = "python-playground"
        destination = str(Path(self.directory.name) / "export.tar.gz")
        def choose_destination(*args):
            refresh_selection()
            return destination, ""
        with patch("chrootables.app.QFileDialog.getSaveFileName", side_effect=choose_destination), \
                patch.object(self.window, "perform") as perform:
            self.window.export()
        perform.assert_called_once_with("export", {"name": "python-playground", "destination": destination})

    def test_failed_operation_restores_controls_without_inventory_changes(self):
        self.enable_shared_folder_controls()
        self.window.client.is_ready = True
        self.window.health_ready = True
        callbacks = {}
        def request(action, params, callback):
            if action == "inventory":
                callback(True, self.window.data)
            else:
                callbacks[action] = callback
        with patch.object(self.window.client, "request", side_effect=request):
            self.window.refresh()
            self.window.run_operation("snapshot", {"name": "python-playground", "target": "checkpoint"})
            self.assertFalse(self.window.terminal_button.isEnabled())
            callbacks["snapshot"](False, "Workspace is busy.")
        self.assertFalse(self.window.busy)
        self.assertTrue(self.window.terminal_button.isEnabled())
        self.assertTrue(self.window.save_snapshot_button.isEnabled())
        self.assertTrue(self.window.share_choose.isEnabled())

    def test_client_stop_rejects_requests_from_cancelled_callbacks(self):
        from unittest.mock import MagicMock
        client = self.window.client
        client.process = MagicMock()
        client.is_ready = client.writable = True
        results = []
        def cancelled(ok, data):
            results.append((ok, client.is_ready, client.writable))
            client.request("inventory", {}, lambda ok, data: results.append((ok, str(data))))
        client.callbacks[1] = cancelled
        client.stop()
        self.assertEqual(results[0], (False, False, False))
        self.assertEqual(results[1], (False, "The workspace helper is not connected."))
        self.assertEqual(client.callbacks, {})

    def test_recovery_selection_follows_snapshot_across_refresh(self):
        self.window.recovery_table.selectRow(1)
        selected = self.window.chosen_snapshot().copy()
        self.window.data["snapshots"].reverse()
        self.window.data["snapshots"].insert(0, {**selected, "label": "new-checkpoint"})
        self.window.render()
        self.assertEqual(self.window.chosen_snapshot(), selected)
        self.window.data["snapshots"].remove(selected)
        self.window.render()
        self.assertIsNone(self.window.chosen_snapshot())

    def test_activity_selection_follows_log_across_refresh(self):
        self.window.demo = False
        self.window.activity_table.selectRow(0)
        selected = self.window.data["activity"][0].copy()
        self.window.data["activity"].insert(0, {**selected, "log": "new-operation.log"})
        self.window.render()
        with patch.object(self.window, "show_log") as show_log:
            self.window.activity_log()
        show_log.assert_called_once_with(selected["name"], selected["log"])

        self.window.data["activity"][1] = {**selected, "log": "replacement-operation.log"}
        self.window.render()
        with patch.object(self.window, "show_log") as show_log:
            self.window.activity_log()
        show_log.assert_not_called()

    def test_recovery_filter_actions_keep_identity_after_refresh(self):
        selected = self.window.data["snapshots"][-1].copy()
        self.window.recovery_search.setText(selected["label"])
        self.window.recovery_table.selectRow(0)
        self.assertEqual(self.window.chosen_snapshot(), selected)
        self.window.data["snapshots"].reverse()
        self.window.render()
        with patch.object(self.window, "confirm", return_value=True), patch.object(self.window, "perform") as perform:
            self.window.restore()
        perform.assert_called_once_with("restore", {"name": selected["name"], "target": selected["label"], "confirmed": True})
        self.window.recovery_search.setText("no-matching-recovery")
        self.assertIsNone(self.window.chosen_snapshot())
        self.assertFalse(self.window.restore_button.isEnabled())

    def test_activity_filters_keep_selected_log_identity(self):
        self.window.data["activity"].append({"time": "2026-10-27T10:00:00", "name": "failed-workspace", "action": "create", "status": "failed", "log": "failed.log"})
        self.window.render()
        self.window.activity_filter.setCurrentIndex(1)
        self.window.activity_search.setText("failed-workspace")
        self.assertEqual(self.window.activity_table.rowCount(), 1)
        self.window.activity_table.selectRow(0)
        self.window.data["activity"].reverse()
        self.window.render()
        with patch.object(self.window, "show_log") as show_log:
            self.window.demo = False
            self.window.activity_log()
        show_log.assert_called_once_with("failed-workspace", "failed.log")

    def test_snapshot_cleanup_preview_contains_scope_and_allocated_storage(self):
        item = self.window.data["snapshots"][0]
        item["allocated_bytes"] = 123 * 1048576
        self.window.recovery_table.selectRow(0)
        with patch.object(self.window, "confirm", return_value=False) as confirm, patch.object(self.window, "perform") as perform:
            self.window.drop_snapshot()
        self.assertIn("123 MiB", confirm.call_args.args[1])
        self.assertIn("Only this recovery point", confirm.call_args.args[1])
        perform.assert_not_called()

    def test_operation_output_can_pause_follow_and_shows_elapsed_phase(self):
        self.window.task_panel.show()
        self.window.append_output("line\n" * 300)
        self.app.processEvents()
        self.window.follow_output.setChecked(False)
        scroll = self.window.task_output.verticalScrollBar()
        scroll.setValue(10)
        self.window.append_output("more\n" * 40)
        self.assertEqual(scroll.value(), 10)
        self.window.follow_output.setChecked(True)
        self.window.append_output("last\n")
        self.assertEqual(scroll.value(), scroll.maximum())
        with patch("chrootables.app.time.monotonic", return_value=125):
            self.window.task_started = 60
            self.window.set_task_phase("Downloading packages")
        self.assertEqual(self.window.task_phase.text(), "Downloading packages · 01:05 elapsed")
        self.window.task_started = None

    def test_compact_layout_text_scale_and_view_search_shortcut(self):
        self.window.resize(1024, 768)
        self.app.processEvents()
        self.assertLessEqual(self.window.minimumSizeHint().width(), 1024)
        self.assertLessEqual(self.window.minimumSizeHint().height(), 768)
        self.window.navigate(1)
        self.window.focus_search()
        self.assertEqual(self.window.focusWidget(), self.window.recovery_search)
        self.window.text_scale.setCurrentIndex(1)
        self.app.processEvents()
        self.window.resize(1024, 768)
        self.app.processEvents()
        self.assertIn("font-size: 16px", self.window.styleSheet())
        self.assertEqual(self.window.splitter.orientation(), Qt.Orientation.Vertical)
        self.assertEqual(self.window.content_scroll.horizontalScrollBar().maximum(), 0)
        self.window.activateWindow()
        self.window.recovery_search.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.recovery_search, Qt.Key.Key_3, Qt.KeyboardModifier.AltModifier)
        self.app.processEvents()
        self.assertEqual(self.window.current_view, 2)
        QTest.keyClick(self.window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertEqual(self.window.focusWidget(), self.window.activity_search)
        self.assertFalse(self.settings.contains("appearance/text_scale"))

    def test_mib_create_mode_import_dialog_collision_checks_and_maintenance_actions(self):
        from chrootables.app import CreateDialog, ImportDialog, ValueDialog
        create_dlg = CreateDialog({"python-playground"}, self.window)
        self.addCleanup(create_dlg.close)
        create_dlg.name_input.setText("python-playground")
        create_dlg.validate()
        self.assertIn("already in use", create_dlg.error.text())
        create_dlg.name_input.setText("sub-gib-lab")
        create_dlg.set_size_mib(512)
        self.assertEqual(create_dlg.parameters()["size_mib"], 512)

        value_dlg = ValueDialog(
            "Save recovery point", "Checkpoint", "Label", "clean-base", parent=self.window,
            forbidden={"clean-base"}, collision_message="A recovery point with this label already exists.",
        )
        self.addCleanup(value_dlg.close)
        self.assertFalse(value_dlg.ok_button.isEnabled())
        self.assertIn("already exists", value_dlg.error.text())
        value_dlg.input.setText("fresh-point")
        self.assertTrue(value_dlg.ok_button.isEnabled())

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "export.tar.gz"
            archive.write_bytes(b"tar-gz-placeholder")
            import_dlg = ImportDialog({"python-playground"}, str(archive), self.window)
            self.addCleanup(import_dlg.close)
            import_dlg.name_input.setText("imported-lab")
            import_dlg.validate()
            self.assertEqual(import_dlg.result(), QDialog.DialogCode.Accepted)
            values = import_dlg.parameters()
            self.assertEqual((values["name"], values["archive"]), ("imported-lab", str(archive)))
            self.assertNotIn("size_mib", values, "Capacity should fit the archive by default.")
            import_dlg.auto_size.setChecked(False)
            self.assertTrue(import_dlg.size_input.isEnabled())
            self.assertEqual(import_dlg.parameters()["size_mib"], import_dlg.size_input.value())

    def test_retained_export_imports_by_name_without_reading_the_store(self):
        from chrootables.app import ImportDialog
        unreadable = "/var/lib/chrootable-manager/exports/lab-20261015-120000-AbCd12.tar.gz"
        dialog = ImportDialog(set(), unreadable, self.window, managed_export=Path(unreadable).name)
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.archive_input.isReadOnly())
        self.assertFalse(dialog.browse_button.isEnabled())
        with patch("chrootables.app.archive_file", side_effect=ValueError("must not be called")):
            dialog.validate()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        values = dialog.parameters()
        self.assertEqual(values["export"], Path(unreadable).name)
        self.assertNotIn("archive", values)

    def test_damaged_operation_record_explains_set_aside_without_deleting_staged_files(self):
        self.window.data["operations"] = [{
            "id": "c" * 32, "action": "unknown", "name": "store", "status": "invalid",
            "stage": "invalid-record", "log": "", "active": False, "recoverable": False,
            "discardable": True, "reclaimable_bytes": 0, "invalid": True,
        }]
        self.window.demo = False
        self.window.render_recovery()
        self.assertIn("Damaged operation record", self.window.interrupted_label.text())
        self.assertIn("Staged files remain", self.window.interrupted_label.text())
        self.assertFalse(self.window.recover_operation_button.isVisible())
        self.assertTrue(self.window.discard_operation_button.isEnabled())
        with patch.object(self.window, "confirm", return_value=True) as confirm, \
                patch.object(self.window, "perform") as perform:
            self.window.discard_interrupted_operation()
        self.assertIn("staged files remain", confirm.call_args.args[1])
        perform.assert_called_once_with("discard-operation", {"id": "c" * 32, "confirmed": True})

    def test_interrupted_work_is_visible_and_can_be_discarded(self):
        operations = [
            {"id": "a" * 32, "action": "clone", "name": "lab", "status": "interrupted", "stage": "copying",
             "log": "", "active": False, "recoverable": False, "discardable": True, "reclaimable_bytes": 3 * 1048576},
            {"id": "b" * 32, "action": "restore", "name": "lab", "status": "interrupted", "stage": "publish-metadata",
             "log": "", "active": False, "recoverable": True, "discardable": True, "reclaimable_bytes": 0},
        ]
        self.window.data = {**self.window.data, "operations": operations}
        self.window.demo = False
        self.window.render_recovery()
        self.assertFalse(self.window.interrupted_frame.isHidden())
        self.assertIn("frees 3 MiB", self.window.interrupted_label.text())
        self.assertIn("1 more", self.window.interrupted_label.text())
        self.assertTrue(self.window.recover_operation_button.isHidden())
        self.assertTrue(self.window.discard_operation_button.isEnabled())
        with patch.object(self.window, "confirm", return_value=True), patch.object(self.window, "perform") as perform:
            self.window.discard_interrupted_operation()
        perform.assert_called_once_with("discard-operation", {"id": "a" * 32, "confirmed": True})
        self.window.data = {**self.window.data, "operations": operations[1:]}
        self.window.render_recovery()
        self.assertFalse(self.window.recover_operation_button.isHidden())
        self.window.demo = True

    def test_cancelled_operation_is_not_reported_as_a_failure(self):
        from chrootables.client import BackendError
        captured = {}
        self.window.demo = False
        with patch.object(self.window.client, "request", side_effect=lambda action, params, done: captured.update(done=done)):
            self.window.run_operation("clone", {"name": "lab", "target": "copy"})
            self.assertTrue(self.window.busy)
            captured["done"](False, BackendError("Clone was cancelled.", "cancelled"))
        self.assertFalse(self.window.busy)
        self.assertIn("cancelled", self.window.task_title.text())
        self.assertNotIn("failed", self.window.task_title.text())
        self.window.demo = True

    def test_cancelling_authentication_returns_to_a_read_only_connection(self):
        self.window.demo = False
        self.window.client.requested_writable = True
        with patch.object(self.window.client, "start") as start:
            self.window.disconnected("Administrator access was cancelled or the helper closed.")
            self.app.processEvents()
            start.assert_called_once_with(False)
            start.reset_mock()
            self.window.client.requested_writable = False
            self.window.disconnected("The workspace helper stopped.")
            self.app.processEvents()
            start.assert_not_called()
        self.window.demo = True

    def test_export_keeps_an_importable_suffix(self):
        self.window.selected = "python-playground"
        with patch("chrootables.app.QFileDialog.getSaveFileName", return_value=("/tmp/chrootables-test-backup", "")), \
                patch.object(self.window, "perform") as perform:
            self.window.export()
        perform.assert_called_once_with("export", {"name": "python-playground", "destination": "/tmp/chrootables-test-backup.tar.gz"})


if __name__ == "__main__":
    unittest.main()
