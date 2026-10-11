#!/usr/bin/python3
"""Cube Prompts: a prompt library and test bench for Whitecube.

A native GTK4 front end for the Prompt Manager Studio core (bundled beside this
file as prompt_manager/): SQLite library with full-text search, {{variable}}
templates with a live preview, revisions, reusable templates, exports, and a
side-by-side run of one prompt against up to three models.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading

# The launcher uses isolated Python; import only this application's directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from prompt_manager import __version__ as VERSION
from prompt_manager import config
from prompt_manager.core import exporter
from prompt_manager.core.arena import run_arena_comparison
from prompt_manager.core.keychain import CredentialStoreError, get_key_vault
from prompt_manager.core.models import Folder, Prompt, PromptTemplate
from prompt_manager.core.template_engine import check_syntax_errors, extract_variables, hydrate_template
from prompt_manager.core.token_counter import calculate_metrics
from prompt_manager.integrations.llm_providers import LLMClient, discover_ollama_models, list_models_by_provider
from prompt_manager.storage import backup
from prompt_manager.storage.database import Database
from prompt_manager.storage.repository import PromptRepository

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

PAGES = (
    ('library', 'Library', 'accessories-text-editor-symbolic'),
    ('run', 'Run', 'media-playback-start-symbolic'),
    ('templates', 'Templates', 'view-grid-symbolic'),
    ('history', 'History', 'document-open-recent-symbolic'),
    ('export', 'Export', 'document-save-symbolic'),
    ('settings', 'Settings', 'preferences-system-symbolic'),
)
EXPORTS = (
    ('plain', 'Plain text', '.txt'),
    ('openai', 'OpenAI Chat Completions JSON', '.json'),
    ('anthropic', 'Anthropic Messages JSON', '.json'),
    ('markdown', 'Markdown with front matter', '.md'),
    ('langchain', 'LangChain PromptTemplate (Python)', '.py'),
    ('llamaindex', 'LlamaIndex PromptTemplate (Python)', '.py'),
)
# Providers that take an API key; the second item is an optional endpoint setting.
PROVIDERS = (
    ('anthropic', 'Anthropic', None),
    ('openai', 'OpenAI', 'base_url'),
    ('gemini', 'Google Gemini', None),
    ('openrouter', 'OpenRouter', None),
    ('pollinations', 'Pollinations (optional key)', None),
    ('ollama', 'Ollama (local, no key)', 'base_url'),
)
NO_MODEL = '— none —'
RUN_TIMEOUT = 180


def label(text='', css=None, wrap=False, selectable=False):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_wrap(wrap)
    if wrap:
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_selectable(selectable)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    return widget


def box(vertical=True, spacing=12, css=None):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    return widget


def button(text, callback, css='secondary', icon=None, tooltip=None):
    widget = Gtk.Button()
    content = box(False, 8)
    if icon:
        content.append(Gtk.Image.new_from_icon_name(icon))
    content.append(Gtk.Label(label=text))
    widget.set_child(content)
    for name in css.split():
        widget.add_css_class(name)
    if tooltip:
        widget.set_tooltip_text(tooltip)
    widget.connect('clicked', lambda *_: callback())
    return widget


def card(title=None, description=None, css='card'):
    widget = box(css=css)
    if title:
        widget.append(label(title, 'section-title'))
    if description:
        widget.append(label(description, 'lede', True))
    return widget


def chip(text, kind=None):
    widget = label(text, 'chip' + (f' {kind}' if kind else ''))
    widget.set_valign(Gtk.Align.CENTER)
    return widget


def scroll(child, min_height=0):
    widget = Gtk.ScrolledWindow()
    widget.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    widget.set_child(child)
    widget.set_vexpand(True)
    if min_height:
        widget.set_min_content_height(min_height)
    return widget


def clear(container):
    child = container.get_first_child()
    while child:
        following = child.get_next_sibling()
        container.remove(child)
        child = following


def text_area(height=120, mono=True, editable=True, css='code-wrap'):
    """A word-wrapping TextView inside a framed scroller; returns (frame, view)."""
    view = Gtk.TextView()
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_monospace(mono)
    view.set_editable(editable)
    view.set_cursor_visible(editable)
    view.set_top_margin(8)
    view.set_bottom_margin(8)
    view.set_left_margin(10)
    view.set_right_margin(10)
    frame = Gtk.ScrolledWindow()
    frame.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    frame.set_min_content_height(height)
    frame.set_child(view)
    frame.add_css_class(css)
    return frame, view


def get_text(view):
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


def short_time(value):
    """2026-10-10T12:34:56.123 -> 2026-10-10 12:34; other text unchanged."""
    if isinstance(value, str) and len(value) >= 16 and value[10:11] in ('T', ' '):
        return f'{value[:10]} {value[11:16]}'
    return value or ''


def render_export(kind, prompt, hydrated):
    if kind == 'plain':
        return exporter.to_plain_text(prompt, hydrated)
    if kind == 'openai':
        return exporter.format_json_string(exporter.to_openai_payload(prompt, hydrated))
    if kind == 'anthropic':
        return exporter.format_json_string(exporter.to_anthropic_payload(prompt, hydrated))
    if kind == 'markdown':
        return exporter.to_markdown_frontmatter(prompt, hydrated)
    if kind == 'langchain':
        return exporter.to_langchain_template(prompt)
    return exporter.to_llamaindex_template(prompt)


class PromptsWindow(Adw.ApplicationWindow):
    def __init__(self, app, repo):
        super().__init__(application=app, title='Cube Prompts')
        self.set_default_size(1240, 820)
        self.repo = repo
        self.vault = get_key_vault()
        self.prompts = []
        self.current = None          # the Prompt in the editor, as last saved
        self.dirty = False
        self.loading = False         # True while fields are filled from a Prompt
        self.values = {}             # prompt id -> {variable: value}, kept for the session
        self.var_widgets = {}
        self.folders = []
        self.filters = []            # (kind, value) behind each filter entry
        self.models = [m for m in list_models_by_provider() if m.provider != 'ollama']
        self.ollama_models = []
        self.running = False
        self.revisions = []
        self.templates = []

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        layout = box(spacing=0)
        header = Adw.HeaderBar()
        title = box(False, 8)
        title.append(label('Cube Prompts', 'title'))
        title.append(label(f'Prompt library · {VERSION}', 'muted'))
        header.set_title_widget(title)
        header.pack_start(button('New', self.new_prompt, css='flat', icon='list-add-symbolic', tooltip='New prompt (Ctrl N)'))
        self.save_button = button('Save', lambda: self.save(checkpoint=True), css='flat', icon='document-save-symbolic',
                                  tooltip='Save and keep a revision (Ctrl S)')
        header.pack_end(self.save_button)
        header.pack_end(button('Copy', self.copy_hydrated, css='flat', icon='edit-copy-symbolic',
                               tooltip='Copy the filled-in prompt (Ctrl Shift C)'))
        layout.append(header)

        body = box(False, 0)
        body.set_vexpand(True)
        body.append(self.make_rail())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.stack.set_hexpand(True)
        builders = {'library': self.make_library, 'run': self.make_run, 'templates': self.make_templates,
                    'history': self.make_history, 'export': self.make_export, 'settings': self.make_settings}
        for name, _title, _icon in PAGES:
            page = box(spacing=18, css='page')
            builders[name](page)
            self.stack.add_named(scroll(page), name)
        body.append(self.stack)
        layout.append(body)

        footer = box(False, 12, 'statusbar')
        self.spinner = Gtk.Spinner()
        footer.append(self.spinner)
        self.status = label('Ready.')
        self.status.set_hexpand(True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        footer.append(self.status)
        footer.append(label('Ctrl 1-6  pages  ·  Ctrl N  new  ·  Ctrl S  save  ·  Ctrl F  search  ·  Ctrl R  run', 'shortcut'))
        layout.append(footer)
        self.toast_overlay.set_child(layout)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.on_key)
        self.add_controller(keys)
        self.connect('close-request', self.on_close)
        self.reload_filters()
        self.reload_list()
        if self.prompts:
            self.open_prompt(self.prompts[0])
        else:
            self.new_prompt()
        self.navigate('library')

    # --- layout ---------------------------------------------------------------
    def make_rail(self):
        rail = box(spacing=12, css='rail')
        rail.set_size_request(230, -1)
        rail.set_hexpand(False)
        rail.append(label('Cube Prompts', 'rail-name'))
        rail.append(label('PROMPT LIBRARY', 'rail-caption'))
        rail.append(box(spacing=8))
        self.rail_buttons = {}
        for index, (name, title, icon) in enumerate(PAGES, 1):
            item = button(f'{index:02d}   {title}', lambda n=name: self.navigate(n), css='flat', icon=icon)
            item.get_child().set_halign(Gtk.Align.START)
            self.rail_buttons[name] = item
            rail.append(item)
        spacer = box()
        spacer.set_vexpand(True)
        rail.append(spacer)
        current = box(spacing=6, css='rail-project')
        current.append(label('EDITING', 'rail-caption'))
        self.rail_title = label('', 'project-name')
        self.rail_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.rail_title.set_max_width_chars(22)
        current.append(self.rail_title)
        self.rail_state = label('', 'dim-label')
        current.append(self.rail_state)
        rail.append(current)
        return rail

    def heading(self, page, eyebrow, title, lede):
        page.append(label(eyebrow, 'eyebrow'))
        page.append(label(title, 'page-title'))
        page.append(label(lede, 'lede', True))

    def make_library(self, page):
        split = box(False, 18)
        split.set_vexpand(True)

        listing = box(spacing=8)
        listing.set_size_request(300, -1)
        listing.set_hexpand(False)
        self.search = Gtk.SearchEntry()
        self.search.set_property('placeholder-text', 'Search prompts')
        self.search.connect('search-changed', lambda *_: self.reload_list())
        listing.append(self.search)
        row = box(False, 8)
        self.filter_drop = Gtk.DropDown.new_from_strings(['All prompts'])
        self.filter_drop.set_hexpand(True)
        self.filter_drop.connect('notify::selected', lambda *_: self.reload_list())
        row.append(self.filter_drop)
        folder_button = button('', self.ask_new_folder, icon='folder-new-symbolic', tooltip='New folder')
        row.append(folder_button)
        listing.append(row)
        self.count_label = label('', 'muted')
        listing.append(self.count_label)
        self.prompt_list = Gtk.ListBox()
        self.prompt_list.add_css_class('boxed-list')
        self.prompt_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.prompt_list.connect('row-activated', self.on_prompt_activated)
        listing.append(self.prompt_list)
        split.append(listing)

        work = box(spacing=18)
        work.set_hexpand(True)
        editor = box(spacing=12, css='card')
        top = box(False, 10)
        self.title_entry = Gtk.Entry()
        self.title_entry.set_hexpand(True)
        self.title_entry.add_css_class('title-entry')
        self.title_entry.set_property('placeholder-text', 'Untitled Prompt')
        self.title_entry.connect('changed', self.on_edited)
        top.append(self.title_entry)
        self.star = Gtk.ToggleButton()
        self.star.set_icon_name('starred-symbolic')
        self.star.set_tooltip_text('Favorite')
        self.star.add_css_class('star')
        self.star.connect('toggled', self.on_edited)
        top.append(self.star)
        editor.append(top)

        meta = Gtk.Grid(column_spacing=14, row_spacing=6)
        meta.attach(label('FOLDER', 'meta-key'), 0, 0, 1, 1)
        meta.attach(label('TARGET MODEL', 'meta-key'), 1, 0, 1, 1)
        meta.attach(label('TAGS', 'meta-key'), 2, 0, 1, 1)
        self.folder_drop = Gtk.DropDown.new_from_strings(['No folder'])
        self.folder_drop.connect('notify::selected', self.on_edited)
        meta.attach(self.folder_drop, 0, 1, 1, 1)
        self.model_names = list(config.DEFAULT_TARGET_MODELS)
        self.model_drop = Gtk.DropDown.new_from_strings(self.model_names)
        self.model_drop.connect('notify::selected', self.on_edited)
        meta.attach(self.model_drop, 1, 1, 1, 1)
        self.tags_entry = Gtk.Entry()
        self.tags_entry.set_hexpand(True)
        self.tags_entry.set_property('placeholder-text', 'comma, separated')
        self.tags_entry.connect('changed', self.on_edited)
        meta.attach(self.tags_entry, 2, 1, 1, 1)
        editor.append(meta)

        self.description_entry = Gtk.Entry()
        self.description_entry.set_property('placeholder-text', 'What this prompt is for')
        self.description_entry.connect('changed', self.on_edited)
        editor.append(self.description_entry)
        editor.append(label('SYSTEM INSTRUCTION', 'meta-key'))
        frame, self.system_view = text_area(70)
        self.system_view.get_buffer().connect('changed', self.on_edited)
        editor.append(frame)
        tline = box(False, 8)
        tline.append(label('PROMPT TEMPLATE', 'meta-key'))
        hint = label('{{name}}  {{name:default}}  {{name|multiline}}  {{name|options:a,b}}', 'shortcut')
        hint.set_hexpand(True)
        hint.set_halign(Gtk.Align.END)
        tline.append(hint)
        editor.append(tline)
        frame, self.template_view = text_area(190)
        self.template_view.get_buffer().connect('changed', self.on_edited)
        editor.append(frame)
        self.syntax_label = label('', 'danger', True)
        self.syntax_label.set_visible(False)
        editor.append(self.syntax_label)
        actions = box(False, 10)
        actions.append(button('Run', lambda: self.navigate('run'), css='primary', icon='media-playback-start-symbolic'))
        actions.append(button('Duplicate', self.duplicate, icon='edit-copy-symbolic', tooltip='Ctrl D'))
        actions.append(button('Save as template', self.ask_save_template, icon='view-grid-symbolic'))
        gap = box()
        gap.set_hexpand(True)
        actions.append(gap)
        actions.append(button('Delete', self.ask_delete, css='secondary destructive', icon='user-trash-symbolic'))
        editor.append(actions)
        work.append(editor)

        fill = box(False, 18)
        fill.set_homogeneous(True)
        variables = card('Variables', None)
        self.var_box = box(spacing=10)
        variables.append(self.var_box)
        fill.append(variables)
        preview = card('Preview')
        console = box(spacing=8, css='console')
        frame, self.preview_view = text_area(220, editable=False, css='preview')
        console.append(frame)
        preview.append(console)
        self.metrics = label('', 'muted')
        preview.append(self.metrics)
        fill.append(preview)
        work.append(fill)
        split.append(work)
        page.append(split)

    def make_run(self, page):
        self.heading(page, 'RUN', 'Try it on real models',
                     'Sends the filled-in prompt to up to three models at once and compares answers, time, '
                     'tokens and estimated cost. Pollinations needs no key; others use the keys in Settings.')
        setup = box(False, 12, 'hero')
        self.run_drops = []
        for slot in range(3):
            column = box(spacing=6)
            column.set_hexpand(True)
            column.append(label(f'MODEL {slot + 1}', 'eyebrow'))
            drop = Gtk.DropDown.new_from_strings([NO_MODEL])
            column.append(drop)
            self.run_drops.append(drop)
            setup.append(column)
        temp = box(spacing=6)
        temp.append(label('TEMPERATURE', 'eyebrow'))
        self.temperature = Gtk.SpinButton.new_with_range(0, 2, 0.1)
        self.temperature.set_digits(1)
        self.temperature.set_value(0.7)
        temp.append(self.temperature)
        setup.append(temp)
        page.append(setup)
        self.fill_run_models()
        actions = box(False, 12)
        self.run_button = button('Run prompt', self.run_prompt, css='primary', icon='media-playback-start-symbolic')
        actions.append(self.run_button)
        actions.append(button('Find local Ollama models', self.discover_ollama, icon='view-refresh-symbolic'))
        self.run_hint = label('', 'muted', True)
        self.run_hint.set_valign(Gtk.Align.CENTER)
        self.run_hint.set_hexpand(True)
        actions.append(self.run_hint)
        page.append(actions)
        self.results = box(False, 14)
        self.results.set_homogeneous(True)
        page.append(self.results)

    def make_templates(self, page):
        self.heading(page, 'TEMPLATES', 'Start from a pattern',
                     'Templates are reusable starting points. Save any prompt as one from the Library, '
                     'then create new prompts from it here.')
        split = box(False, 18)
        listing = box(spacing=8)
        listing.set_size_request(300, -1)
        self.template_list = Gtk.ListBox()
        self.template_list.add_css_class('boxed-list')
        self.template_list.connect('row-selected', self.on_template_selected)
        listing.append(self.template_list)
        split.append(listing)
        self.template_detail = box(spacing=12, css='card')
        self.template_detail.set_hexpand(True)
        self.template_detail.set_valign(Gtk.Align.START)
        split.append(self.template_detail)
        page.append(split)

    def make_history(self, page):
        self.heading(page, 'HISTORY', 'Revisions', 'Every Save (Ctrl S) keeps the version it replaces. Pick one to '
                     'compare, then restore it into the editor.')
        split = box(False, 18)
        listing = box(spacing=8)
        listing.set_size_request(300, -1)
        self.revision_list = Gtk.ListBox()
        self.revision_list.add_css_class('boxed-list')
        self.revision_list.connect('row-selected', self.on_revision_selected)
        listing.append(self.revision_list)
        split.append(listing)
        detail = box(spacing=12, css='card')
        detail.set_hexpand(True)
        self.revision_title = label('Select a revision.', 'section-title')
        detail.append(self.revision_title)
        console = box(css='console')
        frame, self.revision_view = text_area(320, editable=False, css='preview')
        console.append(frame)
        detail.append(console)
        self.restore_button = button('Restore into editor', self.restore_revision, css='primary', icon='edit-undo-symbolic')
        self.restore_button.set_sensitive(False)
        detail.append(self.restore_button)
        split.append(detail)
        page.append(split)

    def make_export(self, page):
        self.heading(page, 'EXPORT', 'Take it anywhere', 'Export the current prompt, filled in with the Library '
                     'variables, or back up and restore the whole library.')
        single = card('Current prompt')
        row = box(False, 10)
        self.export_drop = Gtk.DropDown.new_from_strings([title for _key, title, _ext in EXPORTS])
        self.export_drop.connect('notify::selected', lambda *_: self.refresh_export())
        row.append(self.export_drop)
        row.append(button('Copy', self.copy_export, icon='edit-copy-symbolic'))
        row.append(button('Save as…', self.save_export, icon='document-save-as-symbolic'))
        single.append(row)
        console = box(css='console')
        frame, self.export_view = text_area(260, editable=False, css='preview')
        console.append(frame)
        single.append(console)
        page.append(single)
        whole = card('Whole library', 'JSON keeps prompts, folders, tags and templates. CSV and Markdown are for '
                     'spreadsheets and notes; Markdown cannot be imported back.')
        row = box(False, 10)
        row.append(button('Back up as JSON…', lambda: self.save_library('json'), css='primary', icon='document-save-symbolic'))
        row.append(button('Export CSV…', lambda: self.save_library('csv'), icon='x-office-spreadsheet-symbolic'))
        row.append(button('Export Markdown ZIP…', lambda: self.save_library('zip'), icon='package-x-generic-symbolic'))
        whole.append(row)
        row = box(False, 10)
        row.append(button('Import JSON…', lambda: self.import_library('json'), icon='document-open-symbolic'))
        row.append(button('Import CSV…', lambda: self.import_library('csv'), icon='document-open-symbolic'))
        whole.append(row)
        whole.append(label(f'Library file: {self.repo.db.db_path}', 'muted', selectable=True))
        page.append(whole)

    def make_settings(self, page):
        self.heading(page, 'SETTINGS', 'Model providers', 'Keys are only sent to their own provider.')
        self.key_location = label('', 'help', True)
        page.append(self.key_location)
        self.key_entries = {}
        self.endpoint_entries = {}
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        grid.add_css_class('card')
        for index, (provider, title, endpoint) in enumerate(PROVIDERS):
            name = label(title, 'recipe-title')
            name.set_valign(Gtk.Align.CENTER)
            grid.attach(name, 0, index, 1, 1)
            if provider != 'ollama':
                entry = Gtk.PasswordEntry()
                entry.set_show_peek_icon(True)
                entry.set_hexpand(True)
                entry.set_property('placeholder-text', 'API key')
                self.key_entries[provider] = entry
                grid.attach(entry, 1, index, 1, 1)
            if endpoint:
                url = Gtk.Entry()
                url.set_hexpand(True)
                url.set_property('placeholder-text', 'http://localhost:11434' if provider == 'ollama' else 'https://api.openai.com/v1')
                self.endpoint_entries[provider] = url
                grid.attach(url, 2 if provider != 'ollama' else 1, index, 1, 1)
        page.append(grid)
        actions = box(False, 10)
        actions.append(button('Save settings', self.save_settings, css='primary', icon='document-save-symbolic'))
        actions.append(button('Reload', self.load_settings, icon='view-refresh-symbolic'))
        page.append(actions)
        page.append(label(f'Library: {self.repo.db.db_path}', 'muted', selectable=True))

    # --- navigation and shortcuts ---------------------------------------------
    def navigate(self, name):
        self.stack.set_visible_child_name(name)
        for key, item in self.rail_buttons.items():
            if key == name:
                item.add_css_class('selected')
            else:
                item.remove_css_class('selected')
        if name == 'templates':
            self.reload_templates()
        elif name == 'history':
            self.reload_revisions()
        elif name == 'export':
            self.refresh_export()
        elif name == 'settings':
            self.load_settings()
        elif name == 'run':
            self.update_run_hint()

    def on_key(self, _controller, keyval, _keycode, state):
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return False
        shift = state & Gdk.ModifierType.SHIFT_MASK
        if Gdk.KEY_1 <= keyval < Gdk.KEY_1 + len(PAGES):
            self.navigate(PAGES[keyval - Gdk.KEY_1][0])
            return True
        key = Gdk.keyval_to_lower(keyval)
        if key == Gdk.KEY_c and shift:
            self.copy_hydrated()
        elif key == Gdk.KEY_n:
            self.new_prompt()
        elif key == Gdk.KEY_s:
            self.save(checkpoint=True)
        elif key in (Gdk.KEY_f, Gdk.KEY_k):
            self.navigate('library')
            self.search.grab_focus()
        elif key == Gdk.KEY_r:
            self.navigate('run')
            self.run_prompt()
        elif key == Gdk.KEY_d:
            self.duplicate()
        else:
            return False
        return True

    def on_close(self, *_):
        if self.dirty:
            self.save(checkpoint=False, quiet=True)
        return False

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast.new(str(text)[:300]))

    def set_status(self, text):
        self.status.set_text(text)

    def background(self, work, done):
        """Run work() on a thread and done(result, error) back on the main loop."""
        def run():
            try:
                result, error = work(), None
            except Exception as exc:          # reported in the UI, never swallowed
                result, error = None, exc
            GLib.idle_add(lambda: done(result, error) and False)
        threading.Thread(target=run, daemon=True).start()

    # --- library --------------------------------------------------------------
    def reload_filters(self):
        self.folders = self.repo.list_folders()
        tags = self.repo.list_tags()
        self.filters = [('all', None), ('favorites', None)]
        names = ['All prompts', '★ Favorites']
        for folder in self.folders:
            self.filters.append(('folder', folder.id))
            names.append(f'▸ {folder.name}')
        for tag in tags:
            self.filters.append(('tag', tag.id))
            names.append(f'# {tag.name}')
        selected = self.filter_drop.get_selected()
        self.filter_drop.set_model(Gtk.StringList.new(names))
        self.filter_drop.set_selected(selected if selected < len(names) else 0)
        self.loading, was = True, self.loading
        self.folder_drop.set_model(Gtk.StringList.new(['No folder'] + [f.name for f in self.folders]))
        self.loading = was
        if self.current:
            self.select_folder(self.current.folder_id)

    def reload_list(self, select_id=None):
        index = self.filter_drop.get_selected()
        kind, value = self.filters[index] if index < len(self.filters) else ('all', None)
        self.prompts = self.repo.list_prompts(
            folder_id=value if kind == 'folder' else None,
            tag_id=value if kind == 'tag' else None,
            favorite_only=kind == 'favorites',
            search_query=self.search.get_text().strip() or None)
        clear(self.prompt_list)
        folder_names = {f.id: f.name for f in self.folders}
        selected_id = select_id or (self.current.id if self.current else None)
        for prompt in self.prompts:
            row = Gtk.ListBoxRow()
            row.prompt_id = prompt.id
            content = box(spacing=4, css='prompt-row')
            line = box(False, 8)
            name = label(('★ ' if prompt.is_favorite else '') + prompt.title, 'prompt-title')
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_hexpand(True)
            line.append(name)
            count = len(prompt.variable_specs())
            if count:
                line.append(chip(f'{count} var' + ('s' if count != 1 else ''), 'ready'))
            content.append(line)
            details = [folder_names.get(prompt.folder_id, ''), prompt.target_model, short_time(prompt.updated_at)]
            info = label(' · '.join(d for d in details if d), 'muted')
            info.set_ellipsize(Pango.EllipsizeMode.END)
            content.append(info)
            if prompt.description:
                about = label(prompt.description, 'muted')
                about.set_ellipsize(Pango.EllipsizeMode.END)
                content.append(about)
            row.set_child(content)
            self.prompt_list.append(row)
            if prompt.id == selected_id:
                self.prompt_list.select_row(row)
        total = len(self.prompts)
        self.count_label.set_text(f'{total} prompt' + ('s' if total != 1 else ''))
        if not self.prompts:
            empty = label('No prompts match.' if self.search.get_text() else 'Nothing here yet. Ctrl N starts one.', 'muted', True)
            empty.set_margin_top(12)
            empty.set_margin_bottom(12)
            empty.set_margin_start(12)
            self.prompt_list.append(empty)

    def on_prompt_activated(self, _listbox, row):
        prompt_id = getattr(row, 'prompt_id', None)
        if not prompt_id or (self.current and prompt_id == self.current.id):
            return
        if self.dirty:
            self.save(checkpoint=False, quiet=True)
        prompt = self.repo.get_prompt_by_id(prompt_id)
        if prompt:
            self.open_prompt(prompt)

    def select_folder(self, folder_id):
        ids = [None] + [f.id for f in self.folders]
        self.loading, was = True, self.loading
        self.folder_drop.set_selected(ids.index(folder_id) if folder_id in ids else 0)
        self.loading = was

    def open_prompt(self, prompt):
        self.loading = True
        self.current = prompt
        self.title_entry.set_text(prompt.title if prompt.title != 'Untitled Prompt' else '')
        self.star.set_active(prompt.is_favorite)
        self.select_folder(prompt.folder_id)
        if prompt.target_model not in self.model_names:
            self.model_names.append(prompt.target_model)
            self.model_drop.set_model(Gtk.StringList.new(self.model_names))
        self.model_drop.set_selected(self.model_names.index(prompt.target_model))
        self.tags_entry.set_text(', '.join(prompt.tags))
        self.description_entry.set_text(prompt.description)
        self.system_view.get_buffer().set_text(prompt.system_instruction)
        self.template_view.get_buffer().set_text(prompt.template_content)
        self.loading = False
        self.set_dirty(False)
        self.rebuild_variables()
        self.refresh_preview()
        for row in self._rows(self.prompt_list):
            if getattr(row, 'prompt_id', None) == prompt.id:
                self.prompt_list.select_row(row)

    def _rows(self, listbox):
        child = listbox.get_first_child()
        while child:
            yield child
            child = child.get_next_sibling()

    def editor_prompt(self):
        """A Prompt built from the editor fields, keeping the current id."""
        base = self.current or Prompt()
        folder_index = self.folder_drop.get_selected()
        folder_id = self.folders[folder_index - 1].id if 0 < folder_index <= len(self.folders) else None
        model_index = self.model_drop.get_selected()
        model = self.model_names[model_index] if model_index < len(self.model_names) else 'General'
        return Prompt(
            id=base.id, title=self.title_entry.get_text(), description=self.description_entry.get_text(),
            folder_id=folder_id, template_content=get_text(self.template_view),
            system_instruction=get_text(self.system_view), target_model=model, temperature=base.temperature,
            is_favorite=self.star.get_active(), use_count=base.use_count,
            tags=[t for t in self.tags_entry.get_text().split(',') if t.strip()],
            template_id=base.template_id, created_at=base.created_at, updated_at=base.updated_at)

    def on_edited(self, *_):
        if self.loading:
            return
        self.set_dirty(True)
        names = [s.name for s in extract_variables(get_text(self.template_view) + '\n' + get_text(self.system_view))]
        if names != list(self.var_widgets):
            self.rebuild_variables()
        self.refresh_preview()

    def set_dirty(self, dirty):
        self.dirty = dirty
        title = self.title_entry.get_text().strip() or 'Untitled Prompt'
        self.rail_title.set_text(title)
        self.rail_state.set_text('Unsaved changes' if dirty else 'Saved')
        self.save_button.set_sensitive(True)

    def save(self, checkpoint=True, quiet=False):
        prompt = self.editor_prompt()
        try:
            saved = self.repo.save_prompt(prompt, create_revision=checkpoint)
        except Exception as error:
            self.toast(f'Could not save: {error}')
            return False
        self.current = saved
        self.set_dirty(False)
        self.reload_filters()
        self.reload_list(select_id=saved.id)
        if not quiet:
            self.set_status(f'Saved “{saved.title}”' + (' and kept the previous version.' if checkpoint else '.'))
        return True

    def new_prompt(self):
        if self.dirty:
            self.save(checkpoint=False, quiet=True)
        index = self.filter_drop.get_selected()
        kind, value = self.filters[index] if index < len(self.filters) else ('all', None)
        prompt = self.repo.save_prompt(Prompt(title='Untitled Prompt', folder_id=value if kind == 'folder' else None,
                                              template_content='', is_favorite=kind == 'favorites'))
        self.search.set_text('')
        self.reload_list(select_id=prompt.id)
        self.open_prompt(prompt)
        self.navigate('library')
        self.title_entry.grab_focus()
        self.set_status('New prompt.')

    def duplicate(self):
        if not self.current:
            return
        source = self.editor_prompt()
        copy = Prompt(title=f'{source.title} (copy)', description=source.description, folder_id=source.folder_id,
                      template_content=source.template_content, system_instruction=source.system_instruction,
                      target_model=source.target_model, temperature=source.temperature, tags=source.tags)
        if self.dirty:
            self.save(checkpoint=False, quiet=True)
        saved = self.repo.save_prompt(copy)
        self.values[saved.id] = dict(self.values.get(source.id, {}))
        self.reload_list(select_id=saved.id)
        self.open_prompt(saved)
        self.set_status(f'Duplicated as “{saved.title}”.')

    def ask_delete(self):
        if not self.current:
            return
        title = self.current.title

        def confirmed(_value):
            self.repo.delete_prompt(self.current.id)
            self.values.pop(self.current.id, None)
            self.current = None
            self.reload_filters()
            self.reload_list()
            if self.prompts:
                self.open_prompt(self.prompts[0])
            else:
                self.new_prompt()
            self.set_status(f'Deleted “{title}”.')
        self.ask(f'Delete “{title}”?', 'The prompt and its revisions are removed from the library.', 'Delete',
                 confirmed, destructive=True)

    def ask_new_folder(self):
        def create(name):
            name = name.strip()
            if not name:
                return
            if any(f.name.lower() == name.lower() for f in self.folders):
                self.toast(f'A folder named {name} already exists.')
                return
            folder = self.repo.save_folder(Folder(name=name, sort_order=len(self.folders)))
            self.reload_filters()
            self.filter_drop.set_selected(2 + [f.id for f in self.folders].index(folder.id))
            self.set_status(f'Folder {name} created.')
        self.ask('New folder', 'Prompts can be filed into it from the editor.', 'Create', create, entry_text='')

    # --- variables and preview ------------------------------------------------
    def rebuild_variables(self):
        clear(self.var_box)
        self.var_widgets = {}
        if not self.current:
            return
        values = self.values.setdefault(self.current.id, {})
        specs = extract_variables(get_text(self.template_view) + '\n' + get_text(self.system_view))
        if not specs:
            self.var_box.append(label('No variables. Write {{name}} in the template to add a field.', 'muted', True))
            return
        for spec in specs:
            self.var_box.append(label(spec.name, 'var-name'))
            if spec.options:
                drop = Gtk.DropDown.new_from_strings(spec.options)
                chosen = values.get(spec.name) or spec.default_value
                if chosen in spec.options:
                    drop.set_selected(spec.options.index(chosen))
                drop.connect('notify::selected', lambda d, *_ , n=spec.name, o=spec.options: self.set_value(n, o[d.get_selected()]))
                values.setdefault(spec.name, spec.options[drop.get_selected()])
                widget = drop
            elif spec.is_multiline:
                widget, view = text_area(80, mono=True)
                view.get_buffer().set_text(values.get(spec.name, ''))
                view.get_buffer().connect('changed', lambda b, n=spec.name: self.set_value(
                    n, b.get_text(b.get_start_iter(), b.get_end_iter(), False)))
            else:
                widget = Gtk.Entry()
                widget.set_text(values.get(spec.name, ''))
                if spec.default_value:
                    widget.set_property('placeholder-text', spec.default_value)
                widget.connect('changed', lambda e, n=spec.name: self.set_value(n, e.get_text()))
            self.var_widgets[spec.name] = widget
            self.var_box.append(widget)

    def set_value(self, name, value):
        if self.current:
            self.values.setdefault(self.current.id, {})[name] = value
            self.refresh_preview()

    def hydrated(self):
        """(system, prompt) with variables filled; unfilled ones stay as {{name}}."""
        typed = self.values.get(self.current.id, {}) if self.current else {}
        system, body = get_text(self.system_view), get_text(self.template_view)
        # A default written once ({{lang:Python}}) applies to every {{lang}}, not only that one.
        values = {spec.name: typed.get(spec.name, '').strip() and typed[spec.name] or spec.default_value
                  for spec in extract_variables(body + '\n' + system)}
        return (hydrate_template(system, values, preserve_unfilled=True),
                hydrate_template(body, values, preserve_unfilled=True))

    def refresh_preview(self):
        system, body = self.hydrated()
        text = (f'[system]\n{system.strip()}\n\n' if system.strip() else '') + body
        self.preview_view.get_buffer().set_text(text)
        metrics = calculate_metrics(text)
        values = self.values.get(self.current.id, {}) if self.current else {}
        missing = [s.name for s in extract_variables(get_text(self.template_view) + '\n' + get_text(self.system_view))
                   if not values.get(s.name, '').strip() and not s.default_value]
        summary = f'{metrics.characters} chars · {metrics.words} words · ~{metrics.estimated_tokens} tokens'
        if missing:
            summary += '  ·  unfilled: ' + ', '.join(missing)
        self.metrics.set_text(summary)
        errors = check_syntax_errors(get_text(self.template_view))
        self.syntax_label.set_text(' '.join(errors))
        self.syntax_label.set_visible(bool(errors))

    def copy_hydrated(self):
        system, body = self.hydrated()
        self.copy_text(body if not system.strip() else f'{system.strip()}\n\n{body}')
        if self.current:
            self.repo.increment_use_count(self.current.id)
        self.toast('Prompt copied.')

    def copy_text(self, text):
        self.get_display().get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))

    # --- templates ------------------------------------------------------------
    def ask_save_template(self):
        if not self.current:
            return
        prompt = self.editor_prompt()

        def create(name):
            try:
                self.repo.save_template(PromptTemplate(name=name, description=prompt.description,
                                                       content=prompt.template_content,
                                                       system_instruction=prompt.system_instruction))
            except ValueError as error:
                self.toast(str(error))
                return
            self.set_status(f'Template {name.strip()} saved.')
            self.toast(f'Saved template {name.strip()}.')
        self.ask('Save as template', 'A reusable starting point for new prompts.', 'Save', create, entry_text=prompt.title)

    def reload_templates(self):
        self.templates = self.repo.list_templates()
        clear(self.template_list)
        clear(self.template_detail)
        if not self.templates:
            self.template_list.append(label('No templates yet.', 'muted'))
            self.template_detail.append(label('No templates yet', 'section-title'))
            self.template_detail.append(label('Open a prompt in the Library and choose Save as template.', 'lede', True))
            return
        for template in self.templates:
            row = Gtk.ListBoxRow()
            row.template = template
            content = box(spacing=4, css='prompt-row')
            content.append(label(template.name, 'prompt-title'))
            content.append(label(f'{template.category} · {len(template.variable_specs())} variables', 'muted'))
            row.set_child(content)
            self.template_list.append(row)
        self.template_list.select_row(self.template_list.get_row_at_index(0))

    def on_template_selected(self, _listbox, row):
        template = getattr(row, 'template', None) if row else None
        clear(self.template_detail)
        if not template:
            return
        self.template_detail.append(label(template.name, 'detail-name'))
        if template.description:
            self.template_detail.append(label(template.description, 'lede', True))
        console = box(css='console')
        frame, view = text_area(220, editable=False, css='preview')
        view.get_buffer().set_text((f'[system]\n{template.system_instruction}\n\n' if template.system_instruction else '')
                                   + template.content)
        console.append(frame)
        self.template_detail.append(console)
        actions = box(False, 10)
        actions.append(button('New prompt from template', lambda: self.prompt_from_template(template), css='primary',
                              icon='list-add-symbolic'))
        actions.append(button('Delete template', lambda: self.ask(f'Delete template {template.name}?',
                              'Prompts made from it are kept.', 'Delete',
                              lambda _v: (self.repo.delete_template(template.id), self.reload_templates()),
                              destructive=True), css='secondary destructive', icon='user-trash-symbolic'))
        self.template_detail.append(actions)

    def prompt_from_template(self, template):
        if self.dirty:
            self.save(checkpoint=False, quiet=True)
        prompt = self.repo.create_prompt_from_template(template.id)
        self.reload_list(select_id=prompt.id)
        self.open_prompt(prompt)
        self.navigate('library')
        self.set_status(f'New prompt from {template.name}.')

    # --- history --------------------------------------------------------------
    def reload_revisions(self):
        clear(self.revision_list)
        self.revisions = self.repo.get_revisions(self.current.id) if self.current else []
        self.revision_view.get_buffer().set_text('')
        self.restore_button.set_sensitive(False)
        if not self.revisions:
            self.revision_title.set_text('No revisions yet. Ctrl S saves one.')
            self.revision_list.append(label('No revisions.', 'muted'))
            return
        self.revision_title.set_text('Select a revision.')
        for revision in self.revisions:
            row = Gtk.ListBoxRow()
            row.revision = revision
            content = box(spacing=4, css='prompt-row')
            content.append(label(f'Revision {revision.revision_number}', 'prompt-title'))
            content.append(label(f'{short_time(revision.created_at)} UTC · {revision.title}', 'muted'))
            row.set_child(content)
            self.revision_list.append(row)

    def on_revision_selected(self, _listbox, row):
        revision = getattr(row, 'revision', None) if row else None
        if not revision:
            return
        self.revision_title.set_text(f'Revision {revision.revision_number} · {revision.title}')
        self.revision_view.get_buffer().set_text(
            (f'[system]\n{revision.system_instruction}\n\n' if revision.system_instruction else '') + revision.template_content)
        self.restore_button.set_sensitive(True)
        self.selected_revision = revision

    def restore_revision(self):
        revision = getattr(self, 'selected_revision', None)
        if not revision:
            return
        self.title_entry.set_text(revision.title)
        self.system_view.get_buffer().set_text(revision.system_instruction)
        self.template_view.get_buffer().set_text(revision.template_content)
        self.navigate('library')
        self.set_status(f'Restored revision {revision.revision_number} into the editor; Ctrl S keeps it.')

    # --- run ------------------------------------------------------------------
    def fill_run_models(self):
        models = self.models + self.ollama_models
        names = [m.display_name for m in models]
        for slot, drop in enumerate(self.run_drops):
            previous = drop.get_selected_item().get_string() if drop.get_selected_item() else None
            options = names if slot == 0 else [NO_MODEL] + names
            drop.set_model(Gtk.StringList.new(options))
            if previous in options:
                drop.set_selected(options.index(previous))
            else:
                drop.set_selected(0)
        self.run_models = models

    def chosen_models(self):
        by_name = {m.display_name: m for m in self.run_models}
        chosen = []
        for drop in self.run_drops:
            item = drop.get_selected_item()
            if item and item.get_string() in by_name and by_name[item.get_string()].id not in [m.id for m in chosen]:
                chosen.append(by_name[item.get_string()])
        return chosen

    def update_run_hint(self):
        if self.running:
            return
        system, body = self.hydrated()
        if not body.strip():
            self.run_hint.set_text('The prompt template is empty.')
        else:
            self.run_hint.set_text(f'“{self.title_entry.get_text().strip() or "Untitled Prompt"}” · '
                                   f'~{calculate_metrics(system + body).estimated_tokens} tokens in')

    def discover_ollama(self):
        try:
            base = self.vault.get_provider_config('ollama').get('base_url') or 'http://localhost:11434'
        except CredentialStoreError as error:
            self.toast(str(error))
            return
        self.set_status(f'Looking for Ollama at {base}…')

        def found(models, error):
            self.ollama_models = models or []
            self.fill_run_models()
            self.set_status(f'Found {len(self.ollama_models)} local Ollama models.' if self.ollama_models
                            else f'No Ollama models answered at {base}.')
        self.background(lambda: discover_ollama_models(base), found)

    def run_prompt(self):
        if self.running:
            return
        system, body = self.hydrated()
        models = self.chosen_models()
        if not body.strip():
            self.toast('Write a prompt first.')
            return
        if not models:
            self.toast('Choose at least one model.')
            return
        self.running = True
        self.run_button.set_sensitive(False)
        self.spinner.start()
        clear(self.results)
        self.result_views = []
        for model in models:
            column = box(spacing=8, css='card')
            column.append(label(model.display_name, 'section-title'))
            column.append(label('Waiting for an answer…', 'muted'))
            self.results.append(column)
        names = ', '.join(m.display_name for m in models)
        self.set_status(f'Running on {names}…')
        self.run_hint.set_text('Running…')
        temperature = round(self.temperature.get_value(), 2)
        client = LLMClient(key_vault=self.vault)
        if self.current:
            self.repo.increment_use_count(self.current.id)
        self.background(lambda: run_arena_comparison(body, [m.id for m in models], system_instruction=system,
                                                     temperature=temperature, timeout=RUN_TIMEOUT, client=client),
                        lambda result, error: self.run_finished(models, result, error))

    def run_finished(self, models, result, error):
        self.running = False
        self.run_button.set_sensitive(True)
        self.spinner.stop()
        clear(self.results)
        if error:
            self.set_status(f'Run failed: {error}')
            self.results.append(card('Run failed', str(error)))
            return
        fastest = result.fastest_response
        cheapest = result.cheapest_response
        for model, response in zip(models, result.responses):
            column = box(spacing=10, css='card')
            top = box(False, 8)
            name = label(model.display_name, 'section-title')
            name.set_hexpand(True)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            top.append(name)
            if response.is_success and len(result.responses) > 1 and response is fastest:
                top.append(chip('fastest', 'ready'))
            if (response.is_success and len(result.responses) > 1 and response is cheapest
                    and response.estimated_cost_usd > 0):
                top.append(chip('cheapest', 'ready'))
            if not response.is_success:
                top.append(chip('failed', 'warn'))
            column.append(top)
            if response.is_success:
                stats = f'{response.elapsed_seconds:.1f} s · {response.prompt_tokens} in · {response.completion_tokens} out'
                stats += ' · free' if model.is_free else f' · ~${response.estimated_cost_usd:.4f}'
            else:
                stats = f'No answer after {response.elapsed_seconds:.1f} s'
            column.append(label(stats, 'muted'))
            console = box(css='console')
            frame, view = text_area(320, editable=False, mono=False, css='preview')
            view.get_buffer().set_text(response.content if response.is_success else (response.error or 'Empty answer.'))
            console.append(frame)
            column.append(console)
            if response.is_success:
                column.append(button('Copy answer', lambda t=response.content: (self.copy_text(t), self.toast('Answer copied.')),
                                     icon='edit-copy-symbolic'))
            self.results.append(column)
        ok = sum(1 for r in result.responses if r.is_success)
        self.set_status(f'{ok} of {len(result.responses)} models answered.')
        self.update_run_hint()

    # --- export ---------------------------------------------------------------
    def export_kind(self):
        return EXPORTS[self.export_drop.get_selected()]

    def refresh_export(self):
        if not self.current:
            self.export_view.get_buffer().set_text('')
            return
        kind = self.export_kind()[0]
        system, body = self.hydrated()
        prompt = self.editor_prompt()
        prompt.system_instruction = system
        try:
            text = render_export(kind, prompt, body)
        except Exception as error:
            text = f'Could not export: {error}'
        self.export_view.get_buffer().set_text(text)

    def copy_export(self):
        self.copy_text(get_text(self.export_view))
        self.toast('Export copied.')

    def save_export(self):
        _key, _title, extension = self.export_kind()
        name = ''.join(c if c.isalnum() or c in '-_' else '-' for c in (self.current.title if self.current else 'prompt'))
        text = get_text(self.export_view)
        self.choose_save(f'{name.strip("-").lower() or "prompt"}{extension}',
                         lambda path: (path.write_text(text, encoding='utf-8'), self.toast(f'Saved {path.name}.')))

    def save_library(self, kind):
        if self.dirty:
            self.save(checkpoint=False, quiet=True)
        writers = {'json': (backup.export_library_to_json, 'cube-prompts-library.json'),
                   'csv': (backup.export_library_to_csv, 'cube-prompts-library.csv'),
                   'zip': (backup.export_library_to_markdown_zip, 'cube-prompts-library.zip')}
        write, suggested = writers[kind]

        def chosen(path):
            count = write(self.repo, path)
            self.toast(f'Exported {count} prompts to {path.name}.')
        self.choose_save(suggested, chosen)

    def import_library(self, kind):
        read = backup.import_library_from_json if kind == 'json' else backup.import_library_from_csv

        def chosen(path):
            count = read(self.repo, path)
            self.reload_filters()
            self.reload_list()
            self.toast(f'Imported {count} prompts from {path.name}.')
        dialog = Gtk.FileDialog(title='Import a library')
        file_filter = Gtk.FileFilter()
        file_filter.set_name('JSON backup' if kind == 'json' else 'CSV')
        file_filter.add_suffix(kind)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog.set_filters(filters)

        def picked(_dialog, result):
            try:
                chosen_file = dialog.open_finish(result)
            except GLib.Error:
                return
            if chosen_file and chosen_file.get_path():
                self.guarded(chosen, Path(chosen_file.get_path()))
        dialog.open(self, None, picked)

    def choose_save(self, suggested, then):
        dialog = Gtk.FileDialog(title='Save as', initial_name=suggested)

        def picked(_dialog, result):
            try:
                chosen = dialog.save_finish(result)
            except GLib.Error:
                return
            if chosen and chosen.get_path():
                self.guarded(then, Path(chosen.get_path()))
        dialog.save(self, None, picked)

    def guarded(self, action, path):
        try:
            action(path)
        except Exception as error:
            self.toast(f'{path.name}: {error}')

    # --- settings -------------------------------------------------------------
    def load_settings(self):
        try:
            for provider, entry in self.key_entries.items():
                entry.set_text(self.vault.get_api_key(provider))
            for provider, entry in self.endpoint_entries.items():
                entry.set_text(self.vault.get_provider_config(provider).get('base_url', ''))
            location = self.vault.location
        except CredentialStoreError as error:
            self.key_location.set_text(str(error))
            return
        if location.startswith('/'):
            self.key_location.set_text(f'No OS keyring is running, so keys are kept in {location}, readable only by '
                                       'you (mode 0600), like cube-chat’s key.')
        else:
            self.key_location.set_text(f'Keys are kept in {location}.')

    def save_settings(self):
        try:
            for provider, entry in self.key_entries.items():
                if entry.get_text().strip() != self.vault.get_api_key(provider):
                    self.vault.set_api_key(provider, entry.get_text())
            for provider, entry in self.endpoint_entries.items():
                cfg = self.vault.get_provider_config(provider)
                cfg.pop('api_key', None)
                url = entry.get_text().strip()
                if url != cfg.get('base_url', ''):
                    if url:
                        cfg['base_url'] = url
                    else:
                        cfg.pop('base_url', None)
                    self.vault.set_provider_config(provider, cfg)
        except CredentialStoreError as error:
            self.toast(str(error))
            return
        self.load_settings()
        self.toast('Settings saved.')

    # --- dialogs --------------------------------------------------------------
    def ask(self, heading, body, confirm, on_confirm, entry_text=None, destructive=False):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('ok', confirm)
        dialog.set_default_response('ok')
        dialog.set_close_response('cancel')
        dialog.set_response_appearance('ok', Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                       else Adw.ResponseAppearance.SUGGESTED)
        entry = None
        if entry_text is not None:
            entry = Gtk.Entry()
            entry.set_text(entry_text)
            entry.set_activates_default(True)
            dialog.set_extra_child(entry)

        def respond(_dialog, response):
            if response == 'ok':
                on_confirm(entry.get_text() if entry else None)
        dialog.connect('response', respond)
        dialog.present(self)


class PromptsApp(Adw.Application):
    def __init__(self, options):
        GLib.set_prgname('org.whitecube.CubePrompts')
        super().__init__(application_id='org.whitecube.CubePrompts', flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.options = options
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        provider = Gtk.CssProvider()
        provider.load_from_path(str(HERE / 'style.css'))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    def do_activate(self):
        if not self.window:
            config.ensure_directories()
            self.window = PromptsWindow(self, PromptRepository(Database()))
        self.window.present()
        if self.options.page:
            self.window.navigate(self.options.page)
        if self.options.screenshot:
            GLib.timeout_add(1600, self.screenshot)

    def screenshot(self):
        window = self.window
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, window.get_width(), window.get_height())
        node = snapshot.to_node()
        if node:
            texture = window.get_native().get_renderer().render_texture(node, None)
            texture.save_to_png(str(self.options.screenshot))
        self.quit()
        return False


def main():
    parser = argparse.ArgumentParser(description='Cube Prompts: a prompt library and test bench for Whitecube')
    parser.add_argument('--version', action='version', version=f'cube-prompts {VERSION}')
    parser.add_argument('--screenshot', type=Path, help='Render a preview PNG and exit')
    parser.add_argument('--page', choices=[page[0] for page in PAGES], help='Initial page')
    options = parser.parse_args()
    app = PromptsApp(options)
    if options.screenshot:
        # A preview run must not join (and screenshot) an already open window.
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    return app.run([sys.argv[0]])


if __name__ == '__main__':
    raise SystemExit(main())
