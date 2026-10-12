#!/usr/bin/python3 -I
"""Exercise the actual native GTK trainer on an isolated Wayland compositor."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def inside(fixture, screenshots):
    sys.path.insert(0, str(ROOT / 'apps/cube-train'))
    import app
    import engine
    fixture, screenshots = Path(fixture), Path(screenshots)
    trainer = engine.Trainer()
    run = trainer.runs_dir / 'fixture-run'
    (run / 'data').mkdir(parents=True)
    engine.atomic_json(run / 'run.json', {'steps': 10, 'model_size': '5m', 'parameters': 4_987_448,
                                          'display_name': 'Native fixture', 'batch_size': 1, 'grad_accum_steps': 1})
    engine.atomic_json(run / 'result.json', {'completed_steps': 4, 'best_val_loss': 2.4, 'stopped_early': True})
    engine.atomic_json(run / 'progress.json', {'completed_steps': 4, 'loss': 4.5, 'tokens_per_second': 1234,
                                             'tokens_processed': 4096, 'gradient_norm': 0.8, 'eta_seconds': 65})
    for split in ('train', 'val'): (run / 'data' / f'{split}.bin').write_bytes(b'UI fixture data')
    engine.atomic_json(run / 'data/corpus.json', {'train_tokens': 700, 'val_tokens': 700, 'total_tokens': 1400})
    (run / 'ckpt_best.pt').write_bytes(b'Never loaded or executed')
    (run / 'ckpt_latest.pt').write_bytes(b'Latest UI fixture')
    (run / 'console.log').write_text('Fixture log\nTraining paused after four steps.\n')
    (run / 'train_log.jsonl').write_text('\n'.join(json.dumps(item) for item in [
        {'step': 0, 'total_loss': 5.2}, {'step': 1, 'total_loss': 4.8}, {'step': 3, 'val_loss': 2.4}]))
    options = argparse.Namespace(page=None, screenshot=None, no_probe=True)

    class SmokeApp(app.TrainerApp):
        failure = None
        def do_activate(self):
            super().do_activate(); app.GLib.timeout_add(200, self.exercise)
        def guarded(self, function):
            try: return function()
            except BaseException:
                self.failure = traceback.format_exc(); print(self.failure, file=sys.stderr); self.quit(); return False
        def exercise(self): return self.guarded(self.actions)
        def actions(self):
            w = self.window
            assert not w.stop_button.get_sensitive()
            w.size_picker.set_selected(2); assert '4,987,448' in w.model_detail.get_text()
            w.size_picker.set_selected(3); assert '10,034,184' in w.model_detail.get_text()
            for index in range(3):
                assert w.key_pressed(None, app.Gdk.KEY_1 + index, 0, app.Gdk.ModifierType.CONTROL_MASK)
                assert w.stack.get_visible_child_name() == ('train', 'runs', 'activity')[index]
            w.run_list.select_row(w.run_list.get_row_at_index(0))
            assert w.selected_id == 'fixture-run'
            assert w.resume_button.get_sensitive() and w.best_button.get_sensitive()
            assert w.latest_button.get_sensitive() and w.delete_button.get_sensitive()
            assert '2.4000' in w.run_detail.get_text()
            assert w.resume_steps.get_value() > 4
            assert 'Fixture log' in w.last_log and len(w.losses) == 3
            assert '1,234 tokens/s' in w.metrics_label.get_text() and '4,096 tokens processed' in w.metrics_label.get_text()
            assert 'Gradient norm 0.8' in w.metrics_label.get_text()
            w.dtype_picker.set_selected(1)
            w.form['eval_iters'].set_value(7); w.form['log_interval'].set_value(2)
            w.form['early_stop_min_delta'].set_value(.025); w.save_draft()
            prefs = trainer.preferences()
            assert (prefs['dtype'], prefs['eval_iters'], prefs['log_interval'], prefs['early_stop_min_delta']) == ('float32', 7, 2, .025)
            assert w.detail_tabs.get_child_by_name('benchmark') and w.benchmark_button.get_sensitive()
            assert w.benchmark_kinds[0] == ('best', 'latest')
            def benchmark_process(command, **kwargs):
                output = Path(command[command.index('--output') + 1])
                kind = command[command.index('--checkpoint') + 1]
                report = {'kind': 'whitecube_checkpoint_benchmark', 'created_at': '2026-10-11T16:00:00Z',
                          'checkpoint': {'name': f'ckpt_{kind}.pt', 'completed_steps': 4},
                          'settings': {'seed': 2026}, 'validation': {'loss': 2.1, 'bits_per_byte': 3.02, 'tokens': 20480},
                          'generation': {'samples': [{'prompt': 'A cube', 'continuation': ' learned to balance.'}]}}
                engine.atomic_json(output, report)
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(report), stderr='')
            with patch.object(w, 'background', side_effect=lambda work, done: done(work(), None)), \
                    patch.object(app.subprocess, 'run', side_effect=benchmark_process):
                w.benchmark()
            assert len(w.benchmark_reports) == 2 and not w.sampling
            assert '2.1000' in w.benchmark_output.get_text() and 'learned to balance' in w.benchmark_output.get_text()
            assert w.benchmark_json_button.get_sensitive() and w.benchmark_csv_button.get_sensitive()
            benchmark_exports = []
            with patch.object(w, 'choose_export', side_effect=lambda title, name, produce: benchmark_exports.append(produce())):
                w.export_benchmarks(); w.export_benchmarks(True)
            assert len(json.loads(benchmark_exports[0])) == 2 and 'checkpoint_sha256' in benchmark_exports[1]
            w.run_search.set_text('no matching run'); w.filter_runs()
            assert '0 of 1' in w.run_count.get_text()
            assert not w.run_list.get_row_at_index(0).get_visible()
            w.run_search.set_text('NATIVE'); w.filter_runs()
            assert '1 of 1' in w.run_count.get_text()
            w.run_filter.set_selected(2); assert '0 of 1' in w.run_count.get_text()
            w.run_filter.set_selected(1); assert '1 of 1' in w.run_count.get_text()
            w.run_filter.set_selected(0); w.run_search.set_text(''); w.filter_runs()
            w.resume_steps.set_value(4)
            assert not w.resume_button.get_sensitive() and 'above 4' in w.resume_hint.get_text()
            w.resume_steps.set_value(20)
            assert w.resume_button.get_sensitive() and '16 additional' in w.resume_hint.get_text()
            w.use_run_settings()
            assert w.form['name'].get_text() == 'Native fixture (copy)'
            assert w.size_picker.get_selected() == 2 and w.form['batch_size'].get_value() == 1
            w.save_draft(); assert trainer.preferences()['draft_name'] == 'Native fixture (copy)'
            assert w.reuse_corpus_button.get_sensitive()
            w.reuse_prepared_corpus()
            assert w.form['dataset'].get_text() == str(run / 'data')
            w.form['deduplicate'].set_active(True); w.form['shuffle_files'].set_active(True)
            w.form['min_document_bytes'].set_value(100); w.form['max_document_bytes'].set_value(8000)
            w.form['batch_rows'].set_value(8); w.save_draft()
            prefs = trainer.preferences()
            assert prefs['deduplicate'] and prefs['shuffle_files'] and prefs['min_document_bytes'] == 100
            assert prefs['max_document_bytes'] == 8000 and prefs['batch_rows'] == 8
            w.form['batch_size'].set_value(2); w.form['grad_accum_steps'].set_value(3)
            assert '1,536 byte tokens' in w.batch_summary.get_text()
            with patch.object(app.engine, 'read_metrics', wraps=app.engine.read_metrics) as read:
                w.refresh(); w.refresh(); assert read.call_count == 0
                with (run / 'train_log.jsonl').open('a') as metrics:
                    metrics.write('\n' + json.dumps({'step': 4, 'total_loss': 4.4}))
                w.refresh(); assert read.call_count == 1 and len(w.losses) == 4
            w.stack.set_visible_child_name('activity')
            assert not w.key_pressed(None, app.Gdk.KEY_Return, 0, app.Gdk.ModifierType.CONTROL_MASK)
            assert w.key_pressed(None, app.Gdk.KEY_f, 0, app.Gdk.ModifierType.CONTROL_MASK)
            w.log_search.set_text('fixture'); w.find_log(restart=True)
            assert 'line 1' in w.log_search_status.get_text() and not w.follow.get_active()
            w.log_search.set_text('absent'); w.find_log(restart=True)
            assert 'No matches' in w.log_search_status.get_text()
            w.log_search.set_text(''); w.find_log(restart=True); w.follow.set_active(True)
            exports = []
            with patch.object(w, 'choose_export', side_effect=lambda title, name, produce: exports.append((name, produce()))):
                w.export_log(); w.export_metrics(); w.export_report()
            assert 'Fixture log' in exports[0][1] and 'total_loss' in exports[1][1]
            assert json.loads(exports[2][1])['configuration']['model_size'] == '5m'
            for name, text in exports:
                app.write_export(fixture / name, text)
                assert (fixture / name).read_bytes().decode('utf-8') == text
            moved = fixture / 'temporarily-moved-run'
            run.rename(moved); w.sample_output.set_text('Old preview'); w.refresh()
            assert w.selected_id is None and not w.losses and not w.sample_output.get_text()
            assert 'Choose a run' in w.run_detail.get_text() and not w.resume_button.get_sensitive()
            moved.rename(run); w.refresh()
            assert w.selected_id == 'fixture-run' and len(w.losses) == 4
            w.sample_kind.set_selected(1); assert w.sample_button.get_sensitive()
            (run / 'ckpt_best.pt').rename(run / 'best-backup')
            w.refresh(True); assert w.sample_button.get_sensitive()
            w.sample_kind.set_selected(0); assert not w.sample_button.get_sensitive()
            (run / 'best-backup').rename(run / 'ckpt_best.pt'); w.refresh(True)
            w.copy_path(); assert w.get_display().get_clipboard().get_formats().to_string()
            w.rename()
            dialog = w.get_visible_dialog(); assert dialog
            found = []
            def walk(widget):
                if isinstance(widget, app.Gtk.Entry): found.append(widget)
                child = widget.get_first_child()
                while child: walk(child); child = child.get_next_sibling()
            walk(dialog)
            found[0].set_text('Renamed native run'); found[0].emit('activate')
            assert trainer.find_run('fixture-run')['name'] == 'Renamed native run'
            assert trainer.chat_command('fixture-run', 'best')[-1] == 'fixture-run/ckpt_best.pt'
            report = trainer.report('fixture-run'); assert report['configuration']['model_size'] == '5m'
            w.form['steps'].set_value(3000); w.save_draft(); assert trainer.preferences()['steps'] == 3000
            w.form['stagger_training'].set_active(True); w.form['low_priority'].set_active(True)
            w.form['burst_seconds'].set_value(20); w.form['cooldown_seconds'].set_value(15)
            w.save_draft()
            prefs = trainer.preferences()
            assert prefs['stagger_training'] and prefs['low_priority']
            assert prefs['burst_seconds'] == 20 and prefs['cooldown_seconds'] == 15
            # 0.8.0: plan presets, tuning, reset, pins, notes, sorting, previews, chart and log tools
            w.plan_preset.set_selected(1); assert w.form['steps'].get_value() == 20
            w.form['steps'].set_value(777); assert w.plan_preset.get_selected() == 0
            assert 'training tokens' in w.plan_label.get_text()
            w.form['learning_rate'].set_value(.0005); w.schedule_picker.set_selected(1); w.form['thermal_pause'].set_active(True)
            w.form['early_stop_patience'].set_value(3); w.save_draft(); prefs = trainer.preferences()
            assert prefs['learning_rate'] == .0005 and prefs['schedule'] == 'wsd' and prefs['thermal_pause'] and prefs['early_stop_patience'] == 3
            w.reset_settings(); w.save_draft(); prefs = trainer.preferences()
            assert prefs['learning_rate'] == 3e-4 and prefs['schedule'] == 'cosine' and prefs['steps'] == 1000
            assert not prefs['thermal_pause'] and prefs['draft_name'] == 'Native fixture (copy)'
            assert not prefs['deduplicate'] and not prefs['shuffle_files'] and prefs['max_document_bytes'] == 0 and prefs['batch_rows'] == 32
            w.stack.set_visible_child_name('runs')
            w.toggle_pin(); assert trainer.find_run('fixture-run')['pinned'] and w.pin_button.get_label() == 'Unpin'
            w.run_filter.set_selected(4); assert '1 of 1' in w.run_count.get_text(); w.run_filter.set_selected(0)
            w.notes_view.get_buffer().set_text('Fixture note'); assert w.notes_button.get_sensitive()
            w.save_notes(); assert trainer.find_run('fixture-run')['notes'] == 'Fixture note'
            w.run_sort.set_selected(2); w.save_draft(); assert trainer.preferences()['run_sort'] == 2
            assert 'Renamed native run' in w.run_list.get_row_at_index(0).run_record['name']
            w.copy_resume_command()
            assert w.weights_button.get_sensitive() and w.command_button.get_sensitive()
            w.sample_kind.set_selected(2); assert w.sample_button.get_sensitive(); w.sample_kind.set_selected(0)
            assert 'Created' in w.run_detail.get_text()
            w.stack.set_visible_child_name('activity')
            w.smooth_check.set_active(True); w.lr_check.set_active(True)
            w.chart_geometry = (48, 500, 4); w.chart_pointer(300)
            assert 'Step' in w.chart_readout.get_text() and w.chart_readout.get_visible()
            w.chart_pointer(None); assert not w.chart_readout.get_visible()
            w.log_search.set_text('paused'); w.matching_only.set_active(True)
            log = w.log_view.get_buffer(); assert log.get_text(log.get_start_iter(), log.get_end_iter(), False) == 'Training paused after four steps.'
            w.matching_only.set_active(False); w.log_search.set_text('')
            assert 'Fixture log' in log.get_text(log.get_start_iter(), log.get_end_iter(), False)
            assert w.key_pressed(None, app.Gdk.KEY_F1, 0, 0) and w.get_visible_dialog()
            w.get_visible_dialog().close()
            app.GLib.timeout_add(250, self.confirmation)
            return False
        def confirmation(self): return self.guarded(self.confirmation_guarded)
        def confirmation_guarded(self):
            w = self.window
            w.delete(); dialog = w.get_visible_dialog(); assert dialog
            widgets = []
            def walk(widget):
                widgets.append(widget)
                child = widget.get_first_child()
                while child: walk(child); child = child.get_next_sibling()
            walk(dialog)
            confirm = next(widget for widget in widgets if isinstance(widget, app.Gtk.CheckButton))
            remove = next(widget for widget in widgets if isinstance(widget, app.Gtk.Button) and widget.get_label() == 'Move to Trash')
            assert not remove.get_sensitive()
            confirm.set_active(True); assert remove.get_sensitive()
            confirm.set_active(False); assert not remove.get_sensitive()
            assert not w.key_pressed(None, app.Gdk.KEY_Return, 0, app.Gdk.ModifierType.CONTROL_MASK)
            dialog.close(); assert (run / 'ckpt_latest.pt').exists()
            w.form['dataset'].set_text(str(fixture / 'missing-dataset'))
            w.start()
            self.deadline = time.monotonic() + 5
            app.GLib.timeout_add(20, self.check_error)
            return False
        def check_error(self): return self.guarded(self.error_ready)
        def error_ready(self):
            w = self.window
            if w.launching:
                assert time.monotonic() < self.deadline; return True
            assert 'existing corpus' in w.status.get_text(), w.status.get_text()
            assert w.start_button.get_sensitive() and not w.stop_button.get_sensitive()
            app.GLib.timeout_add(50, lambda: self.guarded(self.upgrades)); return False
        def wait(self, condition, then, seconds=10):
            deadline = time.monotonic() + seconds
            def poll():
                def check():
                    if condition():
                        then(); return False
                    assert time.monotonic() < deadline, f'timed out waiting in {then.__name__}'
                    return True
                return self.guarded(check)
            app.GLib.timeout_add(40, poll)
        def widgets(self, root):
            found = []
            def walk(widget):
                found.append(widget)
                child = widget.get_first_child()
                while child: walk(child); child = child.get_next_sibling()
            walk(root); return found
        def upgrades(self):
            # 0.9.0: in-place rows, checkpoints incl. snapshots, context menu, compare, notebook export, cleanup, fields
            w = self.window
            second = trainer.runs_dir / 'second-run'; (second / 'data').mkdir(parents=True)
            engine.atomic_json(second / 'run.json', {'steps': 4, 'model_size': '2m', 'parameters': 1_981_512, 'display_name': 'Second fixture'})
            engine.atomic_json(second / 'result.json', {'completed_steps': 4, 'best_val_loss': 2.0, 'stop_reason': 'completed'})
            for split in ('train', 'val'): (second / 'data' / f'{split}.bin').write_bytes(b'UI fixture data')
            (second / 'ckpt_best.pt').write_bytes(b'best'); (second / 'ckpt_step00000002.pt').write_bytes(b'snapshot')
            (second / 'train_log.jsonl').write_text('\n'.join(json.dumps(item) for item in [
                {'step': 0, 'total_loss': 5.0}, {'step': 1, 'val_loss': 3.0}, {'step': 3, 'val_loss': 2.0}]))
            failed = trainer.runs_dir / 'failed-run'; failed.mkdir()
            engine.atomic_json(failed / 'status.json', {'state': 'failed', 'detail': 'No recognized text column'})
            w.stack.set_visible_child_name('runs'); w.selected_id = 'fixture-run'; w.refresh(True)
            row = w.run_rows['fixture-run']
            engine.atomic_json(run / 'progress.json', {'completed_steps': 5, 'loss': 4.4})
            w.refresh(); assert w.run_rows['fixture-run'] is row and '5/' in row.meta.get_text(), row.meta.get_text()
            assert row.is_selected() and len(w.spark_cache['second-run'][1]) == 2
            assert w.cleanup_button.get_visible()
            w.run_list.select_row(w.run_rows['second-run']); assert w.selected_id == 'second-run'
            assert [item['kind'] for item in w.checkpoint_items] == ['best', 'step00000002'], w.checkpoint_items
            assert w.actions['export-notebook'].get_enabled() and not w.actions['chat-latest'].get_enabled()
            assert '2.885' in w.detail_tiles.tile_signature  # bits per byte of 2.0
            w.run_list.select_row(w.run_rows['fixture-run']); w.refresh()
            index = w.compare_ids.index('second-run') + 1; w.compare_picker.set_selected(index)
            assert w.compare_losses and w.compare_name == 'Second fixture'
            w.chart_geometry = (48, 500, 4); w.chart_pointer(300); assert 'Second fixture' in w.chart_readout.get_text()
            w.chart_pointer(None); w.compare_picker.set_selected(0); assert not w.compare_losses
            w.run_list.select_row(w.run_rows['second-run']); w.refresh()
            w.export_notebook('step00000002'); dialog = w.get_visible_dialog(); assert dialog
            entries = [item for item in self.widgets(dialog) if isinstance(item, app.Gtk.Entry)]
            entries[0].set_text(str(fixture / 'exports'))
            assert entries[1].get_text() == 'second-fixture-step00000002-notebook'
            self.export_button = next(item for item in self.widgets(dialog) if isinstance(item, app.Gtk.Button) and item.get_label() == 'Export')
            (fixture / 'exports').mkdir()
            self.dialog = dialog
            app.GLib.timeout_add(350, lambda: self.guarded(self.dialog_shot)); return False
        def dialog_shot(self):
            w, dialog = self.window, self.dialog
            app.screenshot(w, str(screenshots / 'cube-train-notebook-dialog.png'))
            for item in self.widgets(dialog):
                if isinstance(item, app.Gtk.CheckButton) and getattr(item, 'option_text', '').startswith('Open in Jupyter'): item.set_active(False)
            self.export_button.emit('clicked')
            self.wait(lambda: w.last_notebook is not None, self.exported)
            return False
        def exported(self):
            w = self.window
            notebook = Path(w.last_notebook)
            assert notebook == fixture / 'exports/second-fixture-step00000002-notebook/second-fixture.ipynb', notebook
            document = json.loads(notebook.read_text())
            assert document['nbformat'] == 4 and any('ckpt_step00000002.pt' in ''.join(cell['source']) for cell in document['cells'])
            assert (notebook.parent / 'checkpoint/ckpt_step00000002.pt').read_bytes() == b'snapshot'
            assert (notebook.parent / 'whitecube_pipeline/tensegrity_lm.py').is_file() and notebook.with_suffix('.py').is_file()
            assert not w.get_visible_dialog() and 'Last export' in w.notebook_status.get_text()
            w.stack.set_visible_child_name('runs')
            found, bounds = w.run_rows['second-run'].compute_bounds(w.run_list); assert found and bounds.get_y() >= 0
            w.open_run_menu(20, bounds.get_y() + 5); assert w.run_menu.get_visible() and w.selected_id == 'second-run'
            w.run_menu.popdown()
            assert [run['id'] for run in trainer.cleanup_candidates()] == ['failed-run']
            w.clean_up_failed(); dialog = w.get_visible_dialog(); assert dialog
            confirm = next(item for item in self.widgets(dialog) if isinstance(item, app.Gtk.CheckButton))
            go = next(item for item in self.widgets(dialog) if isinstance(item, app.Gtk.Button) and item.get_label() == 'Move to Trash')
            assert not go.get_sensitive(); confirm.set_active(True); go.emit('clicked')
            self.wait(lambda: not (trainer.runs_dir / 'failed-run').exists() and not w.get_visible_dialog(), self.cleaned)
        def cleaned(self):
            w = self.window
            assert 'failed-run' not in w.run_rows and not w.cleanup_button.get_visible()
            corpus = fixture / 'mixed-corpus'; corpus.mkdir()
            (corpus / 'stories.jsonl').write_text(json.dumps({'story': 'A long story. ' * 30}) + '\n')
            (corpus / 'questions.jsonl').write_text(json.dumps({'question': 'Why?', 'answer': 'Because. ' * 30}) + '\n')
            w.form['column'].set_text(''); w.stack.set_visible_child_name('train')
            files = app.Gdk.FileList.new_from_list([app.Gio.File.new_for_path(str(corpus))])
            assert w.dropped(None, files) and w.form['dataset'].get_text() == str(corpus)
            self.wait(lambda: w.field_picker.get_sensitive(), self.fields_ready)
        def fields_ready(self):
            w = self.window
            assert set(w.field_names) == {'story', 'question', 'answer'}, w.field_names
            assert 'questions.jsonl' in w.field_info.get_text() and 'would fail' in w.field_info.get_text()
            w.start(); assert 'no usable text field' in w.status.get_text(), w.status.get_text()
            app.GLib.timeout_add(350, lambda: self.guarded(self.fields_shot))
        def fields_shot(self):
            w = self.window
            app.screenshot(w, str(screenshots / 'cube-train-fields.png'))
            w.field_picker.set_selected(w.field_names.index('answer') + 1); assert w.form['column'].get_text() == 'answer'
            assert 'stories.jsonl' in w.field_info.get_text()
            w.field_picker.set_selected(0); assert w.form['column'].get_text() == ''
            corpus = fixture / 'mixed-corpus'
            trainer.save_preferences({'recent_datasets': [str(corpus)]}); w.update_recent_menu()
            assert w.recent_button.get_sensitive() and w.recent_button.get_menu_model().get_n_items() == 1
            w.use_recent_dataset(str(fixture / 'missing-dataset')); assert w.form['dataset'].get_text().endswith('missing-dataset')
            w.stack.set_visible_child_name('runs'); w.run_list.select_row(w.run_rows['second-run'])
            assert w.key_pressed(None, app.Gdk.KEY_E, 0, app.Gdk.ModifierType.CONTROL_MASK | app.Gdk.ModifierType.SHIFT_MASK)
            assert w.get_visible_dialog(); w.get_visible_dialog().close()
            app.GLib.timeout_add(250, lambda: self.guarded(self.rename_key)); return False
        def rename_key(self):
            w = self.window
            assert w.key_pressed(None, app.Gdk.KEY_F2, 0, 0) and w.get_visible_dialog()
            w.get_visible_dialog().close()
            assert w.run_list.get_selected_row() is w.run_rows['second-run']
            assert w.run_list_key(None, app.Gdk.KEY_Menu, 0, 0) and w.run_menu.get_visible(); w.run_menu.popdown()
            w.run_list.select_row(w.run_rows['fixture-run']); w.refresh()
            self.pages = iter(('train', 'runs', 'activity'))
            app.GLib.timeout_add(250, lambda: self.guarded(self.capture_next)); return False
        def capture_next(self):
            try: self.page = next(self.pages)
            except StopIteration:
                self.window.stack.set_visible_child_name('runs')
                self.window.detail_tabs.set_visible_child_name('benchmark')
                app.GLib.timeout_add(350, lambda: self.guarded(self.capture_benchmark))
                print('PASS: native model options, run selection/search/filters, rename, resume validation, run-settings reuse, deletion confirmation, cooling controls, effective batch summary, cached loss graph, log search/follow, draft names, contextual shortcuts, atomic report/metrics/log exports, checkpoint choices, Chat commands, invalid-start recovery, plan presets/tuning/reset, pins, notes, sorting, preview modes, chart readout, matching log lines, shortcuts dialog, in-place run rows, snapshot checkpoints, run menu, compare overlay, notebook export dialog, failed-run cleanup, text-field probe and start guard, recent corpora, drop target, notebook/rename keys')
                return False
            self.window.stack.set_visible_child_name(self.page)
            app.GLib.timeout_add(350, self.capture); return False
        def capture_benchmark(self):
            app.screenshot(self.window, str(screenshots / 'cube-train-benchmark.png'))
            self.quit(); return False
        def capture(self):
            return self.guarded(self.capture_guarded)
        def capture_guarded(self):
            app.screenshot(self.window, str(screenshots / f'cube-train-{self.page}.png'))
            self.capture_next(); return False

    application = SmokeApp(options, trainer)
    result = application.run([sys.argv[0]])
    return 1 if application.failure else result


def run(screenshots):
    screenshots = Path(screenshots).resolve(); screenshots.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(prefix='whitecube-trainer-ui-') as temp:
        fixture = Path(temp)
        for name in ('home', 'runtime', 'state', 'data', 'cache'): (fixture / name).mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(fixture / 'home'), XDG_RUNTIME_DIR=str(fixture / 'runtime'),
                   XDG_CONFIG_HOME=str(fixture / 'home/.config'), XDG_STATE_HOME=str(fixture / 'state'),
                   XDG_DATA_HOME=str(fixture / 'data'), XDG_CACHE_HOME=str(fixture / 'cache'),
                   WLR_BACKENDS='headless', WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='1',
                   GDK_BACKEND='wayland', GSK_RENDERER='cairo', NO_AT_BRIDGE='1', GSETTINGS_BACKEND='memory')
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'DBUS_SESSION_BUS_ADDRESS', 'WHITECUBE_TRAIN_OUT'):
            env.pop(key, None)
        with (fixture / 'compositor.log').open('w') as output:
            compositor = subprocess.Popen([str(ROOT / 'build/whitecube')], env=env, stdout=output, stderr=output)
            try:
                socket = fixture / 'runtime/wayland-0'; deadline = time.monotonic() + 8
                while not socket.exists() and compositor.poll() is None and time.monotonic() < deadline: time.sleep(.04)
                assert socket.exists(), (fixture / 'compositor.log').read_text()
                env['WAYLAND_DISPLAY'] = socket.name
                result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--inside', str(fixture), str(screenshots)],
                                        env=env, capture_output=True, text=True, timeout=60)
                assert result.returncode == 0 and 'Traceback' not in result.stderr, result.stdout + result.stderr
                print(result.stdout.rstrip())
                for page in ('train', 'runs', 'activity'):
                    image = screenshots / f'cube-train-{page}.png'
                    assert image.is_file() and image.read_bytes().startswith(b'\x89PNG'), f'{page} did not render'
                print('PASS: all three native GTK4 trainer pages rendered on Wayland')
            finally:
                compositor.terminate()
                try: compositor.wait(timeout=5)
                except subprocess.TimeoutExpired: compositor.kill(); compositor.wait()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--inside': raise SystemExit(inside(sys.argv[2], sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT / 'build')
    run(parser.parse_args().screenshots)
