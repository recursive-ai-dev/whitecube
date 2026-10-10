#!/usr/bin/env python3
"""Cube PDF Tools: merge, split and unlock PDFs (Whitecube style)."""
from pathlib import Path

import pikepdf

import cube_theme as ct
from cube_theme import Adw, Gtk

APP_ID = "dev.woez.CubePdf"


def parse_ranges(text: str, total: int) -> list[tuple[int, int]]:
    """'1-3, 5, 8-' -> [(1,3),(5,5),(8,total)]. 1-based, inclusive, validated."""
    out = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        lo, dash, hi = part.partition("-")
        try:
            a = int(lo)
            b = (total if hi == "" else int(hi)) if dash else a
        except ValueError:
            raise ValueError(f"'{part}' is not a page or range")
        if not 1 <= a <= b <= total:
            raise ValueError(f"'{part}' is outside pages 1-{total}")
        out.append((a, b))
    if not out:
        raise ValueError("Enter at least one page or range")
    return out


def merge(paths, dest: Path) -> Path:
    out = pikepdf.new()
    for p in paths:
        try:
            with pikepdf.open(p) as src:
                out.pages.extend(src.pages)
        except pikepdf.PasswordError:
            raise ValueError(f"{Path(p).name} is password-protected; unlock it first")
    dest = ct.unique_path(dest)
    out.save(dest)
    return dest


