"""0.8.0 upgrades: settings, run metadata, previews, exports and CLI summaries."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import engine

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
except (ImportError, ValueError):
    app = None
else:
    import app


class EngineUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trainer = engine.Trainer(home=self.root, state=self.root / 'state', config=self.root / 'config')
        self.trainer.runs_dir = self.root / 'train-out'
        self.corpus = self.root / 'corpus.jsonl'
        self.corpus.write_text('\n'.join(json.dumps({'text': f'Document {i} ' + 'A' * 400}) for i in range(30)))
        self.values = {**self.trainer.preferences(), 'python': sys.executable, 'dataset': str(self.corpus),
                       'name': 'Plan', 'device': 'cpu', 'model_size': '2m'}

    def make_run(self, name='20261011-010203-000001', **meta):
        directory = self.trainer.runs_dir / name
        (directory / 'data').mkdir(parents=True)
        engine.atomic_json(directory / 'run.json', {'steps': 100, 'parameters': 1_981_512, 'model_size': '2m',
                                                    'batch_size': 2, 'grad_accum_steps': 3, 'device': 'cpu', **meta})
        engine.atomic_json(directory / 'result.json', {'completed_steps': 20, 'best_val_loss': 2.1, 'stopped_early': True,
                                                       'stop_reason': 'early_stop', 'total_training_seconds': 75.5,
                                                       'peak_vram_mib': 512.0})
        for split in ('train', 'val'):
            (directory / 'data' / f'{split}.bin').write_bytes(b'fake corpus')
        (directory / 'ckpt_best.pt').write_bytes(b'fixture weights, never unpickled')
        (directory / 'ckpt_latest.pt').write_bytes(b'latest fixture')
        return directory

    def test_new_settings_round_trip_and_invalid_values_are_rejected(self):
        self.trainer.save_preferences({'learning_rate': 1e-3, 'grad_clip': 2, 'schedule': 'wsd', 'thermal_pause': True,
                                       'early_stop_patience': 4, 'preview_top_p': .9, 'window_width': 900, 'run_sort': 3})
        prefs = self.trainer.preferences()
        self.assertEqual((prefs['learning_rate'], prefs['grad_clip'], prefs['schedule']), (1e-3, 2.0, 'wsd'))
        self.assertIsInstance(prefs['grad_clip'], float)
        self.assertTrue(prefs['thermal_pause'])
        self.assertEqual((prefs['early_stop_patience'], prefs['preview_top_p'], prefs['run_sort']), (4, .9, 3))
        for bad in ({'learning_rate': 0}, {'learning_rate': float('nan')}, {'min_lr_ratio': 2}, {'schedule': 'linear'},
                    {'val_fraction': .9}, {'thermal_pause': 'yes'}, {'run_sort': 9}, {'preview_temperature': True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.trainer.save_preferences(bad)
        self.trainer.config.write_text(json.dumps({'learning_rate': 5, 'schedule': 'linear', 'grad_clip': 'x'}))
        recovered = self.trainer.preferences()
        self.assertEqual((recovered['learning_rate'], recovered['schedule'], recovered['grad_clip']), (3e-4, 'cosine', 1.0))

    def test_defaults_exclude_user_paths(self):
        defaults = engine.Trainer.defaults()
        self.assertNotIn('python', defaults)
        self.assertNotIn('dataset', defaults)
        self.assertEqual(defaults['learning_rate'], 3e-4)

    def test_validate_checks_plan_numbers_and_schedule(self):
        values = self.trainer.validate({**self.values, 'grad_clip': 0, 'schedule': 'wsd'})
        self.assertEqual(values['grad_clip'], 0.0)
        for bad in ({'weight_decay': -1}, {'schedule': 'step'}, {'thermal_pause': 1}, {'eval_interval': 0}, {'seed': -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.trainer.validate({**self.values, **bad})

    def test_new_run_command_carries_plan_and_resume_keeps_saved_plan(self):
        values = self.trainer.validate({**self.values, 'learning_rate': 5e-4, 'schedule': 'wsd', 'warmup_steps': 12,
                                        'early_stop_patience': 3, 'max_minutes': 30, 'keep_snapshots': 2, 'thermal_pause': True})
        command = self.trainer.command(values, self.root / 'run')
        for flag, value in (('--learning-rate', '0.0005'), ('--schedule', 'wsd'), ('--warmup-steps', '12'), ('--seed', '1337'),
                            ('--early-stop-patience', '3'), ('--max-minutes', '30'), ('--keep-snapshots', '2')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn('--thermal-pause', command)
        run = self.make_run()
        resume = self.trainer.command({**values, 'steps': 200}, run, resume_id=run.name)
        self.assertNotIn('--learning-rate', resume)
        self.assertNotIn('--seed', resume)
        self.assertIn('--early-stop-patience', resume)
        prepare = self.trainer.command({**values, 'warmup_steps': 0}, self.root / 'run', prepare_only=True)
        self.assertNotIn('--warmup-steps', prepare)
        self.assertNotIn('--max-minutes', prepare)
        self.assertIn('--learning-rate', prepare)

    def test_run_listing_reports_outcome_pins_notes_and_snapshots(self):
        run = self.make_run()
        (run / 'ckpt_step00000010.pt').write_bytes(b'snapshot')
        (run / 'ckpt_step-bad.pt').write_bytes(b'ignored')
        listed = self.trainer.find_run(run.name)
        self.assertEqual((listed['stop_reason'], listed['training_seconds'], listed['snapshots']), ('early_stop', 75.5, 1))
        self.assertFalse(listed['pinned'])
        self.assertGreater(listed['created'], 0)
        self.trainer.update_run(run.name, pinned=True, notes='  Good tone; try a lower rate.  ')
        listed = self.trainer.find_run(run.name)
        self.assertTrue(listed['pinned'])
        self.assertEqual(listed['notes'], 'Good tone; try a lower rate.')
        self.assertEqual(json.loads((run / 'run.json').read_text())['steps'], 100)
        for bad in ({'notes': 'x' * (engine.NOTES_LIMIT + 1)}, {'notes': 'a\0b'}, {'pinned': 'yes'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.trainer.update_run(run.name, **bad)
        self.assertIn('notes', self.trainer.report(run.name))

    def test_disk_usage_throughput_and_created_time(self):
        run = self.make_run()
        self.assertEqual(self.trainer.disk_usage(run.name), engine.directory_size(run))
        self.assertGreater(engine.directory_size(run), 50)
        self.assertIsNone(self.trainer.throughput('2m', 'cpu'))
        for index, rate in enumerate((100, 300, 200)):
            other = self.make_run(f'20261011-01020{index}-000002')
            engine.atomic_json(other / 'progress.json', {'tokens_per_second': rate})
        engine.atomic_json(self.make_run('20261011-010209-000003', device='cuda') / 'progress.json', {'tokens_per_second': 9000})
        self.assertEqual(self.trainer.throughput('2m', 'cpu'), 200)
        self.assertEqual(self.trainer.throughput('2m', 'cuda'), 9000)
        stamp = engine.created_time(Path('20261011-123456-000000'), {})
        self.assertEqual(__import__('time').strftime('%H:%M:%S', __import__('time').localtime(stamp)), '12:34:56')
        self.assertEqual(engine.created_time(run, {'created_at': '2026-01-02T03:04:05+00:00'}), 1767323045.0)

    def test_previews_are_saved_bounded_and_validated(self):
        run = self.make_run()
        self.trainer.save_preview(run.name, {'text': 'Once upon a cube', 'prompt': 'Once', 'seed': 4, 'unknown': 'dropped',
                                             'top_p': float('nan')})
        saved = self.trainer.previews(run.name)
        self.assertEqual(saved[0]['text'], 'Once upon a cube')
        self.assertNotIn('unknown', saved[0])
        self.assertNotIn('top_p', saved[0])
        with (run / 'previews.jsonl').open('a') as handle:
            handle.write('not json\n')
        self.assertEqual(len(self.trainer.previews(run.name)), 1)
        with self.assertRaises(ValueError):
            self.trainer.save_preview(run.name, {'prompt': 'no text'})
        self.assertEqual(self.trainer.report(run.name)['previews'][0]['seed'], 4)

    def test_preview_and_export_commands_validate_inputs(self):
        run = self.make_run()
        command = self.trainer.preview_command(run.name, 'latest', 'Hi', sys.executable, tokens=50, temperature=0, top_k=5, top_p=.5, seed=9)
        for flag, value in (('--max-new-tokens', '50'), ('--temperature', '0.0'), ('--top-k', '5'), ('--top-p', '0.5'), ('--seed', '9')):
            self.assertEqual(command[command.index(flag) + 1], value)
        for bad in ({'tokens': 0}, {'temperature': 3}, {'top_k': 1.5}, {'top_p': 0}, {'seed': -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.trainer.preview_command(run.name, 'best', 'Hi', sys.executable, **bad)
        with self.assertRaises(ValueError):
            self.trainer.preview_command(run.name, 'snapshot', 'Hi', sys.executable)
        export = self.trainer.export_command(run.name, 'best', self.root / 'export', sys.executable)
        self.assertTrue(export[2].endswith('export_model.py'))
        (self.root / 'busy').mkdir(); (self.root / 'busy' / 'file').write_text('x')
        for destination in (self.root / 'busy', 'relative/folder', self.corpus):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.trainer.export_command(run.name, 'best', destination, sys.executable)
        self.assertIn('--resume', self.trainer.resume_command(run.name, 300))

    def test_cli_summaries_and_corpus_largest_file(self):
        self.assertIn('No runs', self.trainer.summary())
        run = self.make_run()
        self.trainer.update_run(run.name, pinned=True)
        line = self.trainer.summary()
        self.assertTrue(line.startswith('*BL'), line)
        self.assertIn('2.1000', line)
        self.assertIn('No desktop training job', self.trainer.status_text())
        (self.root / 'folder').mkdir()
        (self.root / 'folder' / 'small.txt').write_text('a')
        (self.root / 'folder' / 'large.md').write_text('a' * 100)
        info = engine.dataset_info(self.root / 'folder')
        self.assertEqual(info['largest'], ('large.md', 100))

    def test_whitecube_train_lists_runs_without_pytorch(self):
        self.make_run()
        env = {**os.environ, 'HOME': str(self.root), 'WHITECUBE_TRAIN_OUT': str(self.trainer.runs_dir),
               'XDG_CONFIG_HOME': str(self.root / 'config-home'), 'XDG_STATE_HOME': str(self.root / 'state-home')}
        script = HERE.parents[1] / 'bin/whitecube-train'
        listed = subprocess.run([sys.executable, '-I', str(script), '--list-runs'], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('20261011-010203-000001', listed.stdout)
        status = subprocess.run([sys.executable, '-I', str(script), '--status'], env=env, capture_output=True, text=True, timeout=30)
        self.assertIn('No desktop training job', status.stdout)


@unittest.skipUnless(app, 'System GTK bindings are required')
class PresentationUpgradeTests(unittest.TestCase):
    def test_smoothing_follows_trend_without_moving_steps(self):
        smoothed = app.smooth_series([(0, 10), (1, 0), (5, 0)], weight=.5)
        self.assertEqual(smoothed, [(0, 10), (1, 5), (5, 2.5)])
        self.assertEqual(app.smooth_series([]), [])

    def test_sorting_keeps_pinned_runs_first(self):
        runs = [{'id': 'a', 'name': 'beta', 'created': 1, 'val_loss': 2.0, 'parameters': 1, 'pinned': False},
                {'id': 'b', 'name': 'Alpha', 'created': 3, 'val_loss': None, 'parameters': 9, 'pinned': False},
                {'id': 'c', 'name': 'gamma', 'created': 2, 'val_loss': 1.0, 'parameters': 5, 'pinned': True}]
        order = lambda mode: [run['id'] for run in app.sort_runs(runs, mode)]
        self.assertEqual(order(0), ['c', 'b', 'a'])
        self.assertEqual(order(1), ['c', 'a', 'b'])
        self.assertEqual(order(2), ['c', 'b', 'a'])
        self.assertEqual(order(3), ['c', 'a', 'b'])
        self.assertEqual(order(4), ['c', 'b', 'a'])

    def test_bytes_estimates_and_log_kinds(self):
        self.assertEqual(app.format_bytes(512), '512 bytes')
        self.assertEqual(app.format_bytes(3 * 1024**2), '3.0 MiB')
        self.assertEqual(app.format_bytes(None), '—')
        text = app.plan_estimate(100, 2, 2, corpus_bytes=102_400, val_fraction=0, tokens_per_second=1024)
        self.assertIn('102,400 training tokens', text)
        self.assertIn('1.00 passes', text)
        self.assertIn('1m 40s', text)
        self.assertEqual(app.plan_estimate(1, 1, 1), '256 training tokens')
        self.assertEqual(app.log_line_kind('Traceback (most recent call last):'), 'error')
        self.assertEqual(app.log_line_kind('Thermal pause at GPU 66 °C'), 'warning')
        self.assertEqual(app.log_line_kind('step 49 | new best val_loss 2.1'), 'success')
        self.assertIsNone(app.log_line_kind('step 10 | lr 3e-4'))

    def test_run_details_describe_plan_outcome_and_storage(self):
        run = {'name': 'Story model', 'directory': '/runs/x', 'parameters': 1_981_512, 'val_loss': 1.5, 'best': True,
               'latest': False, 'snapshots': 2, 'created': 0, 'training_seconds': 3700, 'peak_vram_mib': 600.0,
               'stop_reason': 'time_limit', 'detail': '', 'meta': {'device': 'cuda', 'batch_size': 4, 'grad_accum_steps': 2,
                                                                   'learning_rate': 5e-4, 'schedule': 'wsd', 'seed': 7, 'dataset': '/data'}}
        text = app.run_details(run, 2048)
        for part in ('1,981,512 parameters', '2 snapshots', 'GPU · batch 4 × 2 · peak LR 0.0005 · WSD schedule · seed 7',
                     'Corpus: /data', 'trained 1h 1m', 'peak VRAM 600 MiB', '2.0 KiB on disk', 'Time limit reached'):
            self.assertIn(part, text)


if __name__ == '__main__':
    unittest.main()
