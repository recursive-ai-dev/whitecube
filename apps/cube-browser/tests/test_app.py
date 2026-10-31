#!/usr/bin/env python3
"""Public CLI and reversible user installation, confined to a temporary HOME."""
import json
import os
import shutil
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_net import serve

ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else ROOT / 'build/cube-browser'

class AppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cube-app-test-')
        self.home = Path(self.temporary.name)
        runtime = self.home/'runtime'
        runtime.mkdir(mode=0o700)
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home/'config'),
                        XDG_DATA_HOME=str(self.home/'data'), XDG_STATE_HOME=str(self.home/'state'),
                        XDG_RUNTIME_DIR=str(runtime))
        self.env.pop('DISPLAY', None)
        self.env.pop('WAYLAND_DISPLAY', None)
        self.env.pop('WAYLAND_SOCKET', None)
    def tearDown(self):
        self.temporary.cleanup()
    def run_browser(self, *args):
        return subprocess.run([str(BROWSER), *args], env=self.env, text=True,
                              capture_output=True, timeout=20)
    def test_local_document_is_brokered_and_scripts_inert(self):
        page = self.home/'a page.html'
        page.write_text('<title>Local title</title><p>Local content &amp; text</p>'
                        '<script>secretScriptMarker</script><p style="display:none">hiddenMarker</p>')
        result = self.run_browser('--dump', str(page))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Local content & text', result.stdout)
        self.assertNotIn('secretScriptMarker', result.stdout)
        self.assertNotIn('hiddenMarker', result.stdout)
        self.assertFalse((self.home/'data').exists())
    def test_symlinks_and_oversize_local_files_fail(self):
        page = self.home/'large.html'
        with page.open('wb') as stream: stream.truncate(2*1024*1024+1)
        self.assertEqual(self.run_browser('--dump', str(page)).returncode, 1)
        target = self.home/'small.html'; target.write_text('<p>File</p>')
        link = self.home/'linked.html'; link.symlink_to(target)
        self.assertEqual(self.run_browser('--dump', str(link)).returncode, 1)
    def test_png_and_cli_errors(self):
        output = self.home/'home.png'
        result = self.run_browser('--render', 'cube:home', str(output), '800x600')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(self.run_browser('--render', 'cube:home', str(output), '99999x600').returncode, 2)
        self.assertEqual(self.run_browser('--unknown').returncode, 2)
        self.assertEqual(self.run_browser('--dump', 'javascript:alert(1)').returncode, 1)
    def test_information_commands_do_not_create_profiles(self):
        for arg in ('--help', '--version', '--check'):
            with self.subTest(arg=arg):
                result = self.run_browser(arg)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.home/'data').exists())
                self.assertFalse((self.home/'config').exists())
        self.assertIn('No Wayland socket', self.run_browser('--check').stdout)

    def test_https_only_preference_blocks_http_before_any_request(self):
        config = self.home/'config/cube-browser/config'
        config.parent.mkdir(parents=True, mode=0o700)
        config.write_text('https_only=true\n')
        config.chmod(0o600)
        document = b'<title>Unreachable HTTP page</title><p>Must never be fetched</p>'
        response = (f'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(document)}\r\n\r\n'.encode() + document)
        with serve({'/page': response}) as (server, url):
            result = self.run_browser('--dump', url+'/page')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('HTTPS-only mode blocked', result.stdout + result.stderr)
            self.assertEqual(server.requests, [], 'HTTPS-only must block before sending an HTTP request')
        self.assertEqual(config.read_text(), 'https_only=true\n')
        self.assertFalse((self.home/'data').exists())

    def test_check_reports_missing_workers(self):
        app = self.home/'incomplete-install'
        app.mkdir()
        shutil.copy2(BROWSER, app/'cube-browser')
        result = subprocess.run([str(app/'cube-browser'), '--check'], env=self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('MISSING or not executable', result.stdout)
        self.assertFalse((self.home/'config').exists())

    def test_relative_local_file_and_ambiguous_cli(self):
        page = self.home/'relative page.html'
        page.write_text('<p>Opened relative document</p>')
        result = subprocess.run([str(BROWSER), '--dump', page.name], cwd=self.home,
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Opened relative document', result.stdout)
        for args in (('--dump', 'cube:home', 'cube:about'),
                     ('--dump', 'cube:home', '--dump', 'cube:about'),
                     ('--', 'cube:home', 'cube:about'), ('--quit-after', 'NaN')):
            with self.subTest(args=args):
                self.assertEqual(self.run_browser(*args).returncode, 2)
        # Once -- ends options, --version is treated as an address, not a command.
        result = self.run_browser('--quit-after', '0.01', '--', '--version')
        self.assertNotIn('interactive-web preview', result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_private_pages_do_not_read_or_write_saved_history(self):
        data = self.home/'data/cube-browser'
        data.mkdir(parents=True, mode=0o700)
        db = data/'history.sqlite3'
        with sqlite3.connect(db) as connection:
            connection.execute('CREATE TABLE history(url TEXT PRIMARY KEY,title TEXT,visited INTEGER)')
            connection.execute("INSERT INTO history VALUES ('https://example.com','SAVED_SECRET',123)")
        before = db.read_bytes()
        result = self.run_browser('--dump', 'cube:history')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('private trail', result.stdout)
        self.assertNotIn('SAVED_SECRET', result.stdout)
        self.assertEqual(before, db.read_bytes())
        self.assertEqual(list(data.iterdir()), [db])
        self.assertFalse((self.home/'config').exists())
        result = self.run_browser('--dump', 'cube:bookmarks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Bookmarks', result.stdout)
        self.assertEqual(list(data.iterdir()), [db])

    def test_installer_preserves_settings_and_backups(self):
        config = self.home/'config/whitecube/config'
        config.parent.mkdir(parents=True)
        original = '# user settings\nbrowser_cmd=old-browser\nterminal=foot\nchat_app_id=cube-chat\n'
        config.write_text(original)
        result = subprocess.run([sys.executable, str(ROOT/'scripts/install-user.py'), '--apply'],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('terminal=foot\nchat_app_id=cube-chat\n', config.read_text())
        self.assertNotIn('old-browser', config.read_text())
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        launcher = self.home/'.local/bin/cube-browser'
        self.assertEqual(launcher.resolve(), ROOT/'cube-browser.sh')
        manifests = list((self.home/'state/cube-browser/install-backups').glob('*/manifest.json'))
        self.assertEqual(len(manifests), 1)
        records = json.loads(manifests[0].read_text())
        saved = next(row for row in records if row['path'] == str(config))
        self.assertEqual(Path(saved['backup']).read_text(), original)
        self.assertTrue((self.home/'data/applications/cube-browser.desktop').is_file())
        version = subprocess.run([str(launcher), '--version'], env=self.env, capture_output=True, text=True)
        self.assertEqual(version.returncode, 0, version.stderr)
        restore = [sys.executable, str(ROOT/'scripts/install-user.py'), '--restore', str(manifests[0])]
        installed = config.read_text()
        plan = subprocess.run(restore, env=self.env, capture_output=True, text=True)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(config.read_text(), installed)
        config.write_text(installed+'# later user change\n')
        refused = subprocess.run(restore+['--apply'], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('changed after installation', refused.stderr)
        self.assertTrue(launcher.is_symlink())
        config.write_text(installed)
        restored = subprocess.run(restore+['--apply'], env=self.env, capture_output=True, text=True)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(config.read_text(), original)
        self.assertFalse(launcher.is_symlink())
        self.assertFalse((self.home/'data/applications/cube-browser.desktop').exists())
        self.assertTrue(manifests[0].exists())

    def test_ui_rejects_mutable_and_nonfinite_renderer_frames(self):
        # Actual UI binary, isolated helper directory; no production test hooks.
        app = self.home/'fake-app'
        app.mkdir()
        shutil.copy2(BROWSER, app/'cube-browser')
        helper = app/'cube-renderd'
        shutil.copy2(ROOT/'tests/faulty_renderer.py', helper)
        helper.chmod(0o700)
        for fault in ('mutable', 'nonfinite'):
            helper.write_text((ROOT/'tests/faulty_renderer.py').read_text().replace("os.environ['CB_TEST_FRAME_FAULT']", repr(fault)))
            with self.subTest(fault=fault):
                result = subprocess.run([str(app/'cube-browser'), '--dump', 'cube:home'],
                                        env=dict(self.env, CB_TEST_FRAME_FAULT=fault),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('Invalid frame', result.stderr)

    def test_ui_rejects_unsolicited_renderer_submit_copy_and_navigation(self):
        app = self.home/'fake-actions'
        app.mkdir()
        shutil.copy2(BROWSER, app/'cube-browser')
        shutil.copy2(BROWSER.parent/'cube-netd', app/'cube-netd')
        helper = app/'cube-renderd'
        shutil.copy2(ROOT/'tests/faulty_renderer.py', helper)
        helper.chmod(0o700)
        document = b'<title>Untrusted renderer test</title><p>No user interaction requested.</p>'
        response = (f'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(document)}\r\n\r\n'.encode() + document)
        for fault, message in (('unsolicited-submit', 'Invalid interaction response'),
                               ('unsolicited-copy', 'Invalid interaction response'),
                               ('unsolicited-link', 'unsolicited navigation')):
            with self.subTest(fault=fault), serve({'/page': response, '/unrequested': response}) as (server, url):
                helper.write_text((ROOT/'tests/faulty_renderer.py').read_text().replace("os.environ['CB_TEST_FRAME_FAULT']", repr(fault)).replace("os.environ['CB_TEST_ACTION_URL']", repr(url+'/unrequested')))
                result = subprocess.run([str(app/'cube-browser'), '--dump', url+'/page'],
                                        env=dict(self.env, CB_TEST_FRAME_FAULT=fault,
                                                 CB_TEST_ACTION_URL=url+'/unrequested'),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(result.stdout, '')
                paths = [request.split(b' ', 2)[1] for request in server.requests]
                self.assertEqual(paths, [b'/page'], 'renderer-initiated action must not cause a network request')

if __name__ == '__main__': unittest.main()
