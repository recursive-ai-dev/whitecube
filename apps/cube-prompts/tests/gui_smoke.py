#!/usr/bin/python3
"""Exercise native GTK workflows on a supplied display with temporary user data.

Run with system Python and an isolated X server, e.g.
DISPLAY=:99 GDK_BACKEND=x11 /usr/bin/python3 -I tests/gui_smoke.py
No API requests are made. --app-dir can target an extracted Debian package.
"""
from pathlib import Path
import argparse
import json
import os
import sys
import tempfile
import time
import traceback

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--app-dir', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--screenshots', type=Path)
args = parser.parse_args()
scratch = tempfile.TemporaryDirectory(prefix='cube-prompts-gui-')
for key, directory in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache')]:
    os.environ[key] = str(Path(scratch.name) / directory)
os.environ['GTK_A11Y'] = 'none'
os.environ['GSETTINGS_BACKEND'] = 'memory'
os.environ['GSK_RENDERER'] = 'cairo'
sys.path.insert(0, str(args.app_dir.resolve()))

import app
from prompt_manager.core.keychain import KeyVault
from prompt_manager.core.run_exporter import export_run_json
from prompt_manager.integrations.llm_providers import LLMResponse


class FakeClient:
    def __init__(self, **_kwargs):
        pass

    def execute(self, request):
        time.sleep(0.12 if request.model_id.startswith('ollama:') else 0.35)
        if request.model_id.startswith('openai:'):
            return LLMResponse(model_id=request.model_id, error='Synthetic provider failure.')
        return LLMResponse(content='Answer: ' + request.prompt, model_id=request.model_id,
                           elapsed_seconds=0.12, prompt_tokens=5, completion_tokens=9, total_tokens=14)


app.LLMClient = FakeClient


