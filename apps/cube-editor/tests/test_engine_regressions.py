"""Project transaction, persisted-input and operation-controller regressions."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from test_cube_editor import ProjectFixture


class ProjectBoundaryTests(ProjectFixture):
    def test_record_check_preserves_all_newer_project_metadata(self):
        stale = engine.Engine(engine.Project.load(self.project.directory))
        output = self.project.output_path
        output.parent.mkdir()
        output.write_bytes(b'release')
        changes = {'name': 'Latest project', 'state': 'built',
                   'recipe': {'install_packages': ['curl']},
                   'last_build': {'path': str(output), 'sha256': 'release'},
                   'releases': [{'path': '/older.iso', 'sha256': 'older'}],
                   'future_metadata': {'retained': True}}
        self.project.save(**changes)
        stale.record_check('BIOS live', 'passed', 'Reached desktop')
        saved = engine.Project.load(self.project.directory).data
        for key, value in changes.items():
            self.assertEqual(saved[key], value, key)
        self.assertEqual(len(saved['release_checks']), 1)

    def test_transaction_refreshes_whole_document_once_and_is_reentrant(self):
        stale = engine.Project.load(self.project.directory)
        self.project.save(name='Latest name', state='built')
        with stale.lock():
            self.assertEqual(stale.data['name'], 'Latest name')
            stale.data['name'] = 'Inside transaction'
            with stale.lock():
                stale.save(last_error='reviewed')
            self.assertEqual(stale.data['name'], 'Inside transaction')
        saved = engine.Project.load(self.project.directory).data
        self.assertEqual(saved['name'], 'Inside transaction')
        self.assertEqual(saved['state'], 'built')

    def test_other_thread_cannot_enter_shared_project_transaction(self):
        attempting = threading.Event()
        entered = threading.Event()
        errors = []

        def other_thread():
            attempting.set()
            try:
                with self.project.lock():
                    entered.set()
            except Exception as error:
                errors.append(error)

        with self.project.lock():
            thread = threading.Thread(target=other_thread)
            thread.start()
            self.assertTrue(attempting.wait(2))
            self.assertFalse(entered.wait(0.1))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(entered.is_set())
        self.assertFalse(errors)

    def test_save_from_other_thread_waits_and_preserves_transaction_state(self):
        attempting = threading.Event()
        completed = threading.Event()
        errors = []

        def other_thread():
            attempting.set()
            try:
                self.project.save(name='Thread update')
                completed.set()
            except Exception as error:
                errors.append(error)

        with self.project.lock():
            thread = threading.Thread(target=other_thread)
            thread.start()
            self.assertTrue(attempting.wait(2))
            self.assertFalse(completed.wait(0.1))
            self.project.save(state='built')
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(completed.is_set())
        self.assertFalse(errors)
        saved = engine.Project.load(self.project.directory).data
        self.assertEqual(saved['name'], 'Thread update')
        self.assertEqual(saved['state'], 'built')

    def test_saved_project_rejects_incomplete_and_wrong_type_fields(self):
        original = copy.deepcopy(self.project.data)
        malformed = [None, [], {'schema': 1}, {**original, 'schema': True}]
        for updates in (
            {'name': []}, {'id': ''}, {'source_iso': None}, {'source_iso': 'relative.iso'},
            {'source_size': True}, {'source_mtime_ns': 'yesterday'}, {'output_dir': 'relative'},
            {'output_name': '../release.iso'}, {'auto_snapshot': 'yes'}, {'processors': '2'},
            {'recipe': None}, {'recipe': {'payloads': [{'source': '/file'}]}},
            {'inspection': []}, {'inspection': {'layer_chain': 'filesystem.squashfs'}},
            {'inspection': {'warnings': [None]}}, {'inspection': {'size_bytes': 'large'}},
            {'inspection': {'bios': 'yes'}}, {'last_build': []}, {'last_build': {'path': 3}},
            {'releases': {}}, {'releases': [None]}, {'release_checks': {}},
            {'release_checks': [{}]},
        ):
            malformed.append({**original, **updates})
        for document in malformed:
            with self.subTest(document=document):
                engine.atomic_json(self.project.directory / 'cube-editor.json', document)
                with self.assertRaises(engine.CubeError):
                    engine.Project.load(self.project.directory)

    def test_optional_fields_are_defaulted_without_requiring_source_file(self):
        required = ('schema', 'id', 'name', 'source_iso', 'source_size', 'source_mtime_ns', 'output_dir')
        data = {key: self.project.data[key] for key in required}
        engine.atomic_json(self.project.directory / 'cube-editor.json', data)
        self.source.unlink()
        loaded = engine.Project.load(self.project.directory)
        self.assertEqual(loaded.data['recipe'], {})
        self.assertEqual(loaded.data['inspection'], {})
        self.assertEqual(loaded.data['releases'], [])
        self.assertEqual(loaded.data['release_checks'], [])
        self.assertEqual(loaded.data['processors'], 2)
        self.assertTrue(engine.Engine(loaded).plan('build'))

    def test_invalid_save_preserves_disk_and_in_memory_state(self):
        before = copy.deepcopy(self.project.data)
        with self.assertRaises(engine.CubeError):
            self.project.save(name=None, recipe=[])
        self.assertEqual(self.project.data, before)
        self.assertEqual(engine.Project.load(self.project.directory).data, before)

    def test_package_text_is_normalized_at_import_load_and_save(self):
        path = self.path / 'recipe.json'
        for value in ('curl nano', ['curl', 'nano']):
            with self.subTest(value=value):
                recipe = {'install_packages': value, 'remove_packages': 'vim', 'script': 'echo done'}
                engine.atomic_json(path, {'format': 'cube-editor-recipe', 'version': 1, 'recipe': recipe})
                imported, missing = engine.import_recipe(path)
                self.assertEqual(imported['install_packages'], ['curl', 'nano'])
                self.assertEqual(imported['remove_packages'], ['vim'])
                self.assertEqual(missing, [])
                engine.atomic_json(self.project.directory / 'cube-editor.json', {**self.project.data, 'recipe': recipe})
                loaded = engine.Project.load(self.project.directory)
                self.assertEqual(loaded.data['recipe'], imported)
                self.project.save(recipe=recipe)
                self.assertEqual(self.project.data['recipe'], imported)

    def test_malformed_project_cli_reports_error_without_traceback(self):
        engine.atomic_json(self.project.directory / 'cube-editor.json', {'schema': 1})
        result = subprocess.run([sys.executable, engine.__file__, 'plan', str(self.project.directory), 'build'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('cube-editor:', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_malformed_project_json_raises_cube_error(self):
        (self.project.directory / 'cube-editor.json').write_text('{broken')
        with self.assertRaisesRegex(engine.CubeError, 'Cannot read project'):
            engine.Project.load(self.project.directory)

    def test_shared_publication_staging_is_visible_but_not_auto_recovered(self):
        output = self.project.output_path
        output.parent.mkdir()
        staging = output.with_name('.' + output.name + '.other-project.building')
        staging.mkdir()
        (staging / 'source.iso').write_bytes(b'active export')
        report = engine.recovery_report(self.project)
        self.assertIn(str(staging), report['output_staging'])
        self.assertNotIn(str(staging), report['staging'])
        self.assertIn('another active project', report['output_staging_note'])


class OperationEventTests(ProjectFixture):
    def child(self, contents, suffix=''):
        path = self.project.directory / 'events.jsonl'
        script = f'from pathlib import Path; import time; Path({str(path)!r}).write_text({contents!r}); ' + suffix
        return [sys.executable, '-c', script]

    def test_malformed_live_events_are_ignored_and_child_completes(self):
        received = []
        api = engine.Engine(self.project, received.append)
        invalid = [None, [], True, 123, 'text', {}, {'type': 1}, {'type': 'log', 'message': []},
                   {'type': 'finished', 'success': 'yes'}, {'type': 'finished'},
                   {'type': 'progress', 'progress': 'half'}, {'type': 'progress', 'progress': float('nan')},
                   {'type': 'progress', 'progress': 10 ** 1000},
                   {'type': 'started', 'pid': []}, {'type': 'finished', 'success': True, 'job': []}]
        contents = '\n'.join(json.dumps(value) for value in invalid) + '\n'
        contents += json.dumps({'type': 'progress', 'progress': 0.5, 'message': 'Working', 'job': api.job_id}) + '\n'
        marker = self.project.directory / 'completed'
        command = self.child(contents, f'time.sleep(0.25); Path({str(marker)!r}).touch()')
        self.assertTrue(api.run(command)['success'])
        self.assertTrue(marker.exists())
        self.assertEqual([item['type'] for item in received], ['state', 'progress'])
        self.assertIsNone(api.process)

    def test_final_tail_handles_invalid_records_and_partial_final_line(self):
        api = engine.Engine(self.project)
        final = {'type': 'finished', 'success': True, 'job': api.job_id}
        contents = 'null\n[]\n' + json.dumps({'type': 'finished', 'success': False, 'job': 'different'}) + '\n' + json.dumps(final)
        self.assertEqual(api.run(self.child(contents)), final)
        self.assertIsNone(api.process)

    def test_controller_callback_failure_terminates_and_reaps_child(self):
        def callback(event):
            if event.get('type') == 'progress':
                raise RuntimeError('Consumer failed')

        api = engine.Engine(self.project, callback)
        processes = []
        real_popen = subprocess.Popen

        def remember_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        command = self.child(json.dumps({'type': 'progress', 'progress': 0.5}) + '\n', 'time.sleep(60)')
        started = time.monotonic()
        with mock.patch('engine.subprocess.Popen', side_effect=remember_process):
            with self.assertRaisesRegex(RuntimeError, 'Consumer failed'):
                api.run(command)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(processes[0].returncode)
        self.assertTrue(processes[0].stdout.closed)
        self.assertIsNone(api.process)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)

    def test_output_callback_failure_also_reaps_child(self):
        def callback(event):
            if event.get('type') == 'log':
                raise RuntimeError('Output consumer failed')

        api = engine.Engine(self.project, callback)
        with self.assertRaisesRegex(RuntimeError, 'Output consumer failed'):
            api.run([sys.executable, '-c', 'import time; print("hello", flush=True); time.sleep(60)'])
        self.assertIsNone(api.process)


if __name__ == '__main__':
    unittest.main()
