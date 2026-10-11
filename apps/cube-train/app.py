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
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'cube-apps'))
import engine
import export_notebook
from cube_theme import Adw, Gdk, Gio, GLib, Gtk, apply_theme, screenshot
from gi.repository import Pango

VERSION = '0.9.0'
PLAN_PRESETS = (('Custom', None), ('Smoke test · 20 steps', 20), ('Short · 1,000 steps', 1000),
                ('Standard · 5,000 steps', 5000), ('Long · 20,000 steps', 20_000))
RUN_SORTS = ('Newest first', 'Oldest first', 'Name', 'Best validation loss', 'Model size')
SHORTCUTS = (('Ctrl+1 / Ctrl+2 / Ctrl+3', 'Train, Runs and Progress pages'), ('Ctrl+N', 'New run draft'),
             ('Ctrl+Enter', 'Start training (Train) or resume (Runs)'), ('Ctrl+F', 'Search runs or the log'),
             ('Ctrl+P', 'Preview the selected checkpoint'), ('Ctrl+E', 'Export the selected run report'),
             ('Ctrl+Shift+E', 'Export the selected run as a Jupyter notebook'), ('F2', 'Rename the selected run'),
             ('Delete', 'Move the selected run to Trash (run list focused)'), ('Menu / Shift+F10', 'Run actions menu'),
             ('Double-click / Enter', 'Open a run on the Progress page'), ('F5', 'Refresh'), ('F1', 'Show keyboard shortcuts'))
PAGES = (('train', '01', 'Train'), ('runs', '02', 'Runs'), ('activity', '03', 'Progress'))
RED = (.784, .063, .18)
# Whitecube Trainer layer over cube_theme: black rail, stat tiles, chips and a terminal-style log.
TRAINER_CSS = '''
.cube-rail { background: #111; padding: 18px 12px; }
.cube-rail label { color: #f2f2f2; }
.cube-rail .rail-caption { font-size: 8px; font-weight: 800; letter-spacing: 3px; color: #9a9a9a; }
.cube-rail .rail-name { font-size: 19px; font-weight: 900; letter-spacing: -1px; }
.cube-rail button { background: transparent; color: #d8d8d8; border: 2px solid transparent; padding: 7px 10px; }
.cube-rail button label { color: inherit; }
.cube-rail button:hover { background: #222; border-color: #f2f2f2; }
.cube-rail button.selected { background: #f2f2f2; color: #111; border-color: #f2f2f2; }
.cube-rail .rail-job { border: 2px solid #f2f2f2; padding: 9px; }
.cube-rail .rail-job .dim-label { color: #b0b0b0; }
.cube-rail progressbar trough { background: #333; border-color: #f2f2f2; min-height: 6px; }
.cube-rail progressbar progress { background: #f2f2f2; }
.cube-rail .live { color: #ff4b4b; font-weight: 900; }
.eyebrow { font-size: 8px; font-weight: 800; letter-spacing: 2.6px; color: #4a4a4a; }
.page-title { font-size: 25px; font-weight: 900; letter-spacing: -1px; }
.detail-name { font-size: 23px; font-weight: 900; letter-spacing: -1px; }
.mono { font-family: "DejaVu Sans Mono", monospace; font-size: 9px; }
.tile { background: #efefef; border: 2px solid #111; box-shadow: 4px 4px 0 rgba(0,0,0,.32); padding: 7px 12px; }
.tile-key { font-size: 7px; font-weight: 800; letter-spacing: 1.8px; color: #4a4a4a; }
.tile-value { font-family: "DejaVu Sans Mono", monospace; font-size: 15px; font-weight: bold; }
.tile.live { background: #111; }
.tile.live label { color: #fff; }
.chip { border: 2px solid #111; padding: 0 5px; font-size: 7px; font-weight: 800; letter-spacing: 1px; background: #efefef; color: #111; }
.chip.dark { background: #111; color: #fff; }
.chip.alert { background: #fff; color: #c8102e; border-color: #c8102e; }
row:selected label.chip { background: #111; color: #fff; border-color: #fff; }
row:selected label.chip.dark { background: #fff; color: #111; }
row:selected label.chip.alert { color: #ff6b6b; border-color: #ff6b6b; }
.run-meta { font-family: "DejaVu Sans Mono", monospace; font-size: 8px; color: #4a4a4a; }
row:selected .run-meta, row:selected label { color: #fff; }
.runs-side { background: #e4e4e4; border-right: 2px solid #111; }
list.boxed-list > row:selected, list.boxed-list > row:selected:hover { background: #111; color: #fff; }
list.boxed-list > row:hover:not(:selected) { background: #fff; }
.card, frame.card { background: #efefef; border: 2px solid #111; border-radius: 0; box-shadow: 6px 6px 0 rgba(0,0,0,.32); padding: 14px; }
frame.card { padding: 0; }
.checkpoint-row { border-top: 2px solid #111; padding-top: 8px; }
.console { border: 2px solid #111; box-shadow: 6px 6px 0 rgba(0,0,0,.32); background: #111; }
.console textview, .console textview text { background: #111; color: #e6e6e6; font-family: "DejaVu Sans Mono", monospace; font-size: 9px; }
.drop-hover { outline: 3px dashed #111; outline-offset: -6px; }
stackswitcher.cube-tabs button { padding: 3px 14px; }
dialog.floating sheet { border-radius: 0; border: 2px solid #111; box-shadow: 8px 8px 0 rgba(0,0,0,.35); }
'''


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


def smooth_series(points, weight=.9):
    """Exponential moving average that tolerates irregular step spacing."""
    result, average = [], None
    for step, value in points:
        average = value if average is None else weight * average + (1 - weight) * value
        result.append((step, average))
    return result


def sort_runs(runs, mode):
    """Pinned runs first, then the chosen order; ties keep the newest first."""
    order = sorted(runs, key=lambda run: (run.get('created') or 0, run['id']), reverse=True)
    if mode == 1:
        order.reverse()
    elif mode == 2:
        order.sort(key=lambda run: run['name'].casefold())
    elif mode == 3:
        order.sort(key=lambda run: (run['val_loss'] is None, run['val_loss'] or 0))
    elif mode == 4:
        order.sort(key=lambda run: -(run['parameters'] or 0))
    return sorted(order, key=lambda run: not run.get('pinned'))


def format_bytes(value):
    if not finite_number(value) or value < 0:
        return '—'
    for unit in ('bytes', 'KiB', 'MiB', 'GiB'):
        if value < 1024 or unit == 'GiB':
            return f'{value:,.0f} {unit}' if unit == 'bytes' else f'{value:,.1f} {unit}'
        value /= 1024


def plan_estimate(steps, batch, accumulation, corpus_bytes=None, val_fraction=.05, tokens_per_second=None):
    """Describe the token budget, corpus passes and, from past runs, duration."""
    tokens = steps * batch * accumulation * 256
    parts = [f'{tokens:,} training tokens']
    if finite_number(corpus_bytes) and corpus_bytes > 0:
        # Byte-level tokens: roughly one per source byte, less the validation share.
        parts.append(f'about {tokens / max(1, corpus_bytes * (1 - val_fraction)):.2f} passes over the corpus')
    if finite_number(tokens_per_second) and tokens_per_second > 0:
        parts.append(f'about {format_duration(tokens / tokens_per_second)} at earlier speed')
    return ' · '.join(parts)


def log_line_kind(line):
    lower = line.casefold()
    if any(word in lower for word in ('traceback', 'error', 'failed', 'exception')):
        return 'error'
    if any(word in lower for word in ('warning', 'watchdog', 'thermal', 'temperature', 'cooling', 'stop')):
        return 'warning'
    if 'new best' in lower or 'early stop' in lower or '[train] done' in lower:
        return 'success'
    return None


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


def sparkline_points(metrics, limit=48):
    """Validation loss when evaluated, else training loss, thinned for a row sparkline."""
    points = loss_series(metrics, 'val_loss')
    if len(points) < 2:
        points = loss_series(metrics, 'total_loss')
    if len(points) <= limit:
        return points
    stride = len(points) / limit
    return [points[min(len(points) - 1, int(index * stride))] for index in range(limit)] + [points[-1]]


def spark_metrics(path, limit=64_000):
    """Recent loss records for a row sparkline; a shorter tail than the chart reads."""
    result = []
    for line in engine.tail_text(path, limit).splitlines():
        try:
            item = engine.strict_json(line)
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def loss_text(loss):
    """Loss in nats/byte with its bits-per-byte equivalent."""
    if not finite_number(loss):
        return '—'
    return f'{loss:.4f} ({engine.bits_per_byte(loss):.3f} bits/byte)'


def checkpoint_line(item):
    parts = [item['label']]
    if finite_number(item.get('step')) and not item['kind'].startswith('step'):
        parts.append(f'{"≈" if item["kind"] == "best" else ""}step {int(item["step"]):,}')
    if finite_number(item.get('val_loss')):
        parts.append(f'val {item["val_loss"]:.4f} · {engine.bits_per_byte(item["val_loss"]):.3f} bpb')
    parts.append(format_bytes(item['bytes']))
    parts.append(time.strftime('%Y-%m-%d %H:%M', time.localtime(item['modified'])))
    return ' · '.join(parts)


def load_snippet(checkpoint):
    """Python that loads a checkpoint in any notebook or script."""
    return (f"import sys\nsys.path.insert(0, {str(engine.PIPELINE)!r})\n"
            "from tensegrity_lm import TensegrityConfig, TensegrityLM, load_checkpoint\n"
            f"saved = load_checkpoint({str(checkpoint)!r}, 'cpu')\n"
            "model = TensegrityLM(TensegrityConfig(**saved['config']))\n"
            "model.load_state_dict(saved['model_state_dict']); model.eval()\n")


def draw_cube(context, x, y, size, rgb=(.95, .95, .95), line=2):
    """Isometric wireframe cube, the desktop's mark."""
    half, quarter = size / 2, size / 4
    top = [(x + half, y), (x + size, y + quarter), (x + half, y + half), (x, y + quarter)]
    context.set_source_rgb(*rgb); context.set_line_width(line)
    context.move_to(*top[0])
    for point in top[1:]:
        context.line_to(*point)
    context.close_path()
    for px, py in (top[1], top[2], top[3]):
        context.move_to(px, py); context.line_to(px, py + half)
    context.move_to(x, y + quarter + half); context.line_to(x + half, y + size); context.line_to(x + size, y + quarter + half)
    context.stroke()


def label(text='', css=None, selectable=False):
    # Only labels worth copying are selectable: GTK focuses the first selectable
    # label on a page and highlights its whole text.
    widget = Gtk.Label(label=text, xalign=0, wrap=True, selectable=selectable or css == 'cube-hash')
    if css:
        widget.add_css_class(css)
    return widget


def flow(widgets):
    """A row of buttons that wraps instead of overflowing narrow windows."""
    container = Adw.WrapBox(child_spacing=8, line_spacing=8)
    for widget in widgets:
        container.append(widget)
    return container


def box(vertical=True, spacing=10):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def button(text, callback, primary=False):
    widget = Gtk.Button(label=text)
    if primary:
        widget.add_css_class('suggested-action')
    widget.connect('clicked', lambda *_: callback())
    return widget


