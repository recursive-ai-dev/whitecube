"""Whitecube look for GTK4/libadwaita apps.

Flat light-grey surfaces, 2px black borders, square corners, a hard offset
shadow, bold DejaVu Sans, and black-on-white selection, matching cube-status.
Also: a CubeApp base class, small file-dialog helpers, a cube icon generator,
and CUBE_SHOT=<path> to render the window to a PNG and quit (for testing in
the headless compositor).
"""
import os
import sys
import threading
import json
import math
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

INK = "#111111"
CSS = """
@define-color window_bg_color #d0d0d0;
@define-color window_fg_color #111111;
@define-color view_bg_color #efefef;
@define-color view_fg_color #111111;
@define-color card_bg_color #efefef;
@define-color card_fg_color #111111;
@define-color headerbar_bg_color #f5f5f5;
@define-color headerbar_fg_color #111111;
@define-color headerbar_border_color #111111;
@define-color accent_bg_color #111111;
@define-color accent_fg_color #ffffff;
@define-color accent_color #111111;
@define-color destructive_bg_color #111111;
@define-color destructive_fg_color #ffffff;
@define-color success_color #111111;
@define-color error_color #111111;

* { font-family: "DejaVu Sans", sans-serif; }
window, window.background { background: #d0d0d0; color: #111; }
headerbar { background: #f5f5f5; color: #111; border-bottom: 2px solid #111;
  box-shadow: none; min-height: 40px; }
headerbar .title { font-weight: bold; }

button { border: 2px solid #111; border-radius: 0; background: #ececec;
  background-image: none; color: #111; box-shadow: none; font-weight: bold;
  min-height: 22px; padding: 3px 12px; }
button:hover { background: #fff; }
button:active, button:checked { background: #111; color: #fff; }
button:disabled { opacity: .4; }
button.suggested-action, button.pill.suggested-action { background: #111; color: #fff; }
button.suggested-action:hover { background: #333; }
button.suggested-action:disabled { background: #111; opacity: .4; }
button.flat { border-color: transparent; background: transparent; }
button.flat:hover { border-color: #111; background: #fff; }
button.pill { border-radius: 0; padding: 8px 40px; }

list.boxed-list, .boxed-list { border: 2px solid #111; border-radius: 0;
  background: #efefef; box-shadow: 6px 6px 0 rgba(0,0,0,.32); }
list.boxed-list > row, row { border-radius: 0; background: transparent; color: #111; }
list.boxed-list > row:not(:first-child) { border-top: 2px solid #111; }
row:selected, row:selected:hover { background: #111; color: #fff; }
row.activatable:hover { background: #fff; }
row .subtitle, row .dim-label, .dim-label { color: #4a4a4a; opacity: 1; }
row.entry, row.password-entry, entry { border-radius: 0; background: #efefef;
  box-shadow: none; }
entry { border: 2px solid #111; }
row.entry:focus-within { background: #fff; outline: none; }
preferencesgroup > box > label.heading, .title-4 { font-weight: bold; }

switch { border: 2px solid #111; border-radius: 0; background: #ececec; box-shadow: none; }
switch slider { border-radius: 0; background: #111; border: none; box-shadow: none;
  min-width: 18px; min-height: 18px; margin: 2px; }
switch:checked { background: #111; }
switch:checked slider { background: #fff; }

dropdown > button, combobox button { background: #ececec; }
popover > contents, popover.menu > contents { border: 2px solid #111; border-radius: 0;
  box-shadow: 6px 6px 0 rgba(0,0,0,.32); background: #efefef; }
progressbar trough { border: 2px solid #111; border-radius: 0; background: #ececec;
  min-height: 12px; }
progressbar progress { background: #111; border-radius: 0; }
toast, .toast { background: #111; color: #fff; border-radius: 0; }
scrollbar slider { border-radius: 0; background: #111; min-width: 6px; }
scrollbar, scrollbar trough { background: transparent; }
viewswitcher button, .cube-tab { border-radius: 0; }
viewswitcher button:checked { background: #111; color: #fff; }
windowcontrols button, windowcontrols button.close { min-width: 22px; min-height: 22px;
  padding: 2px; margin: 4px 6px; }
windowcontrols button image, windowcontrols button.circular, button.circular {
  border-radius: 0; background: transparent; box-shadow: none; }
.cube-hash { font-family: "DejaVu Sans Mono", monospace; font-size: 11px; }
.cube-ok { background: #111; color: #fff; padding: 2px 8px; font-weight: bold; }
.cube-bad { background: #fff; color: #111; border: 2px solid #111; padding: 0 6px;
  font-weight: bold; }
.cube-status { color: #333; }
"""