class SmokeApp(app.PromptsApp):
    failure = None
    stage = 'edit'
    ticks = 0

    def do_activate(self):
        super().do_activate()
        self.window.vault = KeyVault(use_keyring=False)
        self.window.stack.set_transition_duration(0)
        app.GLib.timeout_add(100, self.step)

    def step(self):
        self.ticks += 1
        try:
            assert self.ticks < 200, 'GUI test timed out'
            return self.actions()
        except BaseException:
            self.failure = traceback.format_exc()
            print(self.failure, file=sys.stderr)
            self.quit()
            return False

    def actions(self):
        w = self.window
        if self.stage == 'edit':
            assert len(w.prompts) == 3
            for index, (page, _title, _icon) in enumerate(app.PAGES):
                assert w.on_key(None, app.Gdk.KEY_1 + index, 0, app.Gdk.ModifierType.CONTROL_MASK)
                assert w.stack.get_visible_child_name() == page
            w.new_prompt()
            self.prompt_id = w.current.id
            w.title_entry.set_text('Comparison smoke test')
            w.template_view.get_buffer().set_text('Write for {{audience:engineers}} using {{tone|options:formal,warm}}.')
            w.system_view.get_buffer().set_text('Use {{language:English}}.')
            assert 'engineers' in w.hydrated()[1]
            assert w.var_widgets['tone'].get_model().get_n_items() == 2
            # Changing modifiers without changing a variable's name must rebuild the form.
            w.template_view.get_buffer().set_text('Write for {{audience:engineers}} using {{tone|options:short,clear,kind}}.')
            assert w.var_widgets['tone'].get_model().get_n_items() == 3
            assert w.hydrated()[1] == 'Write for engineers using short.'
            w.var_widgets['audience'].set_text('designers')
            w.save(checkpoint=True)
            w.template_view.get_buffer().set_text('Explain to {{audience:engineers}} in a {{tone|options:short,clear}} tone.')
            w.save(checkpoint=True)
            w.navigate('history')
            w.on_revision_selected(None, w.revision_list.get_row_at_index(0))
            assert '--- Revision' in app.get_text(w.revision_view)
            w.revision_diff.set_active(False)
            assert 'Write for' in app.get_text(w.revision_view)
            preset = w.presets.save_preset(w.current.id, 'Design review', {'audience': 'designers', 'tone': 'clear'})
            w.reload_presets()
            w.preset_drop.set_selected(1)
            assert w.hydrated()[1] == 'Explain to designers in a clear tone.'
            assert w.presets.list_presets(w.current.id)[0].id == preset.id
            w.system_view.get_buffer().set_text('{{broken')
            assert w.syntax_label.get_visible()
            w.system_view.get_buffer().set_text('Use {{language:English}}.')
            # A failed save must keep the current draft and block close/switch/new.
            save_prompt = w.repo.save_prompt
            def fail_save(*_args, **_kwargs):
                raise OSError('Synthetic full disk')
            w.repo.save_prompt = fail_save
            assert w.on_close() is True
            assert not w.closing
            w.new_prompt()
            assert w.current.id == self.prompt_id and w.dirty
            w.repo.save_prompt = save_prompt
            assert w.save(checkpoint=False)
            # A settings I/O failure must not wedge the run button/spinner.
            set_setting = app.config.set_setting
            def fail_setting(*_args, **_kwargs):
                raise OSError('Synthetic settings failure')
            app.config.set_setting = fail_setting
            w.run_prompt()
            assert not w.running and w.run_button.get_sensitive()
            app.config.set_setting = set_setting
            for model_id in ('ollama:smoke:latest', 'openai:smoke'):
                w.custom_model_entry.set_text(model_id)
                w.add_custom_model()
            assert w.custom_model_entry.get_text() == ''
            names = [m.id for m in w.run_models]
            w.run_drops[0].set_selected(names.index('ollama:smoke:latest'))
            w.run_drops[1].set_selected(1 + names.index('openai:smoke'))
            w.navigate('run')
            w.max_tokens.set_value(4096)
            w.run_prompt()
            assert w.running and w.cancel_button.get_sensitive()
            # Switch prompt while a run is inflight: history must keep the original.
            w.new_prompt()
            assert w.current.id != self.prompt_id
            self.stage = 'running'
            return True
        if self.stage == 'running':
            if w.running:
                return True
            assert w.status.get_text() == '1 of 2 models answered.', w.status.get_text()
            runs = w.run_history.list_runs()
            assert len(runs) == 1
            run = runs[0]
            assert run.prompt_id == self.prompt_id
            assert run.title == 'Comparison smoke test'
            assert run.prompt == 'Explain to designers in a clear tone.'
            assert run.system_instruction == 'Use English.'
            assert run.max_tokens == 4096
            assert run.responses[1].error == 'Synthetic provider failure.'
            assert json.loads(export_run_json(run))['variables']['audience'] == 'designers'
            w.navigate('history')
            assert w.saved_run_detail.get_first_child()
            w.prompt_from_run(run)
            assert app.get_text(w.template_view) == run.prompt
            assert w.current.id != self.prompt_id
            # The next comparison is cancelled immediately; retain errors in history.
            w.navigate('run')
            w.run_prompt()
            w.cancel_run()
            self.stage = 'cancelled'
            return True
        if self.stage == 'cancelled':
            if w.running:
                return True
            assert len(w.run_history.list_runs()) == 2
            assert 'cancelled' in w.status.get_text().lower()
            print('PASS: GTK editing, same-name variable modifiers, presets, revisions/diff, save-failure protection, '
                  'custom models, progress, success/failure comparison snapshots, exports, and cancellation')
            self.pages = iter(app.PAGES)
            self.stage = 'pages'
            return True
        if self.stage == 'pages':
            if hasattr(self, 'capture_page') and args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                paintable = app.Gtk.WidgetPaintable.new(w)
                snapshot = app.Gtk.Snapshot()
                paintable.snapshot(snapshot, w.get_width(), w.get_height())
                node = snapshot.to_node()
                if node is None:
                    w.queue_draw()
                    return True
                texture = w.get_native().get_renderer().render_texture(node, None)
                texture.save_to_png(str(args.screenshots / f'{self.capture_page}.png'))
            page = next(self.pages, None)
            if page:
                w.navigate(page[0])
                self.capture_page = page[0]
                return True
            print('PASS: all six native pages rendered')
            # Closing during a comparison must wait for its cancelled snapshot.
            w.navigate('run')
            w.run_prompt()
            assert w.running
            assert w.on_close() is True
            assert w.close_pending and not w.closing
            self.stage = 'closing'
            # Keep the application alive after window.close to inspect persistence.
            self.hold()
            return True
        if self.stage == 'closing':
            if w.running:
                return True
            assert w.closing
            assert len(w.run_history.list_runs()) == 3
            print('PASS: close during run waits for cancellation and saves history')
            self.release()
            self.quit()
            return False


application = SmokeApp(argparse.Namespace(page=None, screenshot=None))
application.set_flags(app.Gio.ApplicationFlags.NON_UNIQUE)
result = application.run([sys.argv[0]])
scratch.cleanup()
raise SystemExit(1 if application.failure else result)
