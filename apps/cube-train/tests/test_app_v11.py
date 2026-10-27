"""Readiness/comparison regressions and a native desktop workflow smoke test."""
import csv
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
except (ImportError, ValueError):
    app = None
else:
    import app


@unittest.skipUnless(app, 'System GTK bindings are required')
class WorkshopPresentationTests(unittest.TestCase):
    def test_unknown_diagnostics_are_not_reported_ready(self):
        self.assertIn('No checks', app.diagnostic_summary({}))
        self.assertIn('Action needed', app.diagnostic_summary({'ok': True, 'checks': [{'status': 'error'}]}))

    def test_incomparable_losses_never_identify_a_winner(self):
        report = {'runs': [{'id': 'a', 'name': 'First'}, {'id': 'b', 'name': 'Second'}],
                  'comparable': False, 'best_run_id': 'b'}
        self.assertNotIn('Lowest', app.comparison_summary(report))
        report['comparable'] = True
        self.assertIn('Lowest validation loss: Second', app.comparison_summary(report))

    def test_comparison_export_keeps_fingerprints_and_escapes_spreadsheet_names(self):
        report = {'runs': [{'id': 'a', 'name': '=1+1', 'val_loss': 2.4, 'corpus_fingerprint': 'abc',
                            'step': 10, 'ignored': 'not a table column'}]}
        rows = list(csv.DictReader(io.StringIO(app.metrics_csv(app.comparison_rows(report)))))
        self.assertEqual(rows[0]['name'], "'=1+1")
        self.assertEqual(rows[0]['corpus_fingerprint'], 'abc')
        self.assertEqual(rows[0]['val_loss'], '2.4')
        self.assertNotIn('ignored', rows[0])

    def test_visible_comparison_respects_hidden_rows_limit_and_selected_baseline(self):
        ids = [str(index) for index in range(30)]
        rows = {run_id: MagicMock() for run_id in ids}
        for run_id, row in rows.items(): row.get_visible.return_value = run_id != '1'
        window = SimpleNamespace(comparison_scope=MagicMock(), row_order=ids, run_rows=rows, selected_id='25')
        window.comparison_scope.get_selected.return_value = 0
        selected = app.TrainerWindow.comparison_selection(window)
        self.assertEqual(len(selected), 20)
        self.assertEqual(selected[0], '25')
        self.assertNotIn('1', selected)
        self.assertEqual(len(selected), len(set(selected)))

    def test_readiness_captures_form_before_worker_and_marks_changed_settings(self):
        callbacks = []
        window = SimpleNamespace(readiness_checking=False, closed=False, trainer=MagicMock(),
            readiness_button=MagicMock(), readiness_status=MagicMock(), readiness_copy=MagicMock(),
            readiness_export=MagicMock(), readiness_checks=MagicMock(),
            form_values=MagicMock(side_effect=[{'python': '/old'}, {'python': '/new'}]),
            background=lambda work, done: callbacks.append((work, done)))
        window.readiness_checks.get_first_child.return_value = None
        window.readiness_status.get_text.return_value = 'Previous summary'
        app.TrainerWindow.check_readiness(window)
        self.assertEqual(window.form_values.call_count, 1)
        with patch.object(app.diagnostics, 'collect_diagnostics', return_value={'checks': []}) as collect:
            report = callbacks[0][0]()
            collect.assert_called_once_with(window.trainer, check_runtime=True, values={'python': '/old'})
        callbacks[0][1](report, None)
        self.assertIn('Settings changed', window.readiness_status.set_text.call_args.args[0])
        self.assertFalse(window.readiness_checking)


@unittest.skipUnless(app, 'System GTK bindings are required')
class NativeWorkshopTests(unittest.TestCase):
    def test_readiness_and_comparison_workflow(self):
        if not app.Gtk.init_check() or app.Gdk.Display.get_default() is None:
            self.skipTest('A GTK display is required for the native workflow')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'WHITECUBE_TRAIN_OUT': str(root / 'runs')}):
                trainer = app.engine.Trainer(home=root, state=root / 'state', config=root / 'config')
            for index in (1, 2):
                run = trainer.runs_dir / f'run-{index}'
                run.mkdir(parents=True)
                app.engine.atomic_json(run / 'run.json', {'steps': 10, 'model_size': '2m', 'device': 'cpu',
                                                          'display_name': f'Example {index}'})
                app.engine.atomic_json(run / 'result.json', {'completed_steps': 10, 'best_val_loss': 3 - index / 10})
            options = SimpleNamespace(page=None, screenshot=None, no_probe=True)
            application = app.TrainerApp(options, trainer=trainer)
            application.register(None)
            # Run filesystem work synchronously for this deterministic UI exercise.
            def immediate(window, work, done):
                try: result, error = work(), None
                except Exception as exception: result, error = None, exception
                done(result, error)
            with patch.object(app.TrainerWindow, 'background', immediate):
                application.activate()
                window = application.window
                try:
                    self.assertIsNotNone(window.detail_tabs.get_child_by_name('compare'))
                    window.detail_tabs.set_visible_child_name('compare')
                    window.compare_runs()
                    self.assertEqual(len(window.comparison_report['runs']), 2)
                    self.assertIn('common corpus', window.comparison_status.get_text())
                    self.assertIn('checksums', window.comparison_warning.get_text())
                    self.assertTrue(window.comparison_json.get_sensitive())
                    exports = []
                    with patch.object(window, 'choose_export', side_effect=lambda title, name, produce: exports.append(produce())):
                        window.export_comparison(); window.export_comparison(True)
                    self.assertEqual(len(json.loads(exports[0])['runs']), 2)
                    self.assertIn('corpus_fingerprint', exports[1])
                    spreadsheet = list(csv.DictReader(io.StringIO(exports[1])))
                    self.assertEqual(spreadsheet[0]['comparable'], 'False')
                    self.assertIn('checksums', spreadsheet[0]['warnings'])
                    window.refresh()
                    self.assertNotIn('Selection changed', window.comparison_status.get_text())
                    window.run_search.set_text('Example 1'); window.filter_runs()
                    self.assertIn('Selection changed', window.comparison_status.get_text())
                    window.compare_runs()
                    self.assertEqual(len(window.comparison_report['runs']), 1)
                    window.comparison_scope.set_selected(1)
                    self.assertTrue(window.comparison_picker.get_sensitive())
                    window.compare_runs()
                    self.assertEqual(len(window.comparison_report['runs']), 2)
                    report = {'ok': False, 'checks': [
                        {'id': 'python', 'title': 'Training Python', 'status': 'error', 'detail': 'Missing torch',
                         'remedy': 'Choose your training environment.'}]}
                    with patch.object(app.diagnostics, 'collect_diagnostics', return_value=report):
                        window.key_pressed(None, app.Gdk.KEY_4, 0, app.Gdk.ModifierType.CONTROL_MASK)
                    self.assertEqual(window.stack.get_visible_child_name(), 'system')
                    self.assertIn('Action needed', window.readiness_status.get_text())
                    self.assertTrue(window.readiness_export.get_sensitive())
                    self.assertIsNotNone(window.readiness_checks.get_first_child())
                    exports.clear()
                    with patch.object(window, 'choose_export', side_effect=lambda title, name, produce: exports.append(produce())):
                        window.export_readiness()
                    self.assertEqual(json.loads(exports[0])['checks'][0]['status'], 'error')
                finally:
                    window.closing(); window.destroy(); application.quit()


if __name__ == '__main__':
    unittest.main()