def apply_theme():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    provider = Gtk.CssProvider()
    extra = "button:focus-visible, entry:focus-within { outline: 2px solid #111; outline-offset: 2px; }"
    try:
        state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
        path = state / 'whitecube/accessibility.json'
        if path.stat().st_size <= 1048576:
            settings = json.loads(path.read_text(encoding='utf-8'))
            scale = settings.get('ui_scale', 1)
            if type(scale) in (int, float) and math.isfinite(scale) and .8 <= scale <= 1.6:
                extra += f" * {{ font-size: {round(11 * scale, 2)}pt; }}"
            if settings.get('contrast') == 'strong':
                extra += "window, row, entry, list.boxed-list { background: #fff; color: #000; } .dim-label, row .subtitle { color: #111; }"
            if settings.get('reduced_motion') is True:
                Gtk.Settings.get_default().set_property('gtk-enable-animations', False)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    provider.load_from_string(CSS + extra)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)


def screenshot(window: Gtk.Window, path: str) -> None:
    w, h = window.get_width(), window.get_height()
    paintable = Gtk.WidgetPaintable.new(window)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    texture = window.get_native().get_renderer().render_texture(node, None)
    texture.save_to_png(path)


class CubeApp(Adw.Application):
    """Subclasses set window_class; opened files go to window.add_files()."""
    window_class = None

    def __init__(self, app_id: str):
        # GTK uses the program name for Wayland identity without a session bus.
        GLib.set_prgname(app_id)
        super().__init__(application_id=app_id,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        apply_theme()
        for name, keys, callback in (
            ('quit', ['<Control>q'], lambda: self.quit()),
            ('close', ['<Control>w'], lambda: self.window().close()),
            ('open', ['<Control>o'], self.pick),
            ('run', ['<Control>Return'], self.run_current),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda _a, _p, cb=callback: cb())
            self.add_action(action)
            self.set_accels_for_action('app.' + name, keys)

    def pick(self):
        win = self.window()
        if getattr(win, 'busy', False):
            return
        pick_files(win, 'Choose files', win.add_files,
                   'application/pdf' if 'Pdf' in self.get_application_id() else None)

    def run_current(self):
        win = self.window()
        target = win.stack.get_visible_child() if hasattr(win, 'stack') else win
        if target.go.get_sensitive():
            target.go.emit('clicked')

    def window(self):
        if self.win is None:
            self.win = self.window_class(self)
        return self.win

    def do_activate(self):
        win = self.window()
        win.present()
        shot = os.environ.get("CUBE_SHOT")
        if shot:
            def go():
                screenshot(win, shot)
                self.quit()
                return False
            GLib.timeout_add(int(os.environ.get("CUBE_SHOT_DELAY", "1200")), go)

    def do_open(self, files, _n, _hint):
        win = self.window()
        win.add_files([f.get_path() for f in files if f.get_path()])
        self.do_activate()


def file_filter(name: str, mime: str | None = None) -> Gio.ListStore:
    store = Gio.ListStore.new(Gtk.FileFilter)
    flt = Gtk.FileFilter(name=name)
    if mime:
        flt.add_mime_type(mime)
    else:
        flt.add_pattern("*")
    store.append(flt)
    return store


def _last_folder_file():
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "cube-apps", "last-folder")


def last_folder():
    """The folder the previous pick happened in (shared by the Cube apps), if it still exists."""
    try:
        with open(_last_folder_file(), encoding="utf-8") as f:
            folder = f.read(4096).strip()
    except (OSError, UnicodeError):
        return None
    return folder if folder and os.path.isabs(folder) and os.path.isdir(folder) else None


def remember_folder(folder):
    """Record where the user picked from; private, written atomically, never fatal."""
    if not folder or not os.path.isdir(folder):
        return
    path = _last_folder_file()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.last-folder-', dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(folder + "\n")
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        if 'tmp' in locals():
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _dialog(title, **props):
    dialog = Gtk.FileDialog(title=title, modal=True, **props)
    start = last_folder()
    if start:
        dialog.set_initial_folder(Gio.File.new_for_path(start))
    return dialog


def pick_files(parent, title, callback, mime=None, multiple=True):
    dialog = _dialog(title, filters=file_filter(
        "PDF documents" if mime == "application/pdf" else "All files", mime))

    def done(d, res):
        try:
            result = d.open_multiple_finish(res) if multiple else [d.open_finish(res)]
        except GLib.Error:
            return
        paths = [f.get_path() for f in result if f.get_path()]
        if paths:
            remember_folder(os.path.dirname(paths[0]))
            callback(paths)

    (dialog.open_multiple if multiple else dialog.open)(parent, None, done)


