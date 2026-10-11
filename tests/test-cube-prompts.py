#!/usr/bin/python3 -I
"""Cube Prompts checks: credential storage, Claude exports, and a GUI smoke run.

The unit tests run on the bundled prompt_manager core. The GUI part starts a
private headless whitecube compositor, renders every page, then drives one
window through editing, variables, revisions, templates, exports, settings and
a model run against a fake client. Nothing reaches the network or the user's
library, keys or desktop. --screenshots chooses where preview PNGs go.
"""
from pathlib import Path
import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / 'apps/cube-prompts'
PAGES = ('library', 'run', 'templates', 'history', 'export', 'settings')
SCRATCH = ROOT / 'build' if (ROOT / 'build').is_dir() else None
sys.path.insert(0, str(APP))


class PrivateFileVaultTest(unittest.TestCase):
    def setUp(self):
        from prompt_manager.core import keychain
        self.keychain = keychain
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'config/credentials.json'
        self.vault = keychain.KeyVault(vault_path=Path(self.temp.name) / 'vault.dat', private_file=self.path)
        self.vault._probed, self.vault._cached_backend = True, None   # as on a desktop with no keyring

    def tearDown(self):
        self.temp.cleanup()

    def test_keys_round_trip_through_a_private_file(self):
        self.vault.set_api_key('anthropic', ' sk-ant-test ')
        self.assertEqual(self.vault.get_api_key('anthropic'), 'sk-ant-test')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(self.vault.location, str(self.path))
        self.vault.delete_api_key('anthropic')
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_provider_config_keeps_key_out_of_config_entry(self):
        self.vault.set_provider_config('openai', {'base_url': 'http://localhost:8080/v1', 'api_key': 'k'})
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored['api_key_openai'], 'k')
        self.assertNotIn('api_key', json.loads(stored['config_openai']))
        self.assertEqual(self.vault.get_provider_config('openai')['base_url'], 'http://localhost:8080/v1')

    def test_without_private_file_there_is_still_no_disk_fallback(self):
        vault = self.keychain.KeyVault(vault_path=Path(self.temp.name) / 'vault.dat')
        vault._backend = lambda required=False: (_ for _ in ()).throw(self.keychain.CredentialStoreError('no keyring'))
        with self.assertRaises(self.keychain.CredentialStoreError):
            vault.set_api_key('openai', 'secret')
        self.assertFalse(self.path.exists())


class ClaudeTest(unittest.TestCase):
    def test_anthropic_export_uses_current_ids_and_no_sampling(self):
        from prompt_manager.core.exporter import to_anthropic_payload
        from prompt_manager.core.models import Prompt
        for target, expected in (('Claude Sonnet 5.5', 'claude-sonnet-5-5'), ('claude-haiku-5-5', 'claude-haiku-5-5'),
                                 ('GPT-4o', 'claude-opus-5-5'), ('General', 'claude-opus-5-5')):
            payload = to_anthropic_payload(Prompt(template_content='hi', target_model=target))
            self.assertEqual(payload['model'], expected)
            self.assertNotIn('temperature', payload)
            self.assertGreater(payload['max_tokens'], 0)

    def test_catalog_lists_current_claude_models(self):
        from prompt_manager.integrations.llm_providers import list_models_by_provider
        ids = {m.id for m in list_models_by_provider('anthropic')}
        self.assertEqual(ids, {'anthropic:claude-opus-5-5', 'anthropic:claude-sonnet-5-5', 'anthropic:claude-haiku-5-5'})

    def test_client_omits_temperature_for_current_claude(self):
        from unittest.mock import patch
        from prompt_manager.core.keychain import KeyVault
        from prompt_manager.integrations import llm_providers
        vault = KeyVault(use_keyring=False)
        vault.set_api_key('anthropic', 'sk-test')
        sent = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({'content': [{'type': 'text', 'text': 'ok'}]}).encode()

        def fake_urlopen(request, timeout=None):
            sent.update(json.loads(request.data))
            return Reply()
        with patch.object(llm_providers.urllib.request, 'urlopen', fake_urlopen):
            response = llm_providers.LLMClient(key_vault=vault).execute(
                llm_providers.LLMRequest(prompt='hi', model_id='anthropic:claude-opus-5-5', temperature=0.3))
        self.assertTrue(response.is_success, response.error)
        self.assertEqual(sent['model'], 'claude-opus-5-5')
        self.assertNotIn('temperature', sent)


