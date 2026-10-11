#!/usr/bin/python3 -I
"""Whitecube Trainer: GTK4 controls for small local Tensegrity models."""
import argparse
import csv
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'cube-apps'))
import engine
from cube_theme import Adw, Gdk, Gio, GLib, Gtk, apply_theme, screenshot

VERSION = '0.7.0'


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def format_duration(seconds):
    if not finite_number(seconds) or seconds < 0:
        return '—'
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days: return f'{days}d {hours}h'
    if hours: return f'{hours}h {minutes}m'
    if minutes: return f'{minutes}m {seconds}s'
    return f'{seconds}s'


def loss_series(metrics, key, limit=1200):
    """Keep malformed telemetry out of Cairo and bound redraw work."""
    points = sorted((item['step'], item[key]) for item in metrics
                    if isinstance(item, dict) and finite_number(item.get('step'))
                    and item['step'] >= 0 and finite_number(item.get(key)))
    if len(points) <= limit:
        return points
    # Preserve peaks and troughs in each bucket, including the final step.
    bucket = max(1, math.ceil(len(points) / max(1, limit // 2)))
    sampled = []
    for offset in range(0, len(points), bucket):
        chunk = points[offset:offset + bucket]
        sampled.extend(sorted(set((min(chunk, key=lambda p: p[1]), max(chunk, key=lambda p: p[1])))))
    return sorted(set([points[0], *sampled, points[-1]]))


def metrics_csv(metrics):
    fields = ['step', *sorted({key for item in metrics if isinstance(item, dict) for key in item if key != 'step'})]
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    def spreadsheet_text(value):
        # Spreadsheet programs may ignore whitespace before formula prefixes.
        if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
            return "'" + value
        return value
    writer.writerow([spreadsheet_text(field) for field in fields])
    for item in metrics:
        if isinstance(item, dict):
            writer.writerow([spreadsheet_text(item.get(field, '')) for field in fields])
    return stream.getvalue()


def write_export(path, text):
    path = Path(path)
    descriptor, staging = tempfile.mkstemp(prefix=f'.{path.name}-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.replace(staging, path)
    finally:
        Path(staging).unlink(missing_ok=True)


def label(text='', css=None):
    widget = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
    if css:
        widget.add_css_class(css)
    return widget


def box(vertical=True, spacing=10):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def button(text, callback, primary=False):
    widget = Gtk.Button(label=text)
    if primary:
        widget.add_css_class('suggested-action')
    widget.connect('clicked', lambda *_: callback())
    return widget


class TrainerWindow(Adw.ApplicationWindow):
    def __init__(self, application, trainer):
        super().__init__(application=application, title='Whitecube Trainer', default_width=1060, default_height=780)
        self.trainer = trainer
        self.values = trainer.preferences()
        self.selected_id = None
        self.last_job = None
        self.probing = False
        self.launching = False
        self.sampling = False
        self.form = {}
        self.losses = []
        self.rebuilding = False
        self.run_signature = None
        self.last_log = None
        self.save_source = None
        self.runtime_info = None
        self.closed = False
        self.inspect_request = 0
        self.preview_request = 0
        self.metrics_signature = None
        self.observed_active = None
        self.active_job_info = None
        self.displayed_id = None
        self.runs = []
        self.stop_requested = False
        self.toast = Adw.ToastOverlay()
        self.set_content(self.toast)
        layout = box(spacing=0)
        self.toast.set_child(layout)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title='Whitecube Trainer', subtitle='Small models, trained locally'))
        header.pack_start(button('New run', self.new_run))
        header.pack_end(button('Refresh', lambda: self.refresh(True)))
        layout.append(header)
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER)
        switcher.set_margin_top(10); switcher.set_margin_bottom(10)
        layout.append(switcher)
        layout.append(self.stack)
        self.build_train()
        self.build_runs()
        self.build_activity()
        footer = box(False, 12)
        footer.set_margin_top(12); footer.set_margin_bottom(12)
        footer.set_margin_start(18); footer.set_margin_end(18)
        self.status = label('Ready', 'cube-status'); self.status.set_hexpand(True)
        self.stop_button = button('Stop', self.stop)
        self.train_actions.remove(self.prepare_button); self.train_actions.remove(self.start_button)
        footer.append(self.status); footer.append(self.prepare_button); footer.append(self.start_button); footer.append(self.stop_button)
        self.stack.connect('notify::visible-child-name', lambda *_: self.footer_actions())
        self.footer_actions()
        layout.append(footer)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_margin_start(18); self.progress.set_margin_end(18); self.progress.set_margin_bottom(12)
        layout.append(self.progress)
        controller = Gtk.EventControllerKey()
        controller.connect('key-pressed', self.key_pressed)
        self.add_controller(controller)
        self.connect('close-request', self.closing)
        self.refresh(True)
        self.tick_source = GLib.timeout_add(1000, self.tick)
        if not application.options.no_probe:
            GLib.idle_add(self.check_runtime)

    def page(self, name, title):
        page = box(spacing=16)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(page, setter)(22)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_child(page)
        self.stack.add_titled(scroll, name, title)
        return page

    def field(self, page, title, control, hint=None):
        row = box(spacing=5)
        caption = label(title, 'heading')
        caption.set_mnemonic_widget(control)
        row.append(caption)
        row.append(control)
        if hint:
            row.append(label(hint, 'dim-label'))
        page.append(row)

    def entry(self, key, placeholder=''):
        entry = Gtk.Entry(text=str(self.values.get(key, '')), placeholder_text=placeholder, hexpand=True)
        entry.connect('changed', lambda *_: self.queue_save())
        if key == 'dataset':
            entry.connect('changed', self.dataset_changed)
        if key == 'python':
            entry.connect('changed', self.runtime_changed)
        self.form[key] = entry
        return entry

    def spin(self, key, low, high):
        control = Gtk.SpinButton.new_with_range(low, high, 1)
        control.set_value(self.values.get(key, low))
        control.set_numeric(True)
        control.connect('value-changed', lambda *_: self.queue_save())
        self.form[key] = control
        return control

    def check(self, key, title):
        control = Gtk.CheckButton(label=title, active=bool(self.values.get(key)))
        control.connect('toggled', lambda *_: self.queue_save())
        self.form[key] = control
        return control

    def build_train(self):
        page = self.page('train', 'Train')
        page.append(label('Create a small language model', 'title-1'))
        page.append(label('Train on your local text, Markdown, JSONL, or Parquet corpus. Checkpoints appear in Desktop Chat. Models use a 256-byte context and learn prose continuation.', 'dim-label'))
        corpus = box(False, 8)
        corpus.append(self.entry('dataset', '~/train-in'))
        corpus.append(button('Folder…', lambda: self.choose_dataset(True)))
        corpus.append(button('File…', lambda: self.choose_dataset(False)))
        self.inspect_button = button('Inspect', self.inspect_dataset)
        corpus.append(self.inspect_button)
        self.field(page, 'Corpus', corpus)
        self.dataset_info = label('Choose one corpus file or a folder of shards.', 'dim-label')
        page.append(self.dataset_info)
        row = box(False, 14)
        names = box(); names.set_hexpand(True)
        self.form['name'] = Gtk.Entry(placeholder_text='e.g. Whitecube stories', text=str(self.values.get('draft_name', '')), max_length=80)
        self.form['name'].connect('changed', lambda *_: self.queue_save())
        self.field(names, 'Run name', self.form['name'])
        sizes = box(); sizes.set_hexpand(True)
        self.size_picker = Gtk.DropDown.new_from_strings([f'{size.upper()} · {engine.MODEL_PRESETS[size]["parameters"] / 1e6:.2f}M parameters' for size in engine.DESKTOP_SIZES])
        self.size_picker.set_selected(engine.DESKTOP_SIZES.index(self.values['model_size']) if self.values['model_size'] in engine.DESKTOP_SIZES else 1)
        self.size_picker.connect('notify::selected', lambda *_: (self.size_detail(), self.queue_save()))
        self.field(sizes, 'Model size', self.size_picker)
        row.append(names); row.append(sizes); page.append(row)
        self.model_detail = label('', 'dim-label'); page.append(self.model_detail)
        self.size_detail()
        plan = box(False, 14)
        for key, title, low, high in [('steps', 'Total optimizer steps', 1, 10_000_000), ('max_tokens', 'Corpus token cap', 6000, 10_000_000_000)]:
            group = box(); group.set_hexpand(True)
            self.field(group, title, self.spin(key, low, high)); plan.append(group)
        page.append(plan)
        self.device_picker = Gtk.DropDown.new_from_strings(['NVIDIA GPU (CUDA)', 'CPU'])
        self.device_picker.set_selected(0 if self.values.get('device') == 'cuda' else 1)
        self.device_picker.connect('notify::selected', lambda *_: self.queue_save())
        self.field(page, 'Train on', self.device_picker)
        runtime = box(False, 8)
        runtime.append(self.entry('python'))
        self.check_button = button('Check runtime', self.check_runtime)
        runtime.append(self.check_button)
        self.field(page, 'Python training environment', runtime)
        self.runtime_label = label('The window uses system GTK4; training uses the Python environment above.', 'dim-label')
        page.append(self.runtime_label)
        cooling = Gtk.Expander(label='Heat and workload controls (optional)')
        heat = box(spacing=12)
        stagger = self.check('stagger_training', 'Stagger training with cooling breaks')
        heat.append(stagger)
        timing = box(False, 12)
        for key, title, low, high in [('burst_seconds', 'Train for (seconds)', 1, 3600), ('cooldown_seconds', 'Then pause (seconds)', 1, 600)]:
            group = box(); group.set_hexpand(True)
            self.field(group, title, self.spin(key, low, high)); timing.append(group)
        timing.set_sensitive(stagger.get_active())
        stagger.connect('toggled', lambda *_: timing.set_sensitive(stagger.get_active()))
        heat.append(timing)
        heat.append(self.check('low_priority', 'Give training lower CPU and disk priority'))
        heat.append(label('Cooling pauses reduce sustained CPU/GPU work. Priority affects CPU scheduling and disk access; it does not limit GPU power. Stop also works during a break.', 'dim-label'))
        for key, title, low, high in [('max_temp', 'GPU stop temperature (°C)', 50, 90), ('max_cpu_temp', 'CPU stop temperature (°C)', 50, 95)]:
            self.field(heat, title, self.spin(key, low, high))
        cooling.set_child(heat); page.append(cooling)
        advanced = Gtk.Expander(label='Advanced training settings')
        options = box(spacing=12)
        for key, title, low, high in [('batch_size', 'Batch size', 1, 256), ('grad_accum_steps', 'Gradient accumulation', 1, 256),
                                     ('cpu_threads', 'CPU threads', 1, 128), ('schedule_steps', 'Learning rate decay horizon (0 preserves the saved plan)', 0, 10_000_000)]:
            self.field(options, title, self.spin(key, low, high))
        self.field(options, 'Text field override (optional)', self.entry('column', 'text or document.html'))
        advanced.set_child(options); page.append(advanced)
        self.batch_summary = label('', 'dim-label'); page.append(self.batch_summary)
        for key in ('batch_size', 'grad_accum_steps'):
            self.form[key].connect('value-changed', lambda *_: self.update_batch_summary())
        self.update_batch_summary()
        actions = box(False, 10)
        self.train_actions = actions
        self.prepare_button = button('Prepare corpus only', lambda: self.start(True))
        self.start_button = button('Start training', self.start, True)
        actions.append(self.prepare_button); actions.append(self.start_button); page.append(actions)
        page.append(label(f'Runs are saved in {self.trainer.runs_dir}. Stop saves a checkpoint after the current step. Training continues when this window closes.', 'dim-label'))

    def footer_actions(self):
        visible = self.stack.get_visible_child_name() == 'train'
        self.start_button.set_visible(visible); self.prepare_button.set_visible(visible)

    def build_runs(self):
        page = self.page('runs', 'Runs & checkpoints')
        page.append(label('Training runs', 'title-1'))
        filters = box(False, 8)
        self.run_search = Gtk.SearchEntry(placeholder_text='Search name, model size, or run ID', hexpand=True)
        self.run_search.connect('search-changed', lambda *_: self.filter_runs())
        self.run_filter = Gtk.DropDown.new_from_strings(['All runs', 'With checkpoints', 'Prepared only', 'Needs attention'])
        self.run_filter.connect('notify::selected', lambda *_: self.filter_runs())
        filters.append(self.run_search); filters.append(self.run_filter); page.append(filters)
        self.run_count = label('', 'dim-label'); page.append(self.run_count)
        self.run_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.run_list.add_css_class('boxed-list')
        self.run_list.connect('row-selected', self.run_selected)
        page.append(self.run_list)
        self.run_detail = label('Choose a run to inspect its checkpoints.', 'dim-label')
        page.append(self.run_detail)
        self.resume_steps = Gtk.SpinButton.new_with_range(1, 10_000_000, 1)
        self.resume_steps.set_value(2000)
        self.resume_steps.connect('value-changed', lambda *_: self.update_resume_action())
        self.field(page, 'Resume to this total step count', self.resume_steps, 'The saved model size, prepared corpus, batch and accumulation are reused.')
        self.resume_hint = label('', 'dim-label'); page.append(self.resume_hint)
        actions = box(False, 8)
        self.resume_button = button('Resume run', self.resume, True)
        self.rename_button = button('Rename', self.rename)
        self.report_button = button('Export report…', self.export_report)
        self.copy_button = button('Copy run path', self.copy_path)
        self.delete_button = button('Delete…', self.delete)
        for widget in (self.resume_button, self.rename_button, self.report_button, self.copy_button, self.delete_button):
            actions.append(widget)
        page.append(actions)
        utilities = box(False, 8)
        self.duplicate_button = button('Use run settings', self.use_run_settings)
        self.open_button = button('Open folder', self.open_folder)
        self.metrics_button = button('Export plotted metrics…', self.export_metrics)
        for widget in (self.duplicate_button, self.open_button, self.metrics_button):
            utilities.append(widget)
        page.append(utilities)
        chats = box(False, 8)
        self.best_button = button('Chat with Best', lambda: self.chat('best'))
        self.latest_button = button('Chat with Latest', lambda: self.chat('latest'))
        chats.append(self.best_button); chats.append(self.latest_button); page.append(chats)
        page.append(label('Checkpoint preview', 'heading'))
        sample_row = box(False, 8)
        self.sample_prompt = Gtk.Entry(placeholder_text='Short prompt…', text='Once upon a time', hexpand=True)
        self.sample_prompt.connect('activate', lambda *_: self.sample() if self.sample_button.get_sensitive() else None)
        self.sample_kind = Gtk.DropDown.new_from_strings(['Best', 'Latest'])
        self.sample_kind.connect('notify::selected', lambda *_: self.refresh())
        self.sample_button = button('Preview', self.sample)
        sample_row.append(self.sample_prompt); sample_row.append(self.sample_kind); sample_row.append(self.sample_button); page.append(sample_row)
        self.sample_output = label('', 'cube-hash'); page.append(self.sample_output)
        self.copy_sample_button = button('Copy preview', self.copy_preview)
        self.copy_sample_button.set_sensitive(False); page.append(self.copy_sample_button)

    def build_activity(self):
        page = self.page('activity', 'Progress & logs')
        page.append(label('Training progress', 'title-1'))
        self.metrics_label = label('Training and validation losses appear here.', 'dim-label'); page.append(self.metrics_label)
        self.chart = Gtk.DrawingArea(content_height=180, hexpand=True)
        self.chart.set_draw_func(self.draw_loss)
        self.chart.set_tooltip_text('Training loss (black) and validation loss (blue), against optimizer steps.')
        page.append(self.chart)
        page.append(label('Black: training loss · Blue: validation loss · Lower is better', 'dim-label'))
        controls = box(False, 8)
        controls.append(button('Copy log', self.copy_log))
        self.export_log_button = button('Export displayed log…', self.export_log); controls.append(self.export_log_button)
        self.follow = Gtk.CheckButton(label='Follow latest output', active=True)
        self.follow.connect('toggled', lambda *_: self.scroll_to_end() if self.follow.get_active() else None)
        controls.append(self.follow); page.append(controls)
        search = box(False, 8)
        self.log_search = Gtk.SearchEntry(placeholder_text='Find in displayed log', hexpand=True)
        self.log_search.connect('activate', lambda *_: self.find_log())
        self.log_search.connect('search-changed', lambda *_: self.find_log(restart=True))
        search.append(self.log_search); search.append(button('Find next', self.find_log)); page.append(search)
        self.log_search_status = label('', 'dim-label'); page.append(self.log_search_status)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_scroll = Gtk.ScrolledWindow(hexpand=True, min_content_height=260, vexpand=True)
        self.log_scroll.set_child(self.log_view); page.append(self.log_scroll)

    def form_values(self):
        values = {}
        for key, widget in self.form.items():
            if isinstance(widget, Gtk.SpinButton):
                widget.update()
                values[key] = int(widget.get_value())
            elif isinstance(widget, Gtk.CheckButton):
                values[key] = widget.get_active()
            else:
                values[key] = widget.get_text()
        values.update(model_size=engine.DESKTOP_SIZES[self.size_picker.get_selected()], device='cuda' if self.device_picker.get_selected() == 0 else 'cpu')
        return values

    def queue_save(self):
        if self.closed:
            return
        if self.save_source:
            GLib.source_remove(self.save_source)
        self.save_source = GLib.timeout_add(500, self.save_draft)

    def save_draft(self):
        if self.save_source:
            GLib.source_remove(self.save_source)
        self.save_source = None
        if hasattr(self, 'device_picker'):
            try:
                values = self.form_values()
                values['draft_name'] = values.pop('name')
                self.trainer.save_preferences(values)
            except (OSError, ValueError) as error:
                self.notify(str(error))
        return False

    def closing(self, *_):
        if self.save_source:
            GLib.source_remove(self.save_source)
            self.save_source = None
        self.save_draft()
        self.closed = True
        if self.tick_source:
            GLib.source_remove(self.tick_source)
            self.tick_source = None
        self.get_application().window = None
        return False

    def notify(self, text):
        self.toast.add_toast(Adw.Toast(title=str(text)[:500]))

    def background(self, function, done):
        def deliver(value, error):
            if not self.closed:
                done(value, error)
            return False
        def work():
            try:
                value, error = function(), None
            except Exception as exception:
                value, error = None, exception
            GLib.idle_add(deliver, value, error)
        threading.Thread(target=work, daemon=True).start()

    def check_runtime(self):
        if self.probing or self.closed:
            return False
        self.probing = True; self.check_button.set_sensitive(False)
        self.runtime_label.set_text('Checking PyTorch, GPU, and Parquet support…')
        executable = self.form['python'].get_text()
        def done(info, error):
            self.probing = False; self.check_button.set_sensitive(True)
            if executable != self.form['python'].get_text():
                self.runtime_label.set_text('Python environment changed. Check this runtime before training.')
                return False
            if error:
                self.runtime_info = None; self.runtime_label.set_text(str(error))
            else:
                self.runtime_info = info
                self.runtime_label.set_text(f'PyTorch {info["torch"]} · {info["gpu"] or "CPU available"} · Parquet {"available" if info["parquet"] else "needs pyarrow"}')
                if not info['cuda']:
                    self.device_picker.set_selected(1)
            return False
        self.background(lambda: engine.probe_runtime(executable), done)
        return False

    def runtime_changed(self, *_):
        self.runtime_info = None
        if hasattr(self, 'runtime_label'):
            self.runtime_label.set_text('Python environment changed. Check this runtime before training.')

    def dataset_changed(self, *_):
        self.inspect_request += 1
        if hasattr(self, 'dataset_info'):
            self.dataset_info.set_text('Corpus changed. Inspect it to check supported files and size.')
            self.inspect_button.set_sensitive(True)

    def choose_dataset(self, folder):
        chooser = Gtk.FileChooserNative(title='Choose corpus folder' if folder else 'Choose corpus file', transient_for=self,
                                        action=Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN,
                                        accept_label='Choose', cancel_label='Cancel')
        if not folder:
            filt = Gtk.FileFilter(); filt.set_name('Training corpus')
            for pattern in ('*.txt', '*.md', '*.jsonl', '*.parquet'):
                filt.add_pattern(pattern)
            chooser.add_filter(filt)
        source = Path(self.form['dataset'].get_text()).expanduser()
        if source.exists():
            chooser.set_current_folder(Gio.File.new_for_path(str(source if source.is_dir() else source.parent)))
        def response(dialog, result):
            if result == Gtk.ResponseType.ACCEPT and dialog.get_file():
                path = dialog.get_file().get_path()
                if path:
                    self.form['dataset'].set_text(path); self.inspect_dataset()
                else:
                    self.notify('Choose a local corpus file or folder.')
            dialog.destroy()
        chooser.connect('response', response); chooser.show()

    def inspect_dataset(self):
        value = self.form['dataset'].get_text().strip()
        if not value:
            self.dataset_info.set_text('Choose an existing corpus file or folder.')
            return
        source = Path(value).expanduser()
        self.inspect_request += 1
        request = self.inspect_request
        self.inspect_button.set_sensitive(False)
        self.dataset_info.set_text('Inspecting corpus files…')
        def done(info, error):
            if request != self.inspect_request:
                return False
            self.inspect_button.set_sensitive(True)
            self.dataset_info.set_text(str(error) if error else f'{len(info["files"])} supported files · {info["bytes"] / 1024**2:.1f} MiB · {len(info["empty_files"])} empty files. Full schema checks run before training.')
            return False
        self.background(lambda: engine.dataset_info(source), done)

    def size_detail(self):
        if not hasattr(self, 'model_detail'):
            return
        size = engine.DESKTOP_SIZES[self.size_picker.get_selected()]
        preset = engine.MODEL_PRESETS[size]
        self.model_detail.set_text(f'{preset["parameters"]:,} parameters · 4 layers · width {preset["width"]} · FP32 weights {preset["parameters"] * 4 / 1024**2:.1f} MiB. Training also needs optimizer and activation memory.')

    def new_run(self):
        self.form['name'].set_text(''); self.stack.set_visible_child_name('train'); self.form['name'].grab_focus()

    def update_batch_summary(self):
        batch = int(self.form['batch_size'].get_value())
        accumulation = int(self.form['grad_accum_steps'].get_value())
        self.batch_summary.set_text(f'Effective batch: {batch * accumulation:,} sequences · {batch * accumulation * 256:,} byte tokens per optimizer step. Gradient accumulation increases this without enlarging each GPU batch.')

    def filter_runs(self):
        if not hasattr(self, 'run_list'):
            return
        query = self.run_search.get_text().strip().casefold()
        kind = self.run_filter.get_selected()
        count = 0
        row = self.run_list.get_first_child()
        while row:
            run = getattr(row, 'run_record', None)
            if run is not None:
                matches = not query or query in ' '.join(str(run[key]) for key in ('name', 'id', 'size', 'phase')).casefold()
                matches = matches and (kind == 0 or kind == 1 and (run['best'] or run['latest'])
                                       or kind == 2 and run['prepared'] and not run['best'] and not run['latest']
                                       or kind == 3 and run['phase'] in ('failed', 'interrupted', 'incomplete'))
                row.set_visible(bool(matches)); count += bool(matches)
            row = row.get_next_sibling()
        self.run_count.set_text(f'{count} of {len(self.runs)} runs' + (' · No matching runs. Clear the search or change the filter.' if self.runs and not count else ''))

    def update_resume_action(self):
        if not hasattr(self, 'resume_button'):
            return
        run = next((item for item in self.runs if item['id'] == self.selected_id), None)
        target = int(self.resume_steps.get_value())
        busy = self.launching or self.sampling or bool(self.active_job_info)
        possible = bool(run and run['prepared'] and run['meta'] and (run['latest'] or run['completed_steps'] == 0))
        self.resume_button.set_sensitive(possible and target > run['completed_steps'] and not busy)
        if not run:
            message = 'Select a prepared run to resume.'
        elif not possible:
            message = 'Resume needs prepared data and the Latest checkpoint for previously trained runs.'
        elif target <= run['completed_steps']:
            message = f'Choose a total above {run["completed_steps"]:,} completed steps.'
        else:
            message = f'{target - run["completed_steps"]:,} additional optimizer steps.'
        self.resume_hint.set_text(message)

    def use_run_settings(self):
        run = self.current_run()
        if not run:
            return
        for key, widget in self.form.items():
            if key not in run['meta'] or key == 'name':
                continue
            value = run['meta'][key]
            if isinstance(widget, Gtk.SpinButton):
                if finite_number(value): widget.set_value(value)
            elif isinstance(widget, Gtk.CheckButton):
                if isinstance(value, bool): widget.set_active(value)
            elif isinstance(value, str):
                widget.set_text(value)
        if run['size'] in engine.DESKTOP_SIZES:
            self.size_picker.set_selected(engine.DESKTOP_SIZES.index(run['size']))
        if run['meta'].get('device') in ('cpu', 'cuda'):
            self.device_picker.set_selected(0 if run['meta']['device'] == 'cuda' else 1)
        self.form['name'].set_text((run['name'][:73] + ' (copy)')[:80])
        self.stack.set_visible_child_name('train'); self.queue_save()
        self.notify('Saved run settings copied into a new training plan.')

    def open_folder(self):
        run = self.current_run()
        if run:
            try:
                Gio.AppInfo.launch_default_for_uri(Path(run['directory']).as_uri(), self.get_display().get_app_launch_context())
            except (ValueError, GLib.Error) as error:
                self.notify(error)

    def start(self, prepare_only=False):
        self.launch(self.form_values(), prepare_only=prepare_only)

    def resume(self):
        self.resume_steps.update()
        self.update_resume_action()
        if not self.resume_button.get_sensitive():
            self.notify(self.resume_hint.get_text()); return
        values = self.form_values(); values['steps'] = int(self.resume_steps.get_value())
        self.launch(values, resume_id=self.selected_id)

    def launch(self, values, **options):
        if self.launching or self.sampling:
            return
        self.launching = True; self.refresh(); self.status.set_text('Checking training plan…')
        def done(job, error):
            self.launching = False
            if error:
                self.refresh(); self.notify(error); self.status.set_text(str(error))
            else:
                self.stop_requested = False
                self.last_job = job; self.selected_id = Path(job['run']).name
                self.stack.set_visible_child_name('activity'); self.refresh(True)
            return False
        self.background(lambda: self.trainer.start(values, **options), done)

    def stop(self):
        if self.stop_requested:
            return
        try:
            job = self.trainer.active_job() or self.last_job
            if job:
                self.trainer.stop(job); self.stop_requested = True; self.stop_button.set_sensitive(False)
                self.status.set_text('Stopping; finishing the current step and saving when weights are available…')
        except (ValueError, OSError) as error:
            self.notify(error)

    def current_run(self):
        return next((run for run in self.trainer.list_runs() if run['id'] == self.selected_id), None)

    def run_selected(self, _, row):
        if self.rebuilding or row is None:
            return
        if not hasattr(row, 'run_id'):
            return
        changed = self.selected_id != row.run_id
        self.selected_id = row.run_id
        run = self.current_run()
        if not run:
            self.refresh(True); return
        self.resume_steps.set_value(max(run['steps'], run['completed_steps'] + 1000))
        if changed:
            self.preview_request += 1
            self.sample_output.set_text(''); self.copy_sample_button.set_sensitive(False)
        self.last_log = None; self.refresh()

    def refresh(self, force=False):
        runs = self.trainer.list_runs()
        self.runs = runs
        active = self.trainer.active_job()
        self.active_job_info = active
        busy = bool(active) or self.launching
        if active:
            self.last_job = active
            self.observed_active = active
            if not self.selected_id:
                self.selected_id = Path(active['run']).name
        elif self.observed_active:
            previous = self.observed_active
            self.observed_active = None; self.stop_requested = False
            completed = next((run for run in runs if run['directory'] == previous.get('run')), None)
            if completed:
                self.notify(f'{completed["name"]}: {completed["phase"]}.')
        if self.selected_id and not any(run['id'] == self.selected_id for run in runs):
            self.selected_id = None; self.last_log = None; self.metrics_signature = None
            self.preview_request += 1; self.sample_output.set_text(''); self.copy_sample_button.set_sensitive(False)
        if not self.selected_id and runs:
            self.selected_id = runs[0]['id']
        if self.displayed_id != self.selected_id:
            self.displayed_id = self.selected_id
            self.preview_request += 1; self.sample_output.set_text(''); self.copy_sample_button.set_sensitive(False)
            self.last_log = None; self.metrics_signature = None
        signature = json.dumps([{key: run[key] for key in ('id', 'name', 'phase', 'size', 'completed_steps', 'steps', 'prepared', 'best', 'latest')} for run in runs])
        if force or signature != self.run_signature:
            self.run_signature = signature; self.rebuilding = True
            child = self.run_list.get_first_child()
            while child:
                following = child.get_next_sibling(); self.run_list.remove(child); child = following
            for run in runs:
                row = Gtk.ListBoxRow(); row.run_id = run['id']; row.run_record = run
                content = box(spacing=3)
                for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
                    getattr(content, setter)(10)
                content.append(label(run['name'], 'heading'))
                content.append(label(f'{run["size"].upper()} · {run["phase"]} · {run["completed_steps"]:,} / {run["steps"]:,} steps', 'dim-label'))
                row.set_child(content); self.run_list.append(row)
                if run['id'] == self.selected_id:
                    self.run_list.select_row(row)
            if not runs:
                row = Gtk.ListBoxRow(selectable=False, activatable=False)
                row.set_child(label('No runs yet. Create one on the Train page.', 'dim-label')); self.run_list.append(row)
            self.rebuilding = False
            self.filter_runs()
        run = next((run for run in runs if run['id'] == self.selected_id), None)
        for widget in (self.start_button, self.prepare_button):
            widget.set_sensitive(not busy and not self.sampling)
        self.stop_button.set_sensitive(bool(active) and not self.stop_requested and active.get('state') != 'stopping')
        self.update_resume_action()
        self.rename_button.set_sensitive(bool(run and run['meta'] and not (run['job'] and run['job']['running'])))
        self.delete_button.set_sensitive(bool(run and not busy and not self.sampling))
        for widget in (self.report_button, self.copy_button):
            widget.set_sensitive(bool(run))
        self.open_button.set_sensitive(bool(run))
        self.duplicate_button.set_sensitive(bool(run and run['meta']))
        self.metrics_button.set_sensitive(bool(run))
        self.best_button.set_sensitive(bool(run and run['best']))
        self.latest_button.set_sensitive(bool(run and run['latest']))
        checkpoint = 'best' if self.sample_kind.get_selected() == 0 else 'latest'
        self.sample_button.set_sensitive(bool(run and run[checkpoint] and not busy and not self.sampling))
        if run:
            validation = f'{run["val_loss"]:.4f}' if isinstance(run['val_loss'], (int, float)) else 'not evaluated'
            self.run_detail.set_text(f'{run["name"]}\n{run["directory"]}\n{run["parameters"] or "?":} parameters · validation loss {validation}\nBest: {"saved" if run["best"] else "pending"} · Latest: {"saved" if run["latest"] else "pending"}\n{run["detail"]}')
            metrics_path = Path(run['directory']) / 'train_log.jsonl'
            try:
                stat = metrics_path.stat()
                metrics_signature = (str(metrics_path), stat.st_mtime_ns, stat.st_size)
            except OSError:
                metrics_signature = (str(metrics_path), None)
            if force or metrics_signature != self.metrics_signature:
                self.metrics_signature = metrics_signature
                self.losses = engine.read_metrics(run['directory']); self.chart.queue_draw()
            latest = next((metric for metric in reversed(self.losses) if 'total_loss' in metric), {})
            progress = engine.read_json(Path(run['directory']) / 'progress.json')
            loss_value = latest.get('total_loss', progress.get('loss'))
            loss = f'{loss_value:.4f}' if finite_number(loss_value) else '—'
            self.metrics_label.set_text(f'{run["name"]} · Training loss {loss} · best validation {validation} · {run["completed_steps"]:,} completed steps')
            performance = []
            if finite_number(progress.get('tokens_per_second')) and progress['tokens_per_second'] >= 0:
                performance.append(f'{progress["tokens_per_second"]:,.0f} tokens/s')
            if finite_number(progress.get('tokens_processed')) and progress['tokens_processed'] >= 0:
                performance.append(f'{progress["tokens_processed"]:,.0f} tokens processed')
            if run['job'] and run['job'].get('running') and finite_number(progress.get('eta_seconds')) and progress['eta_seconds'] >= 0:
                performance.append(f'Estimated remaining {format_duration(progress["eta_seconds"])}')
            if finite_number(progress.get('gradient_norm')):
                performance.append(f'Gradient norm {progress["gradient_norm"]:.3g}')
            if performance:
                self.metrics_label.set_text(self.metrics_label.get_text() + '\n' + ' · '.join(performance))
            if run['detail']:
                self.metrics_label.set_text(self.metrics_label.get_text() + '\n' + str(run['detail']))
            telemetry = engine.latest_telemetry(run['directory'])
            thermal = []
            if telemetry.get('temperature_c') is not None:
                thermal.append(f'GPU {telemetry["temperature_c"]} °C')
            if telemetry.get('cpu_temperature_c') is not None:
                thermal.append(f'CPU {telemetry["cpu_temperature_c"]} °C')
            if telemetry.get('memory_used_mib') is not None:
                thermal.append(f'GPU memory {telemetry["memory_used_mib"]:,} MiB')
            if thermal:
                self.metrics_label.set_text(self.metrics_label.get_text() + '\n' + ' · '.join(thermal))
            job = run['job']
            logfile = job['log'] if job else str(Path(run['directory']) / 'console.log')
            total = run['steps']
            self.progress.set_fraction(min(1, run['completed_steps'] / total) if total else 0)
            self.progress.set_text(f'{run["completed_steps"]:,} / {total:,} steps · {run["phase"]}')
            if not busy:
                self.status.set_text(f'{run["phase"].capitalize()} · {run["name"]}')
        else:
            self.run_detail.set_text('Choose a run to inspect its checkpoints.')
            self.losses = []; self.metrics_signature = None; self.chart.queue_draw()
            self.metrics_label.set_text('Training and validation losses appear here.')
            self.progress.set_fraction(0)
            self.progress.set_text('Preparing…' if busy else 'Ready')
            job = self.last_job
            if job:
                stored = engine.read_json(job['path']); job = {**job, **stored}
            logfile = job['log'] if job else None
            if not busy:
                self.status.set_text(f'Job {job.get("state", "ready")}. See logs for details.' if job else 'Ready')
        if active:
            active_run = next((item for item in runs if item['directory'] == active['run']), None)
            phase = active_run['phase'] if active_run else 'starting'
            self.status.set_text('Stopping and saving…' if active.get('state') == 'stopping' or self.stop_requested else f'{phase.capitalize()} · {active_run["name"] if active_run else Path(active["run"]).name}')
            if phase in ('preparing', 'starting', 'queued'):
                self.progress.pulse()
        text = engine.tail_text(logfile) if logfile else ''
        self.export_log_button.set_sensitive(bool(text))
        if text != self.last_log:
            self.last_log = text
            adjustment = self.log_scroll.get_vadjustment(); previous = adjustment.get_value()
            at_bottom = previous + adjustment.get_page_size() >= adjustment.get_upper() - 24
            self.log_view.get_buffer().set_text(text)
            def restore_scroll():
                if self.closed:
                    return False
                adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()) if self.follow.get_active() and at_bottom else previous)
                return False
            GLib.idle_add(restore_scroll)

    def tick(self):
        if self.closed:
            return False
        try:
            self.refresh()
        except (OSError, ValueError) as error:
            self.status.set_text(str(error))
        return True

    def rename(self):
        run = self.current_run()
        if not run:
            return
        dialog = Adw.Dialog(title='Rename run', content_width=440)
        layout = box(); layout.set_margin_top(18); layout.set_margin_bottom(18); layout.set_margin_start(18); layout.set_margin_end(18)
        entry = Gtk.Entry(text=run['name'], max_length=80)
        layout.append(label('Display name')); layout.append(entry)
        layout.append(label('The run folder and checkpoint IDs stay in place.', 'dim-label'))
        actions = box(False, 8)
        def save():
            try:
                self.trainer.rename_run(run['id'], entry.get_text()); dialog.close(); self.refresh(True)
            except (ValueError, OSError) as error:
                self.notify(error)
        actions.append(button('Cancel', dialog.close)); actions.append(button('Save name', save, True))
        entry.connect('activate', lambda *_: save())
        layout.append(actions); dialog.set_child(layout); dialog.present(self); entry.grab_focus()

    def copy_path(self):
        run = self.current_run()
        if run:
            self.get_display().get_clipboard().set(run['directory']); self.notify('Run path copied')

    def delete(self):
        run = self.current_run()
        if not run:
            return
        dialog = Adw.Dialog(title='Move model to Trash', content_width=500)
        layout = box()
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(layout, setter)(18)
        layout.append(label(run['name'], 'heading')); layout.append(label(run['directory'], 'dim-label'))
        choices = [('run', 'Entire run: prepared corpus, checkpoints and logs')]
        if run['best']: choices.append(('best', 'Best checkpoint only'))
        if run['latest']: choices.append(('latest', 'Latest checkpoint only'))
        picker = Gtk.DropDown.new_from_strings([text for _, text in choices]); layout.append(picker)
        layout.append(label('Files are moved to your Trash. Removing Latest disables further training until you restore it. Other training runs stay available.', 'dim-label'))
        confirm = Gtk.CheckButton(label='I want to move the selected files to Trash')
        layout.append(confirm)
        actions = box(False, 10)
        actions.append(button('Cancel', dialog.close))
        pending = False
        def remove():
            nonlocal pending
            if not confirm.get_active() or pending: return
            pending = True
            kind = choices[picker.get_selected()][0]
            def done(destination, error):
                nonlocal pending
                pending = False
                if error:
                    self.notify(error); remove_button.set_sensitive(confirm.get_active())
                    confirm.set_sensitive(True); picker.set_sensitive(True)
                else:
                    if kind == 'run': self.selected_id = None
                    dialog.close(); self.refresh(True); self.notify('Moved to Trash')
                return False
            remove_button.set_sensitive(False)
            confirm.set_sensitive(False); picker.set_sensitive(False)
            self.background(lambda: self.trainer.trash(run['id'], kind), done)
        remove_button = button('Move to Trash', remove)
        remove_button.set_sensitive(False)
        confirm.connect('toggled', lambda *_: remove_button.set_sensitive(confirm.get_active() and not pending))
        actions.append(remove_button); layout.append(actions); dialog.set_child(layout); dialog.present(self)

    def copy_log(self):
        self.get_display().get_clipboard().set(self.last_log or ''); self.notify('Log copied')

    def scroll_to_end(self):
        if hasattr(self, 'log_scroll'):
            adjustment = self.log_scroll.get_vadjustment()
            adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))

    def find_log(self, restart=False):
        query = self.log_search.get_text()
        if not hasattr(self, 'log_view'):
            return
        if not query:
            self.log_search_status.set_text(''); return
        buffer = self.log_view.get_buffer()
        start = buffer.get_start_iter() if restart else buffer.get_iter_at_mark(buffer.get_insert())
        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE | Gtk.TextSearchFlags.TEXT_ONLY
        match = start.forward_search(query, flags, None)
        if not match and start.get_offset():
            match = buffer.get_start_iter().forward_search(query, flags, None)
        if match:
            first, last = match
            buffer.select_range(last, first)
            self.follow.set_active(False)
            self.log_view.scroll_to_iter(first, .1, False, 0, 0)
            self.log_search_status.set_text(f'Match at line {first.get_line() + 1:,}. Enter finds the next match.')
        else:
            self.log_search_status.set_text('No matches in the displayed log.')

    def choose_export(self, title, filename, produce):
        chooser = Gtk.FileChooserNative(title=title, transient_for=self, action=Gtk.FileChooserAction.SAVE,
                                        accept_label='Export', cancel_label='Cancel')
        chooser.set_current_name(filename)
        def response(dialog, result):
            if result == Gtk.ResponseType.ACCEPT and dialog.get_file():
                path = dialog.get_file().get_path()
                if path:
                    self.background(lambda: write_export(path, produce()),
                                    lambda _, error: self.notify(error or f'Exported {Path(path).name}'))
                else:
                    self.notify('Choose a local destination for the export.')
            dialog.destroy()
        chooser.connect('response', response); chooser.show()

    def export_log(self):
        text = self.last_log or ''
        self.choose_export('Export displayed log', f'{self.selected_id or "training"}-log.txt', lambda: text)

    def export_metrics(self):
        if self.selected_id:
            metrics = list(self.losses)
            self.choose_export('Export plotted metrics', f'{self.selected_id}-metrics.csv', lambda: metrics_csv(metrics))

    def copy_preview(self):
        self.get_display().get_clipboard().set(self.sample_output.get_text())
        self.notify('Preview copied')

    def export_report(self):
        if not self.selected_id:
            return
        run_id = self.selected_id
        self.choose_export('Export training report', f'{run_id}-report.json',
                           lambda: json.dumps(self.trainer.report(run_id), indent=2, ensure_ascii=False) + '\n')

    def chat(self, kind):
        try:
            subprocess.Popen(self.trainer.chat_command(self.selected_id, kind), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (ValueError, OSError) as error:
            self.notify(error)

    def sample(self):
        run = self.current_run()
        prompt = self.sample_prompt.get_text().strip()
        kind = 'best' if self.sample_kind.get_selected() == 0 else 'latest'
        if not run or not prompt or self.sampling or self.launching or self.trainer.active_job() or not run[kind]:
            self.notify('Choose an available checkpoint, enter a prompt, and wait for training or preview to finish.'); return
        self.preview_request += 1
        request = self.preview_request
        run_id = run['id']
        self.copy_sample_button.set_sensitive(False)
        self.sampling = True; self.sample_output.set_text('Generating a short CPU preview…'); self.refresh()
        command = [str(Path(self.form['python'].get_text()).expanduser()), '-u', str(engine.PIPELINE / 'preview.py'),
                   '--checkpoint', str(Path(run['directory']) / f'ckpt_{kind}.pt'), '--prompt', prompt]
        def work():
            result = subprocess.run(command, capture_output=True, text=True, timeout=45)
            if result.returncode:
                raise ValueError(result.stderr[-1200:] or 'Preview failed.')
            return result.stdout
        def done(value, error):
            self.sampling = False
            if request == self.preview_request and run_id == self.selected_id:
                self.sample_output.set_text(str(error) if error else value.strip())
                self.copy_sample_button.set_sensitive(not error and bool(value.strip()))
            self.refresh()
            return False
        self.background(work, done)

    def draw_loss(self, _, context, width, height):
        context.set_source_rgb(.94, .94, .94); context.paint()
        if width < 100 or height < 65:
            return
        points = loss_series(self.losses, 'total_loss')
        val = loss_series(self.losses, 'val_loss')
        if not points and not val:
            context.set_source_rgb(.25, .25, .25); context.move_to(16, 30); context.show_text('Loss graph appears after the first training step.'); return
        all_points = points + val
        xmax = max(1, max(point[0] for point in all_points))
        raw_ymin, raw_ymax = min(point[1] for point in all_points), max(point[1] for point in all_points)
        # Work in a bounded range: subtracting opposite finite extremes can
        # overflow, while adding a fixed pad to a large constant can round away.
        yscale = max(1, abs(raw_ymin), abs(raw_ymax))
        ymin, ymax = raw_ymin / yscale, raw_ymax / yscale
        if ymax - ymin < max(.05 / yscale, 1e-15):
            middle = (ymin + ymax) / 2
            padding = max(.025 / yscale, abs(middle) * .025, 1e-15)
            ymin, ymax = middle - padding, middle + padding
        span = ymax - ymin
        context.set_source_rgb(.1, .1, .1); context.set_line_width(1)
        context.move_to(48, 15); context.line_to(48, height - 32); context.line_to(width - 15, height - 32); context.stroke()
        def tick_text(bound, fallback):
            value = bound * yscale
            if not math.isfinite(value):
                value = fallback
            return f'{value:.3g}' if abs(value) >= 10_000 else f'{value:.2f}'
        context.move_to(5, 25); context.show_text(tick_text(ymax, raw_ymax))
        context.move_to(5, height - 35); context.show_text(tick_text(ymin, raw_ymin))
        context.move_to(48, height - 10); context.show_text('0'); context.move_to(width - 90, height - 10); context.show_text(f'{xmax:g} steps')
        for series, color in ((points, (.05, .05, .05)), (val, (.2, .35, .7))):
            context.set_source_rgb(*color); context.set_line_width(2)
            for index, (step, loss) in enumerate(series):
                x = 48 + step / xmax * (width - 65); y = 15 + (ymax - loss / yscale) / span * (height - 47)
                if index == 0: context.move_to(x, y)
                else: context.line_to(x, y)
            context.stroke()
            if len(series) == 1:
                step, loss = series[0]; context.arc(48 + step / xmax * (width - 65), 15 + (ymax - loss / yscale) / span * (height - 47), 3, 0, 2 * math.pi); context.fill()

    def key_pressed(self, _, key, code, state):
        if self.get_visible_dialog():
            return False
        if state & Gdk.ModifierType.CONTROL_MASK:
            if key in (Gdk.KEY_1, Gdk.KEY_2, Gdk.KEY_3):
                self.stack.set_visible_child_name(('train', 'runs', 'activity')[key - Gdk.KEY_1]); return True
            if key in (Gdk.KEY_n, Gdk.KEY_N): self.new_run(); return True
            if key in (Gdk.KEY_f, Gdk.KEY_F):
                if self.stack.get_visible_child_name() == 'runs': self.run_search.grab_focus()
                else: self.stack.set_visible_child_name('activity'); self.log_search.grab_focus()
                return True
            if key == Gdk.KEY_Return:
                page = self.stack.get_visible_child_name()
                if page == 'train' and self.start_button.get_sensitive(): self.start(); return True
                if page == 'runs' and self.resume_button.get_sensitive(): self.resume(); return True
        if key == Gdk.KEY_F5: self.refresh(True); return True
        return False


class TrainerApp(Adw.Application):
    def __init__(self, options, trainer=None):
        GLib.set_prgname('org.whitecube.Trainer')
        super().__init__(application_id='org.whitecube.Trainer', flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.options = options
        self.trainer = trainer or engine.Trainer()
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self); apply_theme()

    def do_activate(self):
        if not self.window:
            self.window = TrainerWindow(self, self.trainer)
        self.window.present()
        if self.options.page:
            self.window.stack.set_visible_child_name(self.options.page)
        if self.options.screenshot:
            GLib.timeout_add(1300, self.take_screenshot)

    def take_screenshot(self):
        screenshot(self.window, str(self.options.screenshot)); self.quit(); return False


def main():
    parser = argparse.ArgumentParser(description='Whitecube Trainer — native small-model workshop')
    parser.add_argument('--version', action='version', version=f'Whitecube Trainer {VERSION}')
    parser.add_argument('--page', choices=('train', 'runs', 'activity'))
    parser.add_argument('--screenshot', type=Path)
    parser.add_argument('--no-probe', action='store_true', help='Skip automatic runtime probe (for screenshots/testing)')
    options = parser.parse_args()
    return TrainerApp(options).run([sys.argv[0]])


if __name__ == '__main__':
    raise SystemExit(main())