def split(path: Path, ranges, folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    with pikepdf.open(path) as src:
        for a, b in ranges:
            piece = pikepdf.new()
            piece.pages.extend(src.pages[a - 1:b])
            label = f"p{a}" if a == b else f"p{a}-{b}"
            dest = ct.unique_path(folder / f"{Path(path).stem}-{label}.pdf")
            piece.save(dest)
            made.append(dest)
    return made


def unlock(path: Path, password: str, dest: Path) -> Path:
    try:
        with pikepdf.open(path, password=password) as pdf:
            dest = ct.unique_path(dest)
            pdf.save(dest, encryption=False)
    except pikepdf.PasswordError:
        raise ValueError("wrong password")
    return dest


def page_count(path: Path) -> int:
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


class Tab(Adw.PreferencesPage):
    """One tool: a file list, its options, a status line and a go button."""
    verb = "Go"

    def __init__(self, win, single=False, reorder=False):
        super().__init__()
        self.win = win
        group = Adw.PreferencesGroup(title="PDFs")
        add = Gtk.Button(label="Add PDFs…", valign=Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda _b: ct.pick_files(
            win, "Choose PDFs", self.files.add_paths, "application/pdf", multiple=not single))
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.connect("clicked", lambda _b: self.files.clear())
        box = Gtk.Box(spacing=6)
        box.append(clear)
        box.append(add)
        group.set_header_suffix(box)
        self.files = ct.FileList({".pdf"}, reorder=reorder, single=single,
                                 on_changed=self.update)
        group.add(self.files)
        self.add(group)
        self.options(self)
        action = Adw.PreferencesGroup()
        self.status = ct.status_label()
        self.go = Gtk.Button(label=self.verb, halign=Gtk.Align.CENTER)
        self.go.add_css_class("suggested-action")
        self.go.add_css_class("pill")
        self.go.connect("clicked", lambda _b: self.start())
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        holder.append(self.status)
        holder.append(self.go)
        action.add(holder)
        self.add(action)
        self.update()

    def options(self, page):
        pass

    def message(self):
        """(text, ok) describing readiness."""
        return ("Add a PDF.", False) if not self.files.paths else ("Ready.", True)

    def update(self, *_):
        if not hasattr(self, "go"):
            return
        text, ok = self.message()
        self.status.set_text(text)
        self.go.set_sensitive(ok)

    def finish(self, text):
        self.status.set_text(text)
        self.win.toast(text)
        self.go.set_sensitive(self.message()[1])

    def run(self, fn, ok_text):
        self.go.set_sensitive(False)
        self.status.set_text("Working…")

        def done(result, error):
            self.finish(f"Failed: {error}" if error else ok_text(result))
        ct.run_task(fn, done)


class MergeTab(Tab):
    verb = "Merge"

    def __init__(self, win):
        super().__init__(win, reorder=True)

    def options(self, page):
        g = Adw.PreferencesGroup(title="Output")
        self.name = Adw.EntryRow(title="File name", text="merged.pdf")
        g.add(self.name)
        page.add(g)

    def message(self):
        n = len(self.files.paths)
        return ("Add at least two PDFs.", False) if n < 2 else (f"Merge {n} files in the order shown.", True)

    def start(self):
        paths = list(self.files.paths)
        name = (self.name.get_text().strip() or "merged.pdf")
        name = Path(name).name
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = paths[0].parent / name
        self.run(lambda: merge(paths, dest), lambda d: f"Saved {d.name}")


class SplitTab(Tab):
    verb = "Split"

    def __init__(self, win):
        super().__init__(win, single=True)

    def options(self, page):
        g = Adw.PreferencesGroup(title="Split by")
        self.mode = Adw.ComboRow(title="Mode", model=Gtk.StringList.new(
            ["Every page", "Page ranges"]))
        self.mode.connect("notify::selected", lambda *_: self.update())
        self.ranges = Adw.EntryRow(title="Ranges, e.g. 1-3, 5, 8-")
        self.ranges.connect("changed", lambda *_: self.update())
        g.add(self.mode)
        g.add(self.ranges)
        page.add(g)

    def message(self):
        if not self.files.paths:
            return "Add a PDF.", False
        if self.mode.get_selected() == 1 and not self.ranges.get_text().strip():
            return "Enter the pages to extract.", False
        return "Pieces go into a new folder next to the original.", True

    def start(self):
        path = self.files.paths[0]
        every = self.mode.get_selected() == 0
        text = self.ranges.get_text()

        def job():
            total = page_count(path)
            ranges = [(i, i) for i in range(1, total + 1)] if every else parse_ranges(text, total)
            return split(path, ranges, ct.unique_path(path.parent / f"{path.stem}-split"))
        self.run(job, lambda made: f"Wrote {len(made)} files to {made[0].parent.name}/")


class UnlockTab(Tab):
    verb = "Unlock"

    def options(self, page):
        g = Adw.PreferencesGroup(
            title="Password", description="Removes the password from a copy; the original is kept.")
        self.pw = Adw.PasswordEntryRow(title="Current password")
        self.pw.connect("changed", lambda *_: self.update())
        g.add(self.pw)
        page.add(g)

    def message(self):
        if not self.files.paths:
            return "Add a PDF.", False
        return ("Enter the password.", False) if not self.pw.get_text() else ("Ready.", True)

    def start(self):
        paths, pw = list(self.files.paths), self.pw.get_text()

        def job():
            done, bad = [], []
            for p in paths:
                try:
                    done.append(unlock(p, pw, p.with_name(f"{p.stem}-unlocked.pdf")))
                except ValueError as exc:
                    bad.append(f"{p.name} ({exc})")
            return done, bad
        self.run(job, lambda r: f"Unlocked {len(r[0])}." + (f" Failed: {'; '.join(r[1])}" if r[1] else ""))


class PdfToolsWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Cube PDF Tools",
                         default_width=600, default_height=760)
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Gtk.Stack()
        switcher = Gtk.StackSwitcher(stack=self.stack)
        header.set_title_widget(switcher)
        view.add_top_bar(header)
        self.tabs = {"merge": MergeTab(self), "split": SplitTab(self), "unlock": UnlockTab(self)}
        for key, tab in self.tabs.items():
            self.stack.add_titled(tab, key, key.capitalize())
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.add_controller(ct.drop_target(self.add_files))

    def add_files(self, paths):
        self.stack.get_visible_child().files.add_paths(paths)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))


class PdfToolsApp(ct.CubeApp):
    window_class = PdfToolsWindow

    def __init__(self):
        super().__init__(APP_ID)


if __name__ == "__main__":
    ct.quit_on_error_main(PdfToolsApp)