def inside_gui(screenshot):
    import app
    from prompt_manager.integrations.llm_providers import LLMResponse

    class FakeClient:
        def __init__(self, key_vault=None):
            pass

        def execute(self, request):
            if request.model_id.startswith('anthropic'):
                return LLMResponse(model_id=request.model_id, error='Anthropic API key not configured.')
            return LLMResponse(content=f'echo: {request.prompt}', elapsed_seconds=.2, model_id=request.model_id,
                               provider='pollinations', prompt_tokens=5, completion_tokens=7)
    app.LLMClient = FakeClient
    options = argparse.Namespace(page=None, screenshot=None)

    class SmokeApp(app.PromptsApp):
        failure = None
        stage = 'edit'

        def do_activate(self):
            super().do_activate()
            app.GLib.timeout_add(300, self.step)

        def step(self):
            try:
                return self.actions()
            except BaseException:
                self.failure = traceback.format_exc()
                print(self.failure, file=sys.stderr)
                self.quit()
                return False

        def actions(self):
            w = self.window
            ctrl = app.Gdk.ModifierType.CONTROL_MASK
            if self.stage == 'edit':
                assert len(w.prompts) == 3, 'starter prompts were not seeded'
                for index, page in enumerate(PAGES):
                    assert w.on_key(None, app.Gdk.KEY_1 + index, 0, ctrl)
                    assert w.stack.get_visible_child_name() == page
                assert w.on_key(None, app.Gdk.KEY_n, 0, ctrl)
                assert len(w.prompts) == 4 and w.stack.get_visible_child_name() == 'library'
                w.title_entry.set_text('Release notes')
                w.tags_entry.set_text('#Writing, docs')
                w.system_view.get_buffer().set_text('You write for {{audience:engineers}}.')
                w.template_view.get_buffer().set_text('Summarize {{change|multiline}} in a {{tone|options:short,warm}} tone.')
                assert list(w.var_widgets) == ['change', 'tone', 'audience'], list(w.var_widgets)
                w.var_widgets['change'].get_child().get_buffer().set_text('the new Wi-Fi panel')
                w.var_widgets['tone'].set_selected(1)
                preview = app.get_text(w.preview_view)
                assert 'Summarize the new Wi-Fi panel in a warm tone.' in preview, preview
                assert 'You write for engineers.' in preview, preview
                assert 'unfilled' not in w.metrics.get_text()
                w.template_view.get_buffer().set_text('Use {{lang:Python}}.\n```{{lang}}\n{{change|multiline}}\n```')
                assert '```Python' in w.hydrated()[1], w.hydrated()[1]
                w.template_view.get_buffer().set_text('Summarize {{change|multiline}} in a {{tone|options:short,warm}} tone.')
                assert w.dirty
                assert w.on_key(None, app.Gdk.KEY_s, 0, ctrl)
                saved = w.repo.get_prompt_by_id(w.current.id)
                assert saved.title == 'Release notes' and saved.tags == ['docs', 'writing'], saved
                w.template_view.get_buffer().set_text('Second draft about {{change|multiline}}.')
                w.save(checkpoint=True)
                assert w.on_key(None, app.Gdk.KEY_4, 0, ctrl)
                assert len(w.revisions) == 2, w.revisions
                w.on_revision_selected(None, w.revision_list.get_row_at_index(0))
                w.restore_revision()
                assert app.get_text(w.template_view).startswith('Summarize'), 'revision was not restored'
                assert w.dirty and w.stack.get_visible_child_name() == 'library'
                w.template_view.get_buffer().set_text('{{broken')
                assert w.syntax_label.get_visible()
                w.template_view.get_buffer().set_text('Summarize {{change|multiline}} in a {{tone|options:short,warm}} tone.')
                w.save(checkpoint=False)
                w.star.set_active(True)
                w.save(checkpoint=False)
                w.filter_drop.set_selected(1)
                favorites = {p.id for p in w.repo.list_prompts() if p.is_favorite}
                assert w.current.id in favorites and {p.id for p in w.prompts} == favorites, [p.title for p in w.prompts]
                w.filter_drop.set_selected(0)
                w.search.set_text('vulnerability-nothing-matches')
                w.reload_list()
                assert not w.prompts
                w.search.set_text('')
                w.reload_list()
                print('PASS: editing, variables, preview, revisions, restore, favorites and search')

                w.repo.save_template(app.PromptTemplate(name='Notes', content=app.get_text(w.template_view)))
                w.navigate('templates')
                assert [t.name for t in w.templates] == ['Notes']
                before = w.current.id
                w.prompt_from_template(w.templates[0])
                assert w.current.id != before and w.current.title == 'Notes'
                w.duplicate()
                assert w.current.title == 'Notes (copy)'
                print('PASS: templates and duplicates')

                w.open_prompt(w.repo.list_prompts(search_query='Release')[0])
                w.navigate('export')
                for index, (kind, _title, _ext) in enumerate(app.EXPORTS):
                    w.export_drop.set_selected(index)
                    text = app.get_text(w.export_view)
                    assert text and not text.startswith('Could not export'), (kind, text)
                    if kind == 'anthropic':
                        payload = json.loads(text)
                        assert 'the new Wi-Fi panel' in payload['messages'][0]['content'], payload
                        assert payload['system'] == 'You write for engineers.', payload
                        assert 'temperature' not in payload
                out = Path(os.environ['XDG_DATA_HOME']) / 'backup.json'
                count = app.backup.export_library_to_json(w.repo, out)
                assert count == 6 and json.loads(out.read_text())['prompts'], count
                print('PASS: all six exports render the filled-in prompt; library backup writes JSON')

                w.navigate('settings')
                w.vault._probed, w.vault._cached_backend = True, None
                w.key_entries['openai'].set_text('sk-test-key')
                w.endpoint_entries['ollama'].set_text('http://127.0.0.1:9')
                w.save_settings()
                credentials = Path(os.environ['XDG_CONFIG_HOME']) / 'cube-prompts/credentials.json'
                assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
                stored = json.loads(credentials.read_text())
                assert stored['api_key_openai'] == 'sk-test-key', stored
                assert 'readable only by you' in w.key_location.get_text()
                print('PASS: settings keep keys in a 0600 file when no keyring runs')

                w.navigate('run')
                names = [m.display_name for m in w.run_models]
                w.run_drops[0].set_selected(names.index('Pollinations: OpenAI Fast'))
                w.run_drops[1].set_selected(1 + names.index('Anthropic: Claude Opus 5.5'))
                assert w.on_key(None, app.Gdk.KEY_r, 0, ctrl)
                assert w.running
                self.stage = 'running'
                return True
            if self.stage == 'running':
                if w.running:
                    return True
                assert w.status.get_text() == '1 of 2 models answered.', w.status.get_text()
                columns = list(w._rows(w.results))
                assert len(columns) == 2
                self.stage = 'done'
                options.screenshot = Path(screenshot)
                app.GLib.timeout_add(300, self.screenshot)
                print('PASS: a run compares two models and reports the failing one')
                return False
            return False

    application = SmokeApp(options)
    application.set_flags(app.Gio.ApplicationFlags.NON_UNIQUE)
    result = application.run([sys.argv[0]])
    return 1 if application.failure else result


