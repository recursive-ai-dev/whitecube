#!/usr/bin/python3 -I
"""Preferences, terminal launch, and HTTP body regression tests without the live desktop."""
import http.client
import http.server
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'workspace'))
from desktop_state import DesktopStore
from server import Handler
terminal = runpy.run_path(str(ROOT / 'bin/whitecube-terminal'))


class Preferences(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DesktopStore(self.root / 'state')

    def test_corrupt_settings_restore_safe_defaults(self):
        path = self.store.state / 'accessibility.json'
        for data in (b'\xff', b'{broken', b'[]', b'null', b'{"terminal_scale":NaN,"reduced_motion":"false"}'):
            path.write_bytes(data)
            self.assertEqual(self.store.get_settings()['terminal_scale'], 1)
            self.assertFalse(self.store.get_settings()['reduced_motion'])

    def test_individual_valid_settings_survive_invalid_neighbors(self):
        (self.store.state / 'accessibility.json').write_text('{"terminal_scale":1.5,"ui_scale":false,"contrast":"strong"}')
        self.assertEqual(self.store.get_settings(), {'terminal_scale':1.5,'ui_scale':1,'contrast':'strong','reduced_motion':False})

    def test_partial_update_preserves_other_settings(self):
        self.store.save_settings({'terminal_scale':1.5, 'contrast':'strong'})
        result = self.store.save_settings({'ui_scale':1.2})
        self.assertEqual(result['terminal_scale'], 1.5)
        self.assertEqual(result['contrast'], 'strong')
        self.assertEqual((self.store.state / 'accessibility.json').stat().st_mode & 0o777, 0o600)

    def test_invalid_layouts_do_not_replace_valid_layout(self):
        layout = {'version':1,'workspaces':[{'name':'Code','panes':[{'id':'pane-1','session':None}], 'sizes':[1], 'terminalBounds':{'x':0,'y':10,'width':80,'height':60}} for _ in range(4)]}
        self.store.save_layout(layout)
        previous = (self.store.state / 'desktop-layout.json').read_bytes()
        for bad in ({'version':True}, {'terminalVisible':'false'}, {'sizes':[float('nan')]}, {'axis':'bad'}, {'panes':[{'id':'same'},{'id':'same'}]}, {'project':{'url':[]}}, {'terminalBounds':{'x':0,'y':0,'width':0,'height':60}}):
            candidate = json.loads(json.dumps(layout))
            if 'version' in bad: candidate.update(bad)
            else: candidate['workspaces'][0].update(bad)
            with self.assertRaises(ValueError): self.store.save_layout(candidate)
            self.assertEqual((self.store.state / 'desktop-layout.json').read_bytes(), previous)

    def test_terminal_reads_custom_xdg_config_and_only_main_font(self):
        config = self.root / 'config/foot'; config.mkdir(parents=True)
        (config / 'foot.ini').write_text('[main]\n font = My Font:size=12\n[colors]\nfont=wrong:size=99\n')
        with patch.dict(os.environ, {'XDG_CONFIG_HOME':str(config.parent)}):
            argv = terminal['terminal_argv']([], {'terminal_scale':1.5,'contrast':'normal'}, self.root)
        self.assertIn('font=My Font:size=18.0', argv)

    def test_terminal_config_equals_and_command_arguments_are_literal(self):
        config = self.root / 'custom.ini'; config.write_text('font=monospace:size=10\n')
        args = ['--config=' + str(config), '--', 'echo', '$(touch nowhere)']
        argv = terminal['terminal_argv'](args, {'terminal_scale':1.2,'contrast':'strong'}, self.root)
        self.assertIn('font=monospace:size=12.0', argv)
        self.assertEqual(argv[-len(args):], args)
        self.assertIn('colors.foreground=ffffff', argv)
        # Child command flags after -e are never mistaken for foot's own --config.
        ignored = self.root / 'ignored.ini'; ignored.write_text('font=wrong:size=50\n')
        e_args = ['--config=' + str(config), '-e', 'python3', '-c', str(ignored)]
        e_argv = terminal['terminal_argv'](e_args, {'terminal_scale':1.0,'contrast':'normal'}, self.root)
        self.assertIn('font=monospace:size=10.0', e_argv)

    def test_terminal_survives_invalid_font_and_encoding(self):
        config = self.root / 'bad.ini'
        for contents in (b'font=Mono:size=12.3.4', b'\xff'):
            config.write_bytes(contents)
            argv = terminal['terminal_argv'](['-c', str(config)], {'terminal_scale':'bad'}, self.root)
            self.assertEqual(argv[0], 'foot')
            self.assertIn(':size=11', argv[2])


class RequestBodies(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.apps = False
        self.calls = []
        def api(method, path, query, data):
            self.calls.append(data)
            return {'accepted':True}
        self.server.workspace = SimpleNamespace(authorize=lambda *_a, **_k:None, api=api, frame_sources=lambda:"'none'")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def post(self, body, extra=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request('POST', '/api/settings', body, headers={'Content-Type':'application/json', **(extra or {})})
            response = connection.getresponse(); response.read(); return response.status
        finally: connection.close()

    def test_valid_body_reaches_api(self):
        self.assertEqual(self.post('{"ui_scale":1.2}'), 200)
        self.assertEqual(self.calls, [{'ui_scale':1.2}])

    def test_nonfinite_and_chunked_bodies_are_rejected(self):
        self.assertEqual(self.post('{"ui_scale":NaN}'), 400)
        self.assertEqual(self.post('{"ui_scale":1e999}'), 400)
        self.assertEqual(self.post('{}', {'Transfer-Encoding':'chunked'}), 400)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