def pick_folder(parent, title, callback):
    dialog = _dialog(title)

    def done(d, res):
        try:
            folder = d.select_folder_finish(res).get_path()
        except GLib.Error:
            return
        if folder:
            remember_folder(folder)
            callback(folder)

    dialog.select_folder(parent, None, done)


def drop_target(callback):
    """A DropTarget that hands a list of paths to callback."""
    target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)

    def on_drop(_t, value, _x, _y):
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if paths:
            callback(paths)
        return bool(paths)

    target.connect("drop", on_drop)
    return target


def cube_icon(glyph: str) -> str:
    """SVG in the style of the other Cube icons; glyph is SVG drawn on the front face."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
 <rect x="4" y="4" width="120" height="120" rx="26" fill="#fbfbfb" stroke="#d4d4d4" stroke-width="2"/>
 <path d="M64 23 101 43 101 84 64 105 27 84 27 43Z" fill="#f2f2f2" stroke="#111" stroke-width="3" stroke-linejoin="round"/>
 <path d="m27 43 37 20 37-20M64 63v42" fill="none" stroke="#111" stroke-width="3"/>
 {glyph}
</svg>
'''


def unique_path(path: Path) -> Path:
    """path, or path with -2, -3... before the suffix; never an existing file."""
    path = Path(path)
    candidate, n = path, 2
    while os.path.lexists(candidate):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


def write_output(path, writer):
    """Publish a complete private file without replacing any existing directory entry."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.cube-output-', suffix=path.suffix, dir=path.parent)
    os.close(fd)
    try:
        writer(Path(temporary))
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        while True:
            destination = unique_path(path)
            try:
                os.link(temporary, destination)
                return destination
            except FileExistsError:
                continue
    finally:
        os.unlink(temporary)


def run_task(fn, done):
    """Run fn() on a thread; call done(result, error) back on the GTK loop."""
    def work():
        try:
            result, error = fn(), None
        except Exception as exc:  # surfaced to the UI by the caller
            result, error = None, exc
        GLib.idle_add(lambda: (done(result, error), False)[1])
    threading.Thread(target=work, daemon=True).start()


class FileList(Gtk.ListBox):
    """Boxed list of chosen files with remove and optional reordering."""

    def __init__(self, exts=None, reorder=False, single=False, empty="No files chosen",
                 on_changed=None):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE)
        self.add_css_class("boxed-list")
        self.paths: list[Path] = []
        self.exts = {e.lower() for e in exts} if exts else None
        self.reorder, self.single = reorder, single
        self.on_changed = on_changed or (lambda: None)
        self.placeholder = Adw.ActionRow(title=empty)
        self.placeholder.add_css_class("dim-label")
        self.refresh()

    def add_paths(self, paths):
        for value in paths:
            if not value:
                continue
            p = Path(os.path.abspath(Path(value).expanduser()))
            if self.exts and p.suffix.lower() not in self.exts:
                continue
            if p.is_file() and p not in self.paths:
                if self.single:
                    self.paths.clear()
                self.paths.append(p)
                if self.single:
                    break
        self.refresh()

    def clear(self):
        self.paths.clear()
        self.refresh()

    def remove_path(self, path):
        if path in self.paths:
            self.paths.remove(path)
            self.refresh()

    def move(self, path, delta):
        i = self.paths.index(path)
        j = i + delta
        if 0 <= j < len(self.paths):
            self.paths[i], self.paths[j] = self.paths[j], self.paths[i]
            self.refresh()

    def refresh(self):
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        if not self.paths:
            self.append(self.placeholder)
        for index, p in enumerate(self.paths):
            row = Adw.ActionRow(title=GLib.markup_escape_text(p.name),
                                subtitle=GLib.markup_escape_text(str(p.parent)),
                                title_selectable=True, subtitle_selectable=True,
                                tooltip_text=str(p))
            buttons = [("window-close-symbolic", "Remove", lambda _b, q=p: self.remove_path(q))]
            if self.reorder:
                buttons = [("go-up-symbolic", "Move up", lambda _b, q=p: self.move(q, -1)),
                           ("go-down-symbolic", "Move down", lambda _b, q=p: self.move(q, 1))] + buttons
            for icon, tip, handler in buttons:
                b = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tip)
                b.add_css_class("flat")
                b.connect("clicked", handler)
                if tip == 'Move up':
                    b.set_sensitive(index > 0)
                elif tip == 'Move down':
                    b.set_sensitive(index + 1 < len(self.paths))
                row.add_suffix(b)
            self.append(row)
        self.on_changed()


def status_label() -> Gtk.Label:
    label = Gtk.Label(wrap=True, selectable=True, xalign=0, margin_bottom=6)
    label.add_css_class("cube-status")
    return label


def quit_on_error_main(app_cls):
    sys.exit(app_cls().run(sys.argv))
