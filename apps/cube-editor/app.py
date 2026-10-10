#!/usr/bin/python3
"""cube-editor: a native Whitecube image studio. GPL-3.0-or-later."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

# The launcher uses isolated Python; import only this application's directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

VERSION = '0.4.0'
STAGES = [
    ('source', 'Source', 'media-optical-symbolic'),
    ('customize', 'Customize', 'applications-system-symbolic'),
    ('files', 'Files & desktop', 'folder-symbolic'),
    ('build', 'Build image', 'system-run-symbolic'),
    ('test', 'Test & release', 'computer-symbolic'),
    ('activity', 'Activity', 'view-list-symbolic'),
]
RECIPES = [
    ('Developer kit', 'Git, build tools, Python and curl.', ['git', 'build-essential', 'python3', 'python3-venv', 'curl'], []),
    ('Media workstation', 'VLC, GIMP and a complete audio stack.', ['vlc', 'gimp', 'pipewire', 'wireplumber'], []),
    ('Recovery image', 'Disk rescue, partitioning and diagnostics.', ['gddrescue', 'testdisk', 'gparted', 'smartmontools'], []),
    ('Network toolbox', 'Inspect connections and solve network problems.', ['nmap', 'tcpdump', 'dnsutils', 'traceroute'], []),
    ('Small & clean', 'Clear downloaded packages before rebuilding.', [], ['apt-get clean']),
    ('Daily essentials', 'Editor, archive tools and a terminal multiplexer.', ['nano', 'unzip', 'zip', 'tmux'], []),
]


def label(text='', css=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_wrap(wrap)
    if wrap:
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        widget.add_css_class(css)
    return widget


def box(vertical=True, spacing=12, css=None):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback, css='secondary', icon=None):
    widget = Gtk.Button(label=text)
    if icon:
        content = box(False, 8)
        content.append(Gtk.Image.new_from_icon_name(icon))
        content.append(label(text))
        widget.set_child(content)
    widget.add_css_class(css)
    widget.connect('clicked', lambda *_: callback())
    return widget


def card(title=None, description=None):
    widget = box(css='card')
    if title:
        widget.append(label(title, 'section-title'))
    if description:
        widget.append(label(description, 'help', True))
    return widget


def scroll(child, min_height=0):
    widget = Gtk.ScrolledWindow()
    widget.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    widget.set_child(child)
    widget.set_vexpand(True)
    if min_height:
        widget.set_min_content_height(min_height)
    return widget


class CubeArt(Gtk.DrawingArea):
    """Cairo geometry, rendered natively and kept crisp at any scale."""
    def __init__(self):
        super().__init__()
        self.set_content_width(210)
        self.set_content_height(190)
        self.set_draw_func(self.draw)

    def draw(self, area, cr, width, height):
        cr.translate(width / 2, height / 2)
        scale = min(width / 220, height / 190)
        cr.scale(scale, scale)
        cr.set_source_rgba(.14, .23, .08, .12)
        cr.set_line_width(1)
        for r in [48, 69, 91]:
            cr.arc(0, 0, r, 0, 2 * math.pi)
            cr.stroke()
        for x, y in [(-93, -60), (81, 55), (58, -78)]:
            cr.rectangle(x, y, 5, 5)
            cr.fill()
        points = [(0, -58), (58, -28), (58, 35), (0, 65), (-58, 35), (-58, -28)]
        cr.set_source_rgb(.10, .14, .07)
        cr.move_to(*points[0])
        for p in points[1:]:
            cr.line_to(*p)
        cr.close_path()
        cr.fill()
        cr.set_line_width(2)
        cr.set_source_rgb(.82, 1, .54)
        cr.move_to(-58, -28); cr.line_to(0, 3); cr.line_to(58, -28)
        cr.move_to(0, 3); cr.line_to(0, 65)
        cr.stroke()
        cr.set_source_rgba(.82, 1, .54, .55)
        cr.move_to(0, -43); cr.line_to(42, -22); cr.line_to(0, -1); cr.line_to(-42, -22)
        cr.close_path(); cr.stroke()


class EditorWindow(Adw.ApplicationWindow):
    def __init__(self, app, initial=None):
        super().__init__(application=app, title='cube-editor', default_width=1180, default_height=820)
        self.project = None
        self.worker = None
        self.busy = False
        self.running_action = None
        self.hydrating = False
        self.save_timer = None
        self.entries = {}
        self.payloads = []
        self.log_lines = []
        self.stage_buttons = {}
        self.action_buttons = []
        self.current_stage = 'source'
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        layout = box(spacing=0)
        self.toast_overlay.set_child(layout)
        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title='cube-editor', subtitle='Whitecube image studio')
        header.set_title_widget(title)
        header.pack_start(button('Open project', self.open_project, icon='folder-open-symbolic'))
        header.pack_end(button('Commands', self.palette, css='flat', icon='system-search-symbolic'))
        self.saved_label = label('LOCAL WORKSPACE', 'chip')
        header.pack_end(self.saved_label)
        self.context_action = button('Create project', self.run_context_action, 'primary')
        header.pack_end(self.context_action)
        layout.append(header)
        body = box(False, 0)
        body.set_vexpand(True)
        layout.append(body)
        body.append(self.make_rail())
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(140)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        body.append(self.stack)
        for name, title, _ in STAGES:
            page = box(spacing=20)
            page.set_margin_top(24); page.set_margin_bottom(28)
            page.set_margin_start(28); page.set_margin_end(28)
            if name != 'source':
                heading = box(spacing=7)
                heading.append(label(f'IMAGE STUDIO / {name.upper()}', 'eyebrow'))
                heading.append(label(title, 'page-title'))
                page.append(heading)
            getattr(self, f'make_{name}')(page)
            self.stack.add_named(scroll(page), name)
        footer = box(False, 12, 'statusbar')
        self.spinner = Gtk.Spinner()
        footer.append(self.spinner)
        self.status = label('Choose a source image to begin.')
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(85)
        self.status.set_hexpand(True)
        footer.append(self.status)
        self.progress = Gtk.ProgressBar()
        self.progress.set_size_request(160, -1)
        self.progress.set_visible(False)
        footer.append(self.progress)
        footer.append(label('Ctrl K  commands  ·  Ctrl S  save', 'shortcut'))
        layout.append(footer)
        self.key_controller = Gtk.EventControllerKey()
        self.key_controller.connect('key-pressed', self.on_key)
        self.add_controller(self.key_controller)
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect('drop', self.on_drop)
        self.add_controller(drop)
        self.refresh_dependencies()
        self.refresh_recent()
        if initial:
            self.load_project(initial)
        self.connect('close-request', self.on_close)

    def make_rail(self):
        rail = box(spacing=12, css='rail')
        rail.set_size_request(215, -1)
        rail.append(label('◈  cube-editor', 'rail-name'))
        rail.append(label('MAKE IT YOURS', 'rail-caption'))
        spacer = box(); spacer.set_size_request(-1, 20); rail.append(spacer)
        for i, (name, title, icon) in enumerate(STAGES, 1):
            item = button(f'{i:02d}   {title}', lambda n=name: self.navigate(n), css='flat', icon=icon)
            if i == 1:
                item.add_css_class('selected')
            item.set_halign(Gtk.Align.FILL)
            rail.append(item)
            self.stage_buttons[name] = item
        gap = box(); gap.set_vexpand(True); rail.append(gap)
        project = box(spacing=8, css='rail-project')
        project.append(label('CURRENT PROJECT', 'rail-caption'))
        self.project_label = label('No project yet', 'project-name', True)
        self.project_state = label('A clean slate.', 'dim-label', True)
        project.append(self.project_label); project.append(self.project_state)
        rail.append(project)
        rail.append(label('WHITE CUBE / 0.4', 'rail-caption'))
        return rail

    def entry(self, parent, title, key, default='', hint=None, picker=None):
        holder = box(spacing=6)
        holder.append(label(title, 'section-title'))
        row = box(False, 8)
        widget = Gtk.Entry(text=str(default))
        widget.set_hexpand(True)
        widget.connect('changed', self.mark_changed)
        self.entries[key] = widget
        row.append(widget)
        if picker:
            row.append(button('Browse', lambda: self.pick(title, widget.set_text, picker)))
        holder.append(row)
        if hint:
            holder.append(label(hint, 'help', True))
        parent.append(holder)
        return widget

    def make_source(self, page):
        hero = box(False, 16, 'hero')
        words = box(spacing=10); words.set_hexpand(True)
        words.append(label('AN OPERATING SYSTEM. ON YOUR TERMS.', 'eyebrow'))
        words.append(label('Your image.\nYour rules.', 'hero-title'))
        words.append(label('Start with a Linux ISO. Add your tools, your desktop,\nand your ideas. Build something worth booting.', 'lede', True))
        hero.append(words); hero.append(CubeArt()); page.append(hero)
        stats = box(False, 12)
        for value, caption in [('01 → 06', 'A GUIDED BUILD'), ('100% local', 'YOUR FILES STAY HERE'), ('BIOS + UEFI', 'ORIGINAL BOOT PRESERVED')]:
            item = card(); item.set_hexpand(True)
            item.append(label(value, 'stat')); item.append(label(caption, 'muted')); stats.append(item)
        page.append(stats)
        source = card('Start a project', 'Ubuntu and Debian live images with an editable SquashFS filesystem. Drop an ISO anywhere in this window.')
        iso_dir = Path.home() / 'cubic/iso'
        images = sorted(iso_dir.glob('*.iso')) if iso_dir.is_dir() else []
        self.entry(source, 'Source ISO', 'source_iso', str(images[0]) if images else '', picker='iso')
        pair = box(False, 16)
        left = box(); left.set_hexpand(True)
        right = box(); right.set_hexpand(True)
        self.entry(left, 'Project name', 'name', 'Whitecube remix')
        self.entry(right, 'Project workspace', 'base_dir', str(Path.home() / 'cubic/work'), picker='folder')
        pair.append(left); pair.append(right); source.append(pair)
        self.entry(source, 'Release folder', 'output_dir', str(Path.home() / 'cubic/complete'), picker='folder')
        actions = box(False, 10)
        self.create_button = button('Create & inspect image', self.create_project, 'primary', 'list-add-symbolic')
        self.action_buttons.append(self.create_button)
        actions.append(self.create_button)
        actions.append(button('Open an existing project', self.open_project, css='flat'))
        source.append(actions); page.append(source)
        ready = card('Workbench readiness')
        self.tools_box = Gtk.FlowBox(); self.tools_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.tools_box.set_max_children_per_line(5); self.tools_box.set_min_children_per_line(2)
        self.tools_box.set_column_spacing(8); self.tools_box.set_row_spacing(8)
        ready.append(self.tools_box)
        self.root_selector_box = box(spacing=6)
        self.root_selector_box.append(label('Live filesystem to edit', 'section-title'))
        self.root_selector = Gtk.DropDown.new_from_strings(['Inspect the source first'])
        self.root_selector.connect('notify::selected', self.select_root)
        self.root_selector_box.append(self.root_selector)
        self.root_selector_box.set_visible(False)
        ready.append(self.root_selector_box)
        self.inspect_label = label('Create a project to inspect the image, its filesystem and its boot entries.', 'help', True)
        ready.append(self.inspect_label)
        act = box(False, 10)
        self.extract_button = button('Extract filesystem', lambda: self.start_job('extract'), 'primary', 'package-x-generic-symbolic')
        self.action_buttons.append(self.extract_button)
        act.append(self.extract_button)
        act.append(button('Inspect again', self.inspect_project, css='flat'))
        ready.append(act); page.append(ready)
        self.recent_box = card('Recent projects')
        page.append(self.recent_box)

    def make_customize(self, page):
        page.append(label('Build your recipe, then apply it inside the extracted image. Recipes stay with the project so you can revise and rebuild.', 'lede', True))
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        for i, (name, description, packages, commands) in enumerate(RECIPES):
            content = box(spacing=8)
            content.append(label(name, 'recipe-title'))
            content.append(label(description, 'help', True))
            item = Gtk.Button(child=content)
            item.add_css_class('recipe')
            item.connect('clicked', lambda _b, p=packages, c=commands, n=name: self.add_recipe(n, p, c))
            grid.attach(item, i % 3, i // 3, 1, 1)
        page.append(grid)
        packages = card('Package plan', 'Use package names separated by spaces or newlines. Package operations use the repositories configured inside the source image.')
        self.entry(packages, 'Install', 'install_packages', '', 'Example: git curl python3-venv')
        self.entry(packages, 'Remove', 'remove_packages', '', 'Example: thunderbird  —  check the plan before applying.')
        page.append(packages)
        custom = card('Your commands', 'One command per line. Commands run as root inside the image, in order. Stop on the first error.')
        self.commands = Gtk.TextView(monospace=True)
        self.commands.set_top_margin(12); self.commands.set_bottom_margin(12)
        self.commands.set_left_margin(12); self.commands.set_right_margin(12)
        self.commands.get_buffer().connect('changed', self.mark_changed)
        pane = scroll(self.commands, 120); pane.add_css_class('code-wrap')
        custom.append(pane)
        custom.append(label('Tip: use the image terminal for exploratory changes; put repeatable changes here.', 'help', True))
        actions = box(False, 10)
        self.apply_button = button('Apply recipe', lambda: self.start_job('customize'), 'primary', 'emblem-ok-symbolic')
        self.terminal_button = button('Open image terminal', lambda: self.start_job('terminal'), icon='utilities-terminal-symbolic')
        self.action_buttons.extend([self.apply_button, self.terminal_button])
        actions.append(self.apply_button); actions.append(self.terminal_button)
        custom.append(actions); page.append(custom)
        tip = card('A terminal with a boundary', 'The editor stays in your user session. Extraction and image changes open a dedicated terminal for sudo authentication, and the chroot gets its own mount namespace.')
        page.append(tip)

    def make_files(self, page):
        page.append(label('Bring Whitecube into the image, or layer in your own configuration, scripts and artwork.', 'lede', True))
        desktop = card('Put your desktop on the image', 'Inject a rebuilt Whitecube desktop package. The package is copied into the image and installed during Apply recipe.')
        debs = sorted((Path.home() / 'whitecube/packaging/out').glob('whitecube-desktop_*.deb'))
        self.entry(desktop, 'Whitecube desktop package', 'desktop_deb', str(debs[-1]) if debs else '', picker='deb')
        self.inject_switch = Gtk.Switch(halign=Gtk.Align.END)
        self.inject_switch.connect('notify::active', self.mark_changed)
        row = box(False, 12)
        title = label('Include this desktop package in the recipe', 'section-title'); title.set_hexpand(True)
        row.append(title); row.append(self.inject_switch); desktop.append(row)
        page.append(desktop)
        files = card('File overlays', 'Copy a file or a directory into the extracted root. Paths are relative to the image, for example /etc/skel/.config or /usr/local/bin. Overlays are applied with the recipe.')
        self.entry(files, 'From your computer', 'payload_source', '', picker='any')
        self.entry(files, 'Destination in image', 'payload_destination', '/etc/skel/')
        files.append(button('Add overlay', self.add_payload, icon='list-add-symbolic'))
        self.payload_list = box(spacing=8)
        files.append(self.payload_list); page.append(files)
        actions = card('Explore the project')
        row = box(False, 8)
        row.append(button('Project folder', lambda: self.reveal(self.project.directory if self.project else None), icon='folder-open-symbolic'))
        row.append(button('Show recipe JSON', self.show_recipe))
        row.append(button('Copy recipe', self.copy_recipe))
        actions.append(row); page.append(actions)

    def make_build(self, page):
        page.append(label('Repack the edited filesystem, preserve the source image’s boot setup, and produce a release with a SHA-256 checksum.', 'lede', True))
        settings = card('Release identity')
        self.entry(settings, 'ISO volume label', 'volume_id', 'WHITECUBE', 'Uppercase letters, digits and underscores. Up to 32 characters.')
        self.entry(settings, 'Output filename', 'output_name', 'whitecube-remix.iso', 'The source image is preserved. Releases go to your release folder.')
        row = box(False, 20)
        title = box(spacing=6); title.set_hexpand(True)
        title.append(label('Filesystem compression', 'section-title'))
        title.append(label('zstd builds quickly; xz usually produces a smaller image.', 'help', True))
        self.compression = Gtk.DropDown.new_from_strings(['zstd', 'xz', 'gzip', 'lz4'])
        self.compression.connect('notify::selected', self.mark_changed)
        row.append(title); row.append(self.compression); settings.append(row)
        page.append(settings)
        plan = card('Build flight plan', 'Preview the main compression and export commands. Full worker steps, including installer preparation, appear in Activity.')
        self.plan_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.plan_view.set_top_margin(12); self.plan_view.set_left_margin(12)
        self.plan_view.set_bottom_margin(12); self.plan_view.set_right_margin(12)
        plan.append(scroll(self.plan_view, 165))
        actions = box(False, 10)
        self.build_button = button('Build release image', lambda: self.start_job('build'), 'primary', 'system-run-symbolic')
        self.action_buttons.append(self.build_button)
        actions.append(self.build_button)
        actions.append(button('Refresh plan', self.refresh_plan))
        actions.append(button('Copy commands', lambda: self.clipboard(self.text(self.plan_view))))
        plan.append(actions); page.append(plan)
        self.build_summary = label('Extract your source image to unlock the build.', 'help', True)
        page.append(self.build_summary)

    def make_test(self, page):
        page.append(label('Boot your release in a virtual machine, verify its checksum, then take it to real hardware.', 'lede', True))
        release = box(False, 24, 'hero')
        info = box(spacing=10); info.set_hexpand(True)
        info.append(label('YOUR NEXT RELEASE', 'eyebrow'))
        self.release_name = label('Waiting for a build.', 'page-title', True)
        info.append(self.release_name)
        self.release_path = label('A finished ISO and its checksum will appear here.', 'help', True)
        self.release_path.set_selectable(True); info.append(self.release_path)
        release.append(info); release.append(CubeArt()); page.append(release)
        vm = card('A first boot without leaving your desktop', 'QEMU boots the image as a live CD. The VM uses temporary storage; closing it discards guest disk changes.')
        row = box(False, 16)
        ram = box(spacing=6); ram.set_hexpand(True)
        ram.append(label('Memory', 'section-title'))
        self.memory = Gtk.DropDown.new_from_strings(['2 GB', '4 GB', '8 GB', '16 GB']); self.memory.set_selected(1)
        ram.append(self.memory); row.append(ram)
        firmware = box(spacing=6); firmware.set_hexpand(True)
        firmware.append(label('Firmware', 'section-title'))
        self.firmware = Gtk.DropDown.new_from_strings(['BIOS', 'UEFI'])
        firmware.append(self.firmware); row.append(firmware); vm.append(row)
        actions = box(False, 10)
        self.vm_button = button('Boot in QEMU', self.launch_vm, 'primary', 'media-playback-start-symbolic')
        self.action_buttons.append(self.vm_button)
        actions.append(self.vm_button)
        self.verify_button = button('Verify release', self.verify_release, icon='emblem-ok-symbolic')
        self.action_buttons.append(self.verify_button); actions.append(self.verify_button)
        actions.append(button('Release folder', lambda: self.reveal(self.entries['output_dir'].get_text())))
        vm.append(actions); page.append(vm)
        tips = card('A release worth testing')
        for text in ['01  Boot BIOS and UEFI. Confirm the live session reaches the desktop.',
                     '02  Try your terminal, network, audio and every added application.',
                     '03  Check the installer before using the image on a machine you care about.',
                     '04  Keep the source ISO, saved recipe and release checksum together.']:
            tips.append(label(text, 'help', True))
        page.append(tips)

    def make_activity(self, page):
        page.append(label('Follow every operation. Failed commands stay visible, and your saved project can be reopened at any time.', 'lede', True))
        actions = box(False, 10)
        self.cancel_button = button('Cancel current operation', self.cancel_job)
        self.cancel_button.set_sensitive(False)
        actions.append(self.cancel_button)
        actions.append(button('Copy log', lambda: self.clipboard('\n'.join(self.log_lines))))
        actions.append(button('Show project folder', lambda: self.reveal(self.project.directory if self.project else None)))
        page.append(actions)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_vexpand(True)
        console = box(css='console'); console.set_vexpand(True)
        self.log_scroll = scroll(self.log_view, 350)
        console.append(self.log_scroll); page.append(console)
        self.log('cube-editor 0.4.0 — workbench ready. Choose a source, or reopen a project.')

    def navigate(self, name):
        self.current_stage = name
        self.stack.set_visible_child_name(name)
        for key, item in self.stage_buttons.items():
            if key == name: item.add_css_class('selected')
            else: item.remove_css_class('selected')
        if name == 'build': self.refresh_plan()
        if name == 'test': self.refresh_release()
        self.refresh_buttons()

    def run_context_action(self):
        if self.current_stage == 'source':
            if not self.project or self.source_changed(): self.create_project()
            elif (self.project.directory / '.extracted').is_file(): self.start_job('terminal')
            else: self.start_job('extract')
        elif self.current_stage in ('customize', 'files'): self.start_job('customize')
        elif self.current_stage == 'build': self.start_job('build')
        elif self.current_stage == 'test': self.launch_vm()
        elif self.current_stage == 'activity': self.cancel_job()

    def mark_changed(self, *_):
        self.refresh_buttons()
        if self.hydrating or not self.project or self.busy:
            return
        self.saved_label.set_text('SAVING…')
        if self.save_timer:
            GLib.source_remove(self.save_timer)
        self.save_timer = GLib.timeout_add(550, self.autosave)

    def autosave(self):
        self.save_timer = None
        self.save_project()
        return False

    @staticmethod
    def text(view):
        buffer = view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def recipe(self):
        return dict(install_packages=self.entries['install_packages'].get_text().split(),
                    remove_packages=self.entries['remove_packages'].get_text().split(),
                    commands=[line.strip() for line in self.text(self.commands).splitlines() if line.strip()],
                    payloads=list(self.payloads),
                    desktop_deb=self.entries['desktop_deb'].get_text() if self.inject_switch.get_active() else '')

    def save_project(self):
        if not self.project or self.hydrating or self.busy:
            return False
        try:
            volume = self.entries['volume_id'].get_text().strip()
            if not re.fullmatch('[A-Z0-9_]{1,32}', volume):
                raise ValueError('Use 1–32 uppercase letters, digits or underscores for the volume label.')
            filename = self.entries['output_name'].get_text().strip()
            if Path(filename).name != filename or not filename.lower().endswith('.iso'):
                raise ValueError('Use a filename ending in .iso, without folders.')
            engine.package_list(self.entries['install_packages'].get_text())
            engine.package_list(self.entries['remove_packages'].get_text())
            self.project.save(name=self.entries['name'].get_text(), output_dir=self.entries['output_dir'].get_text(),
                              recipe=self.recipe(), volume_id=volume,
                              output_name=filename,
                              compression=['zstd', 'xz', 'gzip', 'lz4'][self.compression.get_selected()])
            self.saved_label.set_text('SAVED LOCALLY')
            self.project_label.set_text(self.project.data.get('name', 'Project'))
            return True
        except Exception as exc:
            self.saved_label.set_text('SAVE FAILED')
            self.toast(str(exc))
            return False

    def pick(self, title, callback, kind='any'):
        action = Gtk.FileChooserAction.SELECT_FOLDER if kind == 'folder' else Gtk.FileChooserAction.OPEN
        chooser = Gtk.FileChooserNative.new(title, self, action, 'Choose', 'Cancel')
        if kind in ('iso', 'deb', 'project'):
            filt = Gtk.FileFilter(); filt.set_name({'iso': 'Linux images (*.iso)', 'deb': 'Debian packages (*.deb)', 'project': 'cube-editor project (*.json)'}[kind])
            filt.add_pattern({'iso': '*.iso', 'deb': '*.deb', 'project': '*.json'}[kind]); chooser.add_filter(filt)
        def response(dialog, result):
            if result == Gtk.ResponseType.ACCEPT and dialog.get_file():
                path = dialog.get_file().get_path()
                if path: callback(path)
            dialog.destroy()
        chooser.connect('response', response); chooser.show()

    def open_project(self):
        if self.busy:
            self.toast('Finish or cancel the current operation before switching projects.'); return
        self.pick('Open cube-editor.json', self.load_project, 'project')

    def create_project(self):
        if self.busy: return
        try:
            self.project = engine.Project.create(self.entries['source_iso'].get_text(), self.entries['name'].get_text(),
                        base_dir=self.entries['base_dir'].get_text(), output_dir=self.entries['output_dir'].get_text())
            self.worker = engine.Engine(self.project, on_event=self.on_event)
            self.save_project()
            self.hydrate(); self.refresh_recent()
            self.log(f'Created project: {self.project.directory}')
            self.inspect_project()
        except Exception as exc:
            self.toast(str(exc)); self.log(f'Create failed: {exc}')

    def load_project(self, path):
        if self.busy:
            self.toast('Finish the current operation before switching projects.'); return
        self.save_project()
        try:
            self.project = engine.Project.load(path)
            self.worker = engine.Engine(self.project, on_event=self.on_event)
            self.hydrate(); self.refresh_recent()
            self.restore_log()
            self.log(f'Opened project: {self.project.directory}')
            self.toast('Project restored. Your recipe is ready.')
        except Exception as exc:
            self.toast(str(exc)); self.log(f'Open failed: {exc}')

    def hydrate(self):
        self.hydrating = True
        data = self.project.data
        for key in ['name', 'source_iso', 'output_dir', 'volume_id', 'output_name']:
            if key in data: self.entries[key].set_text(str(data[key]))
        self.entries['base_dir'].set_text(str(self.project.directory.parent))
        recipe = data.get('recipe', {})
        for key in ['install_packages', 'remove_packages']:
            self.entries[key].set_text(' '.join(recipe.get(key, [])))
        self.commands.get_buffer().set_text('\n'.join(recipe.get('commands', [])))
        self.payloads = list(recipe.get('payloads', []))
        if recipe.get('desktop_deb'): self.entries['desktop_deb'].set_text(recipe['desktop_deb'])
        self.inject_switch.set_active(bool(recipe.get('desktop_deb')))
        compression = data.get('compression', 'zstd')
        if compression in ['zstd', 'xz', 'gzip', 'lz4']:
            self.compression.set_selected(['zstd', 'xz', 'gzip', 'lz4'].index(compression))
        self.project_label.set_text(data.get('name', 'Project'))
        self.project_state.set_text(str(data.get('state', 'created')).replace('_', ' ').capitalize())
        self.saved_label.set_text('SAVED LOCALLY')
        self.hydrating = False
        self.refresh_payloads(); self.refresh_release(); self.refresh_buttons()
        inspection = data.get('inspection', {})
        if inspection: self.show_inspection(inspection)

    def inspect_project(self):
        if not self.project:
            self.toast('Create or open a project first.'); return
        if self.busy: return
        self.save_project()
        self.set_busy(True, 'Inspecting source image…')
        def run():
            try:
                result = self.worker.inspect_source()
                GLib.idle_add(self.inspect_done, result, None)
            except Exception as exc:
                GLib.idle_add(self.inspect_done, None, str(exc))
        threading.Thread(target=run, daemon=True).start()

    def inspect_done(self, result, error):
        self.set_busy(False)
        if error:
            self.toast(error); self.log(f'Inspection failed: {error}')
            self.status.set_text('Source inspection failed. See Activity for details.')
        else:
            self.show_inspection(result or {})
            self.status.set_text('Source inspected. Extract the filesystem when you’re ready.')
            self.log(json.dumps(result, indent=2, default=str))
            self.refresh_buttons()
        return False

    def show_inspection(self, data):
        if not isinstance(data, dict):
            self.inspect_label.set_text(str(data)); return
        self.hydrating = True
        paths = data.get('squashfs_paths', [])
        if paths:
            self.root_selector.set_model(Gtk.StringList.new(paths))
            selected = data.get('squashfs_path', paths[-1])
            self.root_selector.set_selected(paths.index(selected) if selected in paths else len(paths) - 1)
            self.root_selector_box.set_visible(True)
            self.root_selector.set_sensitive(not (self.project.directory / '.extracted').is_file())
        self.hydrating = False
        size = data.get('size_bytes', 0) / 1024 ** 3
        firmware = ' + '.join(name for name, key in [('BIOS', 'bios'), ('UEFI', 'uefi')] if data.get(key)) or 'No boot entries detected'
        chain = data.get('layer_chain', [])
        fragments = [f'{size:.2f} GB  ·  {firmware}  ·  {len(chain)} filesystem layer(s)',
                     ' → '.join(Path(path).name for path in chain)]
        fragments.extend(data.get('warnings', []))
        self.inspect_label.set_text('\n'.join(fragments))

    def select_root(self, *_):
        if self.hydrating or self.busy or not self.project: return
        selected = self.root_selector.get_selected_item()
        if selected:
            path = selected.get_string()
            if path != self.project.data.get('squashfs_path'):
                self.project.save(squashfs_path=path)
                self.inspect_project()

    def refresh_dependencies(self):
        try:
            deps = engine.dependencies()
            if isinstance(deps, dict): items = deps.items()
            else: items = [(x['name'], x['available']) if isinstance(x, dict)
                          else (str(x), bool(shutil.which(str(x)))) for x in deps]
        except Exception:
            items = [(name, bool(shutil.which(name))) for name in ['xorriso', 'unsquashfs', 'mksquashfs', 'sudo', 'foot', 'qemu-system-x86_64']]
        for name, found in items:
            if isinstance(found, dict): found = found.get('available', found.get('found', False))
            badge = label(f'{"✓" if found else "○"}  {name}', 'chip')
            badge.add_css_class('ready' if found else 'warn')
            self.tools_box.insert(badge, -1)
        self.refresh_buttons()

    def refresh_recent(self):
        while self.recent_box.get_last_child() and self.recent_box.get_last_child() != self.recent_box.get_first_child():
            self.recent_box.remove(self.recent_box.get_last_child())
        try:
            recent = engine.recent_projects()
        except Exception: recent = []
        if not recent:
            self.recent_box.append(label('Your projects will appear here. Reopening restores your settings and recipe.', 'help', True))
        for item in recent[:4]:
            if isinstance(item, dict):
                path = item.get('path', item.get('directory', ''))
                name = item.get('name', Path(path).name)
            else: path = str(item); name = Path(path).name
            self.recent_box.append(button(name, lambda p=path: self.load_project(p), css='flat', icon='folder-symbolic'))

    def add_recipe(self, name, packages, commands):
        if self.busy: return
        existing = self.entries['install_packages'].get_text().split()
        self.entries['install_packages'].set_text(' '.join(dict.fromkeys(existing + packages)))
        lines = [line for line in self.text(self.commands).splitlines() if line.strip()]
        self.commands.get_buffer().set_text('\n'.join(dict.fromkeys(lines + commands)))
        self.toast(f'{name} added to your recipe.')

    def add_payload(self):
        source = self.entries['payload_source'].get_text().strip()
        target = self.entries['payload_destination'].get_text().strip()
        if not Path(source).expanduser().exists():
            self.toast('Choose an existing file or folder.'); return
        if not target.startswith('/') or '..' in Path(target).parts or target == '/':
            self.toast('Use an image path such as /etc/skel/.config.'); return
        self.payloads.append({'source': str(Path(source).expanduser().resolve()), 'destination': target})
        self.refresh_payloads(); self.mark_changed()

    def refresh_payloads(self):
        while self.payload_list.get_first_child(): self.payload_list.remove(self.payload_list.get_first_child())
        for i, payload in enumerate(self.payloads):
            row = box(False, 10)
            text = label(f'{payload.get("source", "")}  →  {payload.get("destination", "")}', 'help', True); text.set_hexpand(True)
            row.append(text); row.append(button('Remove', lambda n=i: self.remove_payload(n), css='flat'))
            self.payload_list.append(row)

    def remove_payload(self, index):
        if self.busy: return
        self.payloads.pop(index); self.refresh_payloads(); self.mark_changed()

    def show_recipe(self):
        self.text_dialog('Saved recipe', json.dumps(self.recipe(), indent=2))

    def copy_recipe(self):
        self.clipboard(json.dumps(self.recipe(), indent=2))

    def refresh_plan(self):
        if not self.project:
            self.plan_view.get_buffer().set_text('Create or reopen a project to see its build commands.'); return
        self.save_project()
        try:
            plan = self.worker.plan('build')
            if isinstance(plan, dict):
                value = json.dumps(plan, indent=2, default=str)
            elif isinstance(plan, list):
                value = '\n\n'.join(shlex.join(item) if isinstance(item, list) else str(item) for item in plan)
            else: value = str(plan)
            self.plan_view.get_buffer().set_text(value)
        except Exception as exc:
            self.plan_view.get_buffer().set_text(str(exc))

    def refresh_buttons(self):
        if not hasattr(self, 'build_button'): return
        for item in self.action_buttons: item.set_sensitive(not self.busy)
        loaded = self.project is not None
        extracted = loaded and (self.project.directory / '.extracted').is_file()
        supported = loaded and self.project.data.get('inspection', {}).get('can_rebuild', False)
        new_source = self.source_changed()
        self.extract_button.set_sensitive(supported and not self.busy and not extracted and not new_source)
        for item in [self.apply_button, self.terminal_button, self.build_button]: item.set_sensitive(extracted and not self.busy)
        output = self.output_path()
        built = output is not None and output.is_file()
        self.vm_button.set_sensitive(built and not self.busy and bool(shutil.which('qemu-system-x86_64')))
        self.verify_button.set_sensitive(built and not self.busy)
        self.cancel_button.set_sensitive(self.busy and self.running_action is not None)
        self.build_summary.set_text('Ready to rebuild. Apply your recipe first if you changed packages, commands or overlays.' if extracted else 'Extract your source image to unlock the build.')
        if hasattr(self, 'context_action'):
            name = self.current_stage
            title, enabled = {
                'source': ('Create project' if not loaded or new_source else 'Image terminal' if extracted else 'Extract image',
                           not self.busy and (not loaded or new_source or extracted or supported)),
                'customize': ('Apply recipe', extracted and not self.busy),
                'files': ('Apply overlays', extracted and not self.busy),
                'build': ('Build release', extracted and not self.busy),
                'test': ('Boot test', self.vm_button.get_sensitive()),
                'activity': ('Cancel operation', self.cancel_button.get_sensitive()),
            }[name]
            self.context_action.set_label(title)
            self.context_action.set_sensitive(enabled)

    def source_changed(self):
        if not self.project: return False
        try:
            return str(Path(self.entries['source_iso'].get_text()).expanduser().resolve()) != self.project.data['source_iso']
        except (ValueError, OSError): return True

    def set_busy(self, state, message=None):
        self.busy = state
        self.spinner.set_spinning(state)
        self.progress.set_visible(state)
        if state: self.progress.pulse()
        if message: self.status.set_text(message)
        for widget in list(self.entries.values()) + [self.commands, self.compression, self.inject_switch]:
            widget.set_sensitive(not state)
        self.refresh_buttons()

    def start_job(self, action):
        if self.busy or not self.project: return
        if not self.save_project(): return
        try:
            command = getattr(self.worker, f'{action}_command')()
        except Exception as exc:
            self.toast(str(exc)); return
        self.navigate('activity')
        self.log(f'→ {action.capitalize()}\n{shlex.join(command)}')
        self.running_action = action
        self.set_busy(True, f'{action.capitalize()} — follow the dedicated image terminal for authentication.')
        def run():
            try:
                result = self.worker.run(command)
                GLib.idle_add(self.job_done, action, result, None)
            except Exception as exc:
                GLib.idle_add(self.job_done, action, None, str(exc))
        threading.Thread(target=run, daemon=True).start()

    def job_done(self, action, result, error):
        self.running_action = None
        self.set_busy(False)
        try:
            self.project = engine.Project.load(self.project.directory)
            self.worker = engine.Engine(self.project, on_event=self.on_event)
            self.hydrate()
        except Exception as exc: self.log(str(exc))
        if error or (isinstance(result, int) and result != 0):
            text = error or f'Command exited with code {result}'
            self.log(f'{action.capitalize()} failed: {text}'); self.toast(text)
            self.status.set_text(f'{action.capitalize()} did not finish. Review the log.')
        else:
            self.log(f'✓ {action.capitalize()} complete.')
            self.status.set_text(f'{action.capitalize()} finished. Your project is saved.')
            self.toast(f'{action.capitalize()} complete.')
        return False

    def cancel_job(self):
        if self.worker and self.busy:
            self.worker.cancel()
            self.log('Cancellation requested. Waiting for the operation to stop safely.')
            self.status.set_text('Stopping the operation…')

    def on_event(self, event):
        GLib.idle_add(self.accept_event, event)

    def accept_event(self, event):
        if isinstance(event, dict):
            message = str(event.get('message', ''))
            if message: self.log(message)
            if event.get('type') == 'progress':
                fraction = event.get('fraction', event.get('progress'))
                if isinstance(fraction, (float, int)): self.progress.set_fraction(min(1, max(0, fraction)))
                else: self.progress.pulse()
            if event.get('type') == 'state' and message: self.status.set_text(message)
        else: self.log(str(event))
        return False

    def log(self, message):
        stamp = time.strftime('%H:%M:%S')
        self.log_lines.append(f'[{stamp}] {message}')
        self.log_lines = self.log_lines[-2500:]
        if hasattr(self, 'log_view'):
            buffer = self.log_view.get_buffer()
            buffer.set_text('\n'.join(self.log_lines))
            mark = buffer.create_mark(None, buffer.get_end_iter(), False)
            self.log_view.scroll_to_mark(mark, 0, False, 0, 1)
            buffer.delete_mark(mark)

    def restore_log(self):
        """Show persisted worker history without loading an unbounded log."""
        path = self.project.directory / 'events.jsonl'
        self.log_lines = []
        if path.is_file():
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 128 * 1024))
                lines = stream.read().decode('utf-8', errors='replace').splitlines()
            for line in lines[-1000:]:
                try:
                    event = json.loads(line)
                    if event.get('message'):
                        self.log_lines.append(f'[{event.get("time", "previous run")}] {event["message"]}')
                except (ValueError, AttributeError):
                    continue

    def output_path(self):
        if not self.project: return None
        try: return self.project.output_path
        except (ValueError, engine.CubeError): return None

    def refresh_release(self):
        path = self.output_path()
        if path and path.is_file():
            self.release_name.set_text(path.name)
            self.release_path.set_text(f'{path}\n{path.stat().st_size / (1024 ** 3):.2f} GB  ·  ready to test')
        elif self.project:
            self.release_name.set_text(self.project.data.get('output_name', 'Waiting for a build.'))
            self.release_path.set_text('Build your release image to unlock verification and VM testing.')
        self.refresh_buttons()

    def launch_vm(self):
        if not self.project or self.busy: return
        try:
            argv = self.worker.qemu_command(memory=[2048, 4096, 8192, 16384][self.memory.get_selected()], uefi=self.firmware.get_selected() == 1)
            log_dir = self.project.directory / 'logs'; log_dir.mkdir(exist_ok=True)
            with (log_dir / 'qemu.log').open('a') as stream:
                process = subprocess.Popen(argv, stdout=stream, stderr=stream, start_new_session=True)
            self.log(f'VM launch: {shlex.join(argv)}')
            def check():
                code = process.poll()
                if code is not None and code != 0:
                    self.toast('QEMU could not start. See the project logs/qemu.log.')
                    self.log((log_dir / 'qemu.log').read_text()[-8000:])
                else: self.toast('VM launched. Guest changes use temporary storage.')
                return False
            GLib.timeout_add(1800, check)
        except Exception as exc: self.toast(str(exc)); self.log(str(exc))

    def verify_release(self):
        path = self.output_path()
        if not path: return
        self.set_busy(True, 'Verifying release checksum…')
        self.navigate('activity')
        def work():
            try:
                result = self.worker.verify_release()
                if not result.get('success'):
                    raise engine.CubeError('Checksum mismatch. The ISO differs from its release checksum.')
                GLib.idle_add(self.verify_done, result, None)
            except Exception as exc: GLib.idle_add(self.verify_done, None, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def verify_done(self, result, error):
        self.set_busy(False)
        if error: self.toast(error); self.log(f'Verification failed: {error}'); self.status.set_text('Release verification failed.')
        else:
            self.log(f'Verified release: {result}'); self.toast('Release checksum verified.')
            self.status.set_text('The release matches its SHA-256 checksum.')
        return False

    def reveal(self, path):
        if not path:
            self.toast('Create or open a project first.'); return
        try:
            if shutil.which('xdg-open'):
                subprocess.Popen(['xdg-open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.clipboard(str(path)); self.toast('Folder path copied. Open it in your file manager.')
        except OSError as exc: self.toast(str(exc))

    def clipboard(self, text):
        self.get_clipboard().set(text); self.toast('Copied to clipboard.')

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast.new(str(text)[:400]))

    def text_dialog(self, title, text):
        dialog = Adw.Dialog(title=title, content_width=760, content_height=540)
        view = Adw.ToolbarView(); view.add_top_bar(Adw.HeaderBar())
        child = box(spacing=12, css='palette')
        code = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        code.get_buffer().set_text(text); child.append(scroll(code))
        child.append(button('Copy', lambda: self.clipboard(text)))
        view.set_content(child); dialog.set_child(view); dialog.present(self)

    def palette(self):
        dialog = Adw.Dialog(title='Commands', content_width=560, content_height=440)
        view = Adw.ToolbarView(); view.add_top_bar(Adw.HeaderBar())
        content = box(spacing=14, css='palette')
        search = Gtk.SearchEntry(placeholder_text='Jump to a page or run an action…')
        content.append(search)
        commands = [('Go to ' + title, lambda n=name: self.navigate(n)) for name, title, _ in STAGES]
        commands += [('Open project', self.open_project), ('Save project', self.save_project), ('Inspect source', self.inspect_project),
                     ('Copy recipe JSON', self.copy_recipe), ('Open image terminal', lambda: self.start_job('terminal'))]
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        content.append(scroll(listing))
        def populate(*_):
            while listing.get_first_child(): listing.remove(listing.get_first_child())
            query = search.get_text().lower()
            for name, callback in commands:
                if query in name.lower():
                    def activate(cb=callback): dialog.close(); cb()
                    listing.append(button(name, activate, css='flat'))
        search.connect('search-changed', populate); populate()
        view.set_content(content); dialog.set_child(view); dialog.present(self)
        search.grab_focus()

    def on_key(self, controller, keyval, keycode, state):
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval in (Gdk.KEY_k, Gdk.KEY_K): self.palette(); return True
            if keyval in (Gdk.KEY_s, Gdk.KEY_S): self.save_project(); return True
            if keyval in (Gdk.KEY_o, Gdk.KEY_O): self.open_project(); return True
            if Gdk.KEY_1 <= keyval <= Gdk.KEY_6:
                self.navigate(STAGES[keyval - Gdk.KEY_1][0]); return True
        return False

    def on_drop(self, target, file, x, y):
        if self.busy: return False
        path = file.get_path()
        if path and path.lower().endswith('.iso'):
            self.entries['source_iso'].set_text(path); self.navigate('source'); self.toast('Source image selected.'); return True
        if path and path.endswith('.json'): self.load_project(path); return True
        if path and path.endswith('.deb'):
            self.entries['desktop_deb'].set_text(path); self.inject_switch.set_active(True); self.navigate('files'); return True
        return False

    def on_close(self, *_):
        if self.busy:
            self.toast('Finish or cancel the running operation before closing the editor.'); return True
        self.save_project(); return False


class EditorApp(Adw.Application):
    def __init__(self, options):
        super().__init__(application_id='org.whitecube.CubeEditor', flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.options = options
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        provider = Gtk.CssProvider(); provider.load_from_path(str(HERE / 'style.css'))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    def do_activate(self):
        if not self.window: self.window = EditorWindow(self, self.options.project)
        self.window.present()
        if self.options.page: self.window.navigate(self.options.page)
        if self.options.screenshot:
            GLib.timeout_add(1300, self.screenshot)

    def screenshot(self):
        window = self.window
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot(); paintable.snapshot(snapshot, window.get_width(), window.get_height())
        node = snapshot.to_node()
        if node:
            texture = window.get_native().get_renderer().render_texture(node, None)
            texture.save_to_png(str(self.options.screenshot))
        self.quit(); return False


def main():
    parser = argparse.ArgumentParser(description='cube-editor — Whitecube Linux image studio')
    parser.add_argument('project', nargs='?', help='Project directory or cube-editor.json')
    parser.add_argument('--version', action='version', version=f'cube-editor {VERSION}')
    parser.add_argument('--screenshot', type=Path, help='Render a preview PNG and exit')
    parser.add_argument('--page', choices=[stage[0] for stage in STAGES], help='Initial studio page')
    options = parser.parse_args()
    return EditorApp(options).run([sys.argv[0]])


if __name__ == '__main__':
    raise SystemExit(main())