def run_gui(screenshots):
    compositor_binary = ROOT / 'build/whitecube'
    if not compositor_binary.is_file():
        print('SKIP: build/whitecube is missing; run BUILD.sh for the GUI smoke test')
        return
    screenshots = Path(screenshots).resolve()
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cube-prompts-ui-', dir=SCRATCH) as temp:
        fixture = Path(temp)
        for name in ('home', 'runtime', 'state', 'data', 'cache'):
            (fixture / name).mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(fixture / 'home'), XDG_RUNTIME_DIR=str(fixture / 'runtime'),
                   XDG_CONFIG_HOME=str(fixture / 'home/.config'), XDG_STATE_HOME=str(fixture / 'state'),
                   XDG_DATA_HOME=str(fixture / 'data'), XDG_CACHE_HOME=str(fixture / 'cache'),
                   GSETTINGS_BACKEND='memory', GSETTINGS_SCHEMA_DIR=str(ROOT / 'build/schemas'),
                   WLR_BACKENDS='headless', WLR_RENDERER='pixman', WLR_HEADLESS_OUTPUTS='1',
                   GDK_BACKEND='wayland', GSK_RENDERER='cairo', NO_AT_BRIDGE='1')
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
            env.pop(key, None)
        with (fixture / 'compositor.log').open('w') as log:
            compositor = subprocess.Popen([str(compositor_binary)], env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 8
                socket = fixture / 'runtime/wayland-0'
                while not socket.exists() and compositor.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                if not socket.exists():
                    raise SystemExit('FAIL: the private compositor did not start: ' + (fixture / 'compositor.log').read_text()[-800:])
                env['WAYLAND_DISPLAY'] = socket.name
                for page in PAGES:
                    image = screenshots / f'cube-prompts-{page}.png'
                    image.unlink(missing_ok=True)
                    result = subprocess.run([str(ROOT / 'bin/cube-prompts'), '--page', page, '--screenshot', str(image)],
                                            env=env, capture_output=True, text=True, timeout=20)
                    if result.returncode or not image.is_file() or image.stat().st_size < 4096:
                        raise SystemExit(f'FAIL: page {page} did not render:\n{result.stdout}{result.stderr}')
                    if 'Gtk-CRITICAL' in result.stderr or 'Traceback' in result.stderr:
                        raise SystemExit(f'FAIL: page {page} logged errors:\n{result.stderr}')
                image = screenshots / 'cube-prompts-session.png'
                image.unlink(missing_ok=True)
                result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--inside', str(image)],
                                        env=dict(env, XDG_DATA_HOME=str(fixture / 'data2')),
                                        capture_output=True, text=True, timeout=60)
                print(result.stdout.rstrip())
                if result.returncode or not image.is_file() or 'Traceback' in result.stderr:
                    raise SystemExit(f'FAIL: GUI smoke run:\n{result.stdout}{result.stderr}')
                print(f'PASS: all {len(PAGES)} Cube Prompts pages rendered on an isolated headless Wayland session')
            finally:
                if compositor.poll() is None:
                    compositor.terminate()
                try:
                    compositor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    compositor.kill()
                    compositor.wait()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--inside':
        raise SystemExit(inside_gui(sys.argv[2]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT / 'build')
    parser.add_argument('--no-gui', action='store_true', help='Only the unit checks')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    outcome = unittest.TextTestRunner(verbosity=1).run(suite)
    if not outcome.wasSuccessful():
        raise SystemExit(1)
    if not args.no_gui:
        run_gui(args.screenshots)
