#!/usr/bin/python3
"""Chrootables: Debian workspaces for Whitecube, built on chrootable-manager.

The window runs as the desktop user. Reads come straight from the manager when
the store allows it; changes go through sudo (see backend.py), and interactive
shells open in a terminal that authenticates for itself.
"""
from __future__ import annotations

import argparse
import os
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

VERSION = '1.0.0'
PAGES = (
    ('workspaces', 'Workspaces', 'drive-harddisk-symbolic'),
    ('create', 'New workspace', 'list-add-symbolic'),
    ('import', 'Import', 'document-open-symbolic'),
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


def short_time(value):
    """2026-10-10T12:34:56-03:00 -> 2026-10-10 12:34; other text unchanged."""
    if isinstance(value, str) and len(value) >= 16 and value[10:11] == 'T':
        return f'{value[:10]} {value[11:16]}'
    return value or ''


class ChrootablesWindow(Adw.ApplicationWindow):
    def __init__(self, app, manager):
        super().__init__(application=app, title='Chrootables')
        self.set_default_size(1180, 780)
        self.manager = manager
        self.workspaces = []
        self.selected = None
        self.profiles = list(FALLBACK_PROFILES)
        self.profile_buttons = {}
        self.busy = False
        self.locked = False
        self.job_started = 0.0
        self.closing = False
        self.action_widgets = []
        self.import_archive = None

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        layout = box(spacing=0)
        header = Adw.HeaderBar()
        title = box(False, 8)
        title.append(label('Chrootables', 'title'))
        title.append(label(f'Debian workspaces · {VERSION}', 'muted'))
        header.set_title_widget(title)
        header.pack_end(button('Refresh', self.refresh_all, css='flat', icon='view-refresh-symbolic', tooltip='Reload (Ctrl R)'))
        layout.append(header)

        body = box(False, 0)
        body.set_vexpand(True)
        body.append(self.make_rail())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.stack.set_hexpand(True)
        builders = {'workspaces': self.make_workspaces, 'create': self.make_create, 'import': self.make_import,
                    'activity': self.make_activity, 'health': self.make_health}
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
        self.elapsed = label('', 'shortcut')
        footer.append(self.elapsed)
        self.progress = Gtk.ProgressBar()
        self.progress.set_size_request(140, -1)
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.set_visible(False)
        footer.append(self.progress)
        footer.append(label('Ctrl 1-5  pages  ·  Ctrl N  new  ·  Ctrl R  refresh', 'shortcut'))
        layout.append(footer)
        self.toast_overlay.set_child(layout)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.on_key)
        self.add_controller(keys)
        self.connect('close-request', self.on_close)
        self.navigate('workspaces')
        GLib.idle_add(self.initial_load)

    # --- layout ---------------------------------------------------------------
    def make_rail(self):
        rail = box(spacing=12, css='rail')
        rail.set_size_request(250, -1)
        rail.set_hexpand(False)
        rail.append(label('Chrootables', 'rail-name'))
        rail.append(label('DEBIAN WORKSPACES', 'rail-caption'))
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
        rail.append(store)
        return rail

    def heading(self, page, eyebrow, title, lede):
        page.append(label(eyebrow, 'eyebrow'))
        page.append(label(title, 'page-title'))
        page.append(label(lede, 'lede', True))

    def make_workspaces(self, page):
        self.heading(page, 'DEBIAN WORKSPACES', 'Workspaces', 'Build something. Break things. Keep a recovery point.')
        self.summary = box(False, 28, 'hero')
        page.append(self.summary)

        self.lock_card = card('Your workspaces are in a private store',
                              'Chrootables reads and changes them through sudo. Your password goes to sudo once '
                              'and is never stored.', css='lock')
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
        self.filter.set_property('placeholder-text', 'Filter workspaces')
        self.filter.connect('search-changed', lambda *_: self.fill_list())
        listing.append(self.filter)
        self.workspace_list = Gtk.ListBox()
        self.workspace_list.add_css_class('boxed-list')
        self.workspace_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.workspace_list.connect('row-selected', self.on_row_selected)
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
        self.profile_flow.set_column_spacing(12)
        self.profile_flow.set_row_spacing(12)
        self.profile_flow.set_homogeneous(True)
        page.append(self.profile_flow)
        sizing = card('Capacity', 'MiB. The image is fully allocated when it is created; it can grow later but never shrink.')
        self.create_size = Gtk.SpinButton.new_with_range(512, backend.MAX_SIZE_MIB, 512)
        self.create_size.set_halign(Gtk.Align.START)
        self.create_size.connect('value-changed', lambda *_: self.validate_create())
        sizing.append(self.create_size)
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

    def make_activity(self, page):
        self.heading(page, 'ACTIVITY', 'Recent operations', 'Every create, snapshot, session and repair, newest first.')
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
        console = box(css='console')
        self.health_view = Gtk.TextView()
        self.health_view.set_editable(False)
        self.health_view.set_cursor_visible(False)
        self.health_view.set_monospace(True)
        self.health_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        console.append(self.health_view)
        page.append(console)

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

    def on_key(self, _controller, keyval, _keycode, state):
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return False
        if Gdk.KEY_1 <= keyval < Gdk.KEY_1 + len(PAGES):
            self.navigate(PAGES[keyval - Gdk.KEY_1][0])
            return True
        if keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self.refresh_all()
            return True
        if keyval in (Gdk.KEY_n, Gdk.KEY_N):
            self.navigate('create')
            return True
        return False

    def on_close(self, *_):
        if self.busy and not self.closing:
            # Ending the process mid-operation would leave partial files behind:
            # hide the window and quit once the operation has finished.
            self.closing = True
            self.get_application().hold()
            self.set_visible(False)
            return True
        return False

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast.new(str(text)[:300]))

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
        names = {w['name'] for w in data}
        if select in names:
            self.selected = select
        elif self.selected not in names:
            self.selected = data[0]['name'] if data else None
        self.fill_summary()
        self.fill_list()
        self.empty_card.set_visible(not data)
        self.split.set_visible(bool(data))
        if not self.busy:
            self.set_status(f'{len(data)} workspace{"s" if len(data) != 1 else ""}.')

    def set_locked(self, locked):
        self.locked = locked
        self.lock_card.set_visible(locked)
        self.unlock_button.set_visible(locked)
        if locked:
            self.empty_card.set_visible(False)
            self.split.set_visible(False)
            clear(self.summary)
            self.summary.append(label('Locked: unlock to see your workspaces.', 'section-title'))
            self.access_label.set_text('Private store · locked')
            self.set_status('Unlock to read the private store.')
        else:
            self.access_label.set_text('Private store · unlocked' if self.manager.private_store else 'Readable without a password')

    # --- authentication -------------------------------------------------------
    def unlock(self, then=None):
        self.ask_password('Unlock your workspaces', then or self.refresh)

    def unlock_with(self, entry):
        password = entry.get_text()
        entry.set_text('')
        if not password:
            return
        self.set_status('Checking the password with sudo…')
        self.background(lambda: self.manager.authenticate(password),
                        lambda ok, _error: self.refresh() if ok else self.set_status('Wrong password, or sudo does not allow this account.'))

    def ask_password(self, heading, then, retry=False):
        body = ('Chrootables runs chrootable-manager through sudo. Your password goes to sudo once and is never stored.'
                if not retry else 'That password was not accepted. Try again.')
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
        entry.connect('activate', lambda *_: (dialog.close(), self.password_entered(entry, heading, then)))

        def respond(_dialog, response):
            if response == 'unlock':
                self.password_entered(entry, heading, then)
        dialog.connect('response', respond)
        dialog.present(self)
        entry.grab_focus()
        return dialog

    def password_entered(self, entry, heading, then):
        password = entry.get_text()
        entry.set_text('')
        if not password:
            return
        self.set_status('Checking the password with sudo…')

        def checked(ok, _error):
            if ok:
                self.set_status('Unlocked.')
                then()
            else:
                self.ask_password(heading, then, retry=True)
        self.background(lambda: self.manager.authenticate(password), checked)

    # --- jobs -------------------------------------------------------------------
    def set_status(self, text):
        self.status.set_text(text)

    def set_busy(self, busy, message=None):
        self.busy = busy
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

    def start_job(self, title, args, success=None, select=None):
        """Run a privileged manager command; asks for the password when sudo needs it."""
        if self.busy:
            self.toast('Another operation is still running.')
            return
        self.set_busy(True, f'{title}…')

        def work():
            return self.manager.change(args, on_line=lambda line: GLib.idle_add(self.job_line, line))

        def done(result, error):
            self.set_busy(False)
            if isinstance(error, backend.AuthRequired):
                self.set_status('Authentication needed.')
                self.ask_password(title, lambda: self.start_job(title, args, success, select))
                return
            if error:
                self.job_failed(title, error)
            else:
                self.set_status(f'{title}: done.')
                self.toast((success(result) if success else None) or f'{title}: done.')
            if self.closing:
                # The window was hidden while this ran; close it for real now.
                self.get_application().release()
                self.close()
                return
            self.refresh(select=select)
            if self.stack.get_visible_child_name() == 'import':
                self.load_exports()
        self.background(work, done)

    def job_line(self, line):
        text = line.strip().removeprefix('Error: ')
        if text:
            self.set_status(text)
        return False

    def job_failed(self, title, error):
        self.set_status(f'{title} failed: {error}')
        dialog = Adw.AlertDialog.new(f'{title} failed', str(error))
        output = getattr(error, 'output', '') or ''
        lines = [line for line in output.strip().splitlines() if line.strip()][-14:]
        if len(lines) > 1:
            view = Gtk.TextView()
            view.set_editable(False)
            view.set_monospace(True)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.get_buffer().set_text('\n'.join(lines))
            dialog.set_extra_child(scroll(view, 160))
        dialog.add_response('close', 'Close')
        dialog.present(self)

    # --- workspace list and details ---------------------------------------------
    def fill_summary(self):
        clear(self.summary)
        stats = backend.summarize(self.workspaces)
        free = backend.store_free_bytes()
        for value, caption in ((stats['total'], 'workspaces'), (stats['persistent'], 'persistent'),
                               (stats['ephemeral'], 'disposable'), (stats['busy'], 'busy'),
                               (backend.human_bytes(stats['allocated']), 'in images'), (stats['snapshots'], 'snapshots'),
                               (backend.human_bytes(free) if free is not None else '?', 'host free')):
            block = box(spacing=2)
            block.append(label(str(value), 'stat'))
            block.append(label(caption, 'muted'))
            self.summary.append(block)

    def fill_list(self):
        query = self.filter.get_text().strip().lower()
        clear(self.workspace_list)
        selected_row = None
        for workspace in self.workspaces:
            name = workspace['name']
            if query and query not in name.lower() and query not in (workspace.get('description') or '').lower():
                continue
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

        self.detail.append(label('Recovery points', 'section-title'))
        self.snapshot_box = box(spacing=8)
        self.snapshot_box.append(label('Loading…', 'muted'))
        self.detail.append(self.snapshot_box)
        for widget in self.action_widgets:
            widget.set_sensitive(not self.busy)
        self.background(lambda: self.manager.snapshots(name), lambda data, error: self.snapshots_loaded(name, data, error))

    def snapshots_loaded(self, name, data, error):
        if name != self.selected or self.snapshot_box.get_root() is None:
            return
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
        for snapshot in data:
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
    def ask(self, heading, body, confirm, on_confirm, entry_text=None, destructive=False, check=None, spin=None):
        """A confirmation, optionally with a text entry or a number; on_confirm(value)."""
        dialog = Adw.AlertDialog.new(heading, body)
        field = None
        if spin:
            field = Gtk.SpinButton.new_with_range(*spin[:3])
            field.set_value(spin[3])
        elif entry_text is not None:
            field = Gtk.Entry()
            field.set_text(entry_text)
            field.set_activates_default(True)
        if field:
            dialog.set_extra_child(field)
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
                on_confirm(int(field.get_value()))
            elif field is not None:
                on_confirm(field.get_text().strip())
            else:
                on_confirm(None)
        dialog.connect('response', respond)
        dialog.present(self)
        if field:
            field.grab_focus()
        return dialog

    def action(self, title, action, value=None, success=None, select=None):
        name = self.selected
        try:
            args = backend.workspace_args(action, name, value)
        except ValueError as error:
            self.toast(str(error))
            return
        self.start_job(title, args, success, select)

    def ask_snapshot(self):
        name = self.selected
        self.ask(f'Snapshot {name}', 'An offline recovery point you can restore later. Labels follow the same rules as names.',
                 'Save snapshot', lambda value: self.action(f'Snapshot of {name}', 'snapshot', value, select=name),
                 entry_text=time.strftime('checkpoint-%Y%m%d-%H%M%S'), check=backend.valid_name)

    def ask_clone(self):
        name = self.selected
        taken = [w['name'] for w in self.workspaces]
        self.ask(f'Clone {name}', 'An independent copy. Reflinks make it instant where the filesystem supports them.',
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
        self.ask(f'Resize {workspace["name"]}', f'New capacity in MiB. Currently {current} MiB; images grow but never shrink.',
                 'Resize', lambda value: self.action(f'Resize of {workspace["name"]}', 'resize', value, select=workspace['name']),
                 spin=(current + 1, backend.MAX_SIZE_MIB, 512, min(current + 1024, backend.MAX_SIZE_MIB)))

    def ask_mode(self):
        workspace = self.current()
        other = 'ephemeral' if workspace.get('lifecycle') == 'persistent' else 'persistent'
        self.ask(f'Make {workspace["name"]} {other}', LIFECYCLE_TEXT[other][1], f'Make {other}',
                 lambda _value: self.action('Session mode', 'mode', other, select=workspace['name']))

    def ask_export(self):
        name = self.selected
        self.ask(f'Export {name}', 'Packs the filesystem into a .tar.gz with a .sha256 checksum in the store’s exports directory.',
                 'Export', lambda _value: self.action(f'Export of {name}', 'export', select=name,
                                                      success=lambda result: f'Exported to {result.stdout.strip()}'))

    def ask_repair(self):
        name = self.selected
        self.ask(f'Repair {name}?', 'Runs a forced ext4 check and repair. Repair can change data; take a snapshot first if you need a recovery point.',
                 'Repair', lambda _value: self.action(f'Repair of {name}', 'repair', select=name), destructive=True)

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

    def show_log(self):
        name = self.selected

        def shown(text, error):
            if isinstance(error, backend.AuthRequired):
                self.ask_password('Read the operation log', self.show_log)
                return
            dialog = Adw.AlertDialog.new(f'{name} · latest log', None)
            view = Gtk.TextView()
            view.set_editable(False)
            view.set_monospace(True)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.get_buffer().set_text(str(error) if error else text)
            pane = scroll(view, 320)
            pane.set_size_request(680, -1)
            dialog.set_extra_child(pane)
            dialog.add_response('close', 'Close')
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
        if not backend.valid_name(name):
            hint, ok = 'Letters, digits, dots, underscores and hyphens; start with a letter or digit (64 at most).', False
        elif name in taken:
            hint, ok = f'A workspace named {name} already exists.', False
        else:
            hint, ok = 'Available.', True
        self.create_hint.set_text(hint)
        lifecycle = 'ephemeral' if self.create_lifecycle_active() == 'ephemeral' else 'persistent'
        if profile:
            self.create_summary.set_text(f"{name or '…'} · {profile['name']} · {int(self.create_size.get_value())} MiB · "
                                         f"{lifecycle} · Debian from the configured mirror")
        self.create_button.set_sensitive(ok and profile is not None and not self.busy and not self.locked)

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
        if not self.import_archive:
            hint, ok = 'Choose an archive first.', False
        elif not backend.valid_name(name):
            hint, ok = 'Enter a valid name.', False
        elif name in taken:
            hint, ok = f'A workspace named {name} already exists.', False
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
                use = button('Use', lambda e=item: self.use_archive(e['path'], e.get('size_bytes')), icon='go-next-symbolic')
                use.set_valign(Gtk.Align.CENTER)
                row.append(use)
                self.export_list.append(row)
        self.background(self.manager.exports, loaded)

    # --- activity and health --------------------------------------------------
    def load_activity(self):
        def loaded(data, error):
            clear(self.activity_list)
            if isinstance(error, backend.AuthRequired):
                self.activity_list.append(label('Unlock the store to read its activity.', 'muted'))
                return
            if error or not data:
                self.activity_list.append(label(str(error) if error else 'No operations recorded yet.', 'muted'))
                return
            for entry in data:
                row = box(False, 12, 'list-row')
                when = label(short_time(entry.get('time')), 'shortcut')
                when.set_size_request(120, -1)
                row.append(when)
                what = label(f"{entry.get('operation', '?')} · {entry.get('name', '?')}", 'recipe-title')
                what.set_hexpand(True)
                row.append(what)
                status = entry.get('status', '')
                row.append(chip(status, 'ready' if status == 'ok' else 'warn' if status == 'failed' else None))
                self.activity_list.append(row)
        self.background(lambda: self.manager.activity(200), loaded)

    def load_health(self):
        self.health_view.get_buffer().set_text('Running checks…')
        self.background(self.manager.doctor, lambda text, error: self.health_view.get_buffer().set_text(str(error) if error else text))

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
        provider.load_from_path(str(HERE / 'style.css'))
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
