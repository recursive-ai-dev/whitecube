#!/usr/bin/python3
"""Validation checks without changing the desktop account's dconf database."""
import os
import json
import io
from pathlib import Path
import runpy
import tempfile
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
api = runpy.run_path(str(ROOT / 'bin' / 'cube-preferences'))


class PreferenceValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = api['schema']()

    def test_every_schema_default_is_accepted(self):
        self.assertEqual(len(self.spec.list_keys()), 37)
        for key in self.spec.list_keys():
            with self.subTest(key=key):
                default = self.spec.get_key(key).get_default_value()
                text = default.get_string() if default.get_type_string() == 's' else default.print_(False)
                actual_key, value = api['validate'](self.spec, key.replace('-', '_'), text)
                self.assertEqual(actual_key, key)
                self.assertTrue(default.equal(value))

    def test_invalid_values_cannot_reach_dconf(self):
        cases = [('tab-w', '59'), ('tab-h', '81'), ('grid-step', '0'),
                 ('repeat-rate', '-1'), ('repeat-delay', '2001'), ('repeat-rate', '10x'),
                 ('privacy-lock-minutes', '121'), ('pointer-speed', '101'),
                 ('clock-format', 'weekly'), ('clock-seconds', 'perhaps'),
                 ('bg', 'zzzzzz'), ('grid', '#ffffff'), ('border', 'fffffff'),
                 ('terminal', 'x' * 128), ('browser-cmd', ''), ('launcher', '   '),
                 ('browser-cmd', 'hello\nworld'), ('xkb-layout', 'us;evil'),
                 ('xkb-model', 'x' * 64), ('unknown', '1')]
        for key, text in cases:
            with self.subTest(key=key, value=text):
                with self.assertRaises((ValueError, OverflowError)):
                    api['validate'](self.spec, key, text)

    def test_commands_remain_literal_values(self):
        text = 'foot -T "A title" -- sh -c "printf example"'
        _, value = api['validate'](self.spec, 'terminal', text)
        self.assertEqual(value.get_string(), text)

    def profile(self, values):
        return json.dumps({'format': api['PROFILE_FORMAT'], 'version': 1, 'settings': values})

    def test_profile_preserves_types_and_normalizes_keys(self):
        pairs = api['profile_pairs'](self.spec, self.profile({'tab_w': 200, 'clock-seconds': True, 'bg': '112233'}))
        self.assertEqual(dict(pairs), {'tab-w': '200', 'clock-seconds': 'true', 'bg': '112233'})

    def test_profile_rejects_ambiguous_or_invalid_values(self):
        for values in ({'tab-w': True}, {'clock-seconds': 1}, {'bg': 123456}, {'tab-w': 500},
                       {'unknown': 'value'}, {'tab_w': 180, 'tab-w': 200}, {'browser-cmd': 'a\nb'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                api['profile_pairs'](self.spec, self.profile(values))

    def test_profile_rejects_duplicates_and_unknown_versions(self):
        for text in ('[]', '{}', self.profile({}).replace('"version": 1', '"version": true'),
                     self.profile({}).replace('"version": 1', '"version": 2'),
                     self.profile({}).replace('"settings": {}', '"settings": {"tab-w": 180, "tab-w": 200}'),
                     self.profile({'browser-cmd': 'x' * 66000})):
            with self.subTest(text=text[:100]), self.assertRaises(ValueError):
                api['profile_pairs'](self.spec, text)

    def test_profile_export_contains_only_explicit_overrides(self):
        class Settings:
            def get_user_value(self, key):
                return api['GLib'].Variant('b', False) if key == 'clock-seconds' else None
        text = api['profile_export'](self.spec, Settings())
        self.assertEqual(json.loads(text)['settings'], {'clock-seconds': False})
        self.assertEqual(api['profile_pairs'](self.spec, text), [('clock-seconds', 'false')])

    def test_export_is_private_and_does_not_follow_target_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original'
            original.write_text('keep')
            target = Path(directory) / 'profile.json'
            target.symlink_to(original)
            api['profile_write'](target, self.profile({'tab-w': 200}))
            self.assertEqual(original.read_text(), 'keep')
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_import_forwards_validated_stdin_to_fresh_writer(self):
        text = self.profile({'tab-w': 200})
        environment = {k: v for k, v in os.environ.items() if k != 'WHITECUBE_PREFS_WRITER'}
        for arguments in (['import'], ['import', '-']):
            with self.subTest(arguments=arguments), \
                 mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(api['sys'], 'argv', ['cube-preferences', *arguments]), \
                 mock.patch.object(api['sys'], 'stdin', io.StringIO(text)), \
                 mock.patch.object(api['subprocess'], 'run', return_value=mock.Mock(returncode=0)) as run:
                with self.assertRaises(SystemExit) as outcome:
                    api['main']()
                self.assertEqual(outcome.exception.code, 0)
                command = run.call_args.args[0]
                self.assertEqual(command[:4], ['/usr/bin/dbus-run-session', '--', '/usr/bin/python3', '-I'])
                self.assertEqual(command[5:], ['import'])
                self.assertEqual(run.call_args.kwargs, {'input': text, 'text': True})

    def test_failed_export_preserves_original_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'profile.json'
            target.write_text('keep')
            with mock.patch.object(api['os'], 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    api['profile_write'](target, self.profile({'tab-w': 200}))
            self.assertEqual(target.read_text(), 'keep')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_duplicate_batch_is_rejected_before_any_writer(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate setting'):
            api['write_many'](self.spec, [('tab-w', '180'), ('tab_w', '200')])

    @unittest.skipUnless(Path('/usr/bin/dconf').exists() and Path('/usr/bin/dbus-run-session').exists(),
                         'requires the packaged dconf writer dependencies')
    def test_real_profile_import_roundtrip_and_failed_import_leave_database_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runtime = home / 'runtime'
            runtime.mkdir(mode=0o700)
            environment = dict(os.environ, HOME=directory, XDG_CONFIG_HOME=str(home / 'config'),
                               XDG_RUNTIME_DIR=str(runtime), GSETTINGS_BACKEND='dconf')
            environment.pop('DBUS_SESSION_BUS_ADDRESS', None)
            environment.pop('WHITECUBE_PREFS_WRITER', None)
            def run(command, payload=None):
                return subprocess.run(['/usr/bin/python3', '-I', str(ROOT / 'bin/cube-preferences'), command],
                                      input=payload, text=True, capture_output=True, env=environment, timeout=30)
            values = {'tab-w': 220, 'clock-seconds': False, 'terminal': 'foot -T "Saved title"'}
            result = run('import', self.profile(values))
            self.assertEqual(result.returncode, 0, result.stderr)
            exported = run('export')
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual(json.loads(exported.stdout)['settings'], values)
            failed = run('import', self.profile({'tab-w': 250, 'repeat-rate': -1}))
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(json.loads(run('export').stdout)['settings'], values)
            self.assertEqual(run('import', self.profile({})).returncode, 0)
            self.assertEqual(json.loads(run('export').stdout)['settings'], values)
            # A complete desktop profile exceeds the old 32-key batch limit.
            complete = {key: self.spec.get_key(key).get_default_value().unpack()
                        for key in self.spec.list_keys()}
            result = run('import', self.profile(complete))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(run('export').stdout)['settings'], complete)


if __name__ == '__main__':
    unittest.main()
