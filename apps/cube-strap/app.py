#!/usr/bin/python3 -I
"""Cube Strap: build Debian whitecubes — Chrootables workspaces or bootable VMs — with deb-strap.

The window runs as the desktop user. The build is one sudo call (cube-strap-build); a
password typed here goes to `sudo -S -v` once through the Chrootables backend and is
never kept. Chroot cubes are imported with chrootable-manager, VM cubes with `whitecube import`.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'cube-apps'))
sys.path.insert(0, str(HERE.parent / 'chrootables'))
import strap
import backend
from cube_theme import Adw, Gdk, Gio, GLib, Gtk, apply_theme, screenshot

VERSION = '1.0.0'
APP_ID = 'org.whitecube.CubeStrap'
KIND_TEXT = {
    'chroot': ('Chroot workspace', 'Shares this kernel. Opens in Chrootables; enter it in a terminal.'),
    'vm': ('Virtual machine', 'Its own kernel and UEFI disk. Boots in QEMU through whitecube.'),
}


RAIL_CSS = '''
.cube-rail { background: #111; padding: 18px 12px; }
.cube-rail label { color: #f2f2f2; }
.cube-rail .rail-caption { font-size: 8px; font-weight: 800; letter-spacing: 3px; color: #9a9a9a; }
.cube-rail .rail-name { font-size: 19px; font-weight: 900; letter-spacing: -1px; }
.cube-rail button { background: transparent; color: #d8d8d8; border: 2px solid transparent; padding: 7px 10px; }
.cube-rail button label { color: inherit; }
.cube-rail button:hover { background: #222; border-color: #f2f2f2; }
.cube-rail button.selected { background: #f2f2f2; color: #111; border-color: #f2f2f2; }
'''


def draw_cube(context, x, y, size, rgb=(.95, .95, .95), line=2):
    """Isometric wireframe cube, the desktop's mark."""
    half, quarter = size / 2, size / 4
    top = [(x + half, y), (x + size, y + quarter), (x + half, y + half), (x, y + quarter)]
    context.set_source_rgb(*rgb); context.set_line_width(line)
    context.move_to(*top[0])
    for point in top[1:]:
        context.line_to(*point)
    context.close_path()
    for px, py in top[1:]:
        context.move_to(px, py); context.line_to(px, py + half)
    context.move_to(x, y + quarter + half); context.line_to(x + half, y + size); context.line_to(x + size, y + quarter + half)
    context.stroke()


def label(text='', css=None):
    widget = Gtk.Label(label=text, xalign=0, wrap=True)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    return widget


def box(vertical=True, spacing=10):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def button(text, callback, primary=False):
    widget = Gtk.Button(label=text)
    if primary:
        widget.add_css_class('suggested-action')
    widget.connect('clicked', lambda *_: callback())
    return widget


