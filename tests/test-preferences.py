#!/usr/bin/python3
"""Validation checks without changing the desktop account's dconf database."""
import os
from pathlib import Path
import runpy
import unittest

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


if __name__ == '__main__':
    unittest.main()
