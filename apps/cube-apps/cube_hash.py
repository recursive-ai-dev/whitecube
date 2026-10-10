#!/usr/bin/env python3
"""Cube Hash: checksums for files, with paste-to-verify (Whitecube style)."""
import hashlib
from pathlib import Path

import cube_theme as ct
from cube_theme import Adw, Gdk, GLib, Gtk

APP_ID = "dev.woez.CubeHash"
ALGOS = [("SHA-256", "sha256"), ("SHA-512", "sha512"), ("SHA-1", "sha1"),
         ("MD5", "md5"), ("BLAKE2b", "blake2b")]


def hash_file(path, algo="sha256", progress=None) -> str:
    h = hashlib.new(algo)
    size = Path(path).stat().st_size or 1
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done / size)
    return h.hexdigest()


def clean_expected(text: str) -> str:
    """Accept '<hash>', '<hash>  file', or a 'sha256: <hash>' style paste."""
    text = text.strip()
    if not text:
        return ""
    token = text.split()[0]
    if token.endswith(":") and len(text.split()) > 1:
        token = text.split()[1]
    return token.lower()


class HashWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Cube Hash",
                         default_width=620, default_height=780)
        self.results: list[tuple[Path, str]] = []
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
        clear.connect("clicked", lambda _b: self.files.clear())
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
        page.add(opts)

        act = Adw.PreferencesGroup()
        self.status = ct.status_label()
        self.progress = Gtk.ProgressBar(visible=False)
        self.go = Gtk.Button(label="Compute", halign=Gtk.Align.CENTER)
        self.go.add_css_class("suggested-action")
        self.go.add_css_class("pill")
        self.go.connect("clicked", lambda _b: self.compute())
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for w in (self.status, self.progress, self.go):
            holder.append(w)
        act.add(holder)
        page.add(act)

        self.out_group = Adw.PreferencesGroup(title="Results", visible=False)
        self.copy_all = Gtk.Button(label="Copy all", valign=Gtk.Align.CENTER)
        self.copy_all.connect("clicked", self.on_copy_all)
        self.out_group.set_header_suffix(self.copy_all)
        self.out_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.out_list.add_css_class("boxed-list")
        self.out_group.add(self.out_list)
        page.add(self.out_group)

        self.expected.connect("changed", lambda *_: self.show_results())
        self.add_controller(ct.drop_target(self.add_files))
        self.on_change()

    def add_files(self, paths):
        self.files.add_paths(paths)

    def on_change(self):
        if hasattr(self, "go"):
            n = len(self.files.paths)
            self.go.set_sensitive(n > 0)
            self.status.set_text(f"{n} file{'s' if n != 1 else ''} ready." if n else "Add a file.")

    def compute(self):
        paths, algo = list(self.files.paths), ALGOS[self.algo.get_selected()][1]
        self.go.set_sensitive(False)
        self.progress.set_visible(True)
        self.status.set_text("Hashing…")

        def bump(frac, i):
            GLib.idle_add(lambda: (self.progress.set_fraction((i + frac) / len(paths)), False)[1])

        def job():
            out = []
            for i, p in enumerate(paths):
                try:
                    out.append((p, hash_file(p, algo, lambda f, i=i: bump(f, i))))
                except OSError as exc:
                    out.append((p, f"error: {exc.strerror}"))
            return out

        def done(result, error):
            self.progress.set_visible(False)
            self.results = result or []
            self.show_results()
            self.status.set_text(f"Hashed {len(self.results)} file(s)." if not error else f"Failed: {error}")
            self.on_change()
        ct.run_task(job, done)

    def show_results(self):
        while (child := self.out_list.get_first_child()) is not None:
            self.out_list.remove(child)
        want = clean_expected(self.expected.get_text())
        self.out_group.set_visible(bool(self.results))
        for path, digest in self.results:
            row = Adw.ActionRow(title=GLib.markup_escape_text(path.name),
                                subtitle=GLib.markup_escape_text(digest), subtitle_selectable=True)
            row.add_css_class("cube-hash")
            if want and not digest.startswith("error"):
                ok = digest == want
                badge = Gtk.Label(label="MATCH" if ok else "NO MATCH", valign=Gtk.Align.CENTER)
                badge.add_css_class("cube-ok" if ok else "cube-bad")
                row.add_suffix(badge)
            cp = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copy hash")
            cp.add_css_class("flat")
            cp.connect("clicked", lambda _b, d=digest: self.copy(d))
            row.add_suffix(cp)
            self.out_list.append(row)

    def copy(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toasts.add_toast(Adw.Toast(title="Copied"))

    def on_copy_all(self, _b):
        self.copy("\n".join(f"{d}  {p.name}" for p, d in self.results if not d.startswith("error")))


class HashApp(ct.CubeApp):
    window_class = HashWindow

    def __init__(self):
        super().__init__(APP_ID)


if __name__ == "__main__":
    ct.quit_on_error_main(HashApp)