class StrapWindow(Adw.ApplicationWindow):
    def __init__(self, application, manager):
        super().__init__(application=application, title='Cube Strap', default_width=980, default_height=760)
        self.manager = manager
        self.closed = False
        self.process = None
        self.building = False
        self.cancelled = False
        self.workspaces = set()
        self.recipe_keys = []
        draft = strap.load_draft()
        self.plan = strap.plan_from(draft)
        self.toast = Adw.ToastOverlay()
        self.set_content(self.toast)
        outer = box(False, 0)
        self.toast.set_child(outer)
        self.rail = box(spacing=6); self.rail.add_css_class('cube-rail'); self.rail.set_size_request(168, -1)
        outer.append(self.rail)
        layout = box(spacing=0); layout.set_hexpand(True)
        outer.append(layout)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title='Cube Strap', subtitle='Debian whitecubes, bootstrapped'))
        header.pack_end(button('Refresh', self.refresh))
        layout.append(header)
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        layout.append(self.stack)
        self.build_page()
        self.cubes_page()
        self.log_page()
        self.build_rail()
        footer = box(False, 12)
        footer.set_margin_top(12); footer.set_margin_bottom(6)
        footer.set_margin_start(18); footer.set_margin_end(18)
        self.status = label('Ready', 'cube-status'); self.status.set_hexpand(True)
        self.cancel_button = button('Cancel', self.cancel)
        self.cancel_button.set_sensitive(False)
        self.build_button = button('Build cube', self.start, primary=True)
        for widget in (self.status, self.cancel_button, self.build_button):
            footer.append(widget)
        layout.append(footer)
        self.progress = Gtk.ProgressBar(show_text=True, text='')
        self.progress.set_margin_start(18); self.progress.set_margin_end(18); self.progress.set_margin_bottom(12)
        layout.append(self.progress)
        controller = Gtk.EventControllerKey()
        controller.connect('key-pressed', self.key_pressed)
        self.add_controller(controller)
        self.connect('close-request', self.closing)
        self.load_form()
        self.refresh()

    # --- layout -----------------------------------------------------------------
    def build_rail(self):
        """Black navigation rail with the cube mark, as in Chrootables and the Trainer."""
        mark = Gtk.DrawingArea(content_width=40, content_height=40, halign=Gtk.Align.START)
        mark.set_draw_func(lambda _, context, *__: draw_cube(context, 2, 2, 36))
        self.rail.append(mark)
        for text, css in (('WHITECUBE', 'rail-caption'), ('Cube Strap', 'rail-name')):
            widget = Gtk.Label(label=text, xalign=0); widget.add_css_class(css); self.rail.append(widget)
        self.rail.get_last_child().set_margin_bottom(14)
        self.rail_buttons = {}
        pages = self.stack.get_pages()
        for index in range(pages.get_n_items()):
            page = pages.get_item(index)
            widget = Gtk.Button()
            widget.set_child(Gtk.Label(label=f'{index + 1:02d}   {page.get_title()}', xalign=0))
            widget.connect('clicked', lambda _, name=page.get_name(): self.stack.set_visible_child_name(name))
            self.rail_buttons[page.get_name()] = widget; self.rail.append(widget)
        spacer = box(); spacer.set_vexpand(True); self.rail.append(spacer)
        caption = Gtk.Label(label='debian · bootstrapped', xalign=0); caption.add_css_class('rail-caption'); self.rail.append(caption)
        self.stack.connect('notify::visible-child-name', lambda *_: self.rail_selection())
        self.rail_selection()

    def rail_selection(self):
        current = self.stack.get_visible_child_name()
        for name, widget in self.rail_buttons.items():
            (widget.add_css_class if name == current else widget.remove_css_class)('selected')

    def page(self, name, title):
        page = box(spacing=16)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(page, setter)(22)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_child(page)
        self.stack.add_titled(scroll, name, title)
        return page

    def field(self, parent, title, control, hint=None):
        row = box(spacing=5)
        caption = label(title, 'heading')
        caption.set_mnemonic_widget(control)
        row.append(caption)
        row.append(control)
        if hint:
            row.append(label(hint, 'dim-label'))
        parent.append(row)
        return row

    def build_page(self):
        page = self.page('build', 'Build')
        kinds = box(False, 12)
        kinds.set_homogeneous(True)
        self.kind_buttons = {}
        group = None
        for kind, (title, detail) in KIND_TEXT.items():
            toggle = Gtk.ToggleButton(group=group)
            group = group or toggle
            content = box(spacing=4)
            content.set_margin_top(8); content.set_margin_bottom(8)
            content.append(label(title, 'title-4'))
            content.append(label(detail))
            toggle.set_child(content)
            toggle.connect('toggled', self.kind_changed)
            self.kind_buttons[kind] = toggle
            kinds.append(toggle)
        self.field(page, 'Kind of cube', kinds)
        self.name = Gtk.Entry(placeholder_text='devbox', hexpand=True)
        self.name.connect('changed', self.form_changed)
        self.field(page, 'Name', self.name, 'Letters, digits, dots, underscores and hyphens.')
        columns = box(False, 18)
        columns.set_homogeneous(True)
        left, right = box(spacing=16), box(spacing=16)
        columns.append(left); columns.append(right)
        page.append(columns)
        self.suite = Gtk.DropDown.new_from_strings([f'{title} ({role})' for _key, title, role in strap.SUITES])
        self.suite.connect('notify::selected', self.form_changed)
        self.field(left, 'Debian release', self.suite)
        self.recipe = Gtk.DropDown.new_from_strings([''])
        self.recipe.connect('notify::selected', self.form_changed)
        self.recipe_detail = label('', 'dim-label')
        row = self.field(left, 'Recipe', self.recipe)
        row.append(self.recipe_detail)
        self.size = Gtk.SpinButton.new_with_range(1, strap.MAX_SIZE_GIB, 1)
        self.size.set_numeric(True)
        self.size.connect('value-changed', self.form_changed)
        self.size_hint = label('', 'dim-label')
        row = self.field(left, 'Size (GiB)', self.size)
        row.append(self.size_hint)
        self.username = Gtk.Entry(hexpand=True)
        self.username.connect('changed', self.form_changed)
        self.field(right, 'User', self.username, 'Gets sudo inside the cube. Root stays locked.')
        self.password = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.password.connect('changed', self.form_changed)
        self.field(right, 'Password for that user', self.password, 'Used once for the build; never saved.')
        self.hostname = Gtk.Entry(hexpand=True)
        self.hostname.connect('changed', self.form_changed)
        self.field(right, 'Host name', self.hostname)
        self.lifecycle = Gtk.DropDown.new_from_strings(['Persistent: sessions keep their changes',
                                                        'Ephemeral: every session starts clean'])
        self.lifecycle.connect('notify::selected', self.form_changed)
        self.lifecycle_row = self.field(right, 'Sessions', self.lifecycle)
        self.boot_after = Gtk.CheckButton(label='Boot it when the build finishes', active=True)
        self.vm_row = box(spacing=5)
        self.vm_row.append(label('After building', 'heading'))
        self.vm_row.append(self.boot_after)
        right.append(self.vm_row)
        advanced = Gtk.Expander(label='Advanced')
        inner = box(spacing=12)
        inner.set_margin_top(10)
        self.mirror = Gtk.Entry(hexpand=True)
        self.mirror.connect('changed', self.form_changed)
        self.field(inner, 'Debian mirror', self.mirror)
        advanced.set_child(inner)
        page.append(advanced)
        self.problem = label('', 'cube-bad')
        self.problem.set_visible(False)
        page.append(self.problem)
        self.summary = label('', 'dim-label')
        page.append(self.summary)

    def cubes_page(self):
        page = self.page('cubes', 'Cubes')
        page.append(label('Cubes built here. Chroot cubes live in Chrootables; VM cubes in the whitecube store.',
                          'dim-label'))
        self.cube_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.cube_list.add_css_class('boxed-list')
        page.append(self.cube_list)

    def log_page(self):
        page = self.page('log', 'Log')
        actions = box(False, 8)
        actions.append(button('Copy log', self.copy_log))
        page.append(actions)
        self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                     vexpand=True, cursor_visible=False)
        self.log_view.add_css_class('cube-hash')
        frame = Gtk.ScrolledWindow(vexpand=True, min_content_height=420)
        frame.set_child(self.log_view)
        self.log_scroll = frame
        page.append(frame)

    # --- form ---------------------------------------------------------------------
    def load_form(self):
        p = self.plan
        self.loading = True
        self.kind_buttons.get(p.kind, self.kind_buttons['chroot']).set_active(True)
        self.name.set_text(p.name)
        keys = [key for key, *_ in strap.SUITES]
        self.suite.set_selected(keys.index(p.suite) if p.suite in keys else 0)
        self.username.set_text(p.username)
        self.hostname.set_text(p.hostname)
        self.mirror.set_text(p.mirror)
        self.lifecycle.set_selected(strap.LIFECYCLES.index(p.lifecycle) if p.lifecycle in strap.LIFECYCLES else 0)
        self.loading = False
        self.update_kind(p.recipe, p.size_gib)

    def kind(self):
        return next((kind for kind, toggle in self.kind_buttons.items() if toggle.get_active()), 'chroot')

    def kind_changed(self, toggle):
        if toggle.get_active() and not getattr(self, 'loading', True):
            self.update_kind(None, None)

    def update_kind(self, recipe, size):
        kind = self.kind()
        self.loading = True
        self.recipe_keys = strap.recipes_for(kind)
        self.recipe.set_model(Gtk.StringList.new([strap.RECIPES[key][0] for key in self.recipe_keys]))
        self.recipe.set_selected(self.recipe_keys.index(recipe) if recipe in self.recipe_keys else 0)
        self.size.set_range(strap.MIN_SIZE_GIB[kind], strap.MAX_SIZE_GIB)
        self.size.set_value(size or strap.DEFAULT_SIZE_GIB[kind])
        self.size_hint.set_text('Room for the workspace files; Chrootables can grow it later.' if kind == 'chroot'
                                else 'Virtual disk size. Stored sparse: only used blocks take space.')
        self.lifecycle_row.set_visible(kind == 'chroot')
        self.vm_row.set_visible(kind == 'vm')
        self.loading = False
        self.form_changed()

    def current_plan(self):
        index = self.recipe.get_selected()
        return strap.Plan(
            name=self.name.get_text().strip(), kind=self.kind(),
            suite=strap.SUITES[min(self.suite.get_selected(), len(strap.SUITES) - 1)][0],
            recipe=self.recipe_keys[index] if index < len(self.recipe_keys) else '',
            size_gib=int(self.size.get_value()), username=self.username.get_text().strip(),
            password=self.password.get_text(), hostname=self.hostname.get_text().strip(),
            lifecycle=strap.LIFECYCLES[min(self.lifecycle.get_selected(), 1)],
            mirror=self.mirror.get_text().strip())

    def taken(self, kind):
        if kind == 'chroot':
            return self.workspaces
        store = strap.whitecube_store() / 'images'
        try:
            return {entry.name for entry in store.iterdir()}
        except OSError:
            return set()

    def form_changed(self, *_):
        if getattr(self, 'loading', True):
            return
        plan = self.current_plan()
        index = self.recipe.get_selected()
        if index < len(self.recipe_keys):
            self.recipe_detail.set_text(strap.RECIPES[self.recipe_keys[index]][1])
        self.hostname.set_placeholder_text(strap.hostname_for(plan.name or 'cube'))
        found = strap.problems(plan, self.taken(plan.kind))
        self.problem.set_text(found[0] if found else '')
        self.problem.set_visible(bool(found) and bool(plan.name))
        self.build_button.set_sensitive(not found and not self.building)
        where = ('imported into Chrootables' if plan.kind == 'chroot'
                 else 'added to the whitecube store as a UEFI disk')
        self.summary.set_text(f'{plan.name or "This cube"}: {strap.RECIPES.get(plan.recipe, ("?",))[0]} '
                              f'{plan.suite}, {plan.size_gib} GiB, {where}.')
        strap.save_draft(plan)

    # --- cubes list -----------------------------------------------------------------
    def refresh(self):
        def load():
            try:
                return {item.get('name') for item in self.manager.workspaces()}
            except (backend.ManagerError, OSError, ValueError):
                return None
        self.background(load, self.workspaces_loaded)
        self.fill_cubes()

    def workspaces_loaded(self, names, _error):
        if names is not None:
            self.workspaces = names
        self.fill_cubes()
        self.form_changed()

    def fill_cubes(self):
        while (child := self.cube_list.get_first_child()):
            self.cube_list.remove(child)
        items = list(reversed(strap.load_history()))
        if not items:
            self.cube_list.append(Adw.ActionRow(title='No cubes yet', subtitle='Build one on the Build page.'))
            return
        for item in items:
            name, kind = item['name'], item.get('kind')
            title = strap.RECIPES.get(item.get('recipe'), ('?',))[0]
            state = item.get('status', '?')
            if kind == 'vm' and state == 'built' and not strap.vm_exists(name):
                state = 'removed from the store'
            row = Adw.ActionRow(title=name, subtitle=f'{KIND_TEXT.get(kind, ("?",))[0]} · {item.get("suite")} · '
                                f'{title} · {item.get("size_gib")} GiB · {state} {item.get("finished", "")}')
            if state == 'built' and kind == 'vm':
                row.add_suffix(self.row_button('Boot', lambda n=name, i=item: self.boot(n, i.get('recipe') == 'server')))
            if state == 'built' and kind == 'chroot':
                row.add_suffix(self.row_button('Shell', lambda n=name: self.shell(n)))
                row.add_suffix(self.row_button('Chrootables', self.open_chrootables))
            row.add_suffix(self.row_button('Forget', lambda n=name, k=kind: self.forget(n, k)))
            self.cube_list.append(row)

    @staticmethod
    def row_button(text, callback):
        widget = button(text, callback)
        widget.set_valign(Gtk.Align.CENTER)
        return widget

    def forget(self, name, kind):
        strap.forget(name, kind)
        self.fill_cubes()
        self.notify(f'{name} removed from this list (the cube itself is untouched).')

    def boot(self, name, forward_ssh=False):
        if not strap.whitecube_command():
            self.notify('The whitecube image tool is not installed.')
            return
        self.spawn(strap.vm_run_argv(name, forward_ssh=forward_ssh))
        self.notify(f'Booting {name}' + (' (ssh -p 2222 localhost)' if forward_ssh else ''))

    def shell(self, name):
        self.spawn(backend.terminal_argv(name, 'enter'))

    def open_chrootables(self):
        self.spawn([shutil.which('chrootables') or 'chrootables'])

    def spawn(self, argv):
        try:
            subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            self.notify(f'Could not start {Path(argv[0]).name}: {error.strerror}')

    # --- building ----------------------------------------------------------------------
    def start(self):
        if self.building:
            return
        plan = self.current_plan()
        found = strap.problems(plan, self.taken(plan.kind))
        if found:
            self.notify(found[0])
            return
        if plan.kind == 'vm' and not strap.whitecube_command():
            self.notify('VM cubes need the whitecube image tool (package "whitecube").')
            return
        self.background(self.manager.authenticated,
                        lambda ok, _e: self.run_build(plan) if ok else self.ask_password(plan))

    def ask_password(self, plan):
        dialog = Adw.AlertDialog(heading='Unlock to build',
                                 body='Building a cube needs administrator rights (debootstrap, loop devices '
                                      'and mounts). Enter your password for sudo.')
        entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('unlock', 'Unlock')
        dialog.set_response_appearance('unlock', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('unlock')

        def response(_dialog, answer):
            secret = entry.get_text()
            entry.set_text('')
            if answer != 'unlock':
                return

            def checked(ok, error):
                if error:
                    self.notify(str(error))
                elif ok:
                    self.run_build(plan)
                else:
                    self.notify('sudo did not accept that password.')
            self.background(lambda: self.manager.authenticate(secret), checked)
        dialog.connect('response', response)
        dialog.present(self)

    def set_building(self, building):
        self.building = building
        self.cancel_button.set_sensitive(building)
        self.build_button.set_sensitive(not building)
        for toggle in self.kind_buttons.values():
            toggle.set_sensitive(not building)

    def run_build(self, plan):
        self.set_building(True)
        self.cancelled = False
        self.log_view.get_buffer().set_text('')
        self.stack.set_visible_child_name('log')
        self.set_progress(0.01, 'Starting')
        self.password.set_text('')
        threading.Thread(target=self.build_worker, args=(plan,), daemon=True).start()

    def build_worker(self, plan):
        started = time.monotonic()
        workdir = None
        status, detail = 'failed', ''
        try:
            workdir = strap.new_workdir()
            config = strap.write_config(plan, workdir)
            self.say(f'Cube Strap {VERSION}: {plan.kind} cube {plan.name} ({plan.suite}, {plan.recipe})')
            code = self.stream(strap.build_argv(plan, config, workdir, self.manager.sudo))
            if self.cancelled:
                status, detail = 'cancelled', 'Cancelled'
                return
            if code != 0:
                detail = f'The build failed (status {code}). See the log.'
                return
            self.step(0.93, 'Importing')
            if plan.kind == 'chroot':
                archive = workdir / f'{plan.name}.tar.xz'
                self.manager.change(strap.import_args(plan, archive), on_line=self.say)
            else:
                image = workdir / f'{plan.name}.img'
                code = self.stream(strap.vm_import_argv(plan, image))
                if code != 0:
                    detail = 'whitecube import failed. See the log.'
                    return
                self.say('Making the stored disk sparse again...')
                strap.sparsify(strap.whitecube_store() / 'images' / plan.name / 'disk.img')
            status = 'built'
            detail = f'{plan.name} is ready ({strap.human_seconds(time.monotonic() - started)}).'
        except backend.AuthRequired:
            detail = 'sudo asked for a password again. Build once more to unlock.'
        except (backend.ManagerError, OSError, ValueError) as error:
            detail = str(error)
        finally:
            self.save_log(plan)
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
            strap.record(plan, status, time.monotonic() - started, detail)
            GLib.idle_add(self.finished, plan, status, detail)

    def stream(self, argv):
        """Run argv, feeding each output line to the log; returns the exit status."""
        try:
            self.process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                            stdin=subprocess.DEVNULL, bufsize=1, process_group=0,
                                            env=dict(os.environ, LC_ALL='C.UTF-8'), errors='replace')
        except OSError as error:
            self.say(f'Error: {error}')
            return 127
        for line in self.process.stdout:
            line = strap.clean_line(line)
            fraction = strap.stage_fraction(line)
            if fraction is not None:
                self.step(fraction, line.strip('═ ').strip())
            self.say(line)
        code = self.process.wait()
        self.process = None
        return code

    def cancel(self):
        process = self.process
        if process and process.poll() is None:
            self.cancelled = True
            try:
                process.terminate()
            except OSError:
                pass
            self.status.set_text('Cancelling: deb-strap is unmounting and cleaning up...')

    def finished(self, plan, status, detail):
        self.set_building(False)
        self.set_progress(1 if status == 'built' else 0, status.capitalize())
        self.status.set_text(detail or status)
        self.notify(detail or status)
        self.fill_cubes()
        if status == 'built':
            if plan.kind == 'chroot':
                self.workspaces.add(plan.name)
            self.stack.set_visible_child_name('cubes')
            if plan.kind == 'vm' and self.boot_after.get_active():
                self.boot(plan.name, plan.recipe == 'server')
        self.form_changed()
        return False

    # --- log helpers -----------------------------------------------------------------------
    def say(self, line):
        GLib.idle_add(self.append_log, line)

    def append_log(self, line):
        if self.closed:
            return False
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), line + '\n')
        if buffer.get_line_count() > 20000:
            buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_line(5000)[1])
        adjustment = self.log_scroll.get_vadjustment()
        GLib.idle_add(lambda: adjustment.set_value(adjustment.get_upper()) and False)
        return False

    def step(self, fraction, text):
        GLib.idle_add(self.set_progress, fraction, text)

    def set_progress(self, fraction, text):
        self.progress.set_fraction(max(self.progress.get_fraction(), fraction) if self.building else fraction)
        self.progress.set_text(text)
        self.status.set_text(text)
        return False

    def log_text(self):
        buffer = self.log_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def save_log(self, plan):
        done = threading.Event()
        holder = {}

        def grab():
            holder['text'] = self.log_text()
            done.set()
            return False
        GLib.idle_add(grab)
        if not done.wait(5):
            return
        try:
            logs = strap.STATE / 'logs'
            logs.mkdir(parents=True, exist_ok=True, mode=0o700)
            (logs / f'{plan.kind}-{plan.name}.log').write_text(holder['text'], encoding='utf-8')
        except OSError:
            pass

    def copy_log(self):
        self.get_clipboard().set(self.log_text())
        self.notify('Log copied')

    # --- misc ---------------------------------------------------------------------------------
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

    def key_pressed(self, _controller, key, _code, state):
        if state & Gdk.ModifierType.CONTROL_MASK:
            pages = {Gdk.KEY_1: 'build', Gdk.KEY_2: 'cubes', Gdk.KEY_3: 'log'}
            if key in pages:
                self.stack.set_visible_child_name(pages[key])
                return True
            if key == Gdk.KEY_Return:
                self.start()
                return True
        return False

    def closing(self, *_):
        if self.building:
            self.notify('A build is running. Cancel it first; deb-strap then cleans up its mounts.')
            return True
        self.closed = True
        return False


class StrapApp(Adw.Application):
    def __init__(self, options, manager=None):
        GLib.set_prgname(APP_ID)
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.options = options
        self.manager = manager or backend.Manager()
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        apply_theme()
        provider = Gtk.CssProvider(); provider.load_from_string(RAIL_CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)

    def do_activate(self):
        if not self.window:
            self.window = StrapWindow(self, self.manager)
        self.window.present()
        if self.options.page:
            self.window.stack.set_visible_child_name(self.options.page)
        if self.options.screenshot:
            GLib.timeout_add(1300, self.take_screenshot)

    def take_screenshot(self):
        screenshot(self.window, str(self.options.screenshot))
        self.quit()
        return False


def main():
    parser = argparse.ArgumentParser(description='Cube Strap — build Debian whitecubes (chroot or VM)')
    parser.add_argument('--version', action='version', version=f'Cube Strap {VERSION}')
    parser.add_argument('--page', choices=('build', 'cubes', 'log'))
    parser.add_argument('--screenshot', type=Path)
    options = parser.parse_args()
    return StrapApp(options).run([sys.argv[0]])


if __name__ == '__main__':
    raise SystemExit(main())