def run_details(run, disk_bytes=None):
    """Multi-line summary of a run's configuration, outcome and storage."""
    meta = run['meta']
    validation = f'{run["val_loss"]:.4f}' if isinstance(run['val_loss'], (int, float)) else 'not evaluated'
    lines = [run['name'], run['directory'],
             f'{run["parameters"]:,} parameters · validation loss {validation}' if run['parameters'] else f'Validation loss {validation}',
             f'Best: {"saved" if run["best"] else "pending"} · Latest: {"saved" if run["latest"] else "pending"}'
             + (f' · {run["snapshots"]} snapshots' if run.get('snapshots') else '')]
    plan = []
    if meta.get('device') in ('cpu', 'cuda'):
        plan.append('GPU' if meta['device'] == 'cuda' else 'CPU')
    if finite_number(meta.get('batch_size')) and finite_number(meta.get('grad_accum_steps')):
        plan.append(f'batch {meta["batch_size"]} × {meta["grad_accum_steps"]}')
    if finite_number(meta.get('learning_rate')):
        plan.append(f'peak LR {meta["learning_rate"]:g}')
    if meta.get('schedule') in ('cosine', 'wsd'):
        plan.append('WSD schedule' if meta['schedule'] == 'wsd' else 'cosine schedule')
    if finite_number(meta.get('seed')):
        plan.append(f'seed {meta["seed"]}')
    if plan:
        lines.append(' · '.join(plan))
    if isinstance(meta.get('dataset'), str):
        lines.append(f'Corpus: {meta["dataset"]}')
    timing = []
    if run.get('created'):
        timing.append('Created ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(run['created'])))
    if run.get('training_seconds') is not None:
        timing.append(f'trained {format_duration(run["training_seconds"])}')
    if run.get('peak_vram_mib') is not None:
        timing.append(f'peak VRAM {run["peak_vram_mib"]:,.0f} MiB')
    if disk_bytes is not None:
        timing.append(f'{format_bytes(disk_bytes)} on disk')
    if timing:
        lines.append(' · '.join(timing))
    if run.get('stop_reason') and run['stop_reason'] != 'completed':
        lines.append(engine.STOP_REASONS[run['stop_reason']])
    if run['detail']:
        lines.append(run['detail'])
    return '\n'.join(lines)


class TrainerWindow(Adw.ApplicationWindow):
    def __init__(self, application, trainer):
        values = trainer.preferences()
        super().__init__(application=application, title='Whitecube Trainer',
                         default_width=values['window_width'], default_height=values['window_height'])
        self.trainer = trainer
        self.values = values
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
        self.throughput_cache = {}
        self.corpus_bytes = None
        self.chart_hover = None
        self.chart_geometry = None
        self.preview_result = None
        self.disk_request = 0
        self.total_disk = None
        self.applying_preset = False
        self.run_rows = {}
        self.row_order = []
        self.spark_cache = {}
        self.checkpoint_signature = None
        self.checkpoint_items = []
        self.compare_ids = []
        self.compare_losses = []
        self.compare_signature = None
        self.field_probe = {}
        self.last_notebook = None
        self.actions = {}
        self.toast = Adw.ToastOverlay()
        self.set_content(self.toast)
        outer = box(False, 0)
        self.toast.set_child(outer)
        outer.append(self.build_rail())
        layout = box(spacing=0); layout.set_hexpand(True)
        outer.append(layout)
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title='Whitecube Trainer', subtitle='Small models, trained locally')
        header.set_title_widget(self.window_title)
        header.pack_start(button('New run', self.new_run))
        header.pack_end(button('Refresh', lambda: self.refresh(True)))
        header.pack_end(button('Shortcuts', self.show_shortcuts))
        layout.append(header)
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        layout.append(self.stack)
        self.install_actions()
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
        self.stack.connect('notify::visible-child-name', lambda *_: (self.footer_actions(), self.rail_selection()))
        self.footer_actions(); self.rail_selection()
        layout.append(footer)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_margin_start(18); self.progress.set_margin_end(18); self.progress.set_margin_bottom(12)
        layout.append(self.progress)
        controller = Gtk.EventControllerKey()
        controller.connect('key-pressed', self.key_pressed)
        self.add_controller(controller)
        self.connect('close-request', self.remember_size)
        self.connect('close-request', self.closing)
        self.refresh(True)
        self.tick_source = GLib.timeout_add(1000, self.tick)
        if not application.options.no_probe:
            GLib.idle_add(self.check_runtime)

    def build_rail(self):
        rail = box(spacing=6); rail.add_css_class('cube-rail'); rail.set_size_request(168, -1)
        mark = Gtk.DrawingArea(content_width=40, content_height=40, halign=Gtk.Align.START)
        mark.set_draw_func(lambda _, context, *__: draw_cube(context, 2, 2, 36))
        rail.append(mark)
        rail.append(label('WHITECUBE', 'rail-caption'))
        name = label('Trainer', 'rail-name'); name.set_margin_bottom(14); rail.append(name)
        self.rail_buttons = {}
        for page, number, title in PAGES:
            widget = Gtk.Button()
            widget.set_child(Gtk.Label(label=f'{number}   {title}', xalign=0))
            widget.connect('clicked', lambda _, name=page: self.stack.set_visible_child_name(name))
            widget.set_tooltip_text(f'{title} (Ctrl+{number[-1]})')
            self.rail_buttons[page] = widget; rail.append(widget)
        spacer = box(); spacer.set_vexpand(True); rail.append(spacer)
        job = box(spacing=4); job.add_css_class('rail-job')
        self.rail_live = label('● TRAINING', 'live'); self.rail_live.set_selectable(False)
        self.rail_job_name = label('', 'heading'); self.rail_job_name.set_selectable(False)
        self.rail_job_name.set_ellipsize(Pango.EllipsizeMode.END); self.rail_job_name.set_wrap(False)
        self.rail_job_detail = label('', 'dim-label'); self.rail_job_detail.set_selectable(False)
        self.rail_job_progress = Gtk.ProgressBar()
        for widget in (self.rail_live, self.rail_job_name, self.rail_job_detail, self.rail_job_progress):
            job.append(widget)
        click = Gtk.GestureClick(); click.connect('released', lambda *_: self.stack.set_visible_child_name('activity'))
        job.add_controller(click); job.set_tooltip_text('Show training progress')
        self.rail_job = job; job.set_visible(False); rail.append(job)
        version = label(f'v{VERSION} · local only', 'rail-caption'); version.set_selectable(False); rail.append(version)
        return rail

    def rail_selection(self):
        current = self.stack.get_visible_child_name()
        for page, widget in getattr(self, 'rail_buttons', {}).items():
            (widget.add_css_class if page == current else widget.remove_css_class)('selected')
        if hasattr(self, 'log_view') and (self.get_focus() is None or isinstance(self.get_focus(), Gtk.Label)):
            row = self.run_list.get_selected_row()
            if current == 'train':
                self.form['dataset'].grab_focus_without_selecting()
            elif current == 'runs':
                (row or self.run_search).grab_focus()
            else:
                self.log_view.grab_focus()

    def install_actions(self):
        """Window actions shared by the run context menu, the ⋯ menu and shortcuts."""
        for name, callback in (('resume', self.resume), ('preview', lambda: self.show_detail_tab('preview')),
                               ('chat-best', lambda: self.chat('best')), ('chat-latest', lambda: self.chat('latest')),
                               ('export-notebook', self.export_notebook), ('rename', self.rename), ('pin', self.toggle_pin),
                               ('open-folder', self.open_folder), ('copy-path', self.copy_path),
                               ('copy-snippet', self.copy_load_snippet), ('copy-command', self.copy_resume_command),
                               ('export-weights', self.export_weights), ('export-report', self.export_report),
                               ('export-metrics', self.export_metrics), ('use-settings', self.use_run_settings),
                               ('reuse-corpus', self.reuse_prepared_corpus), ('show-progress', self.show_progress),
                               ('trash', self.delete), ('clean-up', self.clean_up_failed)):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda *_, callback=callback: callback())
            self.add_action(action); self.actions[name] = action
        dataset = Gio.SimpleAction.new('use-dataset', GLib.VariantType.new('s'))
        dataset.connect('activate', lambda _, value: self.use_recent_dataset(value.get_string()))
        self.add_action(dataset)

    def run_menu_model(self):
        menu = Gio.Menu()
        for section in ((('Resume run', 'resume'), ('Show progress', 'show-progress'), ('Preview…', 'preview'),
                         ('Chat with Best', 'chat-best'), ('Chat with Latest', 'chat-latest')),
                        (('Export notebook (.ipynb)…', 'export-notebook'), ('Export weights…', 'export-weights'),
                         ('Export report…', 'export-report'), ('Export plotted metrics…', 'export-metrics'),
                         ('Copy load snippet', 'copy-snippet'), ('Copy resume command', 'copy-command')),
                        (('Rename…', 'rename'), ('Pin / unpin', 'pin'), ('Use run settings', 'use-settings'),
                         ('Reuse prepared corpus', 'reuse-corpus'), ('Open folder', 'open-folder'), ('Copy run path', 'copy-path')),
                        (('Move to Trash…', 'trash'),)):
            part = Gio.Menu()
            for title, name in section:
                part.append(title, f'win.{name}')
            menu.append_section(None, part)
        return menu

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

    def spin(self, key, low, high, step=1, digits=0):
        control = Gtk.SpinButton.new_with_range(low, high, step)
        control.set_digits(digits)
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
        page.append(label('NEW RUN', 'eyebrow'))
        page.append(label('Create a small language model', 'page-title'))
        page.append(label('Train on your local text, Markdown, JSONL, or Parquet corpus. Checkpoints appear in Desktop Chat. Models use a 256-byte context and learn prose continuation.', 'dim-label'))
        corpus = box(False, 8)
        corpus.append(self.entry('dataset', '~/train-in'))
        corpus.append(button('Folder…', lambda: self.choose_dataset(True)))
        corpus.append(button('File…', lambda: self.choose_dataset(False)))
        self.inspect_button = button('Inspect', self.inspect_dataset)
        corpus.append(self.inspect_button)
        self.recent_button = Gtk.MenuButton(label='Recent', tooltip_text='Corpora used by earlier runs')
        corpus.append(self.recent_button); self.update_recent_menu()
        self.field(page, 'Corpus', corpus)
        self.dataset_info = label('Choose a corpus file, a folder of shards, or a prepared data folder from an earlier run. You can also drop one onto this page.', 'dim-label', True)
        page.append(self.dataset_info)
        fields = box(False, 8)
        self.field_picker = Gtk.DropDown.new_from_strings(['Automatic'])
        self.field_picker.set_sensitive(False)
        self.field_picker.connect('notify::selected', lambda *_: self.field_chosen())
        self.field_info = label('JSONL and Parquet text fields are listed after Inspect.', 'dim-label', True); self.field_info.set_hexpand(True)
        fields.append(self.field_picker); fields.append(self.field_info)
        self.field(page, 'Text field', fields)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect('drop', self.dropped)
        drop.connect('enter', lambda *_: (self.stack.get_child_by_name('train').add_css_class('drop-hover'), Gdk.DragAction.COPY)[1])
        drop.connect('leave', lambda *_: self.stack.get_child_by_name('train').remove_css_class('drop-hover'))
        self.stack.get_child_by_name('train').add_controller(drop)
        data_options = Gtk.Expander(label='Corpus selection and filtering')
        data_knobs = box(spacing=12)
        self.corpus_selection = data_knobs
        data_knobs.append(self.check('deduplicate', 'Remove exact duplicate documents'))
        data_knobs.append(self.check('shuffle_files', 'Shuffle source files with the seed before applying the corpus cap'))
        for key, title, low, high in [('min_document_bytes', 'Minimum document bytes (0 = off)', 0, 1_000_000_000),
                                      ('max_document_bytes', 'Maximum bytes per document (0 = unlimited)', 0, 1_000_000_000),
                                      ('batch_rows', 'Parquet rows per read batch', 1, 65536)]:
            self.field(data_knobs, title, self.spin(key, low, high))
        data_knobs.append(label('Limits count UTF-8 bytes. Long documents keep a complete-character prefix. Prepared folders reuse the saved selection and splits; their files are copied and verified.', 'dim-label'))
        data_options.set_child(data_knobs); page.append(data_options)
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
        group = box(); group.set_hexpand(True)
        self.plan_preset = Gtk.DropDown.new_from_strings([title for title, _ in PLAN_PRESETS])
        self.plan_preset.connect('notify::selected', lambda *_: self.apply_plan_preset())
        self.field(group, 'Plan', self.plan_preset); plan.append(group)
        for key, title, low, high in [('steps', 'Total optimizer steps', 1, 10_000_000), ('max_tokens', 'Corpus token cap', 6000, 10_000_000_000)]:
            group = box(); group.set_hexpand(True)
            self.field(group, title, self.spin(key, low, high)); plan.append(group)
        self.form['steps'].connect('value-changed', lambda *_: self.steps_changed())
        page.append(plan)
        self.plan_label = label('', 'dim-label'); page.append(self.plan_label)
        self.device_picker = Gtk.DropDown.new_from_strings(['NVIDIA GPU (CUDA)', 'CPU'])
        self.device_picker.set_selected(0 if self.values.get('device') == 'cuda' else 1)
        self.device_picker.connect('notify::selected', lambda *_: self.queue_save())
        self.field(page, 'Train on', self.device_picker)
        runtime = box(False, 8)
        runtime.append(self.entry('python'))
        self.check_button = button('Check runtime', self.check_runtime)
        runtime.append(self.check_button)
        self.field(page, 'Python training environment', runtime)
        self.runtime_label = label('The window uses system GTK4; training uses the Python environment above.', 'dim-label', True)
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
        heat.append(self.check('thermal_pause', 'Pause when within 5 °C of a stop limit, and continue once 10 °C below it'))
        heat.append(label('Cooling pauses reduce sustained CPU/GPU work. Priority affects CPU scheduling and disk access; it does not limit GPU power. Stop also works during a break.', 'dim-label'))
        for key, title, low, high in [('max_temp', 'GPU stop temperature (°C)', 50, 90), ('max_cpu_temp', 'CPU stop temperature (°C)', 50, 95)]:
            self.field(heat, title, self.spin(key, low, high))
        cooling.set_child(heat); page.append(cooling)
        tuning = Gtk.Expander(label='Training plan: learning rate, schedule and evaluation')
        knobs = box(spacing=12)
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        entries = [('learning_rate', 'Peak learning rate', 1e-6, .1, 1e-5, 6), ('min_lr_ratio', 'Final rate (fraction of peak)', 0, 1, .01, 2),
                   ('warmup_steps', 'Warmup steps (0 = automatic)', 0, 1_000_000, 1, 0), ('weight_decay', 'Weight decay', 0, 1, .01, 2),
                   ('grad_clip', 'Gradient clip (0 = off)', 0, 100, .1, 2), ('eval_interval', 'Validate every (steps)', 1, 10_000_000, 1, 0),
                   ('seed', 'Seed', 0, 2**31 - 1, 1, 0), ('val_fraction', 'Validation share', .01, .5, .01, 2)]
        for index, (key, title, low, high, step, digits) in enumerate(entries):
            cell = box(spacing=5); cell.set_hexpand(True)
            self.field(cell, title, self.spin(key, low, high, step, digits))
            grid.attach(cell, index % 2, index // 2, 1, 1)
        knobs.append(grid)
        self.schedule_picker = Gtk.DropDown.new_from_strings(['Cosine decay', 'Warmup, hold, then decay (WSD)'])
        self.schedule_picker.set_selected(engine.CHOICES['schedule'].index(self.values['schedule']))
        self.schedule_picker.connect('notify::selected', lambda *_: self.queue_save())
        self.field(knobs, 'Learning-rate schedule', self.schedule_picker,
                   'These settings belong to a new run. Resume restores the saved optimizer and schedule.')
        tuning.set_child(knobs); page.append(tuning)
        controls = Gtk.Expander(label='Run controls: early stop, time limit and snapshots')
        limits = box(spacing=12)
        for key, title, low, high, hint in [
                ('early_stop_patience', 'Early stop after evaluations without improvement (0 = off)', 0, 1000, None),
                ('max_minutes', 'Time limit in minutes (0 = none)', 0, 525_600, 'Saves a checkpoint and stops; resume continues later.'),
                ('keep_snapshots', 'Keep step-numbered checkpoint snapshots (0 = off)', 0, 100, 'Each snapshot is a full checkpoint copy.')]:
            self.field(limits, title, self.spin(key, low, high), hint)
        controls.set_child(limits); page.append(controls)
        advanced = Gtk.Expander(label='Advanced training settings')
        options = box(spacing=12)
        for key, title, low, high in [('batch_size', 'Batch size', 1, 256), ('grad_accum_steps', 'Gradient accumulation', 1, 256),
                                     ('cpu_threads', 'CPU threads', 1, 128), ('schedule_steps', 'Learning rate decay horizon (0 preserves the saved plan)', 0, 10_000_000)]:
            self.field(options, title, self.spin(key, low, high))
        self.field(options, 'Text field override (optional)', self.entry('column', 'text or document.html'),
                   'Leave empty for automatic selection (text, content, story, body, document.html in each file). The Text field picker fills this in.')
        self.form['column'].connect('changed', lambda *_: self.show_field_summary())
        advanced.set_child(options); page.append(advanced)
        self.batch_summary = label('', 'dim-label'); page.append(self.batch_summary)
        for key in ('batch_size', 'grad_accum_steps', 'val_fraction'):
            self.form[key].connect('value-changed', lambda *_: self.update_batch_summary())
        self.device_picker.connect('notify::selected', lambda *_: self.update_batch_summary())
        self.size_picker.connect('notify::selected', lambda *_: self.update_batch_summary())
        self.update_batch_summary()
        actions = box(False, 10)
        self.train_actions = actions
        actions.append(button('Reset settings', self.reset_settings))
        self.prepare_button = button('Prepare corpus only', lambda: self.start(True))
        self.start_button = button('Start training', self.start, True)
        actions.append(self.prepare_button); actions.append(self.start_button); page.append(actions)
        page.append(label(f'Runs are saved in {self.trainer.runs_dir}. Stop saves a checkpoint after the current step. Training continues when this window closes.', 'dim-label'))

    def footer_actions(self):
        visible = self.stack.get_visible_child_name() == 'train'
        self.start_button.set_visible(visible); self.prepare_button.set_visible(visible)

    def build_runs(self):
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True, shrink_start_child=False,
                          resize_start_child=False, position=self.values['runs_pane'])
        self.runs_paned = paned
        self.stack.add_titled(paned, 'runs', 'Runs & checkpoints')
        side = box(spacing=10); side.add_css_class('runs-side'); side.set_size_request(260, -1)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(side, setter)(0)
        inner = box(spacing=10)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(inner, setter)(14)
        inner.set_vexpand(True); side.append(inner)
        inner.append(label('TRAINING RUNS', 'eyebrow'))
        self.run_search = Gtk.SearchEntry(placeholder_text='Search name, size, phase or ID', hexpand=True)
        self.run_search.connect('search-changed', lambda *_: self.filter_runs())
        inner.append(self.run_search)
        filters = box(False, 6)
        self.run_filter = Gtk.DropDown.new_from_strings(['All runs', 'With checkpoints', 'Prepared only', 'Needs attention', 'Pinned'])
        self.run_filter.connect('notify::selected', lambda *_: self.filter_runs())
        self.run_sort = Gtk.DropDown.new_from_strings(list(RUN_SORTS))
        self.run_sort.set_selected(self.values['run_sort'])
        self.run_sort.connect('notify::selected', lambda *_: (self.refresh(True), self.queue_save()))
        self.run_filter.set_hexpand(True); self.run_sort.set_hexpand(True)
        filters.append(self.run_filter); filters.append(self.run_sort); inner.append(filters)
        self.run_count = label('', 'dim-label'); inner.append(self.run_count)
        self.run_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False)
        self.run_list.add_css_class('boxed-list')
        self.run_list.update_property([Gtk.AccessibleProperty.LABEL], ['Training runs'])
        self.run_list.connect('row-selected', self.run_selected)
        self.run_list.connect('row-activated', lambda *_: self.show_progress())
        self.run_menu = Gtk.PopoverMenu.new_from_model(self.run_menu_model())
        self.run_menu.set_parent(self.run_list); self.run_menu.set_has_arrow(False)
        secondary = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        secondary.connect('pressed', lambda _, __, x, y: self.open_run_menu(x, y))
        self.run_list.add_controller(secondary)
        hold = Gtk.GestureLongPress(); hold.connect('pressed', lambda _, x, y: self.open_run_menu(x, y))
        self.run_list.add_controller(hold)
        keys = Gtk.EventControllerKey(); keys.connect('key-pressed', self.run_list_key)
        self.run_list.add_controller(keys)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.run_list); inner.append(scroller)
        self.cleanup_button = button('Clean up failed runs…', self.clean_up_failed)
        self.cleanup_button.set_tooltip_text('Move failed runs that saved no data or weights to Trash')
        inner.append(self.cleanup_button)
        paned.set_start_child(side)

        detail_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        page = box(spacing=14)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(page, setter)(22)
        detail_scroll.set_child(page); paned.set_end_child(detail_scroll)
        self.detail_box = page
        heading = box(False, 8)
        heading.append(label('RUN', 'eyebrow'))
        self.detail_chips = box(False, 6); heading.append(self.detail_chips)
        page.append(heading)
        self.detail_title = label('Choose a run', 'detail-name'); page.append(self.detail_title)
        self.detail_path = label('', 'mono'); page.append(self.detail_path)
        self.detail_tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=10, row_spacing=10,
                                        min_children_per_line=3, max_children_per_line=3, homogeneous=True)
        page.append(self.detail_tiles)
        self.resume_button = button('Resume run', self.resume, True)
        self.best_button = button('Chat with Best', lambda: self.chat('best'))
        self.latest_button = button('Chat with Latest', lambda: self.chat('latest'))
        self.notebook_button = button('Export notebook…', self.export_notebook, True)
        self.notebook_button.set_tooltip_text('Write a Jupyter notebook (.ipynb) for this checkpoint (Ctrl+Shift+E)')
        self.pin_button = button('Pin', self.toggle_pin)
        self.rename_button = button('Rename', self.rename)
        more = Gtk.MenuButton(label='More', tooltip_text='All run actions (also on right-click in the list)')
        more.set_menu_model(self.run_menu_model())
        page.append(flow((self.resume_button, self.notebook_button, self.best_button, self.latest_button, self.pin_button, self.rename_button, more)))
        self.detail_tabs = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        tabs = Gtk.StackSwitcher(stack=self.detail_tabs, halign=Gtk.Align.START); tabs.add_css_class('cube-tabs')
        page.append(tabs); page.append(self.detail_tabs)

        overview = box(spacing=12)
        self.detail_tabs.add_titled(overview, 'overview', 'Overview')
        self.run_detail = label('Choose a run to inspect its checkpoints.', 'dim-label', True)
        overview.append(self.run_detail)
        resume = box(spacing=8); resume.add_css_class('card')
        self.resume_steps = Gtk.SpinButton.new_with_range(1, 10_000_000, 1)
        self.resume_steps.set_value(2000)
        self.resume_steps.connect('value-changed', lambda *_: self.update_resume_action())
        self.field(resume, 'Resume to this total step count', self.resume_steps, 'The saved model size, prepared corpus, batch and accumulation are reused.')
        self.resume_hint = label('', 'dim-label'); resume.append(self.resume_hint)
        overview.append(resume)
        checkpoints = box(spacing=8); checkpoints.add_css_class('card')
        checkpoints.append(label('Checkpoints', 'heading'))
        checkpoints.append(label('Preview, export or open any saved checkpoint, including step snapshots.', 'dim-label'))
        self.checkpoint_box = box(spacing=8); checkpoints.append(self.checkpoint_box)
        overview.append(checkpoints)
        self.duplicate_button = button('Use run settings', self.use_run_settings)
        self.reuse_corpus_button = button('Reuse prepared corpus', self.reuse_prepared_corpus)
        self.open_button = button('Open folder', self.open_folder)
        self.copy_button = button('Copy run path', self.copy_path)
        self.delete_button = button('Delete…', self.delete)
        overview.append(flow((self.duplicate_button, self.reuse_corpus_button, self.open_button, self.copy_button, self.delete_button)))
        overview.append(label('Notes', 'heading'))
        self.notes_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.notes_view.set_size_request(-1, 70)
        self.notes_view.update_property([Gtk.AccessibleProperty.LABEL], ['Run notes'])
        notes_frame = Gtk.Frame(); notes_frame.set_child(self.notes_view); overview.append(notes_frame)
        self.notes_view.get_buffer().connect('changed', lambda *_: self.notes_edited())
        notes_actions = box(False, 8)
        self.notes_button = button('Save notes', self.save_notes)
        self.notes_status = label('', 'dim-label')
        notes_actions.append(self.notes_button); notes_actions.append(self.notes_status); overview.append(notes_actions)
        self.notes_id = None

        preview = box(spacing=12)
        self.detail_tabs.add_titled(preview, 'preview', 'Preview')
        preview.append(label('Generate a short continuation on the CPU. Checkpoint previews from the Overview list appear here too.', 'dim-label'))
        sample_row = box(False, 8)
        self.sample_prompt = Gtk.Entry(placeholder_text='Short prompt…', text='Once upon a time', hexpand=True)
        self.sample_prompt.connect('activate', lambda *_: self.sample() if self.sample_button.get_sensitive() else None)
        self.sample_kind = Gtk.DropDown.new_from_strings(['Best', 'Latest', 'Both'])
        self.sample_kind.connect('notify::selected', lambda *_: self.refresh())
        self.sample_button = button('Preview', self.sample, True)
        sample_row.append(self.sample_prompt); sample_row.append(self.sample_kind); sample_row.append(self.sample_button); preview.append(sample_row)
        knobs = Gtk.Grid(column_spacing=14, row_spacing=10)
        for index, (key, title, low, high, step, digits) in enumerate([
                ('preview_tokens', 'Length (bytes)', 1, 4096, 1, 0), ('preview_temperature', 'Temperature (0 = greedy)', 0, 2, .05, 2),
                ('preview_top_k', 'Top-k', 1, 259, 1, 0), ('preview_top_p', 'Top-p (1 = off)', .05, 1, .05, 2),
                ('preview_seed', 'Seed', 0, 2**31 - 1, 1, 0)]):
            cell = box(spacing=5); cell.set_hexpand(True)
            self.field(cell, title, self.spin(key, low, high, step, digits))
            knobs.attach(cell, index % 3, index // 3, 1, 1)
        preview.append(knobs)
        output = box(spacing=0); output.add_css_class('console')
        self.sample_output = label('', 'cube-hash')
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(self.sample_output, setter)(12)
        self.sample_output.set_size_request(-1, 90); output.append(self.sample_output); preview.append(output)
        self.sample_stats = label('', 'dim-label', True); preview.append(self.sample_stats)
        preview_actions = box(False, 8)
        self.copy_sample_button = button('Copy preview', self.copy_preview)
        self.save_sample_button = button('Save to run', self.save_preview)
        for widget in (self.copy_sample_button, self.save_sample_button):
            widget.set_sensitive(False); preview_actions.append(widget)
        preview.append(preview_actions)
        self.preview_history = label('', 'dim-label', True); preview.append(self.preview_history)

        exports = box(spacing=12)
        self.detail_tabs.add_titled(exports, 'export', 'Export')
        notebook = box(spacing=8); notebook.add_css_class('card')
        notebook.append(label('Jupyter notebook', 'heading'))
        notebook.append(label('A runnable .ipynb that loads the checkpoint, plots the curves, generates text, shows next-byte '
                              'probabilities, re-measures validation loss, continues training in a copy and exports safetensors. '
                              'The default folder is self-contained: model code, checkpoint and logs travel with it.', 'dim-label'))
        self.notebook_export_button = button('Export notebook…', self.export_notebook, True)
        self.jupyter_button = button('Open last export in Jupyter', lambda: self.open_in_jupyter(self.last_notebook))
        self.jupyter_button.set_sensitive(False)
        self.snippet_button = button('Copy load snippet', self.copy_load_snippet)
        notebook.append(flow((self.notebook_export_button, self.jupyter_button, self.snippet_button)))
        self.notebook_status = label('', 'dim-label', True); notebook.append(self.notebook_status)
        exports.append(notebook)
        weights = box(spacing=8); weights.add_css_class('card')
        weights.append(label('Weights and records', 'heading'))
        weights.append(label('Safetensors with a model card, the JSON report (configuration, provenance, notes, previews), plotted metrics as CSV, or the equivalent command line.', 'dim-label'))
        self.weights_button = button('Export weights…', self.export_weights)
        self.report_button = button('Export report…', self.export_report)
        self.metrics_button = button('Export plotted metrics…', self.export_metrics)
        self.command_button = button('Copy resume command', self.copy_resume_command)
        weights.append(flow((self.weights_button, self.report_button, self.metrics_button, self.command_button)))
        exports.append(weights)

    def build_activity(self):
        page = self.page('activity', 'Progress & logs')
        page.append(label('PROGRESS', 'eyebrow'))
        page.append(label('Training progress', 'page-title'))
        self.progress_tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=10, row_spacing=10,
                                          min_children_per_line=4, max_children_per_line=4, homogeneous=True)
        page.append(self.progress_tiles)
        self.metrics_label = label('Training and validation losses appear here.', 'dim-label', True); page.append(self.metrics_label)
        self.chart = Gtk.DrawingArea(content_height=200, hexpand=True)
        self.chart.set_draw_func(self.draw_loss)
        self.chart.set_tooltip_text('Training loss (black) and validation loss (red), against optimizer steps. Point at the graph to read values.')
        motion = Gtk.EventControllerMotion()
        motion.connect('motion', lambda _, x, y: self.chart_pointer(x))
        motion.connect('leave', lambda *_: self.chart_pointer(None))
        self.chart.add_controller(motion)
        frame = Gtk.Frame(); frame.add_css_class('card'); frame.set_child(self.chart); page.append(frame)
        page.append(label('Black: training loss · Red: validation loss · Red square: best validation · Grey dashes: learning rate or compared run · Lower is better', 'dim-label'))
        self.chart_readout = label('', 'dim-label'); self.chart_readout.set_visible(False); page.append(self.chart_readout)
        chart_options = box(False, 12)
        self.smooth_check = Gtk.CheckButton(label='Smooth training loss')
        self.lr_check = Gtk.CheckButton(label='Show learning rate')
        for widget in (self.smooth_check, self.lr_check):
            widget.connect('toggled', lambda *_: self.chart.queue_draw()); chart_options.append(widget)
        chart_options.append(label('Compare with', 'heading'))
        self.compare_picker = Gtk.DropDown.new_from_strings(['No other run'])
        self.compare_picker.connect('notify::selected', lambda *_: self.compare_changed())
        chart_options.append(self.compare_picker)
        page.append(chart_options)
        controls = box(False, 8)
        controls.append(button('Copy log', self.copy_log))
        self.export_log_button = button('Export displayed log…', self.export_log); controls.append(self.export_log_button)
        self.follow = Gtk.CheckButton(label='Follow latest output', active=True)
        self.follow.connect('toggled', lambda *_: self.scroll_to_end() if self.follow.get_active() else None)
        controls.append(self.follow); page.append(controls)
        search = box(False, 8)
        self.log_search = Gtk.SearchEntry(placeholder_text='Find in displayed log', hexpand=True)
        self.log_search.connect('activate', lambda *_: self.find_log())
        self.log_search.connect('search-changed', lambda *_: (self.show_log(force=True) if self.matching_only.get_active() else None, self.find_log(restart=True)))
        self.matching_only = Gtk.CheckButton(label='Only matching lines')
        self.matching_only.connect('toggled', lambda *_: self.show_log(force=True))
        search.append(self.log_search); search.append(button('Find next', self.find_log)); search.append(self.matching_only); page.append(search)
        self.log_search_status = label('', 'dim-label'); page.append(self.log_search_status)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        for setter, value in (('set_left_margin', 12), ('set_right_margin', 12), ('set_top_margin', 10), ('set_bottom_margin', 10)):
            getattr(self.log_view, setter)(value)
        self.log_scroll = Gtk.ScrolledWindow(hexpand=True, min_content_height=260, vexpand=True)
        self.log_scroll.add_css_class('console')
        self.log_scroll.set_child(self.log_view); page.append(self.log_scroll)
        buffer = self.log_view.get_buffer()
        buffer.create_tag('error', foreground='#ff5c5c', weight=700)
        buffer.create_tag('warning', foreground='#ffb347')
        buffer.create_tag('success', foreground='#ffffff', weight=700)

    def form_values(self):
        values = {}
        for key, widget in self.form.items():
            if isinstance(widget, Gtk.SpinButton):
                widget.update()
                values[key] = round(widget.get_value(), widget.get_digits()) if widget.get_digits() else int(widget.get_value())
            elif isinstance(widget, Gtk.CheckButton):
                values[key] = widget.get_active()
            else:
                values[key] = widget.get_text()
        values.update(model_size=engine.DESKTOP_SIZES[self.size_picker.get_selected()], device='cuda' if self.device_picker.get_selected() == 0 else 'cpu',
                      schedule=engine.CHOICES['schedule'][self.schedule_picker.get_selected()],
                      run_sort=self.run_sort.get_selected() if hasattr(self, 'run_sort') else self.values['run_sort'])
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

    def remember_size(self, *_):
        width, height = self.get_width(), self.get_height()
        try:
            self.trainer.save_preferences({'runs_pane': max(240, min(900, self.runs_paned.get_position()))})
        except (OSError, ValueError, AttributeError) as error:
            print(f'Unable to save the run list width: {error}', file=sys.stderr)
        if width >= 600 and height >= 500 and not self.is_maximized():
            try:
                self.trainer.save_preferences({'window_width': min(width, 8192), 'window_height': min(height, 8192)})
            except (OSError, ValueError) as error:
                print(f'Unable to save the window size: {error}', file=sys.stderr)
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
                details = [f'PyTorch {info["torch"]}', info['gpu'] or 'CPU available']
                if info.get('memory'):
                    free, total = info['memory']
                    details.append(f'GPU memory {format_bytes(free)} free of {format_bytes(total)}')
                if info.get('cuda'):
                    details.append('bf16 supported' if info.get('bf16') else 'fp16 mixed precision')
                details.append(f'Parquet {"available" if info["parquet"] else "needs pyarrow"}')
                if info.get('training_error'):
                    hint = engine.repair_hint(info['training_error'], executable)
                    details.append(f'\nTraining check failed: {info["training_error"]}. This environment cannot run an optimizer step'
                                   + (f'; repair it with: {hint}' if hint else '.'))
                    self.notify('The training environment fails an optimizer step. See Python training environment.')
                else:
                    details.append('optimizer step OK' if 'training_error' in info else '')
                self.runtime_label.set_text(' · '.join(part for part in details if part).replace(' · \n', '\n'))
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
        self.corpus_bytes = None
        if hasattr(self, 'corpus_selection'):
            self.corpus_selection.set_sensitive(True)
        if 'max_tokens' in self.form:
            self.form['max_tokens'].set_sensitive(True)
        if hasattr(self, 'plan_label'):
            self.update_batch_summary()
        if hasattr(self, 'dataset_info'):
            self.dataset_info.set_text('Corpus changed. Inspect it to check supported files and size.')
            self.inspect_button.set_sensitive(True)
        if hasattr(self, 'field_picker'):
            self.field_probe = {}; self.field_names = []
            self.setting_field = True
            self.field_picker.set_model(Gtk.StringList.new(['Automatic'])); self.field_picker.set_sensitive(False)
            self.setting_field = False
            self.field_info.set_text('JSONL and Parquet text fields are listed after Inspect.')

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
            if error:
                self.dataset_info.set_text(str(error)); return False
            if info.get('prepared'):
                corpus = info['corpus']
                self.corpus_selection.set_sensitive(bool(info['errors']))
                self.form['max_tokens'].set_sensitive(bool(info['errors']))
                self.dataset_info.set_text('; '.join(info['errors']) if info['errors'] else
                    f'Prepared corpus · {corpus.get("train_tokens", 0):,} training + {corpus.get("val_tokens", 0):,} validation tokens · '
                    f'{format_bytes(info["bytes"])}. Saved selection and splits will be reused and verified.')
                self.corpus_bytes = corpus.get('total_tokens')
                self.update_batch_summary()
                return False
            formats = ', '.join(f'{count} {suffix.lstrip(".").upper()}' for suffix, count in sorted(info['formats'].items()))
            largest = f' · largest {info["largest"][0]} ({format_bytes(info["largest"][1])})' if info.get('largest') else ''
            self.dataset_info.set_text(f'{len(info["files"])} supported files ({formats or "none"}) · {format_bytes(info["bytes"])}{largest} · '
                                       f'{len(info["empty_files"])} empty files. Full schema checks run before training.'
                                       + (' Parquet files are compressed, so pass estimates are approximate.' if '.parquet' in info['formats'] else ''))
            self.corpus_bytes = info['bytes'] if info['files'] else None
            self.update_batch_summary()
            if hasattr(self, 'probe_corpus_fields'):
                self.probe_corpus_fields(info, request)
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
        if not hasattr(self, 'batch_summary') or not hasattr(self, 'schedule_picker'):
            return
        batch = int(self.form['batch_size'].get_value())
        accumulation = int(self.form['grad_accum_steps'].get_value())
        self.batch_summary.set_text(f'Effective batch: {batch * accumulation:,} sequences · {batch * accumulation * 256:,} byte tokens per optimizer step. Gradient accumulation increases this without enlarging each GPU batch.')
        size = engine.DESKTOP_SIZES[self.size_picker.get_selected()]
        device = 'cuda' if self.device_picker.get_selected() == 0 else 'cpu'
        if (size, device) not in self.throughput_cache:
            try:
                self.throughput_cache[size, device] = self.trainer.throughput(size, device)
            except OSError:
                self.throughput_cache[size, device] = None
        self.plan_label.set_text('Plan: ' + plan_estimate(int(self.form['steps'].get_value()), batch, accumulation,
                                                          self.corpus_bytes, self.form['val_fraction'].get_value(),
                                                          self.throughput_cache[size, device]))

    def apply_plan_preset(self):
        steps = PLAN_PRESETS[self.plan_preset.get_selected()][1]
        if steps:
            self.applying_preset = True
            self.form['steps'].set_value(steps)
            self.applying_preset = False

    def steps_changed(self):
        if not self.applying_preset and hasattr(self, 'plan_preset'):
            current = int(self.form['steps'].get_value())
            match = next((index for index, (_, steps) in enumerate(PLAN_PRESETS) if steps == current), 0)
            if match != self.plan_preset.get_selected():
                self.plan_preset.set_selected(match)
        self.update_batch_summary()

    def reset_settings(self):
        defaults = engine.Trainer.defaults()
        for key, widget in self.form.items():
            if key not in defaults:
                continue
            if isinstance(widget, Gtk.SpinButton):
                widget.set_value(defaults[key])
            elif isinstance(widget, Gtk.CheckButton):
                widget.set_active(defaults[key])
            elif key == 'column':
                widget.set_text(defaults[key])
        self.size_picker.set_selected(engine.DESKTOP_SIZES.index(defaults['model_size']))
        self.schedule_picker.set_selected(engine.CHOICES['schedule'].index(defaults['schedule']))
        if not self.runtime_info or self.runtime_info.get('cuda'):
            self.device_picker.set_selected(0)
        self.queue_save(); self.notify('Training settings reset. Corpus, Python and run name are unchanged.')

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
                                       or kind == 3 and run['phase'] in ('failed', 'interrupted', 'incomplete')
                                       or kind == 4 and run['pinned'])
                row.set_visible(bool(matches)); count += bool(matches)
            row = row.get_next_sibling()
        disk = f' · {format_bytes(self.total_disk)} on disk' if self.total_disk is not None else ''
        self.run_count.set_text(f'{count} of {len(self.runs)} runs{disk}' + (' · No matching runs. Clear the search or change the filter.' if self.runs and not count else ''))

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
        if run['meta'].get('schedule') in engine.CHOICES['schedule']:
            self.schedule_picker.set_selected(engine.CHOICES['schedule'].index(run['meta']['schedule']))
        if run['meta'].get('warmup_steps') is None and 'warmup_steps' in run['meta']:
            self.form['warmup_steps'].set_value(0)
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

    def reuse_prepared_corpus(self):
        run = self.current_run()
        if not run or not run['prepared']:
            return
        self.use_run_settings()
        self.form['dataset'].set_text(str(Path(run['directory']) / 'data'))
        self.inspect_dataset()
        self.notify('Prepared corpus selected for a new run. Saved splits will be copied and verified.')

    def start(self, prepare_only=False):
        values = self.form_values()
        if not prepare_only and self.cuda_missing(values):
            return
        if not prepare_only and self.runtime_info and self.runtime_info.get('training_error'):
            message = 'The checked Python environment fails an optimizer step; training would stop immediately. Repair it (see the runtime check) or choose another environment.'
            self.notify(message); self.status.set_text(message); return
        if self.field_probe:
            _, __, failing = engine.field_summary(self.field_probe, values.get('column', '').strip())
            if failing:
                message = f'{len(failing)} corpus files have no usable text field ({", ".join(failing[:2])}). Choose a Text field on the Train page.'
                self.notify(message); self.status.set_text(message); return
        self.launch(values, prepare_only=prepare_only)

    def cuda_missing(self, values):
        if values['device'] == 'cuda' and self.runtime_info and not self.runtime_info.get('cuda'):
            message = 'The checked Python environment has no CUDA GPU. Choose CPU on the Train page, or select a CUDA-enabled environment.'
            self.notify(message); self.status.set_text(message)
            return True
        return False

    def resume(self):
        self.resume_steps.update()
        self.update_resume_action()
        if not self.resume_button.get_sensitive():
            self.notify(self.resume_hint.get_text()); return
        values = self.form_values(); values['steps'] = int(self.resume_steps.get_value())
        if not self.cuda_missing(values):
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
                self.stack.set_visible_child_name('activity'); self.refresh(True); self.update_recent_menu()
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
        # The one-second refresh keeps self.runs current; avoid rescanning every run folder per click.
        cached = next((run for run in self.runs if run['id'] == self.selected_id), None)
        return cached or next((run for run in self.trainer.list_runs() if run['id'] == self.selected_id), None)

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
            self.clear_preview()
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
                self.desktop_notification(completed)
        if self.selected_id and not any(run['id'] == self.selected_id for run in runs):
            self.selected_id = None; self.last_log = None; self.metrics_signature = None
            self.preview_request += 1; self.clear_preview()
        if not self.selected_id and runs:
            self.selected_id = runs[0]['id']
        if self.displayed_id != self.selected_id:
            self.displayed_id = self.selected_id
            self.preview_request += 1; self.clear_preview()
            self.last_log = None; self.metrics_signature = None
            self.load_notes(next((run for run in runs if run['id'] == self.selected_id), None))
            self.show_preview_history(); self.measure_disk()
        if force:
            self.throughput_cache.clear(); self.measure_total_disk()
        self.update_run_list(runs, force)
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
        self.pin_button.set_sensitive(bool(run and run['meta']))
        self.pin_button.set_label('Unpin' if run and run['pinned'] else 'Pin')
        self.command_button.set_sensitive(bool(run and run['prepared'] and run['meta']))
        self.reuse_corpus_button.set_sensitive(bool(run and run['prepared'] and run['meta']))
        self.weights_button.set_sensitive(bool(run and (run['best'] or run['latest'])))
        kinds = (('best',), ('latest',), ('best', 'latest'))[self.sample_kind.get_selected()]
        self.sample_button.set_sensitive(bool(run and all(run[kind] for kind in kinds) and not busy and not self.sampling))
        self.notebook_button.set_sensitive(bool(run and (run['best'] or run['latest'] or run['snapshots'])))
        self.notebook_export_button.set_sensitive(self.notebook_button.get_sensitive())
        self.snippet_button.set_sensitive(bool(run and (run['best'] or run['latest'])))
        self.cleanup_button.set_visible(any(item['phase'] in ('failed', 'interrupted', 'incomplete') and not item['prepared']
                                            and not item['best'] and not item['latest'] for item in runs))
        self.sync_actions(run, busy)
        self.show_detail_header(run)
        self.update_checkpoints(run)
        self.update_compare_options(runs)
        self.update_rail_job(active, runs)
        if run:
            self.run_detail.set_text(run_details(run, getattr(self, 'run_disk', {}).get(run['id'])))
            validation = f'{run["val_loss"]:.4f}' if isinstance(run['val_loss'], (int, float)) else 'not evaluated'
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
            running = bool(run['job'] and run['job'].get('running'))
            self.set_tiles(self.progress_tiles, [
                ('STEP', f'{run["completed_steps"]:,}', running), ('TRAINING LOSS', f'{loss_value:.4f}' if finite_number(loss_value) else '—', False),
                ('BEST VALIDATION', f'{run["val_loss"]:.4f}' if finite_number(run['val_loss']) else '—', False),
                ('BITS / BYTE', f'{engine.bits_per_byte(run["val_loss"]):.3f}' if finite_number(run['val_loss']) else '—', False),
                ('TOKENS / S', f'{progress["tokens_per_second"]:,.0f}' if finite_number(progress.get('tokens_per_second')) else '—', False),
                ('REMAINING', format_duration(progress.get('eta_seconds')) if running else '—', False),
                ('GPU', f'{telemetry["temperature_c"]} °C' if telemetry.get('temperature_c') is not None else '—', False),
                ('CPU', f'{telemetry["cpu_temperature_c"]} °C' if telemetry.get('cpu_temperature_c') is not None else '—', False)])
            job = run['job']
            logfile = job['log'] if job else str(Path(run['directory']) / 'console.log')
            total = run['steps']
            self.progress.set_fraction(min(1, run['completed_steps'] / total) if total else 0)
            self.progress.set_text(f'{run["completed_steps"]:,} / {total:,} steps · {run["phase"]}')
            if not busy:
                self.status.set_text(f'{run["phase"].capitalize()} · {run["name"]}')
        else:
            self.run_detail.set_text('Choose a run to inspect its checkpoints.')
            self.set_tiles(self.progress_tiles, [])
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
            self.show_log()

    # Run list: rows are created once per run and updated in place, so a
    # running job's per-second refresh never destroys the row being clicked.
    def update_run_list(self, runs, force=False):
        by_id = {run['id']: run for run in runs}
        order = [run['id'] for run in sort_runs(runs, self.run_sort.get_selected())]
        if order != self.row_order or (force and not runs):
            self.rebuilding = True
            # A removed row keeps its selected flag while the list forgets it; clear first so reselection works.
            self.run_list.unselect_all()
            child = self.run_list.get_first_child()
            while child:
                following = child.get_next_sibling()
                if isinstance(child, Gtk.ListBoxRow):  # the context-menu popover is a child too
                    self.run_list.remove(child)
                child = following
            self.run_rows = {run_id: self.run_rows.get(run_id) or self.make_run_row(run_id) for run_id in order}
            for run_id in order:
                self.run_list.append(self.run_rows[run_id])
            if not runs:
                row = Gtk.ListBoxRow(selectable=False, activatable=False)
                row.set_child(label('No runs yet. Create one on the Train page.', 'dim-label')); self.run_list.append(row)
            self.row_order = order
            self.rebuilding = False
        for run_id in order:
            self.update_run_row(self.run_rows[run_id], by_id[run_id], force)
        selected = self.run_rows.get(self.selected_id)
        if selected is not None and self.run_list.get_selected_row() is not selected:
            self.rebuilding = True; self.run_list.select_row(selected); self.rebuilding = False
        self.filter_runs()

    def make_run_row(self, run_id):
        row = Gtk.ListBoxRow(); row.run_id = run_id; row.run_record = None; row.signature = None
        content = box(False, 10)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(content, setter)(8)
        text = box(spacing=4); text.set_hexpand(True)
        # Plain (non-selectable) labels: selectable text inside a row swallows the click.
        row.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END); row.title.add_css_class('heading')
        row.chips = box(False, 4)
        row.meta = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END); row.meta.add_css_class('run-meta')
        text.append(row.title); text.append(row.chips); text.append(row.meta)
        row.spark = Gtk.DrawingArea(content_width=58, content_height=34, valign=Gtk.Align.CENTER)
        row.spark.set_draw_func(lambda area, context, width, height: self.draw_spark(row, context, width, height))
        content.append(text); content.append(row.spark)
        row.set_child(content)
        return row

    def update_run_row(self, row, run, force=False):
        log = Path(run['directory']) / 'train_log.jsonl'
        try:
            stat = log.stat(); log_signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            log_signature = None
        cached = self.spark_cache.get(run['id'])
        if force or not cached or cached[0] != log_signature:
            self.spark_cache[run['id']] = (log_signature, sparkline_points(spark_metrics(log)) if log_signature else [])
            row.spark.queue_draw()
        signature = json.dumps({key: run[key] for key in ('name', 'phase', 'size', 'completed_steps', 'steps', 'best', 'latest',
                                                           'pinned', 'val_loss', 'snapshots')})
        row.run_record = run
        if signature == row.signature and not force:
            return
        row.signature = signature
        row.title.set_text(('★ ' if run['pinned'] else '') + run['name'])
        validation = f' · val {run["val_loss"]:.3f}' if run['val_loss'] is not None else ''
        row.meta.set_text(f'{run["size"].upper()} · {run["completed_steps"]:,}/{run["steps"]:,}{validation}')
        child = row.chips.get_first_child()
        while child:
            following = child.get_next_sibling(); row.chips.remove(child); child = following
        live = bool(run['job'] and run['job'].get('running'))
        tone = 'dark' if live else 'alert' if run['phase'] in ('failed', 'interrupted') else None
        for text, css in ((run['phase'].upper(), tone), ('BEST', None) if run['best'] else (None, None),
                          ('LATEST', None) if run['latest'] else (None, None),
                          (f'{run["snapshots"]} SNAP', None) if run['snapshots'] else (None, None)):
            if text:
                chip = Gtk.Label(label=text); chip.add_css_class('chip')
                if css: chip.add_css_class(css)
                row.chips.append(chip)
        row.set_tooltip_text(f'{run["name"]}\n{run["directory"]}\nDouble-click: progress · Right-click: actions')

    def draw_spark(self, row, context, width, height):
        points = (self.spark_cache.get(row.run_id) or (None, []))[1]
        if len(points) < 2:
            return
        ink = (1, 1, 1) if row.is_selected() else (.07, .07, .07)
        xs, ys = [step for step, _ in points], [value for _, value in points]
        low, high = min(ys), max(ys)
        span, xspan = (high - low) or 1, (xs[-1] - xs[0]) or 1
        context.set_source_rgb(*ink); context.set_line_width(1.5)
        for index, (step, value) in enumerate(points):
            x = 2 + (step - xs[0]) / xspan * (width - 6)
            y = 3 + (high - value) / span * (height - 8)
            (context.move_to if index == 0 else context.line_to)(x, y)
        context.stroke()
        context.set_source_rgb(*RED)
        context.rectangle(x - 2, y - 2, 4, 4); context.fill()

    def sync_actions(self, run, busy):
        enabled = {'resume': self.resume_button.get_sensitive(), 'preview': bool(run and (run['best'] or run['latest'])),
                   'chat-best': self.best_button.get_sensitive(), 'chat-latest': self.latest_button.get_sensitive(),
                   'export-notebook': self.notebook_button.get_sensitive(), 'rename': self.rename_button.get_sensitive(),
                   'pin': self.pin_button.get_sensitive(), 'open-folder': bool(run), 'copy-path': bool(run),
                   'copy-snippet': self.snippet_button.get_sensitive(), 'copy-command': self.command_button.get_sensitive(),
                   'export-weights': self.weights_button.get_sensitive(), 'export-report': bool(run),
                   'export-metrics': bool(run), 'use-settings': self.duplicate_button.get_sensitive(),
                   'reuse-corpus': self.reuse_corpus_button.get_sensitive(), 'show-progress': bool(run),
                   'trash': self.delete_button.get_sensitive(), 'clean-up': not busy}
        for name, value in enabled.items():
            if self.actions[name].get_enabled() != value:
                self.actions[name].set_enabled(value)

    def set_tiles(self, flow, items):
        signature = json.dumps(items)
        if getattr(flow, 'tile_signature', None) == signature:
            return
        flow.tile_signature = signature
        child = flow.get_first_child()
        while child:
            following = child.get_next_sibling(); flow.remove(child); child = following
        for key, value, live in items:
            tile = box(spacing=2); tile.add_css_class('tile')
            if live: tile.add_css_class('live')
            caption = Gtk.Label(label=key, xalign=0); caption.add_css_class('tile-key')
            reading = Gtk.Label(label=value, xalign=0, ellipsize=Pango.EllipsizeMode.END); reading.add_css_class('tile-value')
            tile.append(caption); tile.append(reading); flow.append(tile)

    def show_detail_header(self, run):
        if not run:
            self.detail_title.set_text('Choose a run'); self.detail_path.set_text('')
            self.set_tiles(self.detail_tiles, [])
            self.window_title.set_subtitle('Small models, trained locally')
            return
        self.detail_title.set_text(('★ ' if run['pinned'] else '') + run['name'])
        self.detail_path.set_text(run['directory'])
        self.window_title.set_subtitle(run['name'])
        chips = json.dumps([run['phase'], run['size'], bool(run['job'] and run['job'].get('running'))])
        if getattr(self.detail_chips, 'signature', None) != chips:
            self.detail_chips.signature = chips
            child = self.detail_chips.get_first_child()
            while child:
                following = child.get_next_sibling(); self.detail_chips.remove(child); child = following
            for text, css in ((run['phase'].upper(), 'dark' if run['job'] and run['job'].get('running') else 'alert' if run['phase'] in ('failed', 'interrupted') else None),
                              (run['size'].upper(), None)):
                chip = Gtk.Label(label=text); chip.add_css_class('chip')
                if css: chip.add_css_class(css)
                self.detail_chips.append(chip)
        disk = getattr(self, 'run_disk', {}).get(run['id'])
        self.set_tiles(self.detail_tiles, [
            ('PARAMETERS', f'{run["parameters"] / 1e6:.2f}M' if run['parameters'] else '—', False),
            ('STEPS', f'{run["completed_steps"]:,}', bool(run['job'] and run['job'].get('running'))),
            ('BEST VALIDATION', f'{run["val_loss"]:.4f}' if finite_number(run['val_loss']) else '—', False),
            ('BITS / BYTE', f'{engine.bits_per_byte(run["val_loss"]):.3f}' if finite_number(run['val_loss']) else '—', False),
            ('TRAINED', format_duration(run.get('training_seconds')), False),
            ('ON DISK', format_bytes(disk) if disk is not None else '…', False)])

    def update_checkpoints(self, run):
        # Checkpoint files change only when saved, unlike the step count, so a training run does not rebuild these rows every second.
        signature = None
        if run:
            try:
                with os.scandir(run['directory']) as entries:
                    files = tuple(sorted((entry.name, entry.stat().st_mtime_ns) for entry in entries
                                         if entry.name.startswith('ckpt_') and entry.name.endswith('.pt')))
            except OSError:
                files = ()
            signature = (run['id'], files, run['val_loss'])
        if signature == self.checkpoint_signature:
            return
        self.checkpoint_signature = signature
        try:
            self.checkpoint_items = self.trainer.checkpoints(run['id']) if run else []
        except (ValueError, OSError):
            self.checkpoint_items = []
        child = self.checkpoint_box.get_first_child()
        while child:
            following = child.get_next_sibling(); self.checkpoint_box.remove(child); child = following
        if not self.checkpoint_items:
            self.checkpoint_box.append(label('No checkpoints saved yet. Best appears after the first evaluation.', 'dim-label'))
        for item in self.checkpoint_items:
            row = box(False, 8); row.add_css_class('checkpoint-row')
            text = Gtk.Label(label=checkpoint_line(item), xalign=0, hexpand=True, wrap=True); text.add_css_class('run-meta')
            row.append(text)
            row.append(button('Preview', lambda kind=item['kind']: self.preview_checkpoint(kind)))
            row.append(button('Notebook…', lambda kind=item['kind']: self.export_notebook(kind)))
            row.append(button('Weights…', lambda kind=item['kind']: self.export_weights(kind)))
            if item['kind'].startswith('step'):
                row.append(button('Trash', lambda kind=item['kind']: self.trash_checkpoint(kind)))
            self.checkpoint_box.append(row)

    def update_compare_options(self, runs):
        candidates = [run for run in runs if run['id'] != self.selected_id and (Path(run['directory']) / 'train_log.jsonl').is_file()]
        ids = [run['id'] for run in candidates]
        if ids != self.compare_ids:
            previous = self.selected_compare()
            self.compare_ids = ids
            self.rebuilding_compare = True
            self.compare_picker.set_model(Gtk.StringList.new(['No other run', *(f'{run["name"]} · {run["size"].upper()}' for run in candidates)]))
            self.compare_picker.set_selected(ids.index(previous) + 1 if previous in ids else 0)
            self.rebuilding_compare = False
        self.load_compare()

    def selected_compare(self):
        index = self.compare_picker.get_selected() if hasattr(self, 'compare_picker') else 0
        return self.compare_ids[index - 1] if 0 < index <= len(self.compare_ids) else None

    def compare_changed(self):
        if getattr(self, 'rebuilding_compare', False):
            return
        self.compare_signature = None; self.load_compare()

    def load_compare(self):
        run_id = self.selected_compare()
        run = next((item for item in self.runs if item['id'] == run_id), None)
        if not run:
            if self.compare_losses:
                self.compare_losses = []; self.compare_signature = None; self.chart.queue_draw()
            return
        try:
            stat = (Path(run['directory']) / 'train_log.jsonl').stat(); signature = (run_id, stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = (run_id, None)
        if signature != self.compare_signature:
            self.compare_signature = signature
            self.compare_losses = engine.read_metrics(run['directory']); self.compare_name = run['name']
            self.chart.queue_draw()

    def update_rail_job(self, active, runs):
        if not active:
            self.rail_job.set_visible(False); return
        run = next((item for item in runs if item['directory'] == active['run']), None)
        self.rail_job.set_visible(True)
        self.rail_job_name.set_text(run['name'] if run else Path(active['run']).name)
        if run and run['steps']:
            self.rail_job_detail.set_text(f'{run["phase"]} · {run["completed_steps"]:,} / {run["steps"]:,}')
            self.rail_job_progress.set_fraction(min(1, run['completed_steps'] / run['steps']))
        else:
            self.rail_job_detail.set_text(run['phase'] if run else 'starting'); self.rail_job_progress.pulse()

    def show_detail_tab(self, name):
        self.stack.set_visible_child_name('runs'); self.detail_tabs.set_visible_child_name(name)

    def show_progress(self):
        if self.selected_id:
            self.stack.set_visible_child_name('activity')

    def open_run_menu(self, x, y):
        row = self.run_list.get_row_at_y(int(y))
        if row is None or not hasattr(row, 'run_id'):
            return
        if self.run_list.get_selected_row() is not row:
            self.run_list.select_row(row)
        rectangle = Gdk.Rectangle(); rectangle.x, rectangle.y, rectangle.width, rectangle.height = int(x), int(y), 1, 1
        self.run_menu.set_pointing_to(rectangle); self.run_menu.popup()

    def run_list_key(self, _, key, code, state):
        if key == Gdk.KEY_Delete and self.delete_button.get_sensitive():
            self.delete(); return True
        if key == Gdk.KEY_Menu or (key == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK):
            row = self.run_list.get_selected_row()
            if row is not None:
                found, bounds = row.compute_bounds(self.run_list)
                if found:
                    self.open_run_menu(bounds.get_x() + 24, bounds.get_y() + bounds.get_height() / 2)
            return True
        return False

    def has_checkpoint(self, run, kind):
        if kind in ('best', 'latest'):
            return bool(run[kind])
        return (Path(run['directory']) / f'ckpt_{kind}.pt').is_file()

    def preview_checkpoint(self, kind):
        self.show_detail_tab('preview'); self.sample((kind,))

    def trash_checkpoint(self, kind):
        run = self.current_run()
        if not run:
            return
        dialog = Adw.AlertDialog(heading=f'Move {engine.checkpoint_label(kind)} to Trash?',
                                 body=f'{run["name"]}: ckpt_{kind}.pt moves to your Trash and can be restored from there.')
        dialog.add_response('cancel', 'Cancel'); dialog.add_response('trash', 'Move to Trash')
        dialog.set_response_appearance('trash', Adw.ResponseAppearance.DESTRUCTIVE)
        def answered(_, response):
            if response != 'trash':
                return
            try:
                self.trainer.trash(run['id'], kind)
            except (ValueError, OSError) as error:
                self.notify(error); return
            self.checkpoint_signature = None; self.refresh(True); self.notify('Snapshot moved to Trash')
        dialog.connect('response', answered); dialog.present(self)

    def clean_up_failed(self):
        candidates = self.trainer.cleanup_candidates()
        if not candidates:
            self.notify('No failed runs without data or weights.'); return
        dialog = Adw.Dialog(title='Clean up failed runs', content_width=520)
        layout = box()
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(layout, setter)(18)
        layout.append(label(f'{len(candidates)} failed or interrupted runs saved no prepared corpus, checkpoints or snapshots.', 'heading'))
        listing = '\n'.join(f'{run["name"]} · {run["phase"]}' + (f' — {run["detail"][:80]}' if run['detail'] else '') for run in candidates[:8])
        layout.append(label(listing + ('\n…' if len(candidates) > 8 else ''), 'run-meta'))
        layout.append(label('Their folders (logs and status only) move to your Trash. Pinned and running runs are never included.', 'dim-label'))
        confirm = Gtk.CheckButton(label=f'Move {len(candidates)} run folders to Trash'); layout.append(confirm)
        actions = box(False, 10)
        actions.append(button('Cancel', dialog.close))
        def remove():
            go.set_sensitive(False); confirm.set_sensitive(False)
            ids = [run['id'] for run in candidates]
            def done(result, error):
                dialog.close()
                if error:
                    self.notify(error); return False
                moved, errors = result
                if self.selected_id in moved:
                    self.selected_id = None
                self.refresh(True)
                self.notify(f'Moved {len(moved)} runs to Trash' + (f'; {len(errors)} could not be moved: {errors[0]}' if errors else ''))
                return False
            self.background(lambda: self.trainer.clean_up_runs(ids), done)
        go = button('Move to Trash', remove, True); go.set_sensitive(False)
        confirm.connect('toggled', lambda *_: go.set_sensitive(confirm.get_active()))
        actions.append(go); layout.append(actions); dialog.set_child(layout); dialog.present(self)

    def export_notebook(self, kind=None):
        run = self.current_run()
        if not run:
            return
        try:
            items = self.trainer.checkpoints(run['id'])
        except ValueError as error:
            self.notify(error); return
        if not items:
            self.notify('Save a checkpoint before exporting a notebook.'); return
        kinds = [item['kind'] for item in items]
        kind = kind if kind in kinds else kinds[0]
        python = self.form['python'].get_text()
        launcher = engine.jupyter_command(python)
        dialog = Adw.Dialog(title='Export Jupyter notebook', content_width=600)
        layout = box(spacing=10)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(layout, setter)(18)
        layout.append(label('NOTEBOOK', 'eyebrow')); layout.append(label(run['name'], 'detail-name'))
        picker = Gtk.DropDown.new_from_strings([' · '.join(checkpoint_line(item).split(' · ')[:3]) for item in items])
        picker.set_selected(kinds.index(kind))
        self.field(layout, 'Checkpoint', picker)
        documents = Path.home() / 'Documents'
        parent = Gtk.Entry(text=str(documents if documents.is_dir() else Path.home()), hexpand=True)
        choose = button('Choose…', lambda: self.choose_folder(parent))
        where = box(False, 8); where.append(parent); where.append(choose)
        self.field(layout, 'Save in', where)
        name = Gtk.Entry(text=f'{export_notebook.slug(run["name"])}-{kind}-notebook', max_length=120)
        picker.connect('notify::selected', lambda *_: name.set_text(f'{export_notebook.slug(run["name"])}-{kinds[picker.get_selected()]}-notebook'))
        self.field(layout, 'New folder name', name)
        def option(text, active=False):
            widget = Gtk.CheckButton(active=active)
            widget.set_child(Gtk.Label(label=text, xalign=0, wrap=True)); widget.option_text = text
            return widget
        bundle = option('Self-contained folder: model code, checkpoint copy and logs (recommended)', True)
        corpus = option('Include the prepared corpus (for evaluation and continued training on another machine)')
        corpus.set_sensitive(run['prepared'])
        script = option('Also write a .py script with # %% cells (VS Code, Spyder, Jupytext)', True)
        opener = option('Open in Jupyter afterwards (registers the training Python as the “Whitecube Trainer (PyTorch)” kernel once)',
                        bool(launcher))
        opener.set_sensitive(bool(launcher))
        for widget in (bundle, corpus, script, opener):
            layout.append(widget)
        size = label('', 'dim-label'); layout.append(size)
        def update_size(*_):
            corpus.set_sensitive(run['prepared'] and bundle.get_active())
            try:
                total = export_notebook.bundle_size(run['directory'], kinds[picker.get_selected()], bundle=bundle.get_active(),
                                                    corpus=corpus.get_active() and corpus.get_sensitive())
                size.set_text(f'Writes about {format_bytes(total)}.' + ('' if bundle.get_active() else
                              ' The notebook refers to the run folder and the installed pipeline by absolute path.')
                              + ('' if launcher else ' No Jupyter found: open the .ipynb in any notebook editor.'))
            except (ValueError, OSError) as error:
                size.set_text(str(error))
        for widget in (bundle, corpus):
            widget.connect('toggled', update_size)
        picker.connect('notify::selected', update_size); update_size()
        actions = box(False, 10)
        actions.append(button('Cancel', dialog.close))
        def run_export():
            folder = name.get_text().strip()
            if not folder or '/' in folder or folder in ('.', '..'):
                self.notify('Enter a folder name without slashes.'); return
            destination = Path(parent.get_text().strip()).expanduser() / folder
            chosen, options = kinds[picker.get_selected()], dict(bundle=bundle.get_active(), corpus=corpus.get_active() and corpus.get_sensitive(),
                                                                 script=script.get_active())
            preview = {**self.preview_settings(), 'prompt': self.sample_prompt.get_text().strip() or 'Once upon a time'}
            go.set_sensitive(False); go.set_label('Exporting…')
            self.notebook_status.set_text(f'Exporting {engine.checkpoint_label(chosen)} to {destination}…')
            def done(path, error):
                if error:
                    go.set_sensitive(True); go.set_label('Export'); self.notebook_status.set_text(str(error)); self.notify(error)
                    return False
                dialog.close()
                self.last_notebook = path; self.jupyter_button.set_sensitive(bool(launcher))
                self.notebook_status.set_text(f'Last export: {path}')
                self.notify(f'Notebook written: {path.name}')
                if opener.get_active():
                    self.open_in_jupyter(path)
                return False
            self.background(lambda: self.trainer.notebook_export(run['id'], chosen, destination, preview=preview, **options), done)
        go = button('Export', run_export, True)
        actions.append(go); layout.append(actions)
        scroller = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=640, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(layout)
        dialog.set_child(scroller); dialog.present(self)

    def choose_folder(self, entry):
        chooser = Gtk.FileChooserNative(title='Choose where to save the notebook folder', transient_for=self,
                                        action=Gtk.FileChooserAction.SELECT_FOLDER, accept_label='Choose', cancel_label='Cancel')
        current = Path(entry.get_text()).expanduser()
        if current.is_dir():
            chooser.set_current_folder(Gio.File.new_for_path(str(current)))
        def response(dialog, result):
            if result == Gtk.ResponseType.ACCEPT and dialog.get_file() and dialog.get_file().get_path():
                entry.set_text(dialog.get_file().get_path())
            dialog.destroy()
        chooser.connect('response', response); chooser.show()

    def open_in_jupyter(self, path):
        if not path:
            return
        python = self.form['python'].get_text()
        launcher = engine.jupyter_command(python)
        if not launcher:
            self.notify('No Jupyter found. Install JupyterLab or Notebook, or open the .ipynb in another editor.'); return
        command = self.trainer.kernel_command(python) if Path(python).expanduser().is_file() else None
        def work():
            note = ''
            kernel = engine.installed_kernel()
            if not kernel and command:
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
                kernel = engine.installed_kernel()
                if result.returncode or not kernel:
                    note = ' The training Python could not register a kernel (it needs ipykernel); choose a kernel with PyTorch in Jupyter.'
            if kernel:
                engine.set_notebook_kernel(path, kernel['display_name'])
            subprocess.Popen([*launcher, str(path)], cwd=str(Path(path).parent), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return note
        self.background(work, lambda note, error: self.notify(error or f'Opening {Path(path).name} in {Path(launcher[0]).name}.{note}'))

    def copy_load_snippet(self):
        run = self.current_run()
        if run and (run['best'] or run['latest']):
            kind = 'best' if run['best'] else 'latest'
            self.get_display().get_clipboard().set(load_snippet(Path(run['directory']) / f'ckpt_{kind}.pt'))
            self.notify(f'Python snippet for {kind.capitalize()} copied')

    def update_recent_menu(self):
        try:
            recent = self.trainer.preferences()['recent_datasets']
        except (OSError, ValueError):
            recent = []
        menu = Gio.Menu()
        home = str(Path.home())
        for path in recent:
            shown = '~' + path[len(home):] if path.startswith(home + '/') else path
            item = Gio.MenuItem.new(shown, None)
            item.set_action_and_target_value('win.use-dataset', GLib.Variant('s', path))
            menu.append_item(item)
        self.recent_button.set_menu_model(menu)
        self.recent_button.set_sensitive(bool(recent))

    def use_recent_dataset(self, path):
        self.form['dataset'].set_text(path); self.inspect_dataset()

    def dropped(self, _, value, *__):
        self.stack.get_child_by_name('train').remove_css_class('drop-hover')
        files = value.get_files() if hasattr(value, 'get_files') else [value]
        path = files[0].get_path() if files and files[0] else None
        if not path:
            self.notify('Drop a local corpus file or folder.'); return False
        self.stack.set_visible_child_name('train'); self.form['dataset'].set_text(path); self.inspect_dataset()
        return True

    def probe_corpus_fields(self, info, request):
        files = [path for path in info['files'] if path.lower().endswith(('.jsonl', '.parquet'))]
        self.field_probe = {}; self.field_names = []
        self.setting_field = True
        self.field_picker.set_model(Gtk.StringList.new(['Automatic'])); self.field_picker.set_sensitive(False)
        self.setting_field = False
        if not files:
            self.field_info.set_text('TXT and Markdown files are read whole; no text field is needed.' if info['files'] else
                                     'JSONL and Parquet text fields are listed after Inspect.')
            return
        self.field_info.set_text('Reading text fields…')
        python = self.form['python'].get_text()
        def done(probe, error):
            if request != self.inspect_request:
                return False
            if error:
                self.field_info.set_text(str(error)); return False
            self.field_probe = probe
            message, self.field_names, _ = engine.field_summary(probe, self.form['column'].get_text().strip())
            self.setting_field = True
            self.field_picker.set_model(Gtk.StringList.new(['Automatic', *self.field_names]))
            current = self.form['column'].get_text().strip()
            self.field_picker.set_selected(self.field_names.index(current) + 1 if current in self.field_names else 0)
            self.setting_field = False
            self.field_picker.set_sensitive(True); self.field_info.set_text(message)
            return False
        self.background(lambda: engine.probe_fields(files, python), done)

    def field_chosen(self):
        if getattr(self, 'setting_field', False):
            return
        index = self.field_picker.get_selected()
        names = getattr(self, 'field_names', [])
        self.form['column'].set_text(names[index - 1] if 0 < index <= len(names) else '')

    def show_field_summary(self):
        if self.field_probe:
            message, names, _ = engine.field_summary(self.field_probe, self.form['column'].get_text().strip())
            self.field_info.set_text(message)
            current = self.form['column'].get_text().strip()
            self.setting_field = True
            self.field_picker.set_selected(names.index(current) + 1 if current in names else 0)
            self.setting_field = False

    def show_log(self, force=False):
        text = self.last_log or ''
        query = self.log_search.get_text().casefold()
        if self.matching_only.get_active() and query:
            text = '\n'.join(line for line in text.splitlines() if query in line.casefold())
        if not force and text == getattr(self, 'displayed_log', None):
            return
        self.displayed_log = text
        adjustment = self.log_scroll.get_vadjustment(); previous = adjustment.get_value()
        at_bottom = previous + adjustment.get_page_size() >= adjustment.get_upper() - 24
        buffer = self.log_view.get_buffer()
        buffer.set_text(text)
        for number, line in enumerate(text.splitlines()):
            kind = log_line_kind(line)
            if kind:
                start = buffer.get_iter_at_line(number)
                start = start[1] if isinstance(start, tuple) else start
                end = start.copy(); end.forward_to_line_end()
                buffer.apply_tag_by_name(kind, start, end)
        def restore_scroll():
            if self.closed:
                return False
            adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()) if self.follow.get_active() and at_bottom else previous)
            return False
        GLib.idle_add(restore_scroll)

    def measure_disk(self):
        run_id = self.selected_id
        if not run_id:
            return
        self.disk_request += 1
        request = self.disk_request
        def done(size, error):
            if not error and request == self.disk_request:
                self.run_disk = {run_id: size}
                run = next((item for item in self.runs if item['id'] == run_id), None)
                if run and run_id == self.selected_id:
                    self.run_detail.set_text(run_details(run, size))
            return False
        self.background(lambda: self.trainer.disk_usage(run_id), done)

    def measure_total_disk(self):
        directory = self.trainer.runs_dir
        def done(size, error):
            self.total_disk = None if error else size
            self.filter_runs()
            return False
        self.background(lambda: engine.directory_size(directory), done)

    def desktop_notification(self, run):
        if self.is_active():
            return
        try:
            notification = Gio.Notification.new('Whitecube Trainer')
            reason = engine.STOP_REASONS.get(run.get('stop_reason'), run['phase'].capitalize())
            notification.set_body(f'{run["name"]}: {reason} · {run["completed_steps"]:,} / {run["steps"]:,} steps')
            self.get_application().send_notification('training-finished', notification)
        except (GLib.Error, TypeError) as error:
            print(f'Desktop notification unavailable: {error}', file=sys.stderr)

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

    def clear_preview(self):
        self.preview_result = None
        self.sample_output.set_text(''); self.sample_stats.set_text('')
        self.copy_sample_button.set_sensitive(False); self.save_sample_button.set_sensitive(False)

    def preview_settings(self):
        values = self.form_values()
        return {'tokens': values['preview_tokens'], 'temperature': values['preview_temperature'],
                'top_k': values['preview_top_k'], 'top_p': values['preview_top_p'], 'seed': values['preview_seed']}

    def sample(self, kinds=None):
        run = self.current_run()
        prompt = self.sample_prompt.get_text().strip()
        kinds = kinds or (('best',), ('latest',), ('best', 'latest'))[self.sample_kind.get_selected()]
        if (not run or not prompt or self.sampling or self.launching or self.trainer.active_job()
                or not all(self.has_checkpoint(run, kind) for kind in kinds)):
            self.notify('Choose an available checkpoint, enter a prompt, and wait for training or preview to finish.'); return
        settings = self.preview_settings()
        try:
            commands = [(kind, self.trainer.preview_command(run['id'], kind, prompt, self.form['python'].get_text(), **settings))
                        for kind in kinds]
        except ValueError as error:
            self.notify(error); return
        self.preview_request += 1
        request = self.preview_request
        run_id = run['id']
        self.clear_preview()
        self.sampling = True; self.sample_output.set_text('Generating a short CPU preview…'); self.refresh()
        def work():
            results = []
            for kind, command in commands:
                started = time.monotonic()
                result = subprocess.run(command, capture_output=True, text=True, timeout=45 + settings['tokens'] // 20)
                if result.returncode:
                    raise ValueError(result.stderr[-1200:] or 'Preview failed.')
                results.append((kind, result.stdout.strip(), time.monotonic() - started))
            return results
        def done(results, error):
            self.sampling = False
            if request == self.preview_request and run_id == self.selected_id:
                if error:
                    self.sample_output.set_text(str(error))
                else:
                    if len(results) == 1:
                        text = results[0][1]
                        if results[0][0] not in ('best', 'latest'):
                            text = f'{engine.checkpoint_label(results[0][0])}:\n{text}'
                    else:
                        text = '\n\n'.join(f'{engine.checkpoint_label(kind)}:\n{output}' for kind, output, _ in results)
                    self.sample_output.set_text(text)
                    generated = sum(len(output.encode()) for _, output, _ in results)
                    seconds = sum(elapsed for *_, elapsed in results)
                    self.sample_stats.set_text(f'{generated:,} new bytes in {seconds:.1f} s on CPU · seed {settings["seed"]} · '
                                               f'temperature {settings["temperature"]:g} · top-k {settings["top_k"]} · top-p {settings["top_p"]:g}')
                    self.preview_result = {**settings, 'text': text, 'prompt': prompt, 'seconds': round(seconds, 3),
                                           'checkpoint': '+'.join(kind for kind, *_ in results), 'run_id': run_id}
                    self.copy_sample_button.set_sensitive(bool(text)); self.save_sample_button.set_sensitive(bool(text))
            self.refresh()
            return False
        self.background(work, done)

    def save_preview(self):
        record = self.preview_result
        if not record or record['run_id'] != self.selected_id:
            return
        try:
            self.trainer.save_preview(record['run_id'], record)
        except (ValueError, OSError) as error:
            self.notify(error); return
        self.save_sample_button.set_sensitive(False)
        self.show_preview_history(); self.notify('Preview saved with the run')

    def show_preview_history(self):
        try:
            saved = self.trainer.previews(self.selected_id, 3) if self.selected_id else []
        except ValueError:
            saved = []
        lines = [f'{item.get("checkpoint", "?")} · “{str(item.get("prompt", ""))[:40]}” → {item["text"][:90].replace(chr(10), " ")}…' for item in reversed(saved)]
        self.preview_history.set_text(('Saved previews (newest first):\n' + '\n'.join(lines)) if lines else '')

    def toggle_pin(self):
        run = self.current_run()
        if run:
            try:
                self.trainer.update_run(run['id'], pinned=not run['pinned']); self.refresh(True)
            except (ValueError, OSError) as error:
                self.notify(error)

    def load_notes(self, run):
        self.notes_id = run['id'] if run else None
        self.notes_view.get_buffer().set_text(run['notes'] if run else '')
        self.notes_status.set_text('')
        self.notes_button.set_sensitive(False)

    def notes_edited(self):
        buffer = self.notes_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        too_long = len(text) > engine.NOTES_LIMIT
        self.notes_button.set_sensitive(bool(self.notes_id) and not too_long)
        self.notes_status.set_text(f'{len(text):,} / {engine.NOTES_LIMIT:,} characters' + (' — too long' if too_long else ' · unsaved'))

    def save_notes(self):
        buffer = self.notes_view.get_buffer()
        try:
            self.trainer.update_run(self.notes_id, notes=buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))
        except (ValueError, OSError) as error:
            self.notify(error); return
        self.notes_button.set_sensitive(False); self.notes_status.set_text('Saved'); self.refresh(True)

    def copy_resume_command(self):
        run = self.current_run()
        if run:
            self.resume_steps.update()
            self.get_display().get_clipboard().set(self.trainer.resume_command(run['id'], int(self.resume_steps.get_value())))
            self.notify('Resume command copied')

    def export_weights(self, kind=None):
        run = self.current_run()
        if not run or not (kind or run['best'] or run['latest']):
            return
        if not kind:
            kind = 'best' if run['best'] else 'latest'
            if self.sample_kind.get_selected() == 1 and run['latest']:
                kind = 'latest'
        chooser = Gtk.FileChooserNative(title=f'Export {engine.checkpoint_label(kind)} weights to a new folder', transient_for=self,
                                        action=Gtk.FileChooserAction.SAVE, accept_label='Export', cancel_label='Cancel')
        chooser.set_current_name(f'{run["id"]}-{kind}-safetensors')
        python = self.form['python'].get_text()
        def response(dialog, result):
            path = dialog.get_file().get_path() if result == Gtk.ResponseType.ACCEPT and dialog.get_file() else None
            dialog.destroy()
            if not path:
                return
            try:
                command = self.trainer.export_command(run['id'], kind, path, python)
            except ValueError as error:
                self.notify(error); return
            self.weights_button.set_sensitive(False); self.notify(f'Exporting {kind} weights…')
            def work():
                result = subprocess.run(command, capture_output=True, text=True, timeout=300)
                if result.returncode:
                    raise ValueError(result.stderr[-900:] or 'Weights export failed.')
                return path
            def done(value, error):
                self.weights_button.set_sensitive(True)
                self.notify(error or f'Exported weights and model card to {value}')
                return False
            self.background(work, done)
        chooser.connect('response', response); chooser.show()

    def show_shortcuts(self):
        dialog = Adw.Dialog(title='Keyboard shortcuts', content_width=460)
        grid = Gtk.Grid(column_spacing=24, row_spacing=8)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(grid, setter)(20)
        for row, (keys, action) in enumerate(SHORTCUTS):
            grid.attach(label(keys, 'heading'), 0, row, 1, 1); grid.attach(label(action), 1, row, 1, 1)
        dialog.set_child(grid); dialog.present(self)

    def chart_pointer(self, x):
        self.chart_hover = x
        self.chart.queue_draw()
        geometry = self.chart_geometry
        if x is None or not geometry or not self.losses:
            self.chart_readout.set_text(''); self.chart_readout.set_visible(False); return
        self.chart_readout.set_visible(True)
        left, plot_width, xmax = geometry
        step = (x - left) / plot_width * xmax
        nearest = {}
        for key in ('total_loss', 'val_loss', 'lr'):
            points = [(item['step'], item[key]) for item in self.losses if finite_number(item.get('step')) and finite_number(item.get(key))]
            if points:
                nearest[key] = min(points, key=lambda point: abs(point[0] - step))
        parts = [f'Step {int(nearest[key][0]):,}' for key in ('total_loss', 'val_loss') if key in nearest][:1]
        if 'total_loss' in nearest: parts.append(f'training {nearest["total_loss"][1]:.4f}')
        if 'val_loss' in nearest: parts.append(f'validation {nearest["val_loss"][1]:.4f} = {engine.bits_per_byte(nearest["val_loss"][1]):.3f} bits/byte (step {int(nearest["val_loss"][0]):,})')
        if 'lr' in nearest: parts.append(f'learning rate {nearest["lr"][1]:.2e}')
        other = [(item['step'], item['val_loss']) for item in getattr(self, 'compare_losses', []) if finite_number(item.get('step')) and finite_number(item.get('val_loss'))]
        if other:
            at = min(other, key=lambda point: abs(point[0] - step))
            parts.append(f'{getattr(self, "compare_name", "compared run")}: validation {at[1]:.4f} (step {int(at[0]):,})')
        self.chart_readout.set_text(' · '.join(parts))

    def draw_loss(self, _, context, width, height):
        context.set_source_rgb(.94, .94, .94); context.paint()
        if width < 100 or height < 65:
            return
        points = loss_series(self.losses, 'total_loss')
        smooth = getattr(self, 'smooth_check', None)
        if smooth is not None and smooth.get_active():
            points = smooth_series(points)
        val = loss_series(self.losses, 'val_loss')
        if not points and not val:
            context.set_source_rgb(.25, .25, .25); context.move_to(16, 30); context.show_text('Loss graph appears after the first training step.'); return
        other = getattr(self, 'compare_losses', None) or []
        other_val = loss_series(other, 'val_loss') or loss_series(other, 'total_loss')
        all_points = points + val + other_val
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
        left, top, plot_width, plot_height = 48, 15, width - 65, height - 47
        self.chart_geometry = (left, plot_width, xmax)
        def position(step, loss):
            return left + step / xmax * plot_width, top + (ymax - loss / yscale) / span * plot_height
        # Light gridlines at quarters, with the middle value labelled.
        context.set_source_rgb(.82, .82, .82); context.set_line_width(1)
        for quarter in (1, 2, 3):
            y = top + plot_height * quarter / 4
            context.move_to(left, y); context.line_to(left + plot_width, y)
            x = left + plot_width * quarter / 4
            context.move_to(x, top); context.line_to(x, top + plot_height)
        context.stroke()
        context.set_source_rgb(.1, .1, .1); context.set_line_width(1)
        context.move_to(48, 15); context.line_to(48, height - 32); context.line_to(width - 15, height - 32); context.stroke()
        def tick_text(bound, fallback):
            value = bound * yscale
            if not math.isfinite(value):
                value = fallback
            return f'{value:.3g}' if abs(value) >= 10_000 else f'{value:.2f}'
        context.move_to(5, 25); context.show_text(tick_text(ymax, raw_ymax))
        context.move_to(5, top + plot_height / 2 + 4); context.show_text(tick_text((ymax + ymin) / 2, (raw_ymax + raw_ymin) / 2))
        context.move_to(5, height - 35); context.show_text(tick_text(ymin, raw_ymin))
        context.move_to(48, height - 10); context.show_text('0'); context.move_to(width - 90, height - 10); context.show_text(f'{xmax:g} steps')
        lr_toggle = getattr(self, 'lr_check', None)
        rates = loss_series(self.losses, 'lr') if lr_toggle is not None and lr_toggle.get_active() else []
        top_rate = max((rate for _, rate in rates), default=0)
        if top_rate > 0:
            context.set_source_rgb(.55, .55, .55); context.set_line_width(1); context.set_dash([4, 3])
            for index, (step, rate) in enumerate(rates):
                x, y = left + step / xmax * plot_width, top + (1 - rate / top_rate) * plot_height
                if index == 0: context.move_to(x, y)
                else: context.line_to(x, y)
            context.stroke(); context.set_dash([])
        if other_val:
            context.set_source_rgb(.45, .45, .45); context.set_line_width(1.5); context.set_dash([6, 4])
            for index, (step, loss) in enumerate(other_val):
                x, y = position(step, loss)
                if index == 0: context.move_to(x, y)
                else: context.line_to(x, y)
            context.stroke(); context.set_dash([])
        for series, color in ((points, (.05, .05, .05)), (val, RED)):
            context.set_source_rgb(*color); context.set_line_width(2)
            for index, (step, loss) in enumerate(series):
                x, y = position(step, loss)
                if index == 0: context.move_to(x, y)
                else: context.line_to(x, y)
            context.stroke()
            if len(series) == 1:
                x, y = position(*series[0]); context.arc(x, y, 3, 0, 2 * math.pi); context.fill()
        if val:
            best = min(val, key=lambda point: point[1])
            x, y = position(*best)
            context.set_source_rgb(*RED); context.set_line_width(2)
            context.rectangle(x - 5, y - 5, 10, 10); context.stroke()
        hover = getattr(self, 'chart_hover', None)
        if hover is not None and left <= hover <= left + plot_width:
            context.set_source_rgb(.4, .4, .4); context.set_line_width(1)
            context.move_to(hover, top); context.line_to(hover, top + plot_height); context.stroke()

    def key_pressed(self, _, key, code, state):
        if self.get_visible_dialog():
            return False
        if state & Gdk.ModifierType.CONTROL_MASK and state & Gdk.ModifierType.SHIFT_MASK and key in (Gdk.KEY_e, Gdk.KEY_E):
            if self.notebook_button.get_sensitive():
                self.export_notebook()
            return True
        if key == Gdk.KEY_F2 and self.rename_button.get_sensitive():
            self.rename(); return True
        if state & Gdk.ModifierType.CONTROL_MASK:
            if key in (Gdk.KEY_1, Gdk.KEY_2, Gdk.KEY_3):
                self.stack.set_visible_child_name(('train', 'runs', 'activity')[key - Gdk.KEY_1]); return True
            if key in (Gdk.KEY_n, Gdk.KEY_N): self.new_run(); return True
            if key in (Gdk.KEY_f, Gdk.KEY_F):
                if self.stack.get_visible_child_name() == 'runs': self.run_search.grab_focus()
                else: self.stack.set_visible_child_name('activity'); self.log_search.grab_focus()
                return True
            if key in (Gdk.KEY_p, Gdk.KEY_P) and self.sample_button.get_sensitive():
                self.stack.set_visible_child_name('runs'); self.sample(); return True
            if key in (Gdk.KEY_e, Gdk.KEY_E) and self.report_button.get_sensitive():
                self.export_report(); return True
            if key == Gdk.KEY_Return:
                page = self.stack.get_visible_child_name()
                if page == 'train' and self.start_button.get_sensitive(): self.start(); return True
                if page == 'runs' and self.resume_button.get_sensitive(): self.resume(); return True
        if key == Gdk.KEY_F5: self.refresh(True); return True
        if key == Gdk.KEY_F1: self.show_shortcuts(); return True
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
        provider = Gtk.CssProvider(); provider.load_from_string(TRAINER_CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)

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
