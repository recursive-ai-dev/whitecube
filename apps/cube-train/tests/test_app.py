"""Pure presentation and async-boundary regressions; no display is required."""
import csv
import io
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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


class Control:
    def __init__(self, value=''):
        self.value = value
        self.sensitive = True

    def get_text(self): return self.value
    def set_text(self, value): self.value = value
    def get_value(self): return self.value
    def set_sensitive(self, value): self.sensitive = value


@unittest.skipUnless(app, 'System GTK bindings are required')
class PresentationTests(unittest.TestCase):
    def test_duration_formats_short_long_and_invalid_estimates(self):
        self.assertEqual(app.format_duration(59), '59s')
        self.assertEqual(app.format_duration(125), '2m 5s')
        self.assertEqual(app.format_duration(3661), '1h 1m')
        self.assertEqual(app.format_duration(90000), '1d 1h')
        for invalid in (-1, None, True, float('nan'), 10 ** 1000):
            self.assertEqual(app.format_duration(invalid), '—')

    def test_chart_rejects_invalid_steps_and_losses(self):
        metrics = [{'step': 4, 'total_loss': 3}, {'step': 1, 'total_loss': 2},
                   {'step': '5', 'total_loss': 2}, {'step': -1, 'total_loss': 2},
                   {'total_loss': 2}, {'step': 2, 'total_loss': math.nan},
                   {'step': math.inf, 'total_loss': 3}, {'step': True, 'total_loss': 1}, None]
        self.assertEqual(app.loss_series(metrics, 'total_loss'), [(1, 2), (4, 3)])

    def test_chart_sampling_preserves_extrema_and_final_step(self):
        metrics = [{'step': i, 'total_loss': 3 + i % 7} for i in range(20_000)]
        metrics[742]['total_loss'] = 999
        metrics[799]['total_loss'] = -20
        points = app.loss_series(metrics, 'total_loss')
        self.assertLessEqual(len(points), 1202)
        self.assertIn((742, 999), points)
        self.assertIn((799, -20), points)
        self.assertEqual(points[0][0], 0)
        self.assertEqual(points[-1][0], 19_999)

    def test_chart_large_constant_and_opposite_extremes_have_finite_coordinates(self):
        for losses in ([1e30], [1e308, -1e308], [sys.float_info.max], [0, 0]):
            with self.subTest(losses=losses):
                context = MagicMock()
                window = SimpleNamespace(losses=[{'step': i, 'total_loss': value} for i, value in enumerate(losses)])
                app.TrainerWindow.draw_loss(window, None, context, 600, 180)
                for call in context.method_calls:
                    for value in call.args:
                        if isinstance(value, (float, int)):
                            self.assertTrue(math.isfinite(value), (call, losses))
                labels = [call.args[0] for call in context.show_text.call_args_list]
                self.assertFalse(any('inf' in text or 'nan' in text for text in labels))

    def test_csv_preserves_union_and_quotes_spreadsheet_formulas(self):
        text = app.metrics_csv([{'step': 1, 'total_loss': 2.5}, {'step': 2, 'val_loss': 2.1, 'note': '=1+1'}])
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(rows[0]['total_loss'], '2.5')
        self.assertEqual(rows[0]['val_loss'], '')
        self.assertEqual(rows[1]['note'], "'=1+1")

    def test_csv_escapes_formula_headers_and_whitespace_prefixes_but_preserves_numbers(self):
        text = app.metrics_csv([{'step': 1, '=HYPERLINK("x")': '\t=1+1', 'note': ' \r\n@SUM(1)',
                                 'loss': -2.5, 'safe': 'plain text'}])
        rows = list(csv.reader(io.StringIO(text)))
        self.assertIn("'=HYPERLINK(\"x\")", rows[0])
        self.assertIn("'\t=1+1", rows[1])
        self.assertIn("' \r\n@SUM(1)", rows[1])
        self.assertIn('-2.5', rows[1])
        self.assertIn('plain text', rows[1])

    def test_atomic_export_replaces_destination_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.txt'
            path.write_text('old')
            app.write_export(path, 'new\nUnicode: é\n')
            self.assertEqual(path.read_text(), 'new\nUnicode: é\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_export_preserves_previous_file_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.txt'
            path.write_text('old')
            with patch.object(app.os, 'replace', side_effect=OSError('full disk')):
                with self.assertRaises(OSError): app.write_export(path, 'new')
            self.assertEqual(path.read_text(), 'old')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_resume_target_requires_additional_steps(self):
        run = {'id': 'run', 'prepared': True, 'meta': {'steps': 10}, 'latest': True, 'completed_steps': 10}
        window = SimpleNamespace(resume_button=Control(), resume_hint=Control(), resume_steps=Control(10),
                                 runs=[run], selected_id='run', launching=False, sampling=False, active_job_info=None)
        app.TrainerWindow.update_resume_action(window)
        self.assertFalse(window.resume_button.sensitive)
        self.assertIn('above 10', window.resume_hint.value)
        window.resume_steps.value = 15
        app.TrainerWindow.update_resume_action(window)
        self.assertTrue(window.resume_button.sensitive)
        self.assertIn('5 additional', window.resume_hint.value)
        window.active_job_info = {'running': True}
        app.TrainerWindow.update_resume_action(window)
        self.assertFalse(window.resume_button.sensitive)

    def test_resume_missing_latest_requires_untrained_run(self):
        run = {'id': 'run', 'prepared': True, 'meta': {'steps': 10}, 'latest': False, 'completed_steps': 1}
        window = SimpleNamespace(resume_button=Control(), resume_hint=Control(), resume_steps=Control(10),
                                 runs=[run], selected_id='run', launching=False, sampling=False, active_job_info=None)
        app.TrainerWindow.update_resume_action(window)
        self.assertFalse(window.resume_button.sensitive)
        run['completed_steps'] = 0
        app.TrainerWindow.update_resume_action(window)
        self.assertTrue(window.resume_button.sensitive)

    def test_inspection_discards_results_after_dataset_change(self):
        calls = []
        window = SimpleNamespace(form={'dataset': Control('/old')}, inspect_request=0,
                                 inspect_button=Control(), dataset_info=Control(), background=lambda work, done: calls.append(done))
        app.TrainerWindow.inspect_dataset(window)
        self.assertFalse(window.inspect_button.sensitive)
        window.form['dataset'].value = '/new'
        app.TrainerWindow.dataset_changed(window)
        calls[0]({'files': ['old'], 'bytes': 500, 'empty_files': []}, None)
        self.assertIn('Corpus changed', window.dataset_info.value)
        self.assertTrue(window.inspect_button.sensitive)

    def test_empty_dataset_is_not_interpreted_as_working_directory(self):
        window = SimpleNamespace(form={'dataset': Control('  ')}, dataset_info=Control(),
                                 background=lambda *_: self.fail('Empty source must not scan current directory'))
        app.TrainerWindow.inspect_dataset(window)
        self.assertIn('Choose an existing', window.dataset_info.value)

    def test_runtime_probe_cannot_apply_to_changed_python(self):
        calls = []
        window = SimpleNamespace(form={'python': Control('/old/python')}, probing=False, closed=False,
                                 check_button=Control(), runtime_label=Control(),
                                 background=lambda work, done: calls.append(done))
        app.TrainerWindow.check_runtime(window)
        window.form['python'].value = '/new/python'
        calls[0]({'torch': '2', 'cuda': False, 'gpu': None, 'parquet': True}, None)
        self.assertIn('environment changed', window.runtime_label.value)
        self.assertFalse(window.probing)
        self.assertTrue(window.check_button.sensitive)

    def test_closed_window_discards_queued_background_callback(self):
        queued = []
        window = SimpleNamespace(closed=False)
        class InlineThread:
            def __init__(self, target, daemon): self.target = target
            def start(self): self.target()
        with patch.object(app.threading, 'Thread', InlineThread), patch.object(app.GLib, 'idle_add', side_effect=lambda *args: queued.append(args)):
            app.TrainerWindow.background(window, lambda: 'complete', lambda *_: self.fail('Closed window was updated'))
        window.closed = True
        callback, value, error = queued[0]
        self.assertFalse(callback(value, error))

    def test_close_cancels_save_and_poll_sources_and_releases_window(self):
        application = SimpleNamespace(window=object())
        saved = []
        window = SimpleNamespace(save_source=4, tick_source=5, closed=False,
                                 save_draft=lambda: saved.append(True), get_application=lambda: application)
        with patch.object(app.GLib, 'source_remove') as remove:
            self.assertFalse(app.TrainerWindow.closing(window))
        self.assertEqual([call.args[0] for call in remove.call_args_list], [4, 5])
        self.assertEqual(saved, [True])
        self.assertIsNone(application.window)
        self.assertTrue(window.closed)

    def test_repeated_stop_only_signals_once(self):
        stopped = []
        job = {'path': '/job'}
        trainer = SimpleNamespace(active_job=lambda: job, stop=lambda item: stopped.append(item))
        window = SimpleNamespace(trainer=trainer, stop_requested=False, stop_button=Control(), status=Control())
        app.TrainerWindow.stop(window); app.TrainerWindow.stop(window)
        self.assertEqual(stopped, [job])
        self.assertFalse(window.stop_button.sensitive)

    def test_explicit_save_removes_pending_debounce_and_preserves_draft_name(self):
        saved = []
        window = SimpleNamespace(save_source=3, device_picker=object(),
                                 trainer=SimpleNamespace(save_preferences=lambda values: saved.append(values)),
                                 form_values=lambda: {'name': 'A useful run', 'steps': 50})
        with patch.object(app.GLib, 'source_remove') as remove:
            self.assertFalse(app.TrainerWindow.save_draft(window))
        remove.assert_called_once_with(3)
        self.assertEqual(saved, [{'draft_name': 'A useful run', 'steps': 50}])
        self.assertIsNone(window.save_source)


if __name__ == '__main__':
    unittest.main()
