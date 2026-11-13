#!/usr/bin/env python3
"""Minimal GTK4/VTE terminal for cube-editor sudo authentication. Apache-2.0.

Opt-in built-in terminal via CUBE_EDITOR_TERMINAL=builtin when python3-gi and
gir1.2-vte-3.91 are installed.
"""
from __future__ import annotations

import os
import signal
import sys

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk

Vte = None


def load_vte():
    """Import Vte 3.91 on demand, raising ImportError if the typelib is absent."""
    global Vte
    if Vte is not None:
        return Vte
    try:
        gi.require_version('Vte', '3.91')
        from gi.repository import Vte as _Vte
    except (ImportError, ValueError) as exc:
        raise ImportError(f'Vte 3.91 typelib unavailable: {exc}') from exc
    Vte = _Vte
    return Vte


def builtin_available():
    """Return True when GTK4 VTE (gir1.2-vte-3.91) can be loaded."""
    try:
        load_vte()
    except ImportError:
        return False
    return True


def _build_terminal_widget(vte_mod, window):
    terminal = vte_mod.Terminal()
    terminal.set_scrollback_lines(1000)
    terminal.set_mouse_autohide(True)
    action_group = Gio.SimpleActionGroup()
    paste_action = Gio.SimpleAction.new('paste', None)
    paste_action.connect('activate', lambda *_: terminal.paste_clipboard())
    action_group.add_action(paste_action)
    copy_action = Gio.SimpleAction.new('copy', None)
    copy_action.connect('activate', lambda *_: terminal.copy_clipboard_format(vte_mod.Format.TEXT))
    action_group.add_action(copy_action)
    window.insert_action_group('term', action_group)
    menu = Gio.Menu()
    menu.append('Copy', 'term.copy')
    menu.append('Paste', 'term.paste')
    terminal.set_context_menu_model(menu)
    return terminal


class CubeTerminal(Gtk.Window):
    """Minimal GTK4/VTE terminal window for running authenticated commands."""

    def __init__(self, command, title='Cube Editor · Authentication', on_exit=None,
                 cancel_event=None, worker_started_cb=None):
        super().__init__(title=title, default_width=720, default_height=480)
        vte_mod = load_vte()
        self.command = list(command)
        self.on_exit = on_exit
        self.cancel_event = cancel_event
        self.worker_started_cb = worker_started_cb
        self.child_pid = None
        self._exited = False
        self._cancel_source = None

        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        self.set_titlebar(header)

        self.terminal = _build_terminal_widget(vte_mod, self)
        self.terminal.connect('child-exited', self._on_child_exited)
        self.set_child(self.terminal)
        self.connect('close-request', self._on_close_request)

        if self.cancel_event is not None:
            self._cancel_source = GLib.timeout_add(100, self._poll_cancel)
        self._spawn_command(vte_mod)

    def _poll_cancel(self):
        if self._exited:
            self._cancel_source = None
            return False
        started = self.worker_started_cb() if self.worker_started_cb else False
        if self.cancel_event.is_set() and not started:
            if self.child_pid:
                try:
                    os.killpg(self.child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self._cancel_source = None
            self.close()
            return False
        return True

    def _spawn_command(self, vte_mod):
        env = [f'{k}={v}' for k, v in os.environ.items()]
        env.extend(['TERM=xterm-256color', 'COLORTERM=truecolor'])
        flags = GLib.SpawnFlags.DO_NOT_REAP_CHILD | GLib.SpawnFlags.SEARCH_PATH
        try:
            self.terminal.spawn_async(
                vte_mod.PtyFlags.DEFAULT,
                os.getcwd(),
                self.command,
                env,
                flags,
                None,
                -1,
                None,
                self._on_spawn_complete,
                None,
            )
        except GLib.Error as exc:
            self._show_error(f'Failed to spawn terminal: {exc}')
            self._schedule_close()

    def _on_spawn_complete(self, terminal, result, _user_data=None):
        try:
            self.child_pid = terminal.spawn_finish(result)
        except GLib.Error as exc:
            self._show_error(f'Spawn failed: {exc}')
            self._schedule_close()

    def _on_child_exited(self, _terminal, status):
        if self._exited:
            return
        self._exited = True
        if self._cancel_source is not None:
            GLib.source_remove(self._cancel_source)
            self._cancel_source = None
        if self.on_exit:
            GLib.idle_add(self.on_exit, status)

    def _on_close_request(self, _window):
        if self._cancel_source is not None:
            GLib.source_remove(self._cancel_source)
            self._cancel_source = None
        if self.child_pid and not self._exited:
            try:
                os.killpg(self.child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        return False

    def _show_error(self, message):
        dialog = Gtk.AlertDialog(message='Terminal Error', detail=message)
        dialog.show(self)

    def _schedule_close(self):
        GLib.timeout_add(500, self.close)


def run_in_terminal(command, title='Cube Editor · Authentication',
                    cancel_event=None, worker_started_cb=None):
    """Run a command in a minimal GTK4/VTE terminal. Blocks until window closes."""
    load_vte()
    app = Gtk.Application(application_id='org.whitecube.CubeTerminal')
    exit_status = {'status': None}

    def on_activate(gtk_app):
        window = CubeTerminal(
            command,
            title,
            on_exit=lambda status: _on_exit(status, gtk_app),
            cancel_event=cancel_event,
            worker_started_cb=worker_started_cb,
        )
        window.set_application(gtk_app)
        window.present()

    def _on_exit(status, gtk_app):
        exit_status['status'] = status
        gtk_app.quit()

    app.connect('activate', on_activate)
    app.run(None)
    return exit_status['status']


if __name__ == '__main__':
    cmd = sys.argv[1:] if len(sys.argv) > 1 else ['bash', '-c', 'echo "Test terminal"; read -p "Press Enter..."']
    sys.exit(run_in_terminal(cmd) or 0)
