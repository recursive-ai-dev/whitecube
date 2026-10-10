#!/usr/bin/env python3
"""Cube Rename: batch rename with a live preview and undo (Whitecube style)."""
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import cube_theme as ct
from cube_theme import Adw, GLib, Gtk

APP_ID = "dev.woez.CubeRename"
CASES = ["Unchanged", "lowercase", "UPPERCASE", "Title Case"]


@dataclass
class Opts:
    find: str = ""
    replace: str = ""
    regex: bool = False
    prefix: str = ""
    suffix: str = ""
    number: bool = False
    start: int = 1
    digits: int = 2
    case: int = 0


def new_stem(stem: str, index: int, o: Opts) -> str:
    if o.find:
        if o.regex:
            stem = re.sub(o.find, o.replace, stem)
        else:
            stem = stem.replace(o.find, o.replace)
    stem = (o.prefix + stem + o.suffix)
    if o.number:
        stem += f"-{o.start + index:0{o.digits}d}"
    if o.case == 1:
        stem = stem.lower()
    elif o.case == 2:
        stem = stem.upper()
    elif o.case == 3:
        stem = stem.title()
    return stem


def plan(paths, o: Opts):
    """[(old, new, problem|None)]. Problems stop the whole batch from applying."""
    rows = []
    try:
        names = [new_stem(p.stem, i, o) + p.suffix for i, p in enumerate(paths)]
    except re.error as exc:
        return [(p, p, f"bad pattern: {exc}") for p in paths]
    batch = set(paths)
    seen: dict[Path, int] = {}
    for p, name in zip(paths, names):
        problem = None
        if not name.strip(" .") or "/" in name or "\0" in name:
            problem, new = "invalid name", p
        else:
            new = p.with_name(name)
            if new != p and new.exists() and new not in batch:
                problem = "already exists"
        seen[new] = seen.get(new, 0) + 1
        rows.append([p, new, problem])
    for r in rows:
        if r[2] is None and seen[r[1]] > 1:
            r[2] = "duplicate name"
    return [tuple(r) for r in rows]


def apply(rows):
    """Rename via temporary names so swaps and chains are safe. Returns undo pairs."""
    todo = [(old, new) for old, new, _ in rows if old != new]
    token = uuid.uuid4().hex[:8]
    temps = []
    for i, (old, new) in enumerate(todo):
        tmp = old.with_name(f".cube-rename-{token}-{i}")
        old.rename(tmp)
        temps.append((tmp, old, new))
    done = []
    try:
        for tmp, old, new in temps:
            tmp.rename(new)
            done.append((new, old))
    except OSError:
        for tmp, old, new in temps:  # put everything back
            target = new if (new, old) in done else tmp
            if target.exists():
                target.rename(old)
        raise
    return done


class RenameWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Cube Rename",
                         default_width=640, default_height=860)
        self.undo: list[tuple[Path, Path]] = []
        self.rows = []
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        page = Adw.PreferencesPage()
        view.set_content(Gtk.ScrolledWindow(child=page, vexpand=True))

        g = Adw.PreferencesGroup(title="Files",
                                 description="Order matters for numbering. Drop files here.")
        add = Gtk.Button(label="Add files…", valign=Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda _b: ct.pick_files(self, "Choose files", self.add_files))
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.connect("clicked", lambda _b: self.files.clear())
        box = Gtk.Box(spacing=6)
        box.append(clear)
        box.append(add)
        g.set_header_suffix(box)
        self.files = ct.FileList(reorder=True, on_changed=self.refresh)
        g.add(self.files)
        page.add(g)

        o = Adw.PreferencesGroup(title="Rules")
        self.find = Adw.EntryRow(title="Find")
        self.replace = Adw.EntryRow(title="Replace with")
        self.regex = Adw.SwitchRow(title="Find is a regular expression")
        self.prefix = Adw.EntryRow(title="Add before the name")
        self.suffix = Adw.EntryRow(title="Add after the name")
        self.number = Adw.SwitchRow(title="Number the files")
        self.start = Adw.SpinRow.new_with_range(0, 99999, 1)
        self.start.set_title("First number")
        self.start.set_value(1)
        self.digits = Adw.SpinRow.new_with_range(1, 8, 1)
        self.digits.set_title("Digits")
        self.digits.set_value(2)
        self.case = Adw.ComboRow(title="Letter case", model=Gtk.StringList.new(CASES))
        for r in (self.find, self.replace, self.regex, self.prefix, self.suffix,
                  self.number, self.start, self.digits, self.case):
            o.add(r)
        for r in (self.find, self.replace, self.prefix, self.suffix):
            r.connect("changed", lambda *_: self.refresh())
        for r in (self.regex, self.number):
            r.connect("notify::active", lambda *_: self.refresh())
        for r in (self.start, self.digits):
            r.connect("notify::value", lambda *_: self.refresh())
        self.case.connect("notify::selected", lambda *_: self.refresh())
        page.add(o)

        self.preview_group = Adw.PreferencesGroup(title="Preview")
        self.preview = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.preview.add_css_class("boxed-list")
        self.preview_group.add(self.preview)
        page.add(self.preview_group)

        act = Adw.PreferencesGroup()
        self.status = ct.status_label()
        self.go = Gtk.Button(label="Rename")
        self.go.add_css_class("suggested-action")
        self.go.connect("clicked", lambda _b: self.do_rename())
        self.undo_btn = Gtk.Button(label="Undo last rename", sensitive=False)
        self.undo_btn.connect("clicked", lambda _b: self.do_undo())
        row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        row.append(self.undo_btn)
        row.append(self.go)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        holder.append(self.status)
        holder.append(row)
        act.add(holder)
        page.add(act)
        self.add_controller(ct.drop_target(self.add_files))
        self.refresh()

    def opts(self) -> Opts:
        return Opts(self.find.get_text(), self.replace.get_text(), self.regex.get_active(),
                    self.prefix.get_text(), self.suffix.get_text(), self.number.get_active(),
                    int(self.start.get_value()), int(self.digits.get_value()),
                    self.case.get_selected())

    def add_files(self, paths):
        self.files.add_paths(paths)

    def refresh(self):
        if not hasattr(self, "go"):
            return
        self.rows = plan(self.files.paths, self.opts())
        while (child := self.preview.get_first_child()) is not None:
            self.preview.remove(child)
        changed = bad = 0
        for old, new, problem in self.rows:
            if old != new:
                changed += 1
            if problem:
                bad += 1
            row = Adw.ActionRow(title=GLib.markup_escape_text(new.name if not problem else old.name),
                                subtitle=GLib.markup_escape_text(
                                    f"was {old.name}" if not problem else f"{problem}"))
            if problem:
                badge = Gtk.Label(label="CONFLICT", valign=Gtk.Align.CENTER)
                badge.add_css_class("cube-bad")
                row.add_suffix(badge)
            elif old != new:
                badge = Gtk.Label(label="RENAME", valign=Gtk.Align.CENTER)
                badge.add_css_class("cube-ok")
                row.add_suffix(badge)
            self.preview.append(row)
        self.preview_group.set_visible(bool(self.rows))
        if not self.rows:
            msg, ok = "Add some files.", False
        elif bad:
            msg, ok = f"{bad} conflict(s); nothing will be renamed until they are fixed.", False
        elif not changed:
            msg, ok = "No changes yet; set a rule.", False
        else:
            msg, ok = f"{changed} file(s) will be renamed.", True
        self.status.set_text(msg)
        self.go.set_sensitive(ok)

    def do_rename(self):
        try:
            self.undo = apply(self.rows)
        except OSError as exc:
            self.toast(f"Rename failed, nothing changed: {exc.strerror or exc}")
            return
        new_paths = {old: new for old, new, _ in self.rows}
        self.files.paths = [new_paths.get(p, p) for p in self.files.paths]
        self.undo_btn.set_sensitive(True)
        self.files.refresh()
        self.toast(f"Renamed {len(self.undo)} file(s)")

    def do_undo(self):
        pairs, self.undo = self.undo, []
        try:
            apply([(cur, orig, None) for cur, orig in pairs])
        except OSError as exc:
            self.toast(f"Undo failed: {exc.strerror or exc}")
            return
        back = {cur: orig for cur, orig in pairs}
        self.files.paths = [back.get(p, p) for p in self.files.paths]
        self.undo_btn.set_sensitive(False)
        self.files.refresh()
        self.toast("Undone")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))


class RenameApp(ct.CubeApp):
    window_class = RenameWindow

    def __init__(self):
        super().__init__(APP_ID)


if __name__ == "__main__":
    ct.quit_on_error_main(RenameApp)
