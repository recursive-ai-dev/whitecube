#!/usr/bin/python3
"""cube-editor: a native Whitecube image studio. Apache-2.0."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
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
MIN_ADWAITA = (1, 5)
COMPRESSIONS = ['zstd', 'xz', 'gzip', 'lz4', 'lzo']


def check_runtime():
    version = (Adw.get_major_version(), Adw.get_minor_version())
    if version < MIN_ADWAITA:
        raise engine.CubeError(
            f'Cube Editor requires libadwaita 1.5 or newer (found {version[0]}.{version[1]}). '
            'Upgrade libadwaita-1-0 and gir1.2-adw-1 before starting the editor.')


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
        cr.set_source_rgba(.07, .07, .07, .14)
        cr.set_line_width(1)
        for r in [48, 69, 91]:
            cr.arc(0, 0, r, 0, 2 * math.pi)
            cr.stroke()
        for x, y in [(-93, -60), (81, 55), (58, -78)]:
            cr.rectangle(x, y, 5, 5)
            cr.fill()
        points = [(0, -58), (58, -28), (58, 35), (0, 65), (-58, 35), (-58, -28)]
        cr.set_source_rgb(.07, .07, .07)
        cr.move_to(*points[0])
        for p in points[1:]:
            cr.line_to(*p)
        cr.close_path()
        cr.fill()
        cr.set_line_width(2)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(-58, -28); cr.line_to(0, 3); cr.line_to(58, -28)
        cr.move_to(0, 3); cr.line_to(0, 65)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, .55)
        cr.move_to(0, -43); cr.line_to(42, -22); cr.line_to(0, -1); cr.line_to(-42, -22)
        cr.close_path(); cr.stroke()


class EditorWindow(Adw.ApplicationWindow):
    def __init__(self, app, initial=None):
        super().__init__(application=app, title='cube-editor', default_width=1180, default_height=820)
        self.project = None
        self.pick_dirs = {}  # last folder per picker kind, for this session
        self.worker = None
        self.busy = False
        self.running_action = None
        self.hydrating = False
        self.save_timer = None
        self.entries = {}
        self.field_errors = {}
        self.selected_release = None
        self.verified_release = None
        self.release_monitor = None
        self.release_monitor_path = None
        self.test_disk = None
        self.editing_payload = None
        self.payloads = []
        self.original_commands = []
        self.original_script_text = ''
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
        error = label('', 'error', True)
        error.set_visible(False)
        holder.append(error)
        self.field_errors[key] = error
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
        act.append(button('Storage estimate', lambda: self.check_storage('extract')))
        act.append(button('Full extraction plan', lambda: self.show_plan('extract')))
        ready.append(act); page.append(ready)
        integrity = card('Source integrity', 'Record a source SHA256 before extraction. Supply the publisher’s digest to check it.')
        self.entry(integrity, 'Expected SHA256 (optional)', 'source_checksum')
        integrity.append(button('Verify source ISO', self.verify_source))
        self.source_hash = label('', 'help', True); self.source_hash.set_selectable(True)
        integrity.append(self.source_hash); page.append(integrity)
        recovery = card('Recovery and restore points', 'Restore points include the editable root and ISO tree. Replaced files are retained in recovery/.')
        self.recovery_label = label('Open a project to see its recovery status.', 'help', True)
        recovery.append(self.recovery_label)
        self.auto_snapshot = Gtk.CheckButton(label='Save a restore point before applying recipes or opening the image terminal', active=True)
        self.auto_snapshot.connect('toggled', self.mark_changed); recovery.append(self.auto_snapshot)
        for title, action in [('Save restore point', 'snapshot'), ('Restore saved point', 'restore'), ('Recover project', 'recover')]:
            recovery.append(button(title, lambda a=action: self.start_job(a)))
        page.append(recovery)
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
        packages.append(button('Preview package changes', lambda: self.start_job('packages')))
        page.append(packages)
        custom = card('Your Bash script', 'A complete Bash script runs with host root privileges inside the image. Use only trusted images and scripts. Loops, functions and heredocs are supported. Stop on the first error.')
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
        self.recipe_error = label('', 'error', True)
        self.recipe_error.set_visible(False); custom.append(self.recipe_error)
        transfer = card('Portable recipes', 'Export a recipe independently of the project. Imported local files must be available on this computer.')
        transfer.append(button('Import recipe', lambda: self.pick('Import recipe JSON', self.import_recipe, 'project')))
        transfer.append(button('Export recipe', lambda: self.save_file('Export recipe', self.export_recipe, 'recipe.json')))
        transfer.append(button('Preview complete customization plan', lambda: self.show_plan('customize')))
        page.append(transfer)
        tip = card('Trusted images and root access', 'Image commands, package maintainer scripts and the image terminal run with host root privileges after sudo authentication. They can read or modify files on this computer. The chroot and namespaces do not provide a security sandbox. Use only images, packages and recipes you trust as host root code.')
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
        self.payload_policy = Gtk.DropDown.new_from_strings(['Replace existing files', 'Skip existing files', 'Fail if files exist'])
        files.append(self.payload_policy)
        files.append(button('Add / update overlay', self.add_payload, icon='list-add-symbolic'))
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
        self.compression = Gtk.DropDown.new_from_strings(COMPRESSIONS)
        self.compression.connect('notify::selected', self.mark_changed)
        row.append(title); row.append(self.compression); settings.append(row)
        self.processors = Gtk.SpinButton.new_with_range(1, 256, 1); self.processors.set_value(2)
        self.build_memory = Gtk.SpinButton.new_with_range(128, 262144, 128); self.build_memory.set_value(1024)
        self.compression_level = Gtk.SpinButton.new_with_range(0, 22, 1)
        for title, widget in [('Build processors', self.processors), ('Compression memory (MiB)', self.build_memory), ('Compression level (0 = default; gzip 1–9, zstd 1–22)', self.compression_level)]:
            widget.connect('value-changed', self.mark_changed)
            settings.append(label(title, 'help', True)); settings.append(widget)
        self.resource_error = label('', 'error', True)
        self.resource_error.set_visible(False); settings.append(self.resource_error)
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
        actions.append(button('Copy plan', lambda: self.clipboard(self.text(self.plan_view))))
        actions.append(button('Storage estimate', lambda: self.check_storage('build')))
        plan.append(button('Full build plan', lambda: self.show_plan('build')))
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
        history = card('Release history', 'Choose a previous release to verify or boot. Test outcomes are attached to the actual ISO hash.')
        self.release_selector = Gtk.DropDown.new_from_strings(['Current output'])
        self.release_selector.connect('notify::selected', self.select_release); history.append(self.release_selector)
        self.history_summary = label('', 'help', True); history.append(self.history_summary)
        page.append(history)
        installer = card('Installer test', 'Create a disposable 32 GiB virtual disk, install into it, then reboot from disk. Disk changes persist until you discard the test disk.')
        installer.append(button('Create test disk', self.create_test_disk))
        installer.append(button('Open existing test disk', lambda: self.pick('Choose a project test disk', self.open_test_disk)))
        self.disk_label = label('No test disk selected.', 'help', True); installer.append(self.disk_label)
        installer.append(button('Boot installer with disk', lambda: self.launch_vm(installer=True)))
        installer.append(button('Reboot installed system', lambda: self.launch_vm(installer=True, installed=True)))
        installer.append(button('Discard test disk', self.discard_test_disk))
        page.append(installer)
        checks = card('Record a release check')
        self.check_kind = Gtk.DropDown.new_from_strings(['BIOS live', 'UEFI live', 'BIOS installer', 'UEFI installer'])
        self.check_outcome = Gtk.DropDown.new_from_strings(['not tested', 'passed', 'failed'])
        checks.append(self.check_kind); checks.append(self.check_outcome)
        self.entry(checks, 'Test notes', 'check_note')
        checks.append(button('Record result for this ISO', self.record_check))
        page.append(checks)

    def make_activity(self, page):
        page.append(label('Follow every operation. Failed commands stay visible, and your saved project can be reopened at any time.', 'lede', True))
        actions = box(False, 10)
        self.cancel_button = button('Cancel current operation', self.cancel_job)
        self.cancel_button.set_sensitive(False)
        actions.append(self.cancel_button)
        actions.append(button('Copy log', lambda: self.clipboard('\n'.join(self.log_lines))))
        actions.append(button('Export log', lambda: self.save_file('Export activity log', self.export_log, 'cube-editor.log')))
        actions.append(button('Next error', self.next_log_error))
        actions.append(button('Show project folder', lambda: self.reveal(self.project.directory if self.project else None)))
        page.append(actions)
        self.log_search = Gtk.SearchEntry(placeholder_text='Filter activity…')
        self.log_search.connect('search-changed', lambda *_: self.render_log()); page.append(self.log_search)
        self.log_errors_only = Gtk.CheckButton(label='Errors only')
        self.log_errors_only.connect('toggled', lambda *_: self.render_log()); page.append(self.log_errors_only)
        self.log_follow = Gtk.CheckButton(label='Follow new activity', active=True); page.append(self.log_follow)
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

    def save_file(self, title, callback, name):
        chooser = Gtk.FileChooserNative.new(title, self, Gtk.FileChooserAction.SAVE, 'Save', 'Cancel')
        chooser.set_current_name(name)
        def response(dialog, result):
            if result == Gtk.ResponseType.ACCEPT and dialog.get_file():
                path = dialog.get_file().get_path()
                if path:
                    try:
                        callback(path)
                    except Exception as exc:
                        self.toast(str(exc))
            dialog.destroy()
        chooser.connect('response', response); chooser.show()

    def background(self, message, task, completed=None):
        if self.busy or not self.project:
            return
        self.set_busy(True, message)
        def work():
            try:
                result = task()
                error = None
            except Exception as exc:
                result, error = None, str(exc)
            def finish():
                self.set_busy(False)
                if error:
                    self.toast(error); self.log(error); self.status.set_text(error)
                elif completed:
                    completed(result)
                return False
            GLib.idle_add(finish)
        threading.Thread(target=work, daemon=True).start()

    def verify_source(self):
        if not self.project: return
        if self.source_changed():
            self.toast('Create a project for the selected source before verifying it.'); return
        expected = self.entries['source_checksum'].get_text()
        worker = self.worker
        def done(result):
            self.source_hash.set_text(result['sha256'])
            self.status.set_text('Source matches the supplied checksum.' if result['matched_supplied_checksum'] else 'Source hash recorded.')
        self.background('Hashing source ISO…', lambda: worker.verify_source(expected), done)

    def check_storage(self, action):
        if not self.project or not self.save_project(): return
        project = self.project
        def done(result):
            lines = ['Conservative estimate; actual space depends on the source image and installer.']
            for check in result['checks']:
                lines.append(f"\n{check['path']}\n{check['required_bytes'] / 1024**3:.1f} GiB estimated · {check['free_bytes'] / 1024**3:.1f} GiB free\n" + '; '.join(check['reasons']))
            lines.append('\nSpace check passed.' if result['sufficient'] else '\nFree more space before starting.')
            self.text_dialog('Storage estimate', '\n'.join(lines))
        self.background('Estimating storage…', lambda: engine.storage_preflight(project, action), done)

    def show_plan(self, action):
        if not self.project or not self.save_project(): return
        worker = self.worker
        self.background('Preparing operation preview…', lambda: worker.detailed_plan(action),
                        lambda result: self.text_dialog('Complete operation plan', json.dumps(result, indent=2)))

    def export_recipe(self, path):
        if not self.project or self.busy or not self.save_project(): return
        engine.export_recipe(self.project, path)
        self.toast('Recipe exported.')

    def import_recipe(self, path):
        if not self.project or self.busy: return
        try:
            recipe, missing = engine.import_recipe(path)
            if not self.save_project():
                self.toast('Correct the highlighted fields before importing a recipe.'); return
            self.project.save(recipe=recipe)
            self.hydrate_recipe(recipe)
            if missing:
                self.text_dialog('Recipe files to locate', 'Update these local paths before applying:\n' + '\n'.join(missing))
            else:
                self.toast('Recipe imported.')
        except Exception as exc: self.toast(str(exc))

    def edit_payload(self, index):
        if self.busy: return
        item = self.payloads[index]
        self.entries['payload_source'].set_text(item['source'])
        self.entries['payload_destination'].set_text(item['destination'])
        self.payload_policy.set_selected(['replace', 'skip', 'error'].index(item.get('overwrite', 'replace')))
        self.editing_payload = index
        self.toast('Edit the mapping, then choose Add / update overlay.')

    def preview_payload(self, index):
        source = Path(self.payloads[index]['source'])
        def task():
            if not source.exists(): raise engine.CubeError('Payload source is missing')
            lines = [str(source)]
            if source.is_dir():
                for parent, directories, files in os.walk(source, followlinks=False):
                    for name in directories + files:
                        lines.append(str((Path(parent) / name).relative_to(source)))
                        if len(lines) >= 201:
                            return '\n'.join(lines) + '\n… preview limited to 200 entries'
            else: lines.append(f'{source.stat().st_size:,} bytes')
            return '\n'.join(lines)
        self.background('Reading payload…', task, lambda result: self.text_dialog('Payload preview', result))

    def selected_output(self):
        return self.selected_release or self.output_path()

    def select_release(self, *_):
        if not hasattr(self, 'release_choices') or self.hydrating: return
        index = self.release_selector.get_selected()
        self.selected_release = self.release_choices[index] if index < len(self.release_choices) else None
        self.update_release_display()

    @staticmethod
    def release_identity(path):
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode): return None
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except (OSError, AttributeError):
            return None

    def watch_release(self, path):
        if path == self.release_monitor_path and self.release_monitor: return
        if self.release_monitor:
            self.release_monitor.cancel()
        self.release_monitor = None
        if path != self.release_monitor_path:
            self.verified_release = None
        self.release_monitor_path = path
        if path and path.is_file():
            try:
                self.release_monitor = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.NONE, None)
                self.release_monitor.connect('changed', lambda *_: self.update_release_display())
            except GLib.Error:
                pass

    def remember_release_digest(self, path, digest, identity):
        # The identity must cover the whole hash operation, not just its completion.
        if path == self.selected_output() and identity and identity == self.release_identity(path):
            self.verified_release = (path, identity, digest)
        self.update_release_display()

    def update_release_display(self):
        path = self.selected_output()
        self.watch_release(path)
        identity = self.release_identity(path)
        verified = self.verified_release
        if verified and (verified[0] != path or verified[1] != identity):
            self.verified_release = verified = None
        digest = verified[2] if verified else None
        if identity:
            self.release_name.set_text(path.name)
            self.release_path.set_text(f'{path}\n{identity[2] / 1024**3:.2f} GB · ready to test')
        elif self.project:
            self.release_name.set_text(self.project.data.get('output_name', 'Waiting for a build.'))
            self.release_path.set_text('Build a release or select an available previous release.')
        if self.project:
            checks = [c for c in self.project.data.get('release_checks', []) if c.get('path') == str(path)]
            heading = (f'Current ISO · SHA256 {digest[:12]}' if digest else
                       'Historical checks only. Verify this release to identify the current ISO contents.')
            rows = [f"{'Current' if digest == c['sha256'] else 'Historical'} · {c['kind']}: {c['outcome']} · SHA256 {c['sha256'][:12]} · {c.get('note', '')}" for c in checks[-8:]]
            self.history_summary.set_text('\n'.join([heading] + rows) if checks else 'No recorded checks for this output.')
        self.refresh_buttons()

    def create_test_disk(self):
        if not self.project or self.busy or self.vm_running(): return
        worker = self.worker
        def done(path):
            self.test_disk = path
            self.disk_label.set_text(str(path))
            self.toast('Disposable installer disk created.')
        self.background('Creating installer test disk…', worker.create_test_disk, done)

    def open_test_disk(self, value):
        if self.busy or self.vm_running() or not self.project: return
        path = Path(value).resolve()
        if path.parent != self.project.directory / 'vm-tests' or path.suffix != '.qcow2' or not path.is_file():
            self.toast('Choose a qcow2 disk from this project’s vm-tests folder.'); return
        self.test_disk = path
        self.disk_label.set_text(str(path))

    def vm_running(self):
        process = getattr(self, 'vm_process', None)
        return process is not None and process.poll() is None

    def discard_test_disk(self):
        if self.busy or self.vm_running() or not self.test_disk: return
        disk = self.test_disk
        def discard():
            if self.project and disk.parent == self.project.directory / 'vm-tests':
                disk.unlink(missing_ok=True)
                Path(str(disk) + '.vars.fd').unlink(missing_ok=True)
                self.test_disk = None; self.disk_label.set_text('No test disk selected.')
        self.confirm('Discard installer test disk?', 'This removes the virtual disk and its installed system.', discard)

    def confirm(self, title, body, callback):
        dialog = Adw.MessageDialog(transient_for=self, heading=title, body=body)
        dialog.add_response('cancel', 'Cancel'); dialog.add_response('continue', 'Continue')
        dialog.set_response_appearance('continue', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel'); dialog.set_close_response('cancel')
        dialog.connect('response', lambda _d, response: callback() if response == 'continue' else None)
        dialog.present()

    def record_check(self):
        if not self.project: return
        kind = ['BIOS live', 'UEFI live', 'BIOS installer', 'UEFI installer'][self.check_kind.get_selected()]
        outcome = ['not tested', 'passed', 'failed'][self.check_outcome.get_selected()]
        note = self.entries['check_note'].get_text()
        worker, path = self.worker, self.selected_output()
        identity = self.release_identity(path)
        self.background('Recording check for exact ISO hash…', lambda: worker.record_check(kind, outcome, note, path),
                        lambda result: self.remember_release_digest(path, result['sha256'], identity))

    @staticmethod
    def is_log_error(line):
        return any(word in line.lower() for word in ('error', 'failed', 'failure', 'traceback', 'mismatch', 'exited with'))

    def render_log(self):
        if not hasattr(self, 'log_view'): return
        query = self.log_search.get_text().lower()
        errors = self.log_errors_only.get_active()
        lines = [line for line in self.log_lines if query in line.lower() and (not errors or self.is_log_error(line))]
        self.log_view.get_buffer().set_text('\n'.join(lines))
        self.log_error_offset = 0
        self.scroll_log()

    def scroll_log(self):
        if not self.log_follow.get_active(): return
        buffer = self.log_view.get_buffer()
        mark = buffer.create_mark(None, buffer.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0, False, 0, 1); buffer.delete_mark(mark)

    def next_log_error(self):
        buffer = self.log_view.get_buffer()
        text = self.text(self.log_view)
        offsets = []
        offset = 0
        for line in text.splitlines(True):
            if self.is_log_error(line): offsets.append(offset)
            offset += len(line)
        if not offsets:
            self.toast('No errors in the visible log.'); return
        current = getattr(self, 'log_error_offset', 0)
        chosen = next((o for o in offsets if o >= current), offsets[0])
        start = buffer.get_iter_at_offset(chosen)
        end = start.copy(); end.forward_to_line_end()
        buffer.select_range(start, end)
        self.log_view.scroll_to_iter(start, .1, False, 0, 0)
        self.log_error_offset = chosen + 1

    def export_log(self, path):
        if self.project and (self.project.directory / 'events.jsonl').is_file():
            shutil.copyfile(self.project.directory / 'events.jsonl', path)
        else:
            Path(path).write_text('\n'.join(self.log_lines) + '\n')
        self.toast('Activity exported.')

    def validate_fields(self):
        if not hasattr(self, 'build_button') or not hasattr(self, 'inject_switch'): return {}
        errors = {}
        volume = self.entries['volume_id'].get_text().strip()
        if not re.fullmatch('[A-Z0-9_]{1,32}', volume):
            errors['volume_id'] = 'Use 1–32 uppercase letters, digits or underscores.'
        name = self.entries['output_name'].get_text().strip()
        if Path(name).name != name or not name.lower().endswith('.iso'):
            errors['output_name'] = 'Use an ISO filename without folders.'
        for key in ('install_packages', 'remove_packages'):
            try: engine.package_list(self.entries[key].get_text())
            except Exception as exc: errors[key] = str(exc)
        if not self.entries['output_dir'].get_text().strip() or not Path(self.entries['output_dir'].get_text()).expanduser().is_absolute():
            errors['output_dir'] = 'Choose an absolute release folder.'
        try: engine.validate_recipe(self.recipe())
        except Exception as exc: errors['_recipe'] = str(exc)
        try: engine.build_resources(self.resource_settings())
        except Exception as exc: errors['_resources'] = str(exc)
        if hasattr(self, 'recipe_error'):
            self.recipe_error.set_text(errors.get('_recipe', '')); self.recipe_error.set_visible('_recipe' in errors)
        if hasattr(self, 'resource_error'):
            self.resource_error.set_text(errors.get('_resources', '')); self.resource_error.set_visible('_resources' in errors)
        for key, widget in self.field_errors.items():
            widget.set_text(errors.get(key, '')); widget.set_visible(key in errors)
            if key in errors: self.entries[key].add_css_class('error')
            else: self.entries[key].remove_css_class('error')
        return errors

    def resource_settings(self):
        return dict(compression=COMPRESSIONS[self.compression.get_selected()],
                    processors=self.processors.get_value_as_int(), memory_mb=self.build_memory.get_value_as_int(),
                    compression_level=self.compression_level.get_value_as_int())

    def mark_changed(self, *_):
        if self.hydrating: return
        self.refresh_buttons()
        if not self.project or self.busy:
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
                    commands=list(self.original_commands) if self.text(self.commands) == self.original_script_text else [self.text(self.commands)] if self.text(self.commands).strip() else [],
                    payloads=list(self.payloads),
                    desktop_deb=self.entries['desktop_deb'].get_text() if self.inject_switch.get_active() else '')

    def save_project(self):
        if not self.project or self.hydrating or self.busy:
            return False
        try:
            errors = self.validate_fields()
            if errors: raise ValueError(next(iter(errors.values())))
            volume = self.entries['volume_id'].get_text().strip()
            if not re.fullmatch('[A-Z0-9_]{1,32}', volume):
                raise ValueError('Use 1–32 uppercase letters, digits or underscores for the volume label.')
            filename = self.entries['output_name'].get_text().strip()
            if Path(filename).name != filename or not filename.lower().endswith('.iso'):
                raise ValueError('Use a filename ending in .iso, without folders.')
            engine.package_list(self.entries['install_packages'].get_text())
            engine.package_list(self.entries['remove_packages'].get_text())
            engine.validate_recipe(self.recipe())
            engine.build_resources(self.resource_settings())
            self.project.save(name=self.entries['name'].get_text(), output_dir=self.entries['output_dir'].get_text(),
                              recipe=self.recipe(), volume_id=volume,
                              output_name=filename,
                              auto_snapshot=self.auto_snapshot.get_active(), **self.resource_settings())
            self.saved_label.set_text('SAVED LOCALLY')
            self.saved_label.set_tooltip_text(None)
            self.project_label.set_text(self.project.data.get('name', 'Project'))
            return True
        except Exception as exc:
            self.saved_label.set_text('SAVE FAILED')
            self.validate_fields()
            self.saved_label.set_tooltip_text(str(exc))
            return False

    def pick(self, title, callback, kind='any'):
        action = Gtk.FileChooserAction.SELECT_FOLDER if kind == 'folder' else Gtk.FileChooserAction.OPEN
        chooser = Gtk.FileChooserNative.new(title, self, action, 'Choose', 'Cancel')
        chooser.set_modal(True)
        # Start where the last choice of this kind was made, else in the project.
        start = self.pick_dirs.get(kind) or (self.project.directory if self.project else None)
        if start and Path(start).is_dir():
            chooser.set_current_folder(Gio.File.new_for_path(str(start)))
        if kind in ('iso', 'deb', 'project'):
            filt = Gtk.FileFilter(); filt.set_name({'iso': 'Linux images (*.iso)', 'deb': 'Debian packages (*.deb)', 'project': 'cube-editor project (*.json)'}[kind])
            filt.add_pattern({'iso': '*.iso', 'deb': '*.deb', 'project': '*.json'}[kind]); chooser.add_filter(filt)
        def response(dialog, result):
            if result == Gtk.ResponseType.ACCEPT and dialog.get_file():
                path = dialog.get_file().get_path()
                if path:
                    self.pick_dirs[kind] = path if kind == 'folder' else str(Path(path).parent)
                    callback(path)
            dialog.destroy()
        chooser.connect('response', response); chooser.show()

    def open_project(self):
        if self.busy:
            self.toast('Finish or cancel the current operation before switching projects.'); return
        self.pick('Open cube-editor.json', self.load_project, 'project')

    def create_project(self):
        if self.busy or self.vm_running(): return
        if self.project and not self.save_project():
            self.toast('Correct the highlighted fields before creating another project.'); return
        try:
            self.selected_release = None
            self.test_disk = None
            self.disk_label.set_text('No test disk selected.')
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
        if self.busy or self.vm_running():
            self.toast('Finish the current operation and close the test VM before switching projects.'); return
        if self.project and not self.save_project():
            self.toast('Correct the highlighted fields before switching projects.'); return
        try:
            candidate = engine.Project.load(path)
            candidate_worker = engine.Engine(candidate, on_event=self.on_event)
            prepared = self.prepare_hydration(candidate)
            previous = (self.project, self.worker, self.selected_release)
            if self.save_timer:
                GLib.source_remove(self.save_timer); self.save_timer = None
            try:
                self.project, self.worker, self.selected_release = candidate, candidate_worker, None
                self.hydrate(prepared)
            except Exception:
                self.project, self.worker, self.selected_release = previous
                if self.project:
                    self.hydrate()
                raise
            self.refresh_recent()
            self.restore_log(); self.render_log()
            self.test_disk = None
            self.disk_label.set_text('No test disk selected.')
            self.log(f'Opened project: {self.project.directory}')
            self.toast('Project restored. Your recipe is ready.')
        except Exception as exc:
            self.toast(str(exc)); self.log(f'Open failed: {exc}')

    def hydrate_recipe(self, recipe):
        engine.validate_recipe(recipe)
        hydrating, self.hydrating = self.hydrating, True
        try:
            for key in ['install_packages', 'remove_packages']:
                self.entries[key].set_text(' '.join(engine.package_list(recipe.get(key, []))))
            self.original_commands = engine.recipe_scripts(recipe)
            self.original_script_text = '\n'.join(self.original_commands)
            self.commands.get_buffer().set_text(self.original_script_text)
            self.editing_payload = None
            self.payloads = list(recipe.get('payloads', []))
            self.entries['desktop_deb'].set_text(recipe.get('desktop_deb', ''))
            self.inject_switch.set_active(bool(recipe.get('desktop_deb')))
            self.refresh_payloads()
        finally:
            self.hydrating = hydrating
        self.refresh_buttons()

    def prepare_hydration(self, project):
        # Complete fallible document/ filesystem reads before replacing a window's project.
        engine.validate_recipe(project.data.get('recipe', {}))
        self.inspection_display(project.data.get('inspection', {}))
        report = engine.recovery_report(project)
        recovery_text = f"Project state: {report['state']}"
        if report['active_operation']: recovery_text += '\nAnother operation currently holds this project.'
        elif report['interrupted']: recovery_text += '\n' + report['recommendation']
        recovery_text += '\nRestore point available.' if report['restore_available'] else '\nNo saved restore point.'
        if report['environment_backups']: recovery_text += '\nGuest DNS or service-policy backups need recovery.'
        if report['pending_whiteouts']: recovery_text += '\nInterrupted boot-layer deletion markers need recovery.'
        if report['staging']: recovery_text += f"\n{len(report['staging'])} unfinished build artifact(s) can be retained by recovery."
        if report.get('output_staging'):
            recovery_text += '\n' + report['output_staging_note']
        return recovery_text

    def hydrate(self, prepared=None):
        recovery_text = self.prepare_hydration(self.project) if prepared is None else prepared
        hydrating, self.hydrating = self.hydrating, True
        try:
            data = self.project.data
            for key in ['name', 'source_iso', 'output_dir', 'volume_id', 'output_name']:
                self.entries[key].set_text(str(data[key]))
            self.entries['base_dir'].set_text(str(self.project.directory.parent))
            self.hydrate_recipe(data.get('recipe', {}))
            self.compression.set_selected(COMPRESSIONS.index(data.get('compression', 'zstd')))
            self.processors.set_value(data.get('processors', 2))
            self.build_memory.set_value(data.get('memory_mb', 1024))
            self.compression_level.set_value(data.get('compression_level', 0))
            self.auto_snapshot.set_active(data.get('auto_snapshot', True))
            self.source_hash.set_text(data.get('source_sha256', ''))
            self.entries['source_checksum'].set_text(data.get('source_expected_sha256', ''))
            self.recovery_label.set_text(recovery_text)
            self.project_label.set_text(data.get('name', 'Project'))
            self.project_state.set_text(str(data.get('state', 'created')).replace('_', ' ').capitalize())
            self.saved_label.set_text('SAVED LOCALLY')
            self.refresh_release()
            self.show_inspection(data.get('inspection', {}))
        finally:
            self.hydrating = hydrating
        self.refresh_buttons()

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

    @staticmethod
    def inspection_display(data):
        if not data:
            return [], None, 'Inspect the current source image to see its filesystem and boot entries.'
        if not isinstance(data, dict):
            raise engine.CubeError('The saved inspection must be an object.')
        paths = data.get('squashfs_paths', [])
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise engine.CubeError('The saved live filesystem choices must be a list of paths.')
        selected = data.get('squashfs_path', paths[-1] if paths else None)
        size = data.get('size_bytes', 0) / 1024 ** 3
        firmware = ' + '.join(name for name, key in [('BIOS', 'bios'), ('UEFI', 'uefi')] if data.get(key)) or 'No boot entries detected'
        chain = data.get('layer_chain', [])
        fragments = [f'{size:.2f} GB  ·  {firmware}  ·  {len(chain)} filesystem layer(s)',
                     ' → '.join(Path(path).name for path in chain)]
        fragments.extend(data.get('warnings', []))
        return paths, selected, '\n'.join(fragments)

    def show_inspection(self, data):
        paths, selected, summary = self.inspection_display(data)
        hydrating, self.hydrating = self.hydrating, True
        try:
            self.root_selector.set_model(Gtk.StringList.new(paths))
            if paths:
                self.root_selector.set_selected(paths.index(selected) if selected in paths else len(paths) - 1)
            self.root_selector_box.set_visible(bool(paths))
            self.refresh_root_selector()
            self.inspect_label.set_text(summary)
        finally:
            self.hydrating = hydrating

    def select_root(self, *_):
        if self.hydrating or self.busy or not self.project: return
        selected = self.root_selector.get_selected_item()
        if selected:
            path = selected.get_string()
            if path not in self.project.data.get('inspection', {}).get('squashfs_paths', []):
                self.show_inspection(self.project.data.get('inspection', {}))
                return
            if (self.project.directory / '.extracted').is_file(): return
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
        script = self.text(self.commands)
        additions = [c for c in commands if c not in script.splitlines()]
        if additions:
            self.commands.get_buffer().set_text(script + ('\n' if script else '') + '\n'.join(additions))
        self.toast(f'{name} added to your recipe.')

    def add_payload(self):
        source = self.entries['payload_source'].get_text().strip()
        target = self.entries['payload_destination'].get_text().strip()
        if not Path(source).expanduser().exists():
            self.toast('Choose an existing file or folder.'); return
        if not target.startswith('/') or '..' in Path(target).parts or target == '/':
            self.toast('Use an image path such as /etc/skel/.config.'); return
        if self.busy: return
        item = {'source': str(Path(source).expanduser().resolve()), 'destination': target,
                'overwrite': ['replace', 'skip', 'error'][self.payload_policy.get_selected()]}
        candidate = list(self.payloads)
        if self.editing_payload is None: candidate.append(item)
        else: candidate[self.editing_payload] = item
        try: engine.validate_recipe({'payloads': candidate})
        except Exception as exc:
            self.toast(str(exc)); return
        self.payloads = candidate
        self.editing_payload = None
        self.refresh_payloads(); self.mark_changed()

    def refresh_payloads(self):
        while self.payload_list.get_first_child(): self.payload_list.remove(self.payload_list.get_first_child())
        for i, payload in enumerate(self.payloads):
            row = box(False, 10)
            text = label(f'{payload.get("source", "")}  →  {payload.get("destination", "")}', 'help', True); text.set_hexpand(True)
            row.append(text)
            row.append(button('Edit', lambda n=i: self.edit_payload(n), css='flat'))
            row.append(button('Preview', lambda n=i: self.preview_payload(n), css='flat'))
            row.append(button('Remove', lambda n=i: self.remove_payload(n), css='flat'))
            self.payload_list.append(row)

    def remove_payload(self, index):
        if self.busy: return
        self.editing_payload = None
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
            plan = {'steps': ['Installer adaptation and clean installer image when applicable', 'Manifest refresh, deletion markers, integrity checks', 'Staged export, SHA256 and release history'], 'commands': self.worker.plan('build')}
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
        invalid = bool(self.validate_fields())
        loaded = self.project is not None
        extracted = loaded and (self.project.directory / '.extracted').is_file()
        supported = loaded and self.project.data.get('inspection', {}).get('can_rebuild', False)
        new_source = self.source_changed()
        self.extract_button.set_sensitive(supported and not self.busy and not extracted and not new_source)
        for item in [self.apply_button, self.terminal_button, self.build_button]: item.set_sensitive(extracted and not self.busy and not invalid)
        output = self.selected_output()
        built = output is not None and output.is_file()
        self.vm_button.set_sensitive(built and not self.busy and bool(shutil.which('qemu-system-x86_64')))
        self.verify_button.set_sensitive(built and not self.busy)
        for item in [self.apply_button, self.terminal_button, self.build_button]:
            item.set_tooltip_text('Finish the current operation.' if self.busy else 'Correct the highlighted fields.' if invalid else 'Extract the source image first.' if not extracted else None)
        self.vm_button.set_tooltip_text('Build or select a release first.' if not built else 'Install qemu-system-x86 to boot this release.' if not shutil.which('qemu-system-x86_64') else None)
        self.cancel_button.set_sensitive(self.busy and self.running_action is not None)
        self.build_summary.set_text('Ready to rebuild. Apply your recipe first if you changed packages, commands or overlays.' if extracted else 'Extract your source image to unlock the build.')
        if hasattr(self, 'context_action'):
            name = self.current_stage
            title, enabled = {
                'source': ('Create project' if not loaded or new_source else 'Image terminal' if extracted else 'Extract image',
                           not self.busy and (not loaded or new_source or extracted or supported)),
                'customize': ('Apply recipe', self.apply_button.get_sensitive()),
                'files': ('Apply overlays', self.apply_button.get_sensitive()),
                'build': ('Build release', self.build_button.get_sensitive()),
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
        for widget in list(self.entries.values()) + [self.commands, self.compression, self.inject_switch, self.auto_snapshot, self.processors, self.build_memory, self.compression_level, self.payload_policy]:
            widget.set_sensitive(not state)
        self.refresh_root_selector()
        self.refresh_buttons()

    def refresh_root_selector(self):
        # The live root is fixed once extracted and must not change during a job.
        if not hasattr(self, 'root_selector_box'): return
        extracted = self.project is not None and (self.project.directory / '.extracted').is_file()
        choices = self.root_selector.get_model().get_n_items() > 0 and self.root_selector_box.get_visible()
        self.root_selector.set_sensitive(choices and self.project is not None and not extracted and not self.busy)

    def start_job(self, action, confirmed=False):
        if self.busy or not self.project: return
        if not self.save_project(): return
        try:
            if action == 'restore' and not confirmed:
                self.confirm('Restore saved point?', 'This replaces the current root and ISO tree. Current trees will be retained under recovery/.', lambda: self.start_job(action, True))
                return
            command = self.worker.worker_command(action)
        except Exception as exc:
            self.toast(str(exc)); return
        self.navigate('activity')
        if action in ('customize', 'terminal', 'packages', 'build'):
            self.log('Trusted-image operation: image executables and package scripts run with host root privileges and can modify this computer.')
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
                if event.get('elapsed_seconds') is not None:
                    eta = event.get('eta_seconds')
                    text = f"{message} · {event['elapsed_seconds']:.0f}s elapsed"
                    if eta is not None: text += f' · about {eta:.0f}s remaining in phase'
                    self.status.set_text(text)
            if event.get('type') == 'state' and message: self.status.set_text(message)
        else: self.log(str(event))
        return False

    def log(self, message):
        stamp = time.strftime('%H:%M:%S')
        self.log_lines.append(f'[{stamp}] {message}')
        self.log_lines = self.log_lines[-2500:]
        if hasattr(self, 'log_view'):
            query = self.log_search.get_text().lower()
            line = self.log_lines[-1]
            if query in line.lower() and (not self.log_errors_only.get_active() or self.is_log_error(line)):
                buffer = self.log_view.get_buffer()
                prefix = '\n' if buffer.get_char_count() else ''
                buffer.insert(buffer.get_end_iter(), prefix + line)
                if buffer.get_line_count() > 3000:
                    begin = buffer.get_start_iter(); end = buffer.get_iter_at_line(500)
                    # GTK 4 returns (success, iterator) on some GI versions.
                    if isinstance(end, tuple): end = end[-1]
                    buffer.delete(begin, end)
                self.scroll_log()

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
        previous = self.selected_release
        history = self.worker.releases() if self.worker else []
        self.release_choices = [None] + [Path(r['path']) for r in reversed(history) if r.get('path')]
        names = ['Current output'] + [p.name for p in self.release_choices[1:]]
        hydrating = self.hydrating
        self.hydrating = True
        self.release_selector.set_model(Gtk.StringList.new(names))
        self.selected_release = previous if previous in self.release_choices else None
        self.release_selector.set_selected(self.release_choices.index(self.selected_release))
        self.hydrating = hydrating
        self.update_release_display()

    def launch_vm(self, installer=False, installed=False):
        if not self.project or self.busy or self.vm_running(): return
        try:
            if installer and not self.test_disk:
                raise engine.CubeError('Create a test disk first.')
            argv = self.worker.qemu_command(memory=[2048, 4096, 8192, 16384][self.memory.get_selected()], uefi=self.firmware.get_selected() == 1, disk=self.test_disk if installer else None, installed=installed, iso=self.selected_output())
            log_dir = self.project.directory / 'logs'; log_dir.mkdir(exist_ok=True)
            with (log_dir / 'qemu.log').open('a') as stream:
                process = subprocess.Popen(argv, stdout=stream, stderr=stream, start_new_session=True)
            self.vm_process = process
            self.log(f'VM launch: {shlex.join(argv)}')
            def check():
                code = process.poll()
                if code is not None and code != 0:
                    self.toast('QEMU could not start. See the project logs/qemu.log.')
                    self.log((log_dir / 'qemu.log').read_text()[-8000:])
                else: self.toast('VM launched. Test disk changes persist.' if installer else 'VM launched. Guest changes use temporary storage.')
                return False
            GLib.timeout_add(1800, check)
        except Exception as exc: self.toast(str(exc)); self.log(str(exc))

    def verify_release(self):
        path = self.selected_output()
        if not path or self.busy: return
        identity = self.release_identity(path)
        self.set_busy(True, 'Verifying release checksum…')
        self.navigate('activity')
        def work():
            try:
                result = self.worker.verify_release(path, record=True)
                result['identity'] = identity
                if not result.get('success'):
                    raise engine.CubeError('Checksum mismatch. The ISO differs from its release checksum.')
                GLib.idle_add(self.verify_done, result, None)
            except Exception as exc: GLib.idle_add(self.verify_done, None, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def verify_done(self, result, error):
        self.set_busy(False)
        if error:
            self.update_release_display()
            self.toast(error); self.log(f'Verification failed: {error}'); self.status.set_text('Release verification failed.')
        else:
            self.remember_release_digest(Path(result['path']), result['actual'], result['identity'])
            self.log(f'Verified release: {result}'); self.toast('Release checksum verified.')
            self.status.set_text('The release matches its SHA-256 checksum.')
        return False

    def reveal(self, path):
        if not path:
            self.toast('Create or open a project first.'); return
        folder = Path(path).expanduser()
        if not folder.is_dir():
            self.toast(f'{folder} does not exist yet.'); return
        # The launch context carries an activation token, so the file manager comes to the front.
        try:
            if Gio.AppInfo.launch_default_for_uri(folder.as_uri(), self.get_display().get_app_launch_context()):
                return
        except GLib.Error:
            pass
        try:
            if shutil.which('xdg-open'):
                subprocess.Popen(['xdg-open', str(folder)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            else:
                self.clipboard(str(folder)); self.toast('Folder path copied. Open it in your file manager.')
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
        if self.project and not self.save_project():
            self.toast('Correct the highlighted fields before closing.'); return True
        if self.save_timer:
            GLib.source_remove(self.save_timer); self.save_timer = None
        if self.release_monitor:
            self.release_monitor.cancel()
        return False


class EditorApp(Adw.Application):
    def __init__(self, options):
        check_runtime()
        GLib.set_prgname('org.whitecube.CubeEditor')
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
    try:
        return EditorApp(options).run([sys.argv[0]])
    except engine.CubeError as exc:
        print(f'cube-editor: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
