#!/usr/bin/env python3
"""Validate the per-user installer in a disposable account directory."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('INSTALL-USER.py')

class InstallTest(unittest.TestCase):
    def test_install_preserves_config_and_records_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, account = root / 'project', root / 'account'
            (project / 'build').mkdir(parents=True)
            account.mkdir()
            script = project / SCRIPT.name
            script.write_bytes(SCRIPT.read_bytes())
            for name in ('whitecube', 'cube-settings', 'cube-status'):
                target = project / 'build' / name
                target.write_text('#!/bin/sh\nexit 0\n'); target.chmod(0o755)
            for name in ('cube-privacy', 'whitecube-session'):
                (project / name).write_text('#!/bin/sh\nexit 0\n')
            config = account / '.config/whitecube/config'
            config.parent.mkdir(parents=True)
            old = '# Custom\nprivacy_hide_titles=1\nsettings_cmd=old\nterminal=my-terminal'
            config.write_text(old)
            env = {**os.environ, 'HOME': str(account)}
            plan = subprocess.run([sys.executable, '-I', str(script)], env=env, check=True, text=True, capture_output=True)
            self.assertIn('destinations', json.loads(plan.stdout))
            self.assertFalse((account / '.local/bin').exists())
            subprocess.run([sys.executable, '-I', str(script), '--apply'], env=env, check=True, capture_output=True)
            self.assertIn('privacy_hide_titles=1\n', config.read_text())
            self.assertIn('terminal=my-terminal\nsettings_cmd=', config.read_text())
            self.assertEqual(config.read_text().count('settings_cmd='), 1)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            manifest = json.loads((account / '.local/state/whitecube/privacy-install.json').read_text())
            record = next(x for x in manifest['files'] if x['path'] == str(config))
            self.assertEqual(Path(record['before']).read_text(), old)
            for name in ('whitecube', 'cube-settings', 'cube-status', 'cube-privacy', 'whitecube-session'):
                self.assertTrue(os.access(account / '.local/bin' / name, os.X_OK))
            # Refuse a symlink destination without modifying any installed files.
            executable = account / '.local/bin/cube-settings'
            executable.unlink(); executable.symlink_to(config)
            snapshot = config.read_bytes()
            failed = subprocess.run([sys.executable, '-I', str(script), '--apply'], env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(config.read_bytes(), snapshot)

if __name__ == '__main__':
    unittest.main()
