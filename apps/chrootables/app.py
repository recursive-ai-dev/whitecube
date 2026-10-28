#!/usr/bin/python3
"""Chrootables: Debian workspaces for Whitecube, built on chrootable-manager.

The window runs as the desktop user. Reads come straight from the manager when
the store allows it; changes go to a root helper started once with your password
(see backend.py and broker.py), and interactive shells open in a terminal that
authenticates for itself.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shlex
from pathlib import Path
import subprocess
import sys
import threading
import time

# The launcher uses isolated Python; import only this application's directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import backend

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

VERSION = '2.0.0'
PAGES = (
    ('workspaces', 'Workspaces', 'drive-harddisk-symbolic'),
    ('create', 'New workspace', 'list-add-symbolic'),
    ('import', 'Import', 'document-open-symbolic'),
    ('run', 'Run command', 'utilities-terminal-symbolic'),
    ('activity', 'Activity', 'document-open-recent-symbolic'),
    ('health', 'Health', 'security-high-symbolic'),
)
# Shown when the manager cannot be asked (it is missing or broken).
FALLBACK_PROFILES = [
    {'name': 'minimal', 'suggested_mib': 2048, 'minimum_mib': 512, 'description': 'Debian minbase; a clean starting point', 'packages': []},
]
LIFECYCLE_TEXT = {
    'persistent': ('Persistent', 'Every session writes to the image.'),
    'ephemeral': ('Ephemeral', 'Sessions start clean; writes vanish on exit.'),
}
KIND_TEXT = {'all': 'All', 'persistent': 'Persistent', 'ephemeral': 'Ephemeral', 'busy': 'In use'}
IDLE_CHOICES = (5, 15, 30, 60)
AUTO_REFRESH_SECONDS = 15
CONSOLE_LINES = 4000
SHORTCUTS = (
    ('Ctrl 1-6', 'Pages'), ('Ctrl N', 'New workspace'), ('Ctrl E', 'Run a command'),
    ('Ctrl R  /  F5', 'Refresh'), ('Ctrl F', 'Filter workspaces'), ('Ctrl Enter', 'Open a shell in the selection'),
    ('Ctrl Shift S', 'Snapshot the selection'), ('Ctrl `', 'Show or hide the console'),
    ('Ctrl .', 'Cancel the running operation'), ('Ctrl L', 'Lock (stop the root helper)'), ('F1', 'This list'),
)


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


def console_view(css='console'):
    """A read-only monospace TextView inside a black console frame."""
    frame = box(css=css)
    view = Gtk.TextView()
    view.set_editable(False)
    view.set_cursor_visible(False)
    view.set_monospace(True)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    frame.append(view)
    return frame, view


def append_text(view, text, limit=CONSOLE_LINES):
    """Append to a TextView, keep at most LIMIT lines and follow the end."""
    buffer = view.get_buffer()
    buffer.insert(buffer.get_end_iter(), text)
    extra = buffer.get_line_count() - limit
    if extra > 0:
        buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_line(extra)[1])
    mark = buffer.create_mark(None, buffer.get_end_iter(), False)
    view.scroll_to_mark(mark, 0, False, 0, 1)
    buffer.delete_mark(mark)


def clear(container):
    child = container.get_first_child()
    while child:
        following = child.get_next_sibling()
        container.remove(child)
        child = following


def short_time(value):
    """2026-10-10T12:34:56-03:00 -> 2026-10-10 12:34; other text unchanged."""
    if isinstance(value, str) and len(value) >= 16 and value[10:11] == 'T':
        return f'{value[:10]} {value[11:16]}'
    return value or ''


def cube_mark(size=30):
    """The Whitecube cube, drawn as an isometric outline."""
    area = Gtk.DrawingArea()
    area.set_content_width(size)
    area.set_content_height(size)

    def draw(_area, cr, width, height):
        s = min(width, height)
        cx, top, r = width / 2, (height - s) / 2 + 2, s / 2 - 2
        dx, dy = r * math.cos(math.pi / 6), r / 2
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(2)
        cr.move_to(cx, top)
        cr.line_to(cx + dx, top + dy)
        cr.line_to(cx + dx, top + dy + r)
        cr.line_to(cx, top + 2 * r)
        cr.line_to(cx - dx, top + dy + r)
        cr.line_to(cx - dx, top + dy)
        cr.close_path()
        cr.move_to(cx - dx, top + dy)
        cr.line_to(cx, top + 2 * dy)
        cr.line_to(cx + dx, top + dy)
        cr.move_to(cx, top + 2 * dy)
        cr.line_to(cx, top + 2 * r)
        cr.stroke()
    area.set_draw_func(draw)
    return area


class ChrootablesWindow(Adw.ApplicationWindow):
    def __init__(self, app, manager):
        super().__init__(application=app, title='Chrootables')
        self.settings = backend.load_settings()
        self.set_default_size(self.settings['width'], self.settings['height'])
        self.manager = manager
        self.manager.idle = self.settings['idle_minutes'] * 60
        self.manager.on_lock = lambda reason: GLib.idle_add(self.helper_closed, reason)
        self.workspaces = []
        self.selected = self.settings['selected']
        self.profiles = list(FALLBACK_PROFILES)
        self.profile_buttons = {}
        self.busy = False
        self.locked = False
        self.job_started = 0.0
        self.closing = False
        self.cancelling = False
        self.current_job = None
        self.queue = []
        self.action_widgets = []
        self.import_archive = None
        self.free_bytes = None
        self.last_activity = time.monotonic()
        self.snapshot_cache = {}

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        layout = box(spacing=0)
        header = Adw.HeaderBar()
        title = box(False, 8)
        title.append(label('Chrootables', 'title'))
        title.append(label(f'Debian workspaces · {VERSION}', 'muted'))
        header.set_title_widget(title)
        header.pack_end(self.make_preferences())
        header.pack_end(button('', self.show_shortcuts, css='flat', icon='input-keyboard-symbolic', tooltip='Shortcuts (F1)'))
        self.console_toggle = Gtk.ToggleButton()
        self.console_toggle.set_icon_name('utilities-terminal-symbolic')
        self.console_toggle.add_css_class('flat')
        self.console_toggle.set_tooltip_text('Operation console (Ctrl `)')
        self.console_toggle.connect('toggled', lambda b: self.show_console(b.get_active()))
        header.pack_end(self.console_toggle)
        header.pack_end(button('Refresh', self.refresh_all, css='flat', icon='view-refresh-symbolic', tooltip='Reload (Ctrl R)'))
        layout.append(header)

        body = box(False, 0)
        body.set_vexpand(True)
        body.append(self.make_rail())
        main = box(spacing=0)
        main.set_hexpand(True)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.stack.set_vexpand(True)
        builders = {'workspaces': self.make_workspaces, 'create': self.make_create, 'import': self.make_import,
                    'run': self.make_run, 'activity': self.make_activity, 'health': self.make_health}
        for name, _title, _icon in PAGES:
            page = box(spacing=18, css='page')
            builders[name](page)
            self.stack.add_named(scroll(page), name)
        main.append(self.stack)
        main.append(self.make_console())
        body.append(main)
        layout.append(body)

        footer = box(False, 12, 'statusbar')
        self.spinner = Gtk.Spinner()
        footer.append(self.spinner)
        self.status = label('Ready.')
        self.status.set_hexpand(True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        footer.append(self.status)
        self.queue_label = label('', 'chip')
        self.queue_label.set_visible(False)
        footer.append(self.queue_label)
        self.elapsed = label('', 'shortcut')
        footer.append(self.elapsed)
        self.progress = Gtk.ProgressBar()
        self.progress.set_size_request(140, -1)
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.set_visible(False)
        footer.append(self.progress)
        self.cancel_button = button('Cancel', self.ask_cancel, css='secondary small', icon='process-stop-symbolic',
                                    tooltip='Stop the running operation (Ctrl .)')
        self.cancel_button.set_visible(False)
        footer.append(self.cancel_button)
        footer.append(label('F1  shortcuts', 'shortcut'))
        layout.append(footer)
        self.toast_overlay.set_child(layout)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.on_key)
        self.add_controller(keys)
        self.connect('close-request', self.on_close)
        self.navigate(self.settings['page'] if self.settings['page'] in dict((p[0], p) for p in PAGES) else 'workspaces')
        if self.settings['console']:
            self.console_toggle.set_active(True)
        GLib.idle_add(self.initial_load)
        GLib.timeout_add_seconds(AUTO_REFRESH_SECONDS, self.auto_refresh)
        GLib.timeout_add_seconds(20, self.update_access)

    # --- layout ---------------------------------------------------------------
    def make_rail(self):
        rail = box(spacing=12, css='rail')
        rail.set_size_request(250, -1)
        rail.set_hexpand(False)
        brand = box(False, 12)
        brand.append(cube_mark())
        names = box(spacing=2)
        names.append(label('Chrootables', 'rail-name'))
        names.append(label('DEBIAN WORKSPACES', 'rail-caption'))
        brand.append(names)
        rail.append(brand)
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
        store = box(spacing=6, css='rail-project')
        store.append(label('STORE', 'rail-caption'))
        self.store_label = label(os.environ.get('CHROOTABLE_HOME') or str(backend.DEFAULT_STORE), 'project-name')
        self.store_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.store_label.set_max_width_chars(22)
        self.store_label.set_tooltip_text(self.store_label.get_text())
        store.append(self.store_label)
        self.access_label = label('Checking access…', 'dim-label', True)
        self.access_label.set_max_width_chars(24)
        store.append(self.access_label)
        self.unlock_button = button('Unlock', self.unlock, css='primary', icon='changes-allow-symbolic')
        self.unlock_button.set_visible(False)
        store.append(self.unlock_button)
        self.lock_button = button('Lock', self.lock_store, icon='changes-prevent-symbolic',
                                  tooltip='Stop the root helper now; the next change asks for your password (Ctrl L)')
        self.lock_button.set_visible(False)
        store.append(self.lock_button)
        rail.append(store)
        return rail

    def make_preferences(self):
        menu = Gtk.MenuButton()
        menu.set_icon_name('preferences-system-symbolic')
        menu.set_tooltip_text('Preferences')
        menu.add_css_class('flat')
        content = box(spacing=12, css='prefs')
        content.append(label('PREFERENCES', 'eyebrow'))
        row = box(False, 10)
        text = label('Lock after idle', 'recipe-title')
        text.set_hexpand(True)
        row.append(text)
        self.idle_choice = Gtk.DropDown.new_from_strings([f'{m} min' for m in IDLE_CHOICES])
        current = self.settings['idle_minutes']
        self.idle_choice.set_selected(IDLE_CHOICES.index(current) if current in IDLE_CHOICES else 1)
        self.idle_choice.connect('notify::selected', lambda *_: self.idle_changed())
        row.append(self.idle_choice)
        content.append(row)
        content.append(label('Applies from the next unlock. The root helper stops by itself after this long '
                             'without a change.', 'help', True))
        for key, title, hint in (('auto_refresh', 'Refresh every 15 s', 'Notices sessions started from a terminal.'),
                                 ('safety_snapshot', 'Safety snapshots', 'Offer a snapshot before repair, resize and mode changes.')):
            row = box(False, 10)
            texts = box(spacing=2)
            texts.set_hexpand(True)
            texts.append(label(title, 'recipe-title'))
            texts.append(label(hint, 'help', True))
            row.append(texts)
            switch = Gtk.Switch(active=self.settings[key], valign=Gtk.Align.CENTER)
            switch.connect('notify::active', lambda s, _p, k=key: self.set_setting(k, s.get_active()))
            row.append(switch)
            content.append(row)
        content.set_size_request(320, -1)
        popover = Gtk.Popover()
        popover.set_child(content)
        menu.set_popover(popover)
        return menu

    def make_console(self):
        self.console_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        drawer = box(spacing=8, css='drawer')
        top = box(False, 10)
        top.append(label('CONSOLE', 'eyebrow'))
        self.console_title = label('No operation yet.', 'muted')
        self.console_title.set_hexpand(True)
        self.console_title.set_ellipsize(Pango.EllipsizeMode.END)
        top.append(self.console_title)
        top.append(button('Copy', lambda: self.copy_text(self.console.get_buffer().get_property('text'), 'Console copied.'),
                          css='flat small', icon='edit-copy-symbolic'))
        top.append(button('Clear', lambda: self.console.get_buffer().set_text(''), css='flat small', icon='edit-clear-symbolic'))
        drawer.append(top)
        frame, self.console = console_view()
        pane = scroll(frame, 190)
        pane.set_vexpand(False)
        pane.set_size_request(-1, 210)
        drawer.append(pane)
        self.console_revealer.set_child(drawer)
        return self.console_revealer

    def heading(self, page, eyebrow, title, lede):
        page.append(label(eyebrow, 'eyebrow'))
        page.append(label(title, 'page-title'))
        page.append(label(lede, 'lede', True))

    def make_workspaces(self, page):
        self.heading(page, 'DEBIAN WORKSPACES', 'Workspaces', 'Build something. Break things. Keep a recovery point.')
        self.summary = box(spacing=14, css='hero')
        page.append(self.summary)

        self.lock_card = card('Your workspaces are in a private store',
                              'Unlocking starts a small root helper with your password, once. It runs only '
                              'chrootable-manager, stops when you lock, close the window or leave it idle, and '
                              'never stores the password.', css='lock')
        row = box(False, 10)
        self.password = Gtk.PasswordEntry()
        self.password.set_show_peek_icon(True)
        self.password.set_hexpand(True)
        self.password.set_property('placeholder-text', 'Password')
        self.password.connect('activate', lambda *_: self.unlock_with(self.password))
        row.append(self.password)
        row.append(button('Unlock', lambda: self.unlock_with(self.password), css='primary', icon='changes-allow-symbolic'))
        self.lock_card.append(row)
        self.lock_card.set_visible(False)
        page.append(self.lock_card)

        self.empty_card = card('No workspaces yet',
                               'A workspace is a Debian image with its own packages and files. Start from a profile, '
                               'or import an archive exported from another machine.')
        actions = box(False, 10)
        actions.append(button('Create a workspace', lambda: self.navigate('create'), css='primary', icon='list-add-symbolic'))
        actions.append(button('Import an archive', lambda: self.navigate('import'), icon='document-open-symbolic'))
        self.empty_card.append(actions)
        self.empty_card.set_visible(False)
        page.append(self.empty_card)

        self.split = box(False, 18)
        listing = box(spacing=8)
        listing.set_size_request(330, -1)
        self.filter = Gtk.SearchEntry()
        self.filter.set_property('placeholder-text', 'Filter workspaces (Ctrl F)')
        self.filter.connect('search-changed', lambda *_: self.fill_list())
        listing.append(self.filter)
        controls = box(False, 6)
        self.kind_buttons = {}
        first = None
        for kind in backend.KINDS:
            item = Gtk.ToggleButton(label=KIND_TEXT[kind])
            item.add_css_class('segment')
            if first:
                item.set_group(first)
            else:
                first = item
            item.set_active(kind == self.settings['kind'])
            item.connect('toggled', lambda b, k=kind: b.get_active() and self.set_setting('kind', k, self.fill_list))
            self.kind_buttons[kind] = item
            controls.append(item)
        listing.append(controls)
        sort_row = box(False, 8)
        sort_row.append(label('Sort', 'meta-key'))
        self.sort_choice = Gtk.DropDown.new_from_strings([title for title, _k, _r in backend.SORTS.values()])
        self.sort_choice.set_selected(list(backend.SORTS).index(self.settings['sort']))
        self.sort_choice.connect('notify::selected',
                                 lambda d, _p: self.set_setting('sort', list(backend.SORTS)[d.get_selected()], self.fill_list))
        sort_row.append(self.sort_choice)
        self.list_count = label('', 'muted')
        self.list_count.set_hexpand(True)
        self.list_count.set_xalign(1)
        sort_row.append(self.list_count)
        listing.append(sort_row)
        self.workspace_list = Gtk.ListBox()
        self.workspace_list.add_css_class('boxed-list')
        self.workspace_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.workspace_list.connect('row-selected', self.on_row_selected)
        self.workspace_list.connect('row-activated', lambda *_: self.open_session('enter'))
        listing.append(self.workspace_list)
        self.split.append(listing)
        self.detail = box(spacing=16, css='card')
        self.detail.set_hexpand(True)
        self.detail.set_valign(Gtk.Align.START)
        self.split.append(self.detail)
        self.split.set_visible(False)
        page.append(self.split)

    def make_create(self, page):
        self.heading(page, 'NEW WORKSPACE', 'Start with a purpose',
                     'Each profile is Debian minbase plus a focused set of tools. Creation downloads Debian, '
                     'so it takes a few minutes.')
        naming = card('Name')
        self.create_name = Gtk.Entry()
        self.create_name.connect('changed', lambda *_: self.validate_create())
        naming.append(self.create_name)
        self.create_hint = label('Letters, digits, dots, underscores and hyphens; start with a letter or digit.', 'help', True)
        naming.append(self.create_hint)
        page.append(naming)
        page.append(label('Profile', 'section-title'))
        self.profile_flow = Gtk.FlowBox()
        self.profile_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.profile_flow.set_max_children_per_line(4)
        self.profile_flow.set_min_children_per_line(2)
        self.profile_flow.set_column_spacing(14)
        self.profile_flow.set_row_spacing(14)
        self.profile_flow.set_homogeneous(True)
        page.append(self.profile_flow)
        sizing = card('Capacity', 'MiB. The image is fully allocated when it is created; it can grow later but never shrink.')
        self.create_size = Gtk.SpinButton.new_with_range(512, backend.MAX_SIZE_MIB, 512)
        self.create_size.set_halign(Gtk.Align.START)
        self.create_size.connect('value-changed', lambda *_: self.validate_create())
        sizing.append(self.create_size)
        self.create_space = label('', 'help', True)
        sizing.append(self.create_space)
        page.append(sizing)
        self.create_lifecycle = self.lifecycle_picker(page)
        actions = box(False, 12)
        self.create_button = button('Create workspace', self.create_workspace, css='primary', icon='list-add-symbolic')
        actions.append(self.create_button)
        self.create_summary = label('', 'muted', True)
        self.create_summary.set_valign(Gtk.Align.CENTER)
        actions.append(self.create_summary)
        page.append(actions)
        self.fill_profiles()

    def lifecycle_picker(self, page):
        holder = card('Sessions')
        row = box(False, 12)
        buttons = {}
        first = None
        for key in backend.LIFECYCLES:
            title, text = LIFECYCLE_TEXT[key]
            item = Gtk.ToggleButton()
            content = box(spacing=4)
            content.append(label(title, 'recipe-title'))
            content.append(label(text, 'muted', True))
            item.set_child(content)
            item.add_css_class('option')
            item.set_hexpand(True)
            if first:
                item.set_group(first)
            else:
                first = item
            buttons[key] = item
            row.append(item)
        buttons['persistent'].set_active(True)
        holder.append(row)
        page.append(holder)
        return buttons

    def make_import(self, page):
        self.heading(page, 'IMPORT', 'Bring a workspace back',
                     'Exports from this store, or any Debian root filesystem tarball: .tar, .tar.gz, .tar.xz or .tar.zst. '
                     'A .sha256 file beside the archive is verified first.')
        source = card('Archive')
        row = box(False, 12)
        row.append(button('Choose an archive…', self.choose_archive, icon='document-open-symbolic'))
        self.archive_label = label('No archive chosen.', 'muted', True)
        self.archive_label.set_valign(Gtk.Align.CENTER)
        self.archive_label.set_hexpand(True)
        row.append(self.archive_label)
        source.append(row)
        page.append(source)
        page.append(label('Exports in the store', 'section-title'))
        self.export_list = Gtk.ListBox()
        self.export_list.add_css_class('boxed-list')
        self.export_list.set_selection_mode(Gtk.SelectionMode.NONE)
        page.append(self.export_list)
        details = card('New workspace')
        self.import_name = Gtk.Entry()
        self.import_name.set_property('placeholder-text', 'Name')
        self.import_name.connect('changed', lambda *_: self.validate_import())
        details.append(self.import_name)
        details.append(label('Capacity in MiB. It must hold the unpacked files, often several times the archive size.', 'help', True))
        self.import_size = Gtk.SpinButton.new_with_range(256, backend.MAX_SIZE_MIB, 512)
        self.import_size.set_value(4096)
        self.import_size.set_halign(Gtk.Align.START)
        self.import_size.connect('value-changed', lambda *_: self.validate_import())
        details.append(self.import_size)
        page.append(details)
        self.import_lifecycle = self.lifecycle_picker(page)
        actions = box(False, 12)
        self.import_button = button('Import workspace', self.import_workspace, css='primary', icon='document-open-symbolic')
        actions.append(self.import_button)
        self.import_hint = label('', 'muted', True)
        self.import_hint.set_valign(Gtk.Align.CENTER)
        actions.append(self.import_hint)
        page.append(actions)
        self.validate_import()

    def make_run(self, page):
        self.heading(page, 'RUN COMMAND', 'One command, no terminal',
                     'Runs a single shell line inside a workspace and shows its output here. Persistent workspaces '
                     'keep what it writes; ephemeral ones forget it when it exits.')
        form = card()
        row = box(False, 10)
        row.append(label('Workspace', 'meta-key'))
        self.run_target = Gtk.DropDown.new_from_strings([])
        self.run_target.set_size_request(220, -1)
        self.run_target.connect('notify::selected', lambda *_: self.validate_run())
        row.append(self.run_target)
        self.run_offline = Gtk.CheckButton(label='Offline (no network)')
        row.append(self.run_offline)
        self.run_mode = label('', 'muted', True)
        self.run_mode.set_hexpand(True)
        row.append(self.run_mode)
        form.append(row)
        line = box(False, 10)
        self.run_command = Gtk.Entry()
        self.run_command.add_css_class('mono')
        self.run_command.set_hexpand(True)
        self.run_command.set_property('placeholder-text', 'apt-get install -y build-essential   ·   uname -a   ·   ls /srv')
        self.run_command.connect('activate', lambda *_: self.run_now())
        self.run_command.connect('changed', lambda *_: self.validate_run())
        line.append(self.run_command)
        self.run_button = button('Run', self.run_now, css='primary', icon='media-playback-start-symbolic')
        line.append(self.run_button)
        form.append(line)
        self.history_box = Gtk.FlowBox()
        self.history_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.history_box.set_max_children_per_line(4)
        self.history_box.set_column_spacing(6)
        self.history_box.set_row_spacing(6)
        form.append(self.history_box)
        page.append(form)
        top = box(False, 10)
        top.append(label('Output', 'section-title'))
        self.run_status = chip('idle')
        top.append(self.run_status)
        spacer = box()
        spacer.set_hexpand(True)
        top.append(spacer)
        top.append(button('Copy', lambda: self.copy_text(self.run_output.get_buffer().get_property('text'), 'Output copied.'),
                          css='flat small', icon='edit-copy-symbolic'))
        page.append(top)
        frame, self.run_output = console_view()
        self.run_output.set_size_request(-1, 300)
        page.append(frame)
        self.fill_history()

    def make_activity(self, page):
        self.heading(page, 'ACTIVITY', 'Recent operations', 'Every create, snapshot, session and repair, newest first.')
        row = box(False, 10)
        self.activity_filter = Gtk.SearchEntry()
        self.activity_filter.set_property('placeholder-text', 'Workspace or operation')
        self.activity_filter.set_hexpand(True)
        self.activity_filter.connect('search-changed', lambda *_: self.fill_activity())
        row.append(self.activity_filter)
        self.activity_failed = Gtk.CheckButton(label='Problems only')
        self.activity_failed.connect('toggled', lambda *_: self.fill_activity())
        row.append(self.activity_failed)
        page.append(row)
        self.activity_data = []
        self.activity_list = Gtk.ListBox()
        self.activity_list.add_css_class('boxed-list')
        self.activity_list.set_selection_mode(Gtk.SelectionMode.NONE)
        page.append(self.activity_list)

    def make_health(self, page):
        self.heading(page, 'HEALTH', 'Host readiness',
                     'Read-only checks of host tools, kernel support and the Debian source. Sessions run as host root '
                     'and see host devices: a development environment, not a security sandbox.')
        actions = box(False, 10)
        actions.append(button('Run checks again', self.load_health, icon='view-refresh-symbolic'))
        actions.append(button('Find leftovers of interrupted operations', self.review_cleanup, icon='edit-clear-all-symbolic'))
        page.append(actions)
        self.health_summary = box(False, 10)
        page.append(self.health_summary)
        self.health_list = Gtk.ListBox()
        self.health_list.add_css_class('boxed-list')
        self.health_list.set_selection_mode(Gtk.SelectionMode.NONE)
        page.append(self.health_list)
        raw = Gtk.Expander(label='Full report')
        frame, self.health_view = console_view()
        raw.set_child(frame)
        page.append(raw)

    # --- settings ---------------------------------------------------------------
    def set_setting(self, key, value, then=None):
        self.settings[key] = value
        backend.save_settings(self.settings)
        if then:
            then()

    def idle_changed(self):
        minutes = IDLE_CHOICES[self.idle_choice.get_selected()]
        self.manager.idle = minutes * 60
        self.set_setting('idle_minutes', minutes, self.update_access)

    def save_window_state(self):
        width, height = self.get_default_size()
        if not self.is_maximized() and width > 0 and height > 0:
            self.settings['width'], self.settings['height'] = width, height
        self.settings['page'] = self.stack.get_visible_child_name()
        self.settings['selected'] = self.selected
        backend.save_settings(self.settings)

    # --- navigation and shortcuts ---------------------------------------------
    def navigate(self, name):
        self.stack.set_visible_child_name(name)
        for key, item in self.rail_buttons.items():
            if key == name:
                item.add_css_class('selected')
            else:
                item.remove_css_class('selected')
        if name == 'activity':
            self.load_activity()
        elif name == 'import':
            self.load_exports()
        elif name == 'health' and not self.health_view.get_buffer().get_char_count():
            self.load_health()
        elif name == 'create':
            self.suggest_create_name()
        elif name == 'run':
            self.fill_run_targets()

    def on_key(self, _controller, keyval, _keycode, state):
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_F1 or (control and keyval == Gdk.KEY_question):
            self.show_shortcuts()
            return True
        if keyval == Gdk.KEY_F5:
            self.refresh_all()
            return True
        if not control:
            return False
        if Gdk.KEY_1 <= keyval < Gdk.KEY_1 + len(PAGES):
            self.navigate(PAGES[keyval - Gdk.KEY_1][0])
            return True
        if keyval in (Gdk.KEY_s, Gdk.KEY_S) and shift:
            if self.current() and not self.busy:
                self.ask_snapshot()
            return True
        actions = {
            Gdk.KEY_r: self.refresh_all, Gdk.KEY_n: lambda: self.navigate('create'),
            Gdk.KEY_e: lambda: (self.navigate('run'), self.run_command.grab_focus()),
            Gdk.KEY_f: lambda: (self.navigate('workspaces'), self.filter.grab_focus()),
            Gdk.KEY_l: self.lock_store, Gdk.KEY_grave: lambda: self.console_toggle.set_active(not self.console_toggle.get_active()),
            Gdk.KEY_period: self.ask_cancel, Gdk.KEY_Return: lambda: self.open_session('enter'),
        }
        action = actions.get(Gdk.keyval_to_lower(keyval))
        if action:
            action()
            return True
        return False

    def show_shortcuts(self):
        dialog = Adw.AlertDialog.new('Shortcuts', None)
        grid = Gtk.Grid(column_spacing=24, row_spacing=8)
        for row, (keys, text) in enumerate(SHORTCUTS):
            grid.attach(label(keys, 'kbd'), 0, row, 1, 1)
            grid.attach(label(text), 1, row, 1, 1)
        dialog.set_extra_child(grid)
        dialog.add_response('close', 'Close')
        dialog.present(self)

    def on_close(self, *_):
        self.save_window_state()
        if (self.busy or self.queue) and not self.closing:
            # Ending the process mid-operation would leave partial files behind:
            # hide the window and quit once the queue has finished.
            self.closing = True
            self.get_application().hold()
            self.set_visible(False)
            return True
        # The root helper reads our end of its pipe; closing it stops the helper.
        self.manager.on_lock = None
        broker = self.manager.broker
        if broker:
            broker.close()
        return False

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast.new(str(text)[:300]))

    def copy_text(self, text, done='Copied.'):
        self.get_clipboard().set(text)
        self.toast(done)

    def show_console(self, visible):
        self.console_revealer.set_reveal_child(visible)
        if self.settings['console'] != visible:
            self.set_setting('console', visible)

    # --- background work ------------------------------------------------------
    def background(self, work, done):
        """Run work() on a thread and done(result, error) back on the main loop."""
        def run():
            try:
                result, error = work(), None
            except Exception as exc:          # reported in the UI, never swallowed
                result, error = None, exc
            GLib.idle_add(lambda: done(result, error) and False)
        threading.Thread(target=run, daemon=True).start()

    def initial_load(self):
        self.background(self.manager.profiles, self.profiles_loaded)
        self.refresh()
        return False

    def refresh_all(self):
        self.refresh()
        page = self.stack.get_visible_child_name()
        if page == 'activity':
            self.load_activity()
        elif page == 'import':
            self.load_exports()
        elif page == 'health':
            self.load_health()

    def refresh(self, select=None):
        self.background(self.manager.workspaces, lambda data, error: self.workspaces_loaded(data, error, select))

    def auto_refresh(self):
        """Quiet periodic refresh: picks up sessions opened in terminals. Never
        starts the root helper and never counts as activity for its idle lock."""
        if self.closing:
            return False
        if (self.settings['auto_refresh'] and not self.busy and not self.locked and self.get_visible()
                and self.stack.get_visible_child_name() in ('workspaces', 'run')):
            def loaded(data, error):
                if isinstance(error, backend.AuthRequired):
                    self.set_locked(True)
                elif not error and data != self.workspaces:
                    self.workspaces_loaded(data, None)
            self.background(lambda: self.manager.workspaces(passive=True), loaded)
        return True

    def workspaces_loaded(self, data, error, select=None):
        if isinstance(error, backend.AuthRequired):
            self.set_locked(True)
            return
        if error:
            self.set_status(f'Could not read workspaces: {error}')
            self.access_label.set_text(str(error))
            return
        self.set_locked(False)
        self.workspaces = data
        self.free_bytes = backend.store_free_bytes()
        names = {w['name'] for w in data}
        if select in names:
            self.selected = select
        elif self.selected not in names:
            self.selected = data[0]['name'] if data else None
        self.fill_summary()
        self.fill_list()
        self.fill_run_targets()
        self.empty_card.set_visible(not data)
        self.split.set_visible(bool(data))
        self.validate_create()
        self.validate_import()
        if not self.busy:
            self.set_status(f'{len(data)} workspace{"s" if len(data) != 1 else ""}.')

    def set_locked(self, locked):
        self.locked = locked
        self.lock_card.set_visible(locked)
        if locked:
            self.empty_card.set_visible(False)
            self.split.set_visible(False)
            clear(self.summary)
            self.summary.append(label('Locked: unlock to see your workspaces.', 'section-title'))
            self.set_status('Unlock to read the private store.')
        self.update_access()

    def update_access(self):
        """The rail's store card: locked, readable, or root helper running (and when it locks)."""
        if self.closing:
            return False
        helper = self.manager.unlocked()
        idle = (self.manager.idle_seconds() or self.manager.idle) // 60
        self.unlock_button.set_visible(self.locked)
        self.lock_button.set_visible(helper)
        if self.locked:
            text = 'Private store · locked'
        elif helper:
            remaining = max(1, idle - int((time.monotonic() - self.last_activity) // 60)) if idle else 0
            where = 'Private store' if self.manager.private_store else 'Root helper on'
            text = f'{where} · unlocked · locks in about {remaining} min' if idle else f'{where} · unlocked'
        elif self.manager.private_store:
            text = 'Private store · locked'
        else:
            text = 'Readable without a password · changes ask once'
        self.access_label.set_text(text)
        return True

    # --- authentication -------------------------------------------------------
    def helper_closed(self, reason):
        """The root helper exited by itself (idle) or was stopped."""
        if self.closing:
            return False
        if reason == 'idle':
            self.toast(f'Locked after {self.settings["idle_minutes"]} minutes without changes.')
        if self.manager.private_store:
            self.set_locked(True)
        else:
            self.update_access()
        return False

    def lock_store(self):
        if self.busy or self.queue:
            self.set_status('Wait for the running operations to finish before locking.')
            return

        def locked(ok, error):
            if self.manager.private_store:
                self.set_locked(True)
            self.update_access()
            if ok:
                self.set_status('Locked. The next change asks for your password.')
            else:
                self.set_status(f'Root helper stopped, but {error or "sudo still runs without a password (NOPASSWD rule?)"}')
        self.background(self.manager.lock, locked)

    def unlock(self, then=None):
        self.ask_password('Unlock your workspaces', then or self.refresh)

    def unlock_with(self, entry):
        password = entry.get_text()
        entry.set_text('')
        if not password:
            return
        self.set_status('Starting the root helper…')

        def checked(ok, error):
            if ok:
                self.last_activity = time.monotonic()
                self.set_status('Unlocked.')
                self.refresh()
            else:
                self.set_status(str(error) if error else 'Wrong password, or sudo does not allow this account.')
                self.lock_card.add_css_class('shake')
                GLib.timeout_add(400, lambda: self.lock_card.remove_css_class('shake') and False)
            self.update_access()
        self.background(lambda: self.manager.authenticate(password), checked)

    def ask_password(self, heading, then, retry=False, cancelled=None):
        body = ('Chrootables starts a small root helper through sudo. Your password goes to sudo once and is never '
                f'stored; the helper stops when you lock, close the window, or after {self.settings["idle_minutes"]} '
                'idle minutes.' if not retry else 'That password was not accepted. Try again.')
        dialog = Adw.AlertDialog.new(heading, body)
        entry = Gtk.PasswordEntry()
        entry.set_show_peek_icon(True)
        entry.set_property('placeholder-text', 'Password')
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('unlock', 'Unlock')
        dialog.set_response_appearance('unlock', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('unlock')
        dialog.set_close_response('cancel')
        answered = []

        def respond(_dialog, response):
            if answered:
                return
            answered.append(response)
            if response == 'unlock':
                self.password_entered(entry, heading, then, cancelled)
            elif cancelled:
                cancelled()
        entry.connect('activate', lambda *_: (respond(dialog, 'unlock'), dialog.close()))
        dialog.connect('response', respond)
        dialog.present(self)
        entry.grab_focus()
        return dialog

    def password_entered(self, entry, heading, then, cancelled=None):
        password = entry.get_text()
        entry.set_text('')
        if not password:
            if cancelled:
                cancelled()
            return
        self.set_status('Starting the root helper…')

        def checked(ok, error):
            self.update_access()
            if ok:
                self.last_activity = time.monotonic()
                self.set_status('Unlocked.')
                then()
            elif error:
                self.set_status(str(error))
                if cancelled:
                    cancelled()
            else:
                self.ask_password(heading, then, retry=True, cancelled=cancelled)
        self.background(lambda: self.manager.authenticate(password), checked)

    # --- jobs -------------------------------------------------------------------
    def set_status(self, text):
        self.status.set_text(text)

    def set_busy(self, busy, message=None):
        self.busy = busy
        self.cancel_button.set_visible(busy)
        self.cancel_button.set_sensitive(True)
        if busy:
            self.job_started = time.monotonic()
            self.spinner.start()
            self.progress.set_visible(True)
            GLib.timeout_add(120, self.pulse)
            GLib.timeout_add(1000, self.tick)
            self.tick()
        else:
            self.spinner.stop()
            self.progress.set_visible(False)
            self.elapsed.set_text('')
        for widget in self.action_widgets:
            widget.set_sensitive(not busy)
        self.validate_create()
        self.validate_import()
        self.validate_run()
        if message:
            self.set_status(message)

    def pulse(self):
        if self.busy:
            self.progress.pulse()
        return self.busy

    def tick(self):
        if not self.busy:
            return False
        seconds = int(time.monotonic() - self.job_started)
        self.elapsed.set_text(f'{seconds // 60}:{seconds % 60:02d}')
        return True

    def update_queue(self):
        self.queue_label.set_text(f'{len(self.queue)} queued')
        self.queue_label.set_visible(bool(self.queue))

    def start_job(self, title, args, success=None, select=None, merge=False, on_line=None, on_done=None,
                  quiet=False, front=False):
        """Run a privileged manager command, or queue it behind the running one.
        Asks for the password when the root helper is not running."""
        job = dict(title=title, args=args, success=success, select=select, merge=merge, on_line=on_line,
                   on_done=on_done, quiet=quiet)
        if self.busy:
            self.queue.insert(0 if front else len(self.queue), job)
            self.update_queue()
            self.toast(f'Queued: {title}')
            return
        self.run_job(job)

    def next_job(self):
        if self.queue and not self.busy:
            job = self.queue.pop(0)
            self.update_queue()
            self.run_job(job)
        return False

    def drop_queue(self, why):
        if self.queue:
            self.toast(f'{why}: {len(self.queue)} queued operation{"s" if len(self.queue) != 1 else ""} dropped.')
        self.queue.clear()
        self.update_queue()

    def run_job(self, job):
        self.set_busy(True, f'{job["title"]}…')
        self.current_job = job
        self.cancelling = False
        self.console_begin(job['title'], job['args'])

        def line(text):
            GLib.idle_add(self.job_line, job, text)

        def work():
            return self.manager.change(job['args'], on_line=line, merge=job['merge'])

        def done(result, error):
            self.set_busy(False)
            self.current_job = None
            self.last_activity = time.monotonic()
            if isinstance(error, backend.AuthRequired):
                self.console_end('needs password')
                self.set_status('Authentication needed.')
                self.queue.insert(0, job)
                self.update_queue()
                self.ask_password(job['title'], self.next_job, cancelled=lambda: self.drop_queue('Not unlocked'))
                self.update_access()
                return
            status = getattr(error, 'status', 1) if error else 0
            self.console_end('done' if not error else f'cancelled (status {status})' if self.cancelling else f'failed (status {status})')
            if job['on_done']:
                job['on_done'](result, error)
            if error and self.cancelling:
                self.set_status(f'{job["title"]}: cancelled.')
                self.toast(f'{job["title"]} cancelled. Partial files were cleaned up by the manager.')
            elif error and not job['quiet']:
                self.job_failed(job['title'], error)
            elif not error:
                self.set_status(f'{job["title"]}: done.')
                self.toast((job['success'](result) if job['success'] else None) or f'{job["title"]}: done.')
            self.cancelling = False
            self.update_access()
            if self.closing and not self.queue:
                # The window was hidden while this ran; close it for real now.
                self.get_application().release()
                self.close()
                return
            self.snapshot_cache.clear()
            self.refresh(select=job['select'])
            if self.stack.get_visible_child_name() == 'import':
                self.load_exports()
            GLib.idle_add(self.next_job)
        self.background(work, done)

    def job_line(self, job, line):
        if job['on_line']:
            job['on_line'](line)
        append_text(self.console, line + '\n')
        text = line.strip().removeprefix('Error: ')
        if text and not job['merge']:
            self.set_status(text)
        return False

    def console_begin(self, title, args):
        self.console_title.set_text(f'{title} · started {time.strftime("%H:%M:%S")}')
        append_text(self.console, f'\n▌ {time.strftime("%H:%M:%S")}  {title}\n  chrootable-manager {" ".join(args)}\n')

    def console_end(self, outcome):
        seconds = int(time.monotonic() - self.job_started)
        append_text(self.console, f'▌ {outcome} after {seconds // 60}:{seconds % 60:02d}\n')

    def ask_cancel(self):
        if not self.busy or not self.current_job:
            return
        title = self.current_job['title']
        extra = f' {len(self.queue)} queued operation{"s are" if len(self.queue) != 1 else " is"} dropped too.' if self.queue else ''
        self.ask(f'Cancel {title}?', 'The manager stops and removes what this operation had written so far.' + extra,
                 'Stop it', lambda _v: self.cancel_now(), destructive=True)

    def cancel_now(self):
        if not self.busy:
            return
        self.cancelling = True
        self.drop_queue('Cancelled')
        self.cancel_button.set_sensitive(False)
        self.set_status('Cancelling…')
        self.background(self.manager.cancel, lambda ok, error: ok or self.set_status(
            'Nothing to cancel.' if not error else f'Could not cancel: {error}'))

    def job_failed(self, title, error):
        self.set_status(f'{title} failed: {error}')
        dialog = Adw.AlertDialog.new(f'{title} failed', str(error))
        output = getattr(error, 'output', '') or ''
        lines = [line for line in output.strip().splitlines() if line.strip()][-14:]
        if len(lines) > 1:
            frame, view = console_view()
            view.get_buffer().set_text('\n'.join(lines))
            dialog.set_extra_child(scroll(frame, 160))
        dialog.add_response('console', 'Open console')
        dialog.add_response('close', 'Close')
        dialog.set_default_response('close')
        dialog.connect('response', lambda _d, r: r == 'console' and self.console_toggle.set_active(True))
        dialog.present(self)

    # --- workspace list and details ---------------------------------------------
    def fill_summary(self):
        clear(self.summary)
        stats = backend.summarize(self.workspaces)
        free = self.free_bytes
        numbers = box(False, 28)
        for value, caption in ((stats['total'], 'workspaces'), (stats['persistent'], 'persistent'),
                               (stats['ephemeral'], 'disposable'), (stats['busy'], 'in use'),
                               (backend.human_bytes(stats['allocated']), 'in images'), (stats['snapshots'], 'snapshots'),
                               (backend.human_bytes(free) if free is not None else '?', 'host free')):
            block = box(spacing=2)
            block.append(label(str(value), 'stat'))
            block.append(label(caption, 'muted'))
            numbers.append(block)
        self.summary.append(numbers)
        if free is not None:
            total = stats['allocated'] + free
            meter = Gtk.LevelBar(min_value=0, max_value=1)
            meter.set_value(stats['allocated'] / total if total else 0)
            meter.add_css_class('store-meter')
            meter.set_tooltip_text('Images against the free space left on the store’s filesystem')
            self.summary.append(meter)
            if free < 4 * 1073741824:
                self.summary.append(label(f'Low on space: {backend.human_bytes(free)} left on the store.', 'warn-text'))

    def fill_list(self):
        shown = backend.arrange(self.workspaces, self.settings['sort'], self.settings['kind'], self.filter.get_text())
        self.list_count.set_text(f'{len(shown)} of {len(self.workspaces)}')
        clear(self.workspace_list)
        selected_row = None
        for workspace in shown:
            name = workspace['name']
            row = Gtk.ListBoxRow()
            row.workspace = name
            content = box(spacing=6, css='workspace-row')
            top = box(False, 8)
            title = label(name, 'workspace-name')
            title.set_hexpand(True)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            top.append(title)
            busy = workspace.get('status') == 'busy'
            top.append(chip('in use' if busy else 'ready', 'busy' if busy else 'ready'))
            content.append(top)
            facts = f"{workspace.get('lifecycle', '?')} · {workspace.get('profile', '?')} · {workspace.get('size_mib', '?')} MiB"
            if workspace.get('snapshots'):
                facts += f" · {workspace['snapshots']} snapshot{'s' if workspace['snapshots'] != 1 else ''}"
            content.append(label(facts, 'muted'))
            if workspace.get('description'):
                description = label(workspace['description'], 'help')
                description.set_ellipsize(Pango.EllipsizeMode.END)
                content.append(description)
            row.set_child(content)
            self.workspace_list.append(row)
            if name == self.selected:
                selected_row = row
        if selected_row:
            self.workspace_list.select_row(selected_row)
        else:
            self.fill_detail()

    def on_row_selected(self, _listbox, row):
        if row is None:
            return
        if row.workspace != self.selected or not self.detail.get_first_child():
            self.selected = row.workspace
        self.fill_detail()

    def current(self):
        return next((w for w in self.workspaces if w['name'] == self.selected), None)

    def fill_detail(self):
        clear(self.detail)
        self.action_widgets = [w for w in self.action_widgets if w.get_root() is not None and w.get_parent() is not None]
        workspace = self.current()
        if not workspace:
            self.detail.append(label('Select a workspace.', 'lede'))
            return
        name = workspace['name']
        top = box(False, 10)
        title = label(name, 'detail-name', selectable=True)
        title.set_hexpand(True)
        top.append(title)
        busy = workspace.get('status') == 'busy'
        top.append(chip('in use' if busy else 'ready', 'busy' if busy else 'ready'))
        top.append(chip(workspace.get('lifecycle', '?'), 'ephemeral' if workspace.get('lifecycle') == 'ephemeral' else None))
        top.append(chip(workspace.get('profile', '?')))
        self.detail.append(top)
        self.detail.append(label(workspace.get('description') or 'No description yet.', 'lede', True))

        sessions = box(False, 10)
        for text, mode, css, icon in (('Open shell', 'enter', 'primary', 'utilities-terminal-symbolic'),
                                      ('Try disposable', 'try', 'secondary', 'edit-undo-symbolic'),
                                      ('Offline shell', 'offline', 'secondary', 'network-offline-symbolic')):
            item = button(text, lambda m=mode: self.open_session(m), css=css, icon=icon)
            self.action_widgets.append(item)
            sessions.append(item)
        run = button('Run command', lambda: (self.navigate('run'), self.select_run_target(name), self.run_command.grab_focus()),
                     icon='media-playback-start-symbolic')
        sessions.append(run)
        copy = button('', lambda: self.copy_text(shlex.join(['sudo', '--', str(backend.manager_path()), '--enter', name]), 'Shell command copied.'),
                      css='flat', icon='edit-copy-symbolic', tooltip='Copy the terminal command for this workspace')
        sessions.append(copy)
        self.detail.append(sessions)

        grid = Gtk.Grid(column_spacing=28, row_spacing=10)
        facts = [
            ('DEBIAN', f"{workspace.get('suite', 'unknown')} · {workspace.get('architecture', '?')}"),
            ('RELEASE', workspace.get('release') or 'unknown'),
            ('CAPACITY', f"{workspace.get('size_mib', '?')} MiB · {backend.human_bytes(workspace.get('allocated_bytes'))} allocated"),
            ('SNAPSHOTS', str(workspace.get('snapshots', 0))),
            ('CREATED', short_time(workspace.get('created_at'))),
            ('LAST RUN', short_time(workspace.get('last_used_at'))),
        ]
        origin = workspace.get('origin')
        if origin and origin not in ('none', 'unknown'):
            facts.append(('IMPORTED FROM' if workspace.get('profile') == 'imported' else 'CLONE OF', origin))
        if workspace.get('restored_from'):
            facts.append(('RESTORED', f"{workspace['restored_from']} · {short_time(workspace.get('restored_at'))}"))
        for index, (key, value) in enumerate(facts):
            cell = box(spacing=2)
            cell.append(label(key, 'meta-key'))
            text = label(value, 'meta-value', True, selectable=True)
            cell.append(text)
            grid.attach(cell, index % 2, index // 2, 1, 1)
        path = box(spacing=2)
        path.append(label('IMAGE', 'meta-key'))
        path.append(label(workspace.get('path', ''), 'meta-value mono', True, selectable=True))
        grid.attach(path, 0, (len(facts) + 1) // 2, 2, 1)
        self.detail.append(grid)

        tools = Gtk.FlowBox()
        tools.set_selection_mode(Gtk.SelectionMode.NONE)
        tools.set_max_children_per_line(5)
        tools.set_column_spacing(8)
        tools.set_row_spacing(8)
        other = 'ephemeral' if workspace.get('lifecycle') == 'persistent' else 'persistent'
        for text, callback, icon, css in (
                ('Snapshot', self.ask_snapshot, 'camera-photo-symbolic', 'secondary'),
                ('Clone', self.ask_clone, 'edit-copy-symbolic', 'secondary'),
                ('Rename', self.ask_rename, 'document-edit-symbolic', 'secondary'),
                ('Describe', self.ask_describe, 'format-text-plaintext-symbolic', 'secondary'),
                ('Resize', self.ask_resize, 'zoom-fit-best-symbolic', 'secondary'),
                (f'Make {other}', self.ask_mode, 'media-playlist-repeat-symbolic', 'secondary'),
                ('Export', self.ask_export, 'document-save-as-symbolic', 'secondary'),
                ('Latest log', self.show_log, 'text-x-generic-symbolic', 'secondary'),
                ('Repair', self.ask_repair, 'system-run-symbolic', 'secondary'),
                ('Delete', self.ask_delete, 'user-trash-symbolic', 'secondary destructive')):
            item = button(text, callback, css=css, icon=icon)
            self.action_widgets.append(item)
            tools.append(item)
        self.detail.append(tools)

        heading = box(False, 10)
        title = label('Recovery points', 'section-title')
        title.set_hexpand(True)
        heading.append(title)
        prune = button('Keep newest…', self.ask_prune, css='flat small', icon='edit-clear-all-symbolic',
                       tooltip='Delete older snapshots, keeping the newest few')
        prune.set_sensitive(int(workspace.get('snapshots') or 0) > 1 and not self.busy)
        heading.append(prune)
        self.detail.append(heading)
        self.snapshot_box = box(spacing=8)
        self.snapshot_box.append(label('Loading…', 'muted'))
        self.detail.append(self.snapshot_box)
        for widget in self.action_widgets:
            widget.set_sensitive(not self.busy)
        cached = self.snapshot_cache.get(name)
        if cached is not None:
            self.snapshots_loaded(name, cached, None)
        else:
            self.background(lambda: self.manager.snapshots(name), lambda data, error: self.snapshots_loaded(name, data, error))

    def snapshots_loaded(self, name, data, error):
        if name != self.selected or self.snapshot_box.get_root() is None:
            return
        if data is not None:
            self.snapshot_cache[name] = data
        clear(self.snapshot_box)
        if error:
            self.snapshot_box.append(label(f'Could not list snapshots: {error}', 'muted danger', True))
            return
        if not data:
            self.snapshot_box.append(label('No snapshots yet. Take one before a risky change.', 'muted'))
            return
        listing = Gtk.ListBox()
        listing.add_css_class('boxed-list')
        listing.set_selection_mode(Gtk.SelectionMode.NONE)
        for snapshot in sorted(data, key=lambda s: s.get('snapshot_at') or '', reverse=True):
            row = box(False, 10, 'list-row')
            text = box(spacing=2)
            text.set_hexpand(True)
            text.append(label(snapshot['label'], 'recipe-title'))
            text.append(label(f"{short_time(snapshot.get('snapshot_at'))} · {backend.human_bytes(snapshot.get('allocated_bytes'))} on disk", 'muted'))
            row.append(text)
            restore = button('Restore', lambda s=snapshot['label']: self.ask_restore(s), icon='edit-undo-symbolic')
            drop = button('Delete', lambda s=snapshot['label']: self.ask_drop(s), css='flat destructive', icon='user-trash-symbolic')
            for item in (restore, drop):
                item.set_valign(Gtk.Align.CENTER)
                item.set_sensitive(not self.busy)
                self.action_widgets.append(item)
                row.append(item)
            listing.append(row)
        self.snapshot_box.append(listing)

    # --- dialogs for workspace actions ----------------------------------------
    def ask(self, heading, body, confirm, on_confirm, entry_text=None, destructive=False, check=None, spin=None,
            safety=False):
        """A confirmation, optionally with a text entry or a number; on_confirm(value).
        safety adds a "snapshot first" check; on_confirm then gets (value, snapshot_first)."""
        dialog = Adw.AlertDialog.new(heading, body)
        field = None
        extra = box(spacing=10)
        if spin:
            field = Gtk.SpinButton.new_with_range(*spin[:3])
            field.set_value(spin[3])
        elif entry_text is not None:
            field = Gtk.Entry()
            field.set_text(entry_text)
            field.set_activates_default(True)
        if field:
            extra.append(field)
        guard = None
        if safety:
            guard = Gtk.CheckButton(label='Take a safety snapshot first')
            guard.set_active(self.settings['safety_snapshot'])
            extra.append(guard)
        if extra.get_first_child():
            dialog.set_extra_child(extra)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('ok', confirm)
        dialog.set_response_appearance('ok', Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('ok')
        dialog.set_close_response('cancel')
        if check and isinstance(field, Gtk.Entry):
            def update(*_):
                dialog.set_response_enabled('ok', bool(check(field.get_text())))
            field.connect('changed', update)
            update()

        def respond(_dialog, response):
            if response != 'ok':
                return
            if isinstance(field, Gtk.SpinButton):
                value = int(field.get_value())
            elif field is not None:
                value = field.get_text().strip()
            else:
                value = None
            if safety:
                on_confirm(value, guard.get_active())
            else:
                on_confirm(value)
        dialog.connect('response', respond)
        dialog.present(self)
        if field:
            field.grab_focus()
        return dialog

    def action(self, title, action, value=None, success=None, select=None, snapshot_first=False):
        name = self.selected
        try:
            args = backend.workspace_args(action, name, value)
        except ValueError as error:
            self.toast(str(error))
            return
        if snapshot_first:
            label_text = time.strftime(f'before-{action}-%Y%m%d-%H%M%S')
            self.start_job(f'Safety snapshot of {name}', backend.workspace_args('snapshot', name, label_text), select=name,
                           success=lambda _r: f'Saved {label_text}.')
        self.start_job(title, args, success, select)

    def ask_snapshot(self):
        name = self.selected
        self.ask(f'Snapshot {name}', 'An offline recovery point you can restore later. Labels follow the same rules as names.',
                 'Save snapshot', lambda value: self.action(f'Snapshot of {name}', 'snapshot', value, select=name),
                 entry_text=time.strftime('checkpoint-%Y%m%d-%H%M%S'), check=backend.valid_name)

    def ask_clone(self):
        workspace = self.current()
        name = workspace['name']
        taken = [w['name'] for w in self.workspaces]
        warning = backend.space_problem(workspace.get('size_mib') or 0, self.free_bytes)
        self.ask(f'Clone {name}', 'An independent copy. Reflinks make it instant where the filesystem supports them.'
                 + (f'\n\nWithout reflinks: {warning}' if warning else ''),
                 'Clone', lambda value: self.action(f'Clone of {name}', 'clone', value, select=value),
                 entry_text=backend.unique_name(f'{name[:59]}-copy', taken),
                 check=lambda text: backend.valid_name(text) and text not in taken)

    def ask_rename(self):
        name = self.selected
        taken = [w['name'] for w in self.workspaces]
        self.ask(f'Rename {name}', 'Snapshots follow the workspace. Logs keep the old name.', 'Rename',
                 lambda value: self.action(f'Rename of {name}', 'rename', value, select=value),
                 entry_text=name, check=lambda text: backend.valid_name(text) and text not in taken)

    def ask_describe(self):
        workspace = self.current()
        self.ask(f'Describe {workspace["name"]}', 'One line about what this workspace is for. Leave empty to clear it.', 'Save',
                 lambda value: self.action('Description', 'describe', value, select=workspace['name']),
                 entry_text=workspace.get('description') or '', check=backend.valid_description)

    def ask_resize(self):
        workspace = self.current()
        current = int(workspace.get('size_mib') or 0)
        if current >= backend.MAX_SIZE_MIB:
            self.toast('This workspace is already at the maximum size.')
            return
        free = f' {backend.human_bytes(self.free_bytes)} is free on the store.' if self.free_bytes is not None else ''
        self.ask(f'Resize {workspace["name"]}', f'New capacity in MiB. Currently {current} MiB; images grow but never shrink.{free}',
                 'Resize', lambda value, first: self.resize_checked(workspace, current, value, first),
                 spin=(current + 1, backend.MAX_SIZE_MIB, 512, min(current + 1024, backend.MAX_SIZE_MIB)), safety=True)

    def resize_checked(self, workspace, current, value, first):
        problem = backend.space_problem(value - current, self.free_bytes)
        if problem:
            self.toast(problem)
            return
        self.action(f'Resize of {workspace["name"]}', 'resize', value, select=workspace['name'], snapshot_first=first)

    def ask_mode(self):
        workspace = self.current()
        other = 'ephemeral' if workspace.get('lifecycle') == 'persistent' else 'persistent'
        self.ask(f'Make {workspace["name"]} {other}', LIFECYCLE_TEXT[other][1], f'Make {other}',
                 lambda _value, first: self.action('Session mode', 'mode', other, select=workspace['name'], snapshot_first=first),
                 safety=True)

    def ask_export(self):
        name = self.selected
        dialog = Adw.AlertDialog.new(
            f'Export {name}',
            'Archive: the filesystem as a .tar.gz, for importing elsewhere.\n'
            'Live ISO / Disk image: a copy gets a kernel and systemd and boots through a signed UKI '
            '(EFI only, amd64). The ISO keeps changes in RAM; the disk image keeps them. '
            'With Secure Boot on, enroll the store’s secureboot/MOK.cer once per machine.')
        dialog.add_response('cancel', 'Cancel')
        for response, text in (('export', 'Archive'), ('export-iso', 'Live ISO'), ('export-disk', 'Disk image')):
            dialog.add_response(response, text)
        dialog.set_response_appearance('export', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('export')
        dialog.set_close_response('cancel')

        def respond(_dialog, response):
            if response == 'cancel':
                return
            self.action(f'Export of {name}', response, select=name,
                        success=lambda result: f'Exported to {(result.stdout.strip().splitlines() or [''])[-1]}')
        dialog.connect('response', respond)
        dialog.present(self)

    def ask_repair(self):
        name = self.selected
        self.ask(f'Repair {name}?', 'Runs a forced ext4 check and repair. Repair can change data.',
                 'Repair', lambda _value, first: self.action(f'Repair of {name}', 'repair', select=name, snapshot_first=first),
                 destructive=True, safety=True)

    def ask_delete(self):
        name = self.selected
        self.ask(f'Delete {name}?', f'Type {name} to delete its image. Snapshots and exports are kept, so you can restore it later.',
                 'Delete', lambda _value: self.action(f'Deletion of {name}', 'delete'), entry_text='', destructive=True,
                 check=lambda text: text == name)

    def ask_restore(self, snapshot):
        name = self.selected
        self.ask(f'Restore {snapshot}?', 'The current state is saved as a before-restore snapshot first, so this can be undone.',
                 'Restore', lambda _value: self.action(f'Restore of {name}', 'restore', snapshot, select=name))

    def ask_drop(self, snapshot):
        name = self.selected
        self.ask(f'Delete snapshot {snapshot}?', 'This removes only this recovery point.', 'Delete',
                 lambda _value: self.action(f'Deletion of {snapshot}', 'drop-snapshot', snapshot, select=name), destructive=True)

    def ask_prune(self):
        name = self.selected
        snapshots = self.snapshot_cache.get(name) or []
        if len(snapshots) < 2:
            self.toast('Nothing to prune.')
            return

        def prune(keep):
            labels = backend.prune_plan(snapshots, keep)
            if not labels:
                self.toast('Nothing to prune.')
                return
            for snapshot in labels:
                self.start_job(f'Deletion of {snapshot}', backend.workspace_args('drop-snapshot', name, snapshot), select=name)
        self.ask(f'Prune snapshots of {name}', f'Keep only the newest recovery points; the older ones are deleted, oldest '
                 f'first. {len(snapshots)} exist now.', 'Prune', prune, destructive=True,
                 spin=(1, len(snapshots) - 1, 1, min(3, len(snapshots) - 1)))

    def show_log(self, name=None):
        name = name or self.selected

        def shown(text, error):
            if isinstance(error, backend.AuthRequired):
                self.ask_password('Read the operation log', lambda: self.show_log(name))
                return
            dialog = Adw.AlertDialog.new(f'{name} · latest log', None)
            frame, view = console_view()
            view.get_buffer().set_text(str(error) if error else text)
            pane = scroll(frame, 320)
            pane.set_size_request(680, -1)
            dialog.set_extra_child(pane)
            dialog.add_response('copy', 'Copy')
            dialog.add_response('close', 'Close')
            dialog.set_default_response('close')
            dialog.connect('response', lambda _d, r: r == 'copy' and self.copy_text(view.get_buffer().get_property('text'), 'Log copied.'))
            dialog.present(self)
        self.background(lambda: self.manager.logs(name), shown)

    def open_session(self, mode):
        workspace = self.current()
        if not workspace:
            return
        if workspace.get('status') == 'busy':
            self.toast(f'{workspace["name"]} is already in use.')
            return
        try:
            argv = backend.terminal_argv(workspace['name'], mode)
            subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, ValueError) as error:
            self.toast(f'Could not open a terminal: {error}')
            return
        self.set_status(f'Opened a terminal for {workspace["name"]}; sudo asks for your password there.')
        GLib.timeout_add_seconds(4, lambda: self.refresh() and False)

    # --- run a command --------------------------------------------------------------
    def fill_run_targets(self):
        names = [w['name'] for w in self.workspaces]
        model = self.run_target.get_model()
        if [model.get_string(i) for i in range(model.get_n_items())] == names:
            return
        current = self.run_target_name() or self.selected
        self.run_target.set_model(Gtk.StringList.new(names))
        self.select_run_target(current)

    def select_run_target(self, name):
        model = self.run_target.get_model()
        for index in range(model.get_n_items()):
            if model.get_string(index) == name:
                self.run_target.set_selected(index)
                break
        self.validate_run()

    def run_target_name(self):
        item = self.run_target.get_selected_item()
        return item.get_string() if item else None

    def validate_run(self):
        if not hasattr(self, 'run_button'):
            return
        name = self.run_target_name()
        workspace = next((w for w in self.workspaces if w['name'] == name), None)
        if workspace:
            busy = ' · in use by a session' if workspace.get('status') == 'busy' else ''
            self.run_mode.set_text(('Writes stay in the image' if workspace.get('lifecycle') == 'persistent'
                                    else 'Writes vanish when it exits') + busy)
        else:
            self.run_mode.set_text('')
        self.run_button.set_sensitive(bool(workspace and self.run_command.get_text().strip()) and not self.busy)

    def fill_history(self):
        clear(self.history_box)
        for command in self.settings['history'][:8]:
            text = command if len(command) <= 40 else command[:39] + '…'
            item = button(text, lambda c=command: (self.run_command.set_text(c), self.run_command.grab_focus()),
                          css='flat small mono', tooltip=command)
            self.history_box.append(item)

    def run_now(self):
        name = self.run_target_name()
        command = self.run_command.get_text().strip()
        try:
            args = backend.exec_args(name or '', command, self.run_offline.get_active())
        except ValueError as error:
            self.toast(str(error))
            return
        history = [command] + [h for h in self.settings['history'] if h != command]
        self.set_setting('history', history[:20], self.fill_history)
        self.run_output.get_buffer().set_text(f'$ {command}\n')
        self.set_run_status('running', 'busy')

        def finished(result, error):
            if error is None:
                self.set_run_status('exit 0', 'ready')
            elif self.cancelling:
                self.set_run_status('stopped', 'warn')
            else:
                status = getattr(error, 'status', 1)
                self.set_run_status(f'exit {status}', 'warn')
                if not self.run_output.get_buffer().get_char_count() > len(command) + 3:
                    append_text(self.run_output, f'{error}\n')
        self.start_job(f'Run in {name}', args, merge=True, quiet=True, select=name,
                       on_line=lambda line: append_text(self.run_output, line + '\n'), on_done=finished,
                       success=lambda _r: f'{command[:60]}: exit 0')

    def set_run_status(self, text, kind):
        self.run_status.set_text(text)
        for name in ('ready', 'busy', 'warn'):
            self.run_status.remove_css_class(name)
        self.run_status.add_css_class(kind)

    # --- create ---------------------------------------------------------------
    def profiles_loaded(self, data, error):
        if data:
            self.profiles = data
        elif error:
            self.set_status(f'Profiles unavailable: {error}')
        self.fill_profiles()

    def fill_profiles(self):
        clear(self.profile_flow)
        self.profile_buttons = {}
        first = None
        for profile in self.profiles:
            item = Gtk.ToggleButton()
            item.add_css_class('option')
            content = box(spacing=6)
            content.append(label(profile['name'].capitalize(), 'recipe-title'))
            content.append(label(profile.get('description', ''), 'help', True))
            suggested, minimum = profile.get('suggested_mib', 2048), profile.get('minimum_mib', 512)
            content.append(label(f'{backend.human_bytes(suggested * 1048576)} suggested · {minimum} MiB minimum', 'muted'))
            packages = ', '.join(profile.get('packages') or []) or 'nothing beyond minbase'
            content.append(label(packages, 'shortcut', True))
            item.set_child(content)
            if first:
                item.set_group(first)
            else:
                first = item
            item.connect('toggled', lambda widget, p=profile: widget.get_active() and self.profile_chosen(p))
            self.profile_buttons[profile['name']] = item
            self.profile_flow.append(item)
        if first:
            first.set_active(True)
            self.profile_chosen(self.profiles[0])

    def chosen_profile(self):
        for profile in self.profiles:
            item = self.profile_buttons.get(profile['name'])
            if item and item.get_active():
                return profile
        return self.profiles[0] if self.profiles else None

    def profile_chosen(self, profile):
        self.create_size.set_range(profile.get('minimum_mib', 512), backend.MAX_SIZE_MIB)
        self.create_size.set_value(profile.get('suggested_mib', 2048))
        self.suggest_create_name()
        self.validate_create()

    def suggest_create_name(self):
        profile = self.chosen_profile()
        current = self.create_name.get_text()
        taken = [w['name'] for w in self.workspaces]
        generated = not current or any(current.startswith(f"{p['name']}-lab") for p in self.profiles)
        if profile and generated:
            self.create_name.set_text(backend.unique_name(f"{profile['name']}-lab", taken))

    def validate_create(self):
        if not hasattr(self, 'create_button'):
            return
        name = self.create_name.get_text().strip()
        taken = {w['name'] for w in self.workspaces}
        profile = self.chosen_profile()
        size = int(self.create_size.get_value())
        space = backend.space_problem(size, self.free_bytes)
        if not backend.valid_name(name):
            hint, ok = 'Letters, digits, dots, underscores and hyphens; start with a letter or digit (64 at most).', False
        elif name in taken:
            hint, ok = f'A workspace named {name} already exists.', False
        else:
            hint, ok = 'Available.', True
        self.create_hint.set_text(hint)
        self.create_space.set_text(space or (f'{backend.human_bytes(self.free_bytes)} free on the store.'
                                             if self.free_bytes is not None else ''))
        self.create_space.set_css_classes(['help', 'warn-text'] if space else ['help'])
        lifecycle = 'ephemeral' if self.create_lifecycle_active() == 'ephemeral' else 'persistent'
        if profile:
            self.create_summary.set_text(f"{name or '…'} · {profile['name']} · {size} MiB · "
                                         f"{lifecycle} · Debian from the configured mirror")
        self.create_button.set_sensitive(ok and not space and profile is not None and not self.busy and not self.locked)

    def create_lifecycle_active(self):
        buttons = getattr(self, 'create_lifecycle', None)
        return 'ephemeral' if buttons and buttons['ephemeral'].get_active() else 'persistent'

    def create_workspace(self):
        profile = self.chosen_profile()
        name = self.create_name.get_text().strip()
        try:
            args = backend.create_args(name, int(self.create_size.get_value()), self.create_lifecycle_active(),
                                       profile['name'], profile.get('minimum_mib', 1))
        except (ValueError, TypeError) as error:
            self.toast(str(error))
            return
        self.navigate('workspaces')
        self.start_job(f'Creating {name}', args, success=lambda _r: f'Created {name}.', select=name)

    # --- import ---------------------------------------------------------------
    def choose_archive(self):
        dialog = Gtk.FileDialog(title='Choose a root filesystem archive')
        archives = Gtk.FileFilter()
        archives.set_name('Tar archives')
        for pattern in ('*.tar', '*.tar.gz', '*.tgz', '*.tar.xz', '*.txz', '*.tar.zst', '*.tar.bz2'):
            archives.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(archives)
        dialog.set_filters(filters)

        def picked(_dialog, result):
            try:
                chosen = dialog.open_finish(result)
            except GLib.Error:
                return
            if chosen and chosen.get_path():
                self.use_archive(Path(chosen.get_path()))
        dialog.open(self, None, picked)

    def use_archive(self, path, size_bytes=None):
        self.import_archive = Path(path)
        if size_bytes is None:
            try:
                size_bytes = self.import_archive.stat().st_size
            except OSError:
                size_bytes = 0
        self.archive_label.set_text(f'{self.import_archive}  ·  {backend.human_bytes(size_bytes)}')
        taken = [w['name'] for w in self.workspaces]
        self.import_name.set_text(backend.suggested_import_name(self.import_archive, taken))
        self.import_size.set_value(backend.suggested_import_size(size_bytes))
        self.validate_import()

    def validate_import(self):
        if not hasattr(self, 'import_button'):
            return
        name = self.import_name.get_text().strip()
        taken = {w['name'] for w in self.workspaces}
        space = backend.space_problem(int(self.import_size.get_value()), self.free_bytes)
        if not self.import_archive:
            hint, ok = 'Choose an archive first.', False
        elif not backend.valid_name(name):
            hint, ok = 'Enter a valid name.', False
        elif name in taken:
            hint, ok = f'A workspace named {name} already exists.', False
        elif space:
            hint, ok = space, False
        else:
            hint, ok = f'Imports into {name}.', True
        self.import_hint.set_text(hint)
        self.import_button.set_sensitive(ok and not self.busy and not self.locked)

    def import_workspace(self):
        name = self.import_name.get_text().strip()
        lifecycle = 'ephemeral' if self.import_lifecycle['ephemeral'].get_active() else 'persistent'
        try:
            args = backend.import_args(name, self.import_archive, int(self.import_size.get_value()), lifecycle)
        except (ValueError, TypeError) as error:
            self.toast(str(error))
            return
        self.navigate('workspaces')
        self.start_job(f'Importing {name}', args, success=lambda _r: f'Imported {name}.', select=name)

    def load_exports(self):
        def loaded(data, error):
            clear(self.export_list)
            if isinstance(error, backend.AuthRequired):
                self.export_list.append(label('Unlock the store to list its exports.', 'muted'))
                return
            if error or not data:
                self.export_list.append(label(str(error) if error else 'No exports yet. Export a workspace from its details.', 'muted'))
                return
            for item in sorted(data, key=lambda e: e.get('created_at', ''), reverse=True):
                row = box(False, 10, 'list-row')
                text = box(spacing=2)
                text.set_hexpand(True)
                text.append(label(item['file'], 'recipe-title'))
                text.append(label(f"{item.get('workspace', '?')} · {short_time(item.get('created_at'))} · {backend.human_bytes(item.get('size_bytes'))}", 'muted'))
                row.append(text)
                row.append(chip('sha256' if item.get('sha256') else 'no checksum', 'ready' if item.get('sha256') else 'warn'))
                if item.get('kind', 'archive') == 'archive':
                    use = button('Use', lambda e=item: self.use_archive(e['path'], e.get('size_bytes')), icon='go-next-symbolic')
                    use.set_valign(Gtk.Align.CENTER)
                    row.append(use)
                else:
                    row.append(chip('boot ISO' if item['kind'] == 'iso' else 'boot disk', 'ready'))
                self.export_list.append(row)
        self.background(self.manager.exports, loaded)

    # --- activity and health --------------------------------------------------
    def load_activity(self):
        def loaded(data, error):
            if isinstance(error, backend.AuthRequired):
                self.activity_data = None
                self.activity_error = 'Unlock the store to read its activity.'
            elif error:
                self.activity_data = None
                self.activity_error = str(error)
            else:
                self.activity_data = data or []
            self.fill_activity()
        self.background(lambda: self.manager.activity(200), loaded)

    def fill_activity(self):
        clear(self.activity_list)
        if self.activity_data is None:
            self.activity_list.append(label(getattr(self, 'activity_error', ''), 'muted'))
            return
        query = self.activity_filter.get_text().strip().lower()
        problems = self.activity_failed.get_active()
        shown = [e for e in self.activity_data
                 if (not query or query in f"{e.get('operation', '')} {e.get('name', '')}".lower())
                 and (not problems or e.get('status', '') != 'ok')]
        if not shown:
            self.activity_list.append(label('No operations match.' if self.activity_data else 'No operations recorded yet.', 'muted'))
            return
        known = {w['name'] for w in self.workspaces}
        for entry in shown:
            row = box(False, 12, 'list-row')
            when = label(short_time(entry.get('time')), 'shortcut')
            when.set_size_request(120, -1)
            row.append(when)
            what = label(f"{entry.get('operation', '?')} · {entry.get('name', '?')}", 'recipe-title')
            what.set_hexpand(True)
            row.append(what)
            status = entry.get('status', '')
            row.append(chip(status, 'ready' if status == 'ok' else 'warn' if status == 'failed' else None))
            if entry.get('name') in known:
                log = button('', lambda n=entry['name']: self.show_log(n), css='flat small', icon='text-x-generic-symbolic',
                             tooltip=f'Latest log of {entry["name"]}')
                row.append(log)
            self.activity_list.append(row)

    def load_health(self):
        self.health_view.get_buffer().set_text('Running checks…')
        clear(self.health_list)
        clear(self.health_summary)
        self.health_summary.append(label('Running checks…', 'muted'))
        self.background(self.manager.doctor, self.health_loaded)

    def health_loaded(self, text, error):
        text = str(error) if error else text
        self.health_view.get_buffer().set_text(text)
        rows = backend.parse_doctor(text)
        clear(self.health_list)
        clear(self.health_summary)
        counts = {}
        for level, message in rows:
            if not level:
                continue
            counts[level] = counts.get(level, 0) + 1
            row = box(False, 12, 'list-row')
            kind = 'ready' if level == 'OK' else None if level == 'INFO' else 'warn'
            badge = chip(level, kind)
            badge.set_size_request(76, -1)
            row.append(badge)
            row.append(label(message, 'meta-value', True))
            self.health_list.append(row)
        problems = sum(n for level, n in counts.items() if level not in ('OK', 'INFO'))
        self.health_summary.append(chip(f'{counts.get("OK", 0)} ok', 'ready'))
        self.health_summary.append(chip(f'{counts.get("INFO", 0)} info'))
        self.health_summary.append(chip(f'{problems} problem{"s" if problems != 1 else ""}', 'warn' if problems else None))
        if not rows:
            self.health_list.append(label('The manager printed no checks.', 'muted'))

    def review_cleanup(self):
        if self.busy:
            self.toast('Another operation is still running.')
            return
        self.set_busy(True, 'Looking for leftovers…')

        def done(result, error):
            self.set_busy(False)
            if isinstance(error, backend.AuthRequired):
                self.ask_password('Look for leftovers', self.review_cleanup)
                return
            if error:
                self.job_failed('Cleanup review', error)
                return
            listing = result.stdout.strip()
            if not listing:
                self.set_status('No leftovers of interrupted operations.')
                self.toast('Nothing to clean up.')
                return
            self.ask('Remove leftovers?', 'These files belong to operations that stopped without cleaning up. '
                     'No running operation uses them.\n\n' + listing[:1500], 'Remove',
                     lambda _value: self.start_job('Cleanup', ['--cleanup', '--yes']), destructive=True)
        self.background(lambda: self.manager.change(['--cleanup']), done)


def accessibility_css():
    """The desktop's accessibility choices (cube-settings), as for every Cube app."""
    extra = ''
    try:
        state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
        path = state / 'whitecube/accessibility.json'
        if path.stat().st_size <= 1048576:
            settings = json.loads(path.read_text(encoding='utf-8'))
            scale = settings.get('ui_scale', 1)
            if type(scale) in (int, float) and math.isfinite(scale) and .8 <= scale <= 1.6:
                extra += f' window {{ font-size: {round(11 * scale, 2)}pt; }}'
            if settings.get('contrast') == 'strong':
                extra += ' window, .card, entry, list.boxed-list { background: #fff; color: #000; } .muted, .help, .lede { color: #111; }'
            if settings.get('reduced_motion') is True:
                Gtk.Settings.get_default().set_property('gtk-enable-animations', False)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return extra


class ChrootablesApp(Adw.Application):
    def __init__(self, options, manager=None):
        GLib.set_prgname('org.whitecube.Chrootables')
        super().__init__(application_id='org.whitecube.Chrootables', flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.options = options
        self.manager = manager or backend.Manager()
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        provider = Gtk.CssProvider()
        provider.load_from_string((HERE / 'style.css').read_text(encoding='utf-8') + accessibility_css())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    def do_activate(self):
        if not self.window:
            self.window = ChrootablesWindow(self, self.manager)
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
    parser = argparse.ArgumentParser(description='Chrootables: Debian workspaces for Whitecube')
    parser.add_argument('--version', action='version', version=f'chrootables {VERSION}')
    parser.add_argument('--screenshot', type=Path, help='Render a preview PNG and exit')
    parser.add_argument('--page', choices=[page[0] for page in PAGES], help='Initial page')
    options = parser.parse_args()
    return ChrootablesApp(options).run([sys.argv[0]])


if __name__ == '__main__':
    raise SystemExit(main())
