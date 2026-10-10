#!/usr/bin/env python3
"""Cube PDF Lock: password-protect PDFs with AES-256 (Whitecube style)."""
import threading
from pathlib import Path

import pikepdf

import cube_theme as ct
from cube_theme import Adw, Gio, GLib, Gtk

APP_ID = "dev.woez.CubeLock"


def output_path(source: Path, folder: Path | None) -> Path:
    """<name>-locked.pdf beside the source (or in folder); never overwrites."""
    base = (folder or source.parent) / f"{source.stem}-locked"
    candidate = base.with_suffix(".pdf")
    n = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{n}").with_suffix(".pdf")
        n += 1
    return candidate


def encrypt_pdf(source: Path, dest: Path, user_pw: str, owner_pw: str,
                allow_print: bool, allow_copy: bool, allow_edit: bool) -> None:
    permissions = pikepdf.Permissions(
        print_lowres=allow_print,
        print_highres=allow_print,
        extract=allow_copy,
        accessibility=True,
        modify_annotation=allow_edit,
        modify_assembly=allow_edit,
        modify_form=allow_edit,
        modify_other=allow_edit,
    )
    with pikepdf.open(source) as pdf:
        pdf.save(
            dest,
            encryption=pikepdf.Encryption(
                user=user_pw, owner=owner_pw or user_pw, R=6, allow=permissions
            ),
        )


def verify_locked(src: Path, dest: Path, password: str) -> None:
    """Raise unless dest is encrypted, opens with password, and keeps every page."""
    try:
        pikepdf.open(dest).close()
        raise ValueError("locked copy opens without a password")
    except pikepdf.PasswordError:
        pass
    with pikepdf.open(src) as original, pikepdf.open(dest, password=password) as locked:
        if len(locked.pages) != len(original.pages):
            raise ValueError("locked copy has a different page count")


def trash_file(path: Path) -> None:
    Gio.File.new_for_path(str(path)).trash(None)


def lock_one(src, dest, pw, owner, p, c, e, remove_original=False, trash=trash_file):
    """Encrypt src to dest. With remove_original, the original goes to the Trash
    only after the locked copy is verified; any doubt keeps it. Returns a note
    (or '') describing why an original was kept."""
    encrypt_pdf(src, dest, pw, owner, p, c, e)
    if not remove_original:
        return ""
    try:
        verify_locked(src, dest, pw)
        trash(src)
    except Exception as exc:  # the locked copy stays; the original stays too
        return f"original kept ({getattr(exc, 'message', None) or exc})"
    return ""


class PdfLockWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Cube PDF Lock",
                         default_width=560, default_height=820)
        self.files: list[Path] = []
        self.out_folder: Path | None = None

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        page = Adw.PreferencesPage()
        scroller = Gtk.ScrolledWindow(child=page, vexpand=True)
        view.set_content(scroller)

        # --- Files ---
        files_group = Adw.PreferencesGroup(
            title="PDFs", description="Choose files, or drop them onto this window.")
        add_btn = Gtk.Button(label="Add PDFs…", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self.on_add)
        clear_btn = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear_btn.connect("clicked", self.on_clear)
        box = Gtk.Box(spacing=6)
        box.append(clear_btn)
        box.append(add_btn)
        files_group.set_header_suffix(box)
        self.file_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.file_list.add_css_class("boxed-list")
        self.placeholder = Adw.ActionRow(title="No files chosen")
        self.placeholder.add_css_class("dim-label")
        self.file_list.append(self.placeholder)
        files_group.add(self.file_list)
        page.add(files_group)

        # --- Passwords ---
        pw_group = Adw.PreferencesGroup(title="Password")
        self.pw = Adw.PasswordEntryRow(title="Password to open")
        self.pw2 = Adw.PasswordEntryRow(title="Confirm password")
        self.owner = Adw.PasswordEntryRow(
            title="Separate owner password (optional)")
        for row in (self.pw, self.pw2, self.owner):
            row.connect("changed", self.on_change)
            pw_group.add(row)
        page.add(pw_group)

        # --- Permissions ---
        perm_group = Adw.PreferencesGroup(
            title="Once opened, allow",
            description="Applies to viewers that honor PDF permissions.")
        self.allow_print = Adw.SwitchRow(title="Printing", active=True)
        self.allow_copy = Adw.SwitchRow(title="Copying text and images", active=True)
        self.allow_edit = Adw.SwitchRow(title="Editing and annotating", active=False)
        for row in (self.allow_print, self.allow_copy, self.allow_edit):
            perm_group.add(row)
        page.add(perm_group)

        # --- Output ---
        out_group = Adw.PreferencesGroup(title="Output")
        self.out_row = Adw.ActionRow(
            title="Save locked copies", subtitle="Next to each original")
        pick = Gtk.Button(label="Choose folder…", valign=Gtk.Align.CENTER)
        pick.connect("clicked", self.on_pick_folder)
        self.out_row.add_suffix(pick)
        out_group.add(self.out_row)
        self.remove_orig = Adw.SwitchRow(
            title="Move originals to Trash after locking",
            subtitle="Only once the locked copy is checked. Restorable from the Trash.")
        out_group.add(self.remove_orig)
        page.add(out_group)

        # --- Action ---
        action_group = Adw.PreferencesGroup()
        self.status = ct.status_label()
        self.go = Gtk.Button(label="Encrypt")
        self.go.add_css_class("suggested-action")
        self.go.add_css_class("pill")
        self.go.set_halign(Gtk.Align.CENTER)
        self.go.connect("clicked", self.on_encrypt)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        holder.append(self.status)
        holder.append(self.go)
        action_group.add(holder)
        page.add(action_group)

        self.add_controller(ct.drop_target(self.add_files))
        self.on_change()

    # --- file list ---
    def add_files(self, paths):
        for p in paths:
            p = Path(p)
            if p.suffix.lower() == ".pdf" and p.is_file() and p not in self.files:
                self.files.append(p)
        self.refresh_files()

    def refresh_files(self):
        while (child := self.file_list.get_first_child()) is not None:
            self.file_list.remove(child)
        if not self.files:
            self.file_list.append(self.placeholder)
        for p in self.files:
            row = Adw.ActionRow(title=GLib.markup_escape_text(p.name),
                                subtitle=GLib.markup_escape_text(str(p.parent)))
            rm = Gtk.Button(icon_name="window-close-symbolic",
                            valign=Gtk.Align.CENTER, tooltip_text="Remove")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, path=p: self.remove(path))
            row.add_suffix(rm)
            self.file_list.append(row)
        self.on_change()

    def remove(self, path):
        self.files.remove(path)
        self.refresh_files()

    def on_clear(self, _b):
        self.files.clear()
        self.refresh_files()

    def on_add(self, _b):
        ct.pick_files(self, "Choose PDFs", self.add_files, "application/pdf")

    def on_pick_folder(self, _b):
        def chosen(path):
            self.out_folder = Path(path)
            self.out_row.set_subtitle(str(self.out_folder))
        ct.pick_folder(self, "Save locked copies in", chosen)

    # --- validation / action ---
    def on_change(self, *_):
        pw, pw2 = self.pw.get_text(), self.pw2.get_text()
        if not self.files:
            msg, ok = "Add at least one PDF.", False
        elif not pw:
            msg, ok = "Enter a password.", False
        elif pw != pw2:
            msg, ok = "Passwords do not match.", False
        else:
            n = len(self.files)
            msg, ok = f"Ready to encrypt {n} file{'s' if n != 1 else ''}.", True
        self.status.set_text(msg)
        self.go.set_sensitive(ok)

    def on_encrypt(self, _b):
        self.go.set_sensitive(False)
        self.status.set_text("Encrypting…")
        args = (list(self.files), self.out_folder, self.pw.get_text(),
                self.owner.get_text(), self.allow_print.get_active(),
                self.allow_copy.get_active(), self.allow_edit.get_active(),
                self.remove_orig.get_active())
        threading.Thread(target=self.work, args=args, daemon=True).start()

    def work(self, files, folder, pw, owner, p, c, e, remove):
        done, errors, notes = [], {}, {}
        for src in files:
            try:
                note = lock_one(src, output_path(src, folder), pw, owner, p, c, e, remove)
                done.append(src)
                if note:
                    notes[src] = note
            except pikepdf.PasswordError:
                errors[src] = "already encrypted"
            except Exception as exc:  # report per file, keep going
                errors[src] = str(exc)
        GLib.idle_add(self.finished, done, errors, notes, remove)

    def finished(self, done, errors, notes, remove):
        self.files = [f for f in self.files if f not in done]
        self.refresh_files()
        text = f"Locked {len(done)} file{'s' if len(done) != 1 else ''}."
        if remove and done:
            moved = len(done) - len(notes)
            text += f" {moved} original{'s' if moved != 1 else ''} moved to Trash."
        for path, note in notes.items():
            text += f" {path.name}: {note}."
        if errors:
            text += " Failed: " + "; ".join(
                f"{p.name} ({why})" for p, why in errors.items())
        self.toasts.add_toast(Adw.Toast(title=text))
        self.on_change()
        self.status.set_text(text)
        return False


class PdfLockApp(ct.CubeApp):
    window_class = PdfLockWindow

    def __init__(self):
        super().__init__(APP_ID)


if __name__ == "__main__":
    ct.quit_on_error_main(PdfLockApp)
