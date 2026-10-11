#!/usr/bin/env python3
"""Cube PDF Tools: merge, split and unlock PDFs (Whitecube style)."""
from pathlib import Path

import pikepdf

import cube_theme as ct
from cube_theme import Adw, Gtk

APP_ID = "dev.woez.CubePdf"


def parse_ranges(text: str, total: int) -> list[tuple[int, int]]:
    """'1-3, 5, 8-' -> [(1,3),(5,5),(8,total)]. 1-based, inclusive, validated."""
    if type(total) is not int or total <= 0:
        raise ValueError('PDF has no pages')
    if text.strip().lower() == 'all':
        return [(1, total)]
    out = []
    for part in ''.join(text.split()).replace('–', '-').replace('—', '-').split(","):
        if not part:
            raise ValueError('Empty page range')
        lo, dash, hi = part.partition("-")
        try:
            a = int(lo) if lo else 1
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
    paths = list(paths)
    if not paths:
        raise ValueError('Choose at least one PDF')
    with pikepdf.new() as out:
        for p in paths:
            try:
                with pikepdf.open(p) as src:
                    out.pages.extend(src.pages)
            except pikepdf.PasswordError:
                raise ValueError(f"{Path(p).name} is password-protected; unlock it first")
        if not len(out.pages):
            raise ValueError('PDFs contain no pages')
        return ct.write_output(dest, out.save)


def split(path: Path, ranges, folder: Path) -> list[Path]:
    ranges = list(ranges)
    made = []
    with pikepdf.open(path) as src:
        total = len(src.pages)
        if not ranges or any(type(a) is not int or type(b) is not int or not 1 <= a <= b <= total for a, b in ranges):
            raise ValueError('Choose valid page ranges before splitting')
        folder.mkdir(parents=True, exist_ok=True)
        for a, b in ranges:
            label = f"p{a}" if a == b else f"p{a}-{b}"
            with pikepdf.new() as piece:
                piece.pages.extend(src.pages[a - 1:b])
                made.append(ct.write_output(folder / f"{Path(path).stem}-{label}.pdf", piece.save))
    return made


def unlock(path: Path, password: str, dest: Path) -> Path:
    try:
        with pikepdf.open(path, password=password) as pdf:
            dest = ct.write_output(dest, lambda p: pdf.save(p, encryption=False))
    except pikepdf.PasswordError:
        raise ValueError("wrong password")
    return dest


def page_count(path: Path) -> int:
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def rotate(path, degrees, ranges, dest):
    if degrees not in (90, 180, 270):
        raise ValueError('Choose 90, 180 or 270 degrees')
    with pikepdf.open(path) as pdf:
        selected = parse_ranges(ranges or 'all', len(pdf.pages))
        indices = {i for a, b in selected for i in range(a - 1, b)}
        for i in sorted(indices):
            pdf.pages[i].rotate(degrees, relative=True)
        return ct.write_output(dest, pdf.save)


class Tab(Adw.PreferencesPage):
    """One tool: a file list, its options, a status line and a go button."""
    verb = "Go"

    def __init__(self, win, single=False, reorder=False):
        super().__init__()
        self.win = win
        self.busy = False
        group = Adw.PreferencesGroup(title="PDFs")
        add = Gtk.Button(label="Add PDFs…", valign=Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda _b: ct.pick_files(
            win, "Choose PDFs", self.add_files, "application/pdf", multiple=not single))
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.connect("clicked", lambda _b: self.files.clear() if not self.busy else None)
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

    def add_files(self, paths):
        if not self.busy:
            self.files.add_paths(paths)

    def message(self):
        """(text, ok) describing readiness."""
        return ("Add a PDF.", False) if not self.files.paths else ("Ready.", True)

    def update(self, *_):
        if not hasattr(self, "go"):
            return
        text, ok = self.message()
        self.status.set_text(text)
        self.go.set_sensitive(ok and not self.busy)

    def finish(self, text):
        self.status.set_text(text)
        self.win.toast(text)
        self.go.set_sensitive(self.message()[1])

    def run(self, fn, ok_text):
        if self.busy:
            return
        self.busy = True
        self.files.set_sensitive(False)
        self.go.set_sensitive(False)
        self.status.set_text("Working…")

        def done(result, error):
            self.busy = False
            self.files.set_sensitive(True)
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
        if name in ('.', '..', '') or any(ord(c) < 32 for c in name):
            self.finish('Enter a valid PDF file name')
            return
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
                except (ValueError, OSError, pikepdf.PdfError) as exc:
                    bad.append(f"{p.name} ({exc})")
            return done, bad
        self.run(job, lambda r: f"Unlocked {len(r[0])}." + (f" Failed: {'; '.join(r[1])}" if r[1] else ""))

    def finish(self, text):
        self.pw.set_text('')
        super().finish(text)


class RotateTab(Tab):
    verb = 'Rotate'

    def __init__(self, win):
        super().__init__(win, single=True)

    def options(self, page):
        group = Adw.PreferencesGroup(title='Rotate a copy')
        self.angle = Adw.ComboRow(title='Clockwise rotation', model=Gtk.StringList.new(['90 degrees', '180 degrees', '270 degrees']))
        self.pages = Adw.EntryRow(title='Pages (blank for all)', text='')
        group.add(self.angle)
        group.add(self.pages)
        page.add(group)

    def start(self):
        path = self.files.paths[0]
        angle = (self.angle.get_selected() + 1) * 90
        pages = self.pages.get_text()
        self.run(lambda: rotate(path, angle, pages, path.with_name(path.stem + '-rotated.pdf')),
                 lambda p: f'Saved {p.name}')


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
        self.tabs = {"merge": MergeTab(self), "split": SplitTab(self), "unlock": UnlockTab(self), 'rotate': RotateTab(self)}
        for key, tab in self.tabs.items():
            self.stack.add_titled(tab, key, key.capitalize())
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.add_controller(ct.drop_target(self.add_files))

    def add_files(self, paths):
        tab = self.stack.get_visible_child()
        if not tab.busy:
            tab.files.add_paths(paths)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))


class PdfToolsApp(ct.CubeApp):
    window_class = PdfToolsWindow

    def __init__(self):
        super().__init__(APP_ID)


if __name__ == "__main__":
    ct.quit_on_error_main(PdfToolsApp)
