#!/usr/bin/env python3
"""Cube Hash: checksums for files, with paste-to-verify (Whitecube style)."""
import hashlib
import os
import re
import stat
import threading
import time
from pathlib import Path

import cube_theme as ct
from cube_theme import Adw, Gdk, GLib, Gtk

APP_ID = "dev.woez.CubeHash"
ALGOS = [("SHA-256", "sha256"), ("SHA-512", "sha512"), ("SHA-1", "sha1"),
         ("MD5", "md5"), ("BLAKE2b", "blake2b"), ("SHA3-256", "sha3_256")]


def hash_file(path, algo="sha256", progress=None, cancel=None) -> str:
    h = hashlib.new(algo)
    done = 0
    last_update = 0
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Choose a regular file to hash')
        size = before.st_size or 1
        while chunk := f.read(1 << 20):
            if cancel and cancel.is_set():
                raise InterruptedError('Hashing cancelled')
            h.update(chunk)
            done += len(chunk)
            current = time.monotonic()
            if progress and current - last_update >= .05:
                progress(min(1, done / size))
                last_update = current
        after = os.fstat(f.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('File changed while hashing; compute again')
    if cancel and cancel.is_set():
        raise InterruptedError('Hashing cancelled')
    if progress:
        progress(1.0)
    return h.hexdigest()


def clean_expected(text: str) -> str:
    """Accept '<hash>', '<hash>  file', or a 'sha256: <hash>' style paste."""
    text = text.strip()
    if not text:
        return ""
    bsd = re.fullmatch(r'[A-Za-z0-9_-]+\s*\(.*\)\s*=\s*([a-fA-F0-9]+)', text)
    if bsd:
        return bsd.group(1).lower()
    token = text.split()[0]
    if token.endswith(":") and len(text.split()) > 1:
        token = text.split()[1]
    return token.lower()


class HashWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Cube Hash",
                         default_width=620, default_height=780)
        self.results: list[tuple[Path, str]] = []
        self.busy = False
        self.cancel = threading.Event()
        self.result_algo = 'sha256'
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        page = Adw.PreferencesPage()
        view.set_content(Gtk.ScrolledWindow(child=page, vexpand=True))

        files = Adw.PreferencesGroup(title="Files", description="Choose files, or drop them here.")
        add = Gtk.Button(label="Add files…", valign=Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda _b: ct.pick_files(self, "Choose files", self.add_files))
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.connect("clicked", lambda _b: self.files.clear() if not self.busy else None)
        box = Gtk.Box(spacing=6)
        box.append(clear)
        box.append(add)
        files.set_header_suffix(box)
        self.files = ct.FileList(on_changed=self.on_change)
        files.add(self.files)
        page.add(files)

        opts = Adw.PreferencesGroup(title="Algorithm")
        self.algo = Adw.ComboRow(title="Hash", model=Gtk.StringList.new([n for n, _ in ALGOS]))
        self.expected = Adw.EntryRow(title="Expected hash (optional, to verify)")
        opts.add(self.algo)
        opts.add(self.expected)
        self.algo.connect('notify::selected', self.invalidate)
        page.add(opts)

        act = Adw.PreferencesGroup()
        self.status = ct.status_label()
        self.progress = Gtk.ProgressBar(visible=False)
        self.go = Gtk.Button(label="Compute", halign=Gtk.Align.CENTER)
        self.go.add_css_class("suggested-action")
        self.go.add_css_class("pill")
        self.go.connect("clicked", lambda _b: self.compute())
        self.stop = Gtk.Button(label='Cancel hashing', visible=False)
        self.stop.connect('clicked', lambda _b: self.cancel.set())
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for w in (self.status, self.progress, self.go, self.stop):
            holder.append(w)
        act.add(holder)
        page.add(act)

        self.out_group = Adw.PreferencesGroup(title="Results", visible=False)
        self.copy_all = Gtk.Button(label="Copy all", valign=Gtk.Align.CENTER)
        self.copy_all.connect("clicked", self.on_copy_all)
        self.export = Gtk.Button(label='Save checksums…', valign=Gtk.Align.CENTER)
        self.export.connect('clicked', self.save_checksums)
        output_actions = Gtk.Box(spacing=6)
        output_actions.append(self.copy_all)
        output_actions.append(self.export)
        self.out_group.set_header_suffix(output_actions)
        self.out_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.out_list.add_css_class("boxed-list")
        self.out_group.add(self.out_list)
        page.add(self.out_group)

        self.expected.connect("changed", lambda *_: self.show_results())
        self.add_controller(ct.drop_target(self.add_files))
        self.on_change()

    def add_files(self, paths):
        if not self.busy:
            self.files.add_paths(paths)

    def invalidate(self, *_):
        if not self.busy:
            self.results = []
            self.show_results()
            self.on_change()

    def on_change(self):
        if hasattr(self, "go"):
            n = len(self.files.paths)
            self.go.set_sensitive(n > 0 and not self.busy)
            if not self.busy:
                if not n:
                    self.results = []
                    self.show_results()
                self.status.set_text(f"{n} file{'s' if n != 1 else ''} ready." if n else "Add a file.")

    def compute(self):
        if self.busy or not self.files.paths:
            return
        paths, algo = list(self.files.paths), ALGOS[self.algo.get_selected()][1]
        self.result_algo = algo
        self.busy = True
        self.cancel.clear()
        self.files.set_sensitive(False)
        self.algo.set_sensitive(False)
        self.stop.set_visible(True)
        self.go.set_sensitive(False)
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.status.set_text("Hashing…")

        def bump(frac, i):
            GLib.idle_add(lambda: (self.progress.set_fraction((i + frac) / len(paths)), False)[1])

        def job():
            out = []
            for i, p in enumerate(paths):
                if self.cancel.is_set():
                    break
                try:
                    out.append((p, hash_file(p, algo, lambda f, i=i: bump(f, i), self.cancel)))
                except InterruptedError:
                    break
                except (OSError, ValueError) as exc:
                    out.append((p, f"error: {exc}"))
            return out

        def done(result, error):
            self.busy = False
            self.files.set_sensitive(True)
            self.algo.set_sensitive(True)
            self.stop.set_visible(False)
            self.progress.set_visible(False)
            self.results = result or []
            self.show_results()
            self.on_change()
            bad = sum(d.startswith('error:') for _, d in self.results)
            self.status.set_text(f"{'Cancelled. ' if self.cancel.is_set() else ''}Hashed {len(self.results) - bad}; failed {bad}." if not error else f"Failed: {error}")
        ct.run_task(job, done)

    def show_results(self):
        while (child := self.out_list.get_first_child()) is not None:
            self.out_list.remove(child)
        want = clean_expected(self.expected.get_text())
        valid = not want or bool(re.fullmatch(r'[a-f0-9]{' + str(hashlib.new(self.result_algo).digest_size * 2) + r'}', want))
        self.expected.remove_css_class('error')
        if not valid:
            self.expected.add_css_class('error')
        self.expected.set_tooltip_text('Expected checksum has the wrong length or contains non-hexadecimal characters' if not valid else 'Paste a checksum, GNU checksum line, or BSD checksum line')
        self.out_group.set_visible(bool(self.results))
        self.copy_all.set_sensitive(any(not d.startswith('error:') for _, d in self.results))
        self.export.set_sensitive(self.copy_all.get_sensitive())
        for path, digest in self.results:
            row = Adw.ActionRow(title=GLib.markup_escape_text(path.name),
                                subtitle=GLib.markup_escape_text(digest), subtitle_selectable=True)
            row.add_css_class("cube-hash")
            if want and valid and not digest.startswith("error"):
                ok = digest == want
                badge = Gtk.Label(label="MATCH" if ok else "NO MATCH", valign=Gtk.Align.CENTER)
                badge.add_css_class("cube-ok" if ok else "cube-bad")
                row.add_suffix(badge)
            cp = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copy hash")
            cp.add_css_class("flat")
            cp.set_sensitive(not digest.startswith('error:'))
            cp.connect("clicked", lambda _b, d=digest: self.copy(d))
            row.add_suffix(cp)
            self.out_list.append(row)

    def copy(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toasts.add_toast(Adw.Toast(title="Copied"))

    def on_copy_all(self, _b):
        self.copy(self.checksums())

    def checksums(self):
        lines = []
        for p, digest in self.results:
            if digest.startswith('error:'):
                continue
            name = str(p)
            escaped = any(c in name for c in ('\\', '\n', '\r'))
            name = name.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r')
            lines.append(('\\' if escaped else '') + f'{digest}  {name}')
        return '\n'.join(lines) + ('\n' if lines else '')

    def save_checksums(self, _b):
        contents, algorithm = self.checksums(), self.result_algo
        def chosen(folder):
            try:
                path = ct.write_output(Path(folder) / (algorithm + 'sums.txt'),
                                       lambda p: p.write_text(contents, encoding='utf-8'))
                self.status.set_text(f'Saved {path}')
            except OSError as exc:
                self.status.set_text(f'Failed: {exc}')
        ct.pick_folder(self, 'Save checksums in', chosen)


class HashApp(ct.CubeApp):
    window_class = HashWindow

    def __init__(self):
        super().__init__(APP_ID)


if __name__ == "__main__":
    ct.quit_on_error_main(HashApp)
