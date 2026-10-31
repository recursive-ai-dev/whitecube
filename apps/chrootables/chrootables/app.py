"""Native desktop workspace, recovery and diagnostics interface.

Adapted for Whitecube: application identity and terminal authentication.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QEvent, QSettings, QSize, QTimer, QProcess
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QSpinBox, QCheckBox,
    QListWidget, QListWidgetItem, QStackedWidget, QSplitter, QScrollArea,
    QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QMessageBox, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QProgressBar, QMenu, QFileDialog, QLayout,
)
from . import __version__
from .capture import save_screenshot
from .backend import DEFAULT_STORE, ROOT, PROFILES, SUITES, archive_file, name, shared_folder, session_command, terminal_command
from .client import Client
from .style import STYLE, scaled_style
from .terminal import TerminalWindow
from .paths import store_path

# QTextDocument counts UTF-16 units. Limit retained text as well as its block
# count, including output which never emits a newline.
MAX_CONSOLE_CHARACTERS = 256 * 1024


def label(text, role=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setObjectName(role)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback=None, role=None):
    widget = QPushButton(text)
    if role:
        widget.setObjectName(role)
    if callback:
        widget.clicked.connect(callback)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


def box(horizontal=False, margins=0, spacing=12):
    widget = QWidget()
    layout = QHBoxLayout(widget) if horizontal else QVBoxLayout(widget)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return widget, layout


def clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear(item.layout())


def capacity(mib):
    return f"{mib / 1024:g} GiB" if mib >= 1024 else f"{mib} MiB"


def byte_size(value):
    return capacity(round(value / 1048576))


@lru_cache(maxsize=512)
def when(value):
    if not value or value in ("unknown", "never"):
        return "Not yet" if value == "never" else "Unknown"
    try:
        return datetime.fromisoformat(value).strftime("%b %d · %H:%M")
    except ValueError:
        return str(value)


_GLYPH_CACHE = {}


def glyph(kind="box", color="#a8edc9", size=40):
    key = (kind, color, size)
    cached = _GLYPH_CACHE.get(key)
    if cached is not None and not cached.isNull():
        return cached
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), max(1.5, size / 22)))
    painter.setBrush(QColor("#21332e"))
    painter.drawRoundedRect(2, 2, size - 4, size - 4, size / 4, size / 4)
    if kind == "box":
        painter.drawLine(int(size * .28), int(size * .38), int(size * .48), int(size * .53))
        painter.drawLine(int(size * .48), int(size * .53), int(size * .28), int(size * .69))
        painter.drawLine(int(size * .55), int(size * .68), int(size * .74), int(size * .68))
    else:
        painter.setFont(QFont("Noto Sans", int(size * .29), QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, kind)
    painter.end()
    _GLYPH_CACHE[key] = pixmap
    return pixmap


class CreateDialog(QDialog):
    def __init__(self, existing, parent=None):
        super().__init__(parent)
        self.existing = existing
        self.setWindowTitle("Create a workspace")
        self.setMinimumWidth(550)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)
        layout.addWidget(label("A fresh place to build.", "title"))
        layout.addWidget(label("A real Debian filesystem, with a lifecycle you control.", "muted"))
        form = QFormLayout()
        form.setSpacing(14)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. python-playground")
        self.name_input.setObjectName("workspaceName")
        self.profile = QComboBox()
        for key, (title, description, suggested) in PROFILES.items():
            self.profile.addItem(title, key)
        self.profile_description = label(PROFILES["minimal"][1], "muted", True)
        self.suite = QComboBox()
        for key, title in SUITES.items():
            self.suite.addItem(title, key)
        size_row_widget, size_row = box(horizontal=True, spacing=8)
        self.size_input = QSpinBox()
        self.size_input.setRange(1, 1024)
        self.size_input.setValue(2)
        self.size_input.setSuffix(" GiB")
        self.size_unit = QComboBox()
        self.size_unit.addItem("GiB", "gib")
        self.size_unit.addItem("MiB (512–1048576)", "mib")
        self.size_unit.setAccessibleName("Capacity unit")
        size_row.addWidget(self.size_input, 1)
        size_row.addWidget(self.size_unit)
        self.lifecycle = QComboBox()
        self.lifecycle.addItem("Persistent — keep my changes", "persistent")
        self.lifecycle.addItem("Disposable — reset after every session", "ephemeral")
        form.addRow("Workspace name", self.name_input)
        form.addRow("Starting profile", self.profile)
        form.addRow("", self.profile_description)
        form.addRow("Distribution", self.suite)
        form.addRow("Image capacity", size_row_widget)
        form.addRow("Session lifecycle", self.lifecycle)
        layout.addLayout(form)
        layout.addWidget(label("Debian packages are verified by debootstrap. Creation checks available capacity and downloads a base system. Sparse image files grow as data is written; free space is not reserved.", "muted", True))
        self.error = label("", "banner", True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.create_button = buttons.addButton("Create workspace", QDialogButtonBox.ButtonRole.AcceptRole)
        self.create_button.setObjectName("primary")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.validate)
        layout.addWidget(buttons)
        self.profile.currentIndexChanged.connect(self.select_profile)
        self.size_unit.currentIndexChanged.connect(self.change_size_unit)
        self.name_input.textChanged.connect(lambda: self.error.clear())
        self.name_input.setFocus()

    def change_size_unit(self):
        current = self.size_input.value()
        if self.size_unit.currentData() == "mib":
            self.size_input.setRange(512, 1048576)
            self.size_input.setSingleStep(512)
            self.size_input.setSuffix(" MiB")
            self.size_input.setValue(max(512, min(1048576, current * 1024)))
        else:
            self.size_input.setRange(1, 1024)
            self.size_input.setSingleStep(1)
            self.size_input.setSuffix(" GiB")
            self.size_input.setValue(max(1, min(1024, round(current / 1024))))

    def set_size_mib(self, mib):
        self.size_unit.setCurrentIndex(self.size_unit.findData("mib"))
        self.size_input.setValue(max(512, min(1048576, int(mib))))

    def select_profile(self):
        data = PROFILES[self.profile.currentData()]
        self.profile_description.setText(data[1])
        if self.size_unit.currentData() == "mib":
            self.size_input.setValue(data[2] * 1024)
        else:
            self.size_input.setValue(data[2])

    def validate(self):
        try:
            value = name(self.name_input.text().strip())
            if value in self.existing:
                raise ValueError("That workspace name is already in use.")
        except ValueError as error:
            self.error.setText(str(error))
            return
        self.accept()

    def parameters(self):
        size_mib = self.size_input.value() if self.size_unit.currentData() == "mib" else self.size_input.value() * 1024
        return {"name": self.name_input.text().strip(), "size_mib": size_mib,
                "profile": self.profile.currentData(), "suite": self.suite.currentData(),
                "lifecycle": self.lifecycle.currentData()}


class ImportDialog(QDialog):
    def __init__(self, existing, initial_archive="", parent=None, managed_export=None):
        super().__init__(parent)
        self.existing = existing
        # A retained export lives in the root-only store, where this unprivileged
        # process cannot read it. The broker resolves and validates it by name.
        self.managed_export = managed_export
        self.setWindowTitle("Import workspace archive")
        self.setMinimumWidth(550)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)
        layout.addWidget(label("Rehydrate an exported workspace.", "title"))
        layout.addWidget(label("Unpack a .tar.gz export into a fresh sparse ext4 workspace image.", "muted", True))
        form = QFormLayout()
        form.setSpacing(14)
        archive_widget, archive_row = box(horizontal=True, spacing=8)
        self.archive_input = QLineEdit(initial_archive)
        self.archive_input.setPlaceholderText("/path/to/workspace-export.tar.gz")
        self.browse_button = button("Browse…", self.browse_archive)
        if managed_export:
            self.archive_input.setReadOnly(True)
            self.browse_button.setEnabled(False)
        archive_row.addWidget(self.archive_input, 1)
        archive_row.addWidget(self.browse_button)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. restored-lab")
        if initial_archive:
            stem = Path(initial_archive).name
            for suffix in (".tar.gz", ".tgz"):
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)]
                    break
            self.name_input.setText(stem[:56] + "-import" if stem else "")
        self.profile = QComboBox()
        for key, (title, description, suggested) in PROFILES.items():
            self.profile.addItem(title, key)
        self.suite = QComboBox()
        for key, title in SUITES.items():
            self.suite.addItem(title, key)
        self.size_input = QSpinBox()
        self.size_input.setRange(512, 1048576)
        self.size_input.setSingleStep(512)
        self.size_input.setValue(2048)
        self.size_input.setSuffix(" MiB")
        self.size_input.setEnabled(False)
        self.auto_size = QCheckBox("Fit capacity to the archive")
        self.auto_size.setChecked(True)
        self.auto_size.toggled.connect(lambda checked: self.size_input.setEnabled(not checked))
        size_widget, size_row = box(horizontal=True, spacing=8)
        size_row.addWidget(self.size_input, 1)
        size_row.addWidget(self.auto_size)
        self.lifecycle = QComboBox()
        self.lifecycle.addItem("Persistent — keep my changes", "persistent")
        self.lifecycle.addItem("Disposable — reset after every session", "ephemeral")
        form.addRow("Archive (.tar.gz)", archive_widget)
        form.addRow("Workspace name", self.name_input)
        form.addRow("Profile metadata", self.profile)
        form.addRow("Distribution", self.suite)
        form.addRow("Image capacity", size_widget)
        form.addRow("Session lifecycle", self.lifecycle)
        layout.addLayout(form)
        self.error = label("", "banner", True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.import_button = buttons.addButton("Import workspace", QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setObjectName("primary")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.validate)
        layout.addWidget(buttons)
        self.profile.currentIndexChanged.connect(lambda: self.size_input.setValue(PROFILES[self.profile.currentData()][2] * 1024))
        self.name_input.textChanged.connect(lambda: self.error.clear())
        self.archive_input.textChanged.connect(lambda: self.error.clear())

    def browse_archive(self):
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose workspace archive", str(Path.home()), "Compressed archive (*.tar.gz *.tgz)")
        if chosen:
            self.archive_input.setText(chosen)

    def validate(self):
        try:
            if not self.managed_export:
                archive_file(self.archive_input.text().strip())
            value = name(self.name_input.text().strip())
            if value in self.existing:
                raise ValueError("That workspace name is already in use.")
        except ValueError as error:
            self.error.setText(str(error))
            return
        self.accept()

    def parameters(self):
        result = {
            "name": self.name_input.text().strip(),
            "profile": self.profile.currentData(),
            "suite": self.suite.currentData(),
            "lifecycle": self.lifecycle.currentData(),
        }
        if self.managed_export:
            result["export"] = self.managed_export
        else:
            result["archive"] = self.archive_input.text().strip()
        if not self.auto_size.isChecked():
            result["size_mib"] = self.size_input.value()
        return result


class ValueDialog(QDialog):
    def __init__(self, title, description, field="Name", value="", minimum=None, parent=None,
                 forbidden=None, collision_message="That name is already in use."):
        super().__init__(parent)
        self.forbidden = set(forbidden or ())
        self.collision_message = collision_message
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(label(title, "detailTitle"))
        layout.addWidget(label(description, "muted", True))
        layout.addWidget(label(field))
        self.input = QSpinBox() if minimum is not None else QLineEdit()
        if minimum is not None:
            self.input.setRange(minimum, 1048576)
            self.input.setValue(minimum)
            self.input.setSuffix(" MiB")
        else:
            self.input.setText(value)
        layout.addWidget(self.input)
        self.error = label("", "banner", True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setObjectName("primary")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if isinstance(self.input, QLineEdit):
            self.input.textChanged.connect(self._check_inline)
            if self.forbidden:
                self._check_inline()

    def _check_inline(self):
        if not isinstance(self.input, QLineEdit):
            return
        candidate = self.input.text().strip()
        if self.forbidden and candidate in self.forbidden:
            self.error.setText(self.collision_message)
            self.ok_button.setEnabled(False)
        else:
            self.error.clear()
            self.ok_button.setEnabled(True)

    def validate(self):
        if isinstance(self.input, QLineEdit):
            try:
                candidate = name(self.input.text().strip())
                if candidate in self.forbidden:
                    raise ValueError(self.collision_message)
            except ValueError as error:
                self.error.setText(str(error))
                return
        self.accept()

    def value(self):
        return self.input.value() if isinstance(self.input, QSpinBox) else self.input.text().strip()


class WorkspaceRow(QWidget):
    def __init__(self, item, favorite=False):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 15)
        self.icon_label = label("")
        layout.addWidget(self.icon_label)
        text, column = box(spacing=5)
        self.title_label = label("", "section")
        self.meta_label = label("", "muted")
        column.addWidget(self.title_label)
        column.addWidget(self.meta_label)
        layout.addWidget(text, 1)
        state, status = box(spacing=5)
        self.status_label = label("", "badge")
        self.mode_label = label("", "muted")
        self.mode_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        status.addWidget(self.status_label)
        status.addWidget(self.mode_label)
        layout.addWidget(state)
        self._state = None
        self.update_row(item, favorite)

    def update_row(self, item, favorite=False):
        profile = item.get("profile", "minimal")
        suite = item.get("suite", "unknown")
        size_mib = item["size_mib"]
        busy = item["status"] == "busy"
        ephemeral = item["lifecycle"] == "ephemeral"
        state = (item["name"], favorite, profile, suite, size_mib, busy, ephemeral)
        if state == self._state:
            return
        self._state = state
        self.icon_label.setPixmap(glyph({
            "python": "Py", "build": "C+", "network": "~",
            "rust": "Rs", "go": "Go", "web": "JS", "minimal": "Min",
        }.get(profile, "box"), size=44))
        self.title_label.setText(("★  " if favorite else "") + item["name"])
        self.meta_label.setText(f"Debian {suite}  ·  {PROFILES.get(profile, (profile,))[0]}  ·  {capacity(size_mib)}")
        self.status_label.setText("●  In use" if busy else "●  Ready")
        self.mode_label.setText("Disposable" if ephemeral else "Persistent")


class NotesDialog(QDialog):
    def __init__(self, target, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Notes · {target}")
        self.resize(540, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(label("Personal notes, saved on this host for this workspace store.", "muted", True))
        self.editor = QPlainTextEdit(text)
        self.editor.setAccessibleName("Workspace notes")
        self.editor.setPlaceholderText("Commands, project details, things to try…")
        layout.addWidget(self.editor, 1)
        self.limit_label = label("", "muted")
        layout.addWidget(self.limit_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.editor.textChanged.connect(self.validate)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.validate()

    def validate(self):
        length = len(self.editor.toPlainText())
        self.limit_label.setText(f"{length:,} / 4,000 characters")
        self.save_button.setEnabled(length <= 4000)

    def value(self):
        return self.editor.toPlainText().strip()


class Window(QMainWindow):
    def __init__(self, store=DEFAULT_STORE, demo=False):
        store = store_path(store)
        super().__init__()
        self.setWindowTitle("Cube Chrootables")
        self.setWindowIcon(QIcon(glyph(size=64)))
        self.resize(1360, 920)
        self.setMinimumSize(900, 640)
        self.store = Path(store)
        self.demo = demo
        self.settings = QSettings("whitecube", "cube-chrootables")
        self.session_settings_prefix = "sessions/" + sha256(str(self.store.resolve()).encode()).hexdigest()
        favorites_key = self.session_settings_prefix + "/favorites"
        if (not demo and not self.settings.contains(favorites_key)
                and self.store.resolve() == DEFAULT_STORE.resolve() and self.settings.contains("favorites")):
            # The former global preference has no store identity. Preserve it
            # for the default store once, without leaking it to custom stores.
            legacy = self.settings.value("favorites", [], type=list)
            self.settings.setValue(favorites_key, [value for value in legacy if isinstance(value, str)])
        self.favorites = set(value for value in
                             ([] if demo else self.settings.value(favorites_key, [], type=list))
                             if isinstance(value, str))
        self.data = {"images": [], "snapshots": [], "activity": [], "free_bytes": 0}
        self.selected = None
        self.network_choices = {}
        self.restricted_choices = {}
        self.shared_folder_choices = {}
        self.notes_cache = {}
        self.busy = False
        self.authenticating = False
        self.refreshing = False
        self.pending_action = None
        self.last_task = ""
        self.task_started = None
        self.task_phase_text = ""
        self.health_ready = False
        self.missing_dependencies = False
        self.store_locked = False
        self.current_view = 0
        self._detail_state = None
        self._render_signature = None
        self.terminals = []
        self.client = Client(store, self)
        self.client.ready.connect(self.connected)
        self.client.disconnected.connect(self.disconnected)
        self.client.output.connect(lambda identifier, text: self.append_output(text))
        self.client.phase.connect(lambda identifier, text: self.set_task_phase(text))
        self.build_ui()
        self.task_timer = QTimer(self)
        self.task_timer.setInterval(1000)
        self.task_timer.timeout.connect(self.update_task_elapsed)
        self.task_timer.start()
        self.change_text_scale()
        if not demo:
            geometry = self.settings.value("window/geometry")
            if geometry is not None:
                try:
                    self.restoreGeometry(geometry)
                except (TypeError, ValueError):
                    pass
            sizes = self.settings.value("window/splitter", [], type=list)
            if len(sizes) == 2:
                try:
                    self.splitter.setSizes([max(1, int(value)) for value in sizes])
                except (ValueError, TypeError):
                    pass
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_timer)
        self.timer.start(6000)
        for keys, callback in [("Ctrl+N", self.create), ("Ctrl+F", self.focus_search), ("F5", self.refresh), ("Ctrl+Return", self.open_terminal)]:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(callback)
        for index in range(4):
            shortcut = QShortcut(QKeySequence(f"Alt+{index + 1}"), self)
            shortcut.activated.connect(lambda i=index: self.navigate(i))
        if demo:
            self.load_demo()
        else:
            QTimer.singleShot(0, self.client.start)

    def build_ui(self):
        central, horizontal = box(horizontal=True, spacing=0)
        self.setCentralWidget(central)
        sidebar = QFrame()
        self.sidebar = sidebar
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(178)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 18, 12, 16)
        side.setSpacing(9)
        logo = label("")
        logo.setPixmap(glyph(size=45))
        side.addWidget(logo)
        side.addSpacing(6)
        side.addWidget(label("cube-chrootables", "brand", True))
        side.addWidget(label("A little space. No limits.", "muted", True))
        side.addSpacing(35)
        side.addWidget(label("WORKBENCH", "eyebrow"))
        self.nav = []
        for index, title in enumerate(["▦   Workspaces", "◷   Recovery", "≡   Activity", "＋   Host health"]):
            control = button(title, lambda checked=False, i=index: self.navigate(i), "nav")
            control.setCheckable(True)
            self.nav.append(control)
            side.addWidget(control)
        self.nav[0].setChecked(True)
        side.addStretch()
        side.addWidget(label("LOCAL ENGINE", "eyebrow"))
        side.addWidget(label("Debian image workspaces", "muted", True))
        self.access_label = label("Connecting…", "muted", True)
        side.addWidget(self.access_label)
        self.unlock_button = button("Unlock store", self.unlock)
        self.unlock_button.setToolTip("Unlock workspace store")
        self.unlock_button.setStyleSheet("padding: 8px 5px;")
        side.addWidget(self.unlock_button)
        self.text_scale = QComboBox()
        self.text_scale.setAccessibleName("Interface text size")
        for value in (100, 125, 150):
            self.text_scale.addItem(f"Text size · {value}%", value)
        try:
            saved_scale = int(self.settings.value("appearance/text_scale", 100)) if not self.demo else 100
        except (TypeError, ValueError):
            saved_scale = 100
        self.text_scale.setCurrentIndex(max(0, self.text_scale.findData(saved_scale)))
        self.text_scale.currentIndexChanged.connect(self.change_text_scale)
        side.addWidget(self.text_scale)
        side.addSpacing(14)
        side.addWidget(label(f"DESKTOP {__version__}", "eyebrow"))
        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setWidget(sidebar)
        self.sidebar_scroll.setFixedWidth(196)
        horizontal.addWidget(self.sidebar_scroll)

        content, self.content_layout = box(margins=16, spacing=12)
        self.content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setWidget(content)
        horizontal.addWidget(self.content_scroll, 1)
        top, row = box(horizontal=True)
        headings, titles = box(spacing=6)
        self.eyebrow = label("WORKSPACE LIBRARY", "eyebrow")
        self.heading = label("Your Linux, on your terms.", "title", True)
        self.subtitle = label("Build, break, rewind. Keep your host out of the experiment.", "subtitle", True)
        titles.addWidget(self.eyebrow)
        titles.addWidget(self.heading)
        titles.addWidget(self.subtitle)
        row.addWidget(headings, 1)
        self.refresh_button = button("↻", self.refresh)
        self.refresh_button.setToolTip("Refresh · F5")
        self.refresh_button.setAccessibleName("Refresh workspaces")
        row.addWidget(self.refresh_button)
        self.import_button = button("Import…", self.import_archive)
        self.import_button.setToolTip("Import a .tar.gz export into a workspace")
        self.import_button.setAccessibleName("Import workspace archive")
        row.addWidget(self.import_button)
        self.new_button = button("＋  New workspace", self.create, "primary")
        row.addWidget(self.new_button)
        self.content_layout.addWidget(top)

        self.notice = QFrame()
        self.notice.setObjectName("notice")
        notice_row = QHBoxLayout(self.notice)
        self.notice_text = label("", "banner", True)
        notice_row.addWidget(self.notice_text, 1)
        self.notice_action = button("View host health", lambda: self.navigate(3))
        notice_row.addWidget(self.notice_action)
        self.notice.hide()
        self.content_layout.addWidget(self.notice)

        self.pages = QStackedWidget()
        self.content_layout.addWidget(self.pages, 1)
        self.build_library()
        self.build_recovery()
        self.build_activity()
        self.build_health()

        self.task_panel, task_layout = box(spacing=8)
        task_top, task_row = box(horizontal=True)
        self.task_title = label("Task output", "section")
        task_row.addWidget(self.task_title, 1)
        self.cancel_task_button = button("Cancel", self.cancel_operation, "danger")
        self.cancel_task_button.hide()
        task_row.addWidget(self.cancel_task_button)
        task_row.addWidget(button("Copy output", lambda: QApplication.clipboard().setText(self.task_output.toPlainText()), "quiet"))
        self.hide_task = button("Hide", self.task_panel.hide, "quiet")
        task_row.addWidget(self.hide_task)
        task_layout.addWidget(task_top)
        task_status, task_status_row = box(horizontal=True, spacing=8)
        self.task_phase = label("", "muted", True)
        task_status_row.addWidget(self.task_phase, 1)
        self.follow_output = QCheckBox("Follow output")
        self.follow_output.setChecked(True)
        self.follow_output.setAccessibleName("Follow operation output")
        task_status_row.addWidget(self.follow_output)
        task_layout.addWidget(task_status)
        self.task_progress = QProgressBar()
        self.task_progress.setRange(0, 0)
        self.task_progress.setTextVisible(False)
        task_layout.addWidget(self.task_progress)
        self.task_output = QPlainTextEdit()
        self.task_output.setReadOnly(True)
        self.task_output.setMaximumBlockCount(4000)
        self.task_output.setFixedHeight(100)
        task_layout.addWidget(self.task_output)
        self.task_panel.hide()
        self.content_layout.addWidget(self.task_panel)
        footer, footer_row = box(horizontal=True)
        self.status = label("Reading local workspaces…", "muted", True)
        footer_row.addWidget(self.status, 1)
        self.task_reopen = button("Task output ↗", self.task_panel.show, "quiet")
        self.task_reopen.hide()
        footer_row.addWidget(self.task_reopen)
        footer_row.addWidget(label("Alt 1–4  views", "muted"))
        self.content_layout.addWidget(footer)

    def build_library(self):
        page, layout = box(spacing=12)
        metrics, row = box(horizontal=True, spacing=12)
        self.metrics = []
        for title, subtitle in [("WORKSPACES", "Your local environments"), ("IN USE", "Sessions or operations"), ("RECOVERY POINTS", "Room to experiment"), ("HOST FREE", "Available for images")]:
            card = QFrame()
            card.setObjectName("card")
            column = QVBoxLayout(card)
            column.setContentsMargins(17, 16, 17, 16)
            column.addWidget(label(title, "eyebrow", True))
            value = label("—", "metric")
            self.metrics.append(value)
            column.addWidget(value)
            column.addWidget(label(subtitle, "muted", True))
            row.addWidget(card, 1)
        layout.addWidget(metrics)
        tools, row = box(horizontal=True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search workspaces, profiles, releases or notes…")
        self.search.setAccessibleName("Search workspaces")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.render_library)
        row.addWidget(self.search, 1)
        self.filter = QComboBox()
        self.filter.setAccessibleName("Filter workspaces")
        self.filter.addItems(["All workspaces", "Persistent", "Disposable", "Favorites", "In use"])
        row.addWidget(self.filter)
        self.sort = QComboBox()
        self.sort.setAccessibleName("Sort workspaces")
        for text, key in [("Favorites first", "favorites"), ("Name", "name"),
                          ("Recently used", "recent"), ("Newest first", "created"),
                          ("Largest capacity", "capacity"), ("Most disk space", "allocated")]:
            self.sort.addItem(text, key)
        if not self.demo:
            saved_filter = self.settings.value(self.session_settings_prefix + "/filter", "All workspaces", type=str)
            self.filter.setCurrentIndex(max(0, self.filter.findText(saved_filter)))
            saved_sort = self.settings.value(self.session_settings_prefix + "/sort", "favorites", type=str)
            self.sort.setCurrentIndex(max(0, self.sort.findData(saved_sort)))
        self.filter.currentIndexChanged.connect(self.library_preferences_changed)
        self.sort.currentIndexChanged.connect(self.library_preferences_changed)
        row.addWidget(self.sort)
        layout.addWidget(tools)
        self.library_count = label("", "muted")
        layout.addWidget(self.library_count)
        self.library_stack = QStackedWidget()
        layout.addWidget(self.library_stack, 1)
        self.splitter = QSplitter()
        self.splitter.setHandleWidth(16)
        self.workspaces = QListWidget()
        self.workspaces.setMinimumWidth(280)
        self.workspaces.setUniformItemSizes(True)
        self.workspaces.currentItemChanged.connect(self.select_workspace)
        self.workspaces.itemDoubleClicked.connect(lambda _: self.open_terminal())
        self.splitter.addWidget(self.workspaces)
        self.details = QScrollArea()
        self.details.setWidgetResizable(True)
        self.details.setMinimumWidth(280)
        self.build_detail_card()
        self.splitter.addWidget(self.details)
        self.splitter.setSizes([620, 350])
        self.library_stack.addWidget(self.splitter)
        empty, empty_layout = box(spacing=18)
        empty_layout.addStretch()
        icon = label("")
        icon.setPixmap(glyph(size=80))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(icon)
        self.empty_title = label("Make room for your next idea.", "title")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        self.empty_text = label("Start with a clean Debian image or a ready-to-build profile.\nKeep your changes, or start fresh every time.", "muted")
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_text)
        self.empty_create = button("Create your first workspace", lambda: self.unlock() if self.store_locked else self.create(), "primary")
        empty_layout.addWidget(self.empty_create, 0, Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch()
        self.library_stack.addWidget(empty)
        self.pages.addWidget(page)

    def make_table(self, columns):
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(54)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setAlternatingRowColors(False)
        return table

    def build_recovery(self):
        page, layout = box(spacing=16)
        layout.addWidget(label("Restore with a safety net", "section"))
        layout.addWidget(label("A restore first saves your current image. Recovery points survive workspace deletion.", "muted", True))
        self.storage_summary = label("", "muted", True)
        layout.addWidget(self.storage_summary)
        self.interrupted_frame, interrupted_row = box(horizontal=True, spacing=8)
        self.interrupted_label = label("", "banner", True)
        interrupted_row.addWidget(self.interrupted_label, 1)
        self.discard_operation_button = button("Discard", self.discard_interrupted_operation, "danger")
        interrupted_row.addWidget(self.discard_operation_button)
        self.recover_operation_button = button("Recover publication", self.recover_interrupted_operation, "primary")
        interrupted_row.addWidget(self.recover_operation_button)
        self.interrupted_frame.hide()
        layout.addWidget(self.interrupted_frame)
        self.exports_frame, exports_row = box(horizontal=True, spacing=8)
        exports_row.addWidget(label("Retained exports:", "muted"))
        self.exports_combo = QComboBox()
        self.exports_combo.setAccessibleName("Retained export archives")
        self.exports_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.exports_combo.setMinimumContentsLength(12)
        exports_row.addWidget(self.exports_combo, 1)
        self.reimport_export_button = button("Re-import…", self.import_selected_export)
        self.delete_export_button = button("Delete export", self.delete_selected_export, "danger")
        exports_row.addWidget(self.reimport_export_button)
        exports_row.addWidget(self.delete_export_button)
        self.exports_frame.hide()
        layout.addWidget(self.exports_frame)
        self.recovery_search = QLineEdit()
        self.recovery_search.setPlaceholderText("Search recovery points by workspace, label or date…")
        self.recovery_search.setAccessibleName("Search recovery points")
        self.recovery_search.setClearButtonEnabled(True)
        self.recovery_search.textChanged.connect(self.render_recovery)
        layout.addWidget(self.recovery_search)
        self.recovery_table = self.make_table(["Workspace", "Recovery point", "Capacity", "Saved"])
        layout.addWidget(self.recovery_table, 1)
        self.recovery_empty = label("No recovery points yet. Select a workspace and choose Save snapshot.", "muted", True)
        layout.addWidget(self.recovery_empty)
        actions, row = box(horizontal=True)
        row.addStretch()
        self.drop_snapshot_button = button("Delete recovery point", self.drop_snapshot, "danger")
        self.restore_button = button("Restore workspace", self.restore, "primary")
        row.addWidget(self.drop_snapshot_button)
        row.addWidget(self.restore_button)
        layout.addWidget(actions)
        self.recovery_table.itemSelectionChanged.connect(self.update_controls)
        self.pages.addWidget(page)

    def build_activity(self):
        page, layout = box(spacing=16)
        layout.addWidget(label("The story of your workspaces", "section"))
        layout.addWidget(label("Completed operations, session history and their full diagnostic output.", "muted"))
        tools, row = box(horizontal=True)
        self.activity_search = QLineEdit()
        self.activity_search.setPlaceholderText("Search operations by workspace, action or date…")
        self.activity_search.setAccessibleName("Search operation history")
        self.activity_search.setClearButtonEnabled(True)
        self.activity_search.textChanged.connect(self.render_activity)
        row.addWidget(self.activity_search, 1)
        self.activity_filter = QComboBox()
        self.activity_filter.setAccessibleName("Filter operation results")
        self.activity_filter.addItems(["All results", "Failed", "Completed"])
        self.activity_filter.currentIndexChanged.connect(self.render_activity)
        row.addWidget(self.activity_filter)
        layout.addWidget(tools)
        self.activity_table = self.make_table(["When", "Operation", "Workspace", "Result"])
        self.activity_table.itemDoubleClicked.connect(lambda _: self.activity_log())
        layout.addWidget(self.activity_table, 1)
        self.activity_empty = label("Your first operation will appear here.", "muted")
        layout.addWidget(self.activity_empty)
        row_widget, row = box(horizontal=True)
        row.addStretch()
        self.prune_logs_button = button("Prune old logs…", self.prune_logs)
        row.addWidget(self.prune_logs_button)
        row.addWidget(button("View operation log", self.activity_log))
        layout.addWidget(row_widget)
        self.pages.addWidget(page)

    def build_health(self):
        page, layout = box(spacing=16)
        layout.addWidget(label("Ready before you press Create", "section", True))
        header, row = box(horizontal=True)
        row.addStretch()
        row.addWidget(button("Run checks", self.check_health))
        row.addWidget(button("Copy report", lambda: QApplication.clipboard().setText(self.health_output.toPlainText()), "quiet"))
        self.setup_button = button("Install missing dependencies", self.setup, "primary")
        row.addWidget(self.setup_button)
        layout.addWidget(header)
        layout.addWidget(label("Checks host commands, filesystem support and Linux namespaces. Mount permissions are tested when a workspace starts.", "muted", True))
        self.health_summary = label("Run checks to inspect this host and workspace store.", "muted", True)
        layout.addWidget(self.health_summary)
        self.health_output = QPlainTextEdit()
        self.health_output.setReadOnly(True)
        layout.addWidget(self.health_output, 1)
        archive_bar, archive_row = box(horizontal=True, spacing=8)
        self.archive_status = label("Offline cache: none synced yet", "muted", True)
        archive_row.addWidget(self.archive_status, 1)
        self.archive_suite = QComboBox()
        self.archive_suite.setAccessibleName("Offline archive release")
        self.archive_suite.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.archive_suite.setMinimumContentsLength(10)
        for key, title in SUITES.items():
            self.archive_suite.addItem(title, key)
        archive_row.addWidget(self.archive_suite)
        self.sync_archive_button = button("Sync offline archive", self.sync_archive)
        archive_row.addWidget(self.sync_archive_button)
        layout.addWidget(archive_bar)
        layout.addWidget(label("WORKSPACE STORE", "eyebrow"))
        layout.addWidget(label(str(self.store), "muted"))
        layout.addWidget(label("Trust model: workspaces run as root and share host devices. Use them for trusted development and testing; they are not a security boundary for hostile software.", "muted", True))
        self.pages.addWidget(page)

    def navigate(self, index):
        self.current_view = index
        self.pages.setCurrentIndex(index)
        titles = [
            ("WORKSPACE LIBRARY", "Your Linux, on your terms.", "Build, break, rewind. Keep your host out of the experiment."),
            ("RECOVERY LIBRARY", "Go ahead. Try the idea.", "Your checkpoints are here when you need a fresh start."),
            ("OPERATION HISTORY", "Nothing happens in the dark.", "A clear trail from the first image to the last session."),
            ("HOST HEALTH", "A solid place to start.", "Understand your machine before starting a workspace."),
        ]
        for widget, text in zip((self.eyebrow, self.heading, self.subtitle), titles[index]):
            widget.setText(text)
        for i, control in enumerate(self.nav):
            control.setChecked(i == index)
        if index == 3 and not self.health_output.toPlainText():
            self.check_health()

    def connected(self, writable):
        self.authenticating = False
        self.access_label.setText("●  Administrator connected" if writable else "●  Read-only connection")
        self.unlock_button.setText("Lock store" if writable else "Unlock store")
        self.unlock_button.setEnabled(True)
        self.update_controls()
        self.render_library()
        self.refresh()
        self.check_health()
        if writable and self.pending_action:
            action, params = self.pending_action
            self.pending_action = None
            self.run_operation(action, params)

    def disconnected(self, error):
        self.authenticating = False
        self.pending_action = None
        self.access_label.setText("○  Disconnected")
        self.unlock_button.setEnabled(True)
        self.unlock_button.setText("Reconnect / unlock")
        self.show_notice(error)
        self.status.setText(error)
        self.update_controls()
        self.render_library()
        # Starting the administrator helper already stopped the read-only one, so
        # a cancelled prompt must not leave the library stale until a later unlock.
        if self.client.requested_writable and not self.demo:
            failed_process = self.client.process
            QTimer.singleShot(0, lambda: self.client.start(False)
                             if self.client.process is failed_process and not self.authenticating else None)

    def unlock(self):
        if self.demo or self.busy:
            return
        if self.authenticating:
            self.authenticating = False
            self.pending_action = None
            self.client.start(False)
            self.unlock_button.setText("Unlock store")
            self.access_label.setText("○  Authentication cancelled")
            self.show_notice("Administrator authentication was cancelled. Unlock store to try again.")
            self.update_controls()
            self.render_library()
            return
        if self.client.writable:
            self.client.start(False)
        else:
            self.authenticating = True
            self.unlock_button.setText("Cancel authentication")
            self.access_label.setText("Authenticate with sudo in the terminal…")
            self.update_controls()
            self.render_library()
            self.client.start(True)

    def refresh(self):
        if self.demo or self.refreshing or not self.client.is_ready:
            return
        self.refreshing = True
        def done(ok, data):
            self.refreshing = False
            if ok:
                was_locked = self.store_locked
                self.store_locked = False
                self.empty_create.setText("Create your first workspace")
                signature = (data, self.busy, self.demo, was_locked)
                self.data = data
                if signature != self._render_signature:
                    self._render_signature = signature
                    self.render()
                self.status.setText(f"{len(data['images'])} workspaces  ·  {self.store}")
                if self.client.writable and self.health_ready:
                    self.notice.hide()
            elif getattr(data, "code", None) == "permission_required" and not self.client.writable:
                self.store_locked = True
                self.library_stack.setCurrentIndex(1)
                self.empty_title.setText("Unlock your workspace library.")
                self.empty_text.setText("Your images and recovery points are stored with administrator permissions.\nAuthenticate once to view and manage them in this window.")
                self.empty_create.setText("Unlock workspace store")
                self.empty_create.show()
                self.notice.hide()
                for metric in self.metrics:
                    metric.setText("—")
                self.status.setText("Workspace store locked · unlock to view and manage your images.")
            else:
                self.show_notice("Could not read the workspace store: " + str(data))
                self.status.setText("Store unavailable. See the message above.")
        self.client.request("inventory", {}, done)

    def render(self):
        images = self.data["images"]
        values = [str(len(images)), str(sum(x["status"] == "busy" for x in images)), str(len(self.data["snapshots"])), byte_size(self.data["free_bytes"])]
        for widget, value in zip(self.metrics, values):
            widget.setText(value)
        self.render_library()
        self.render_recovery()
        self.render_activity()
        self.update_controls()

    def render_recovery(self):
        query = self.recovery_search.text().casefold().strip()
        items = [item for item in self.data["snapshots"] if query in
                 f"{item['name']} {item['label']} {item.get('created_at', '')} {'deleted' if item.get('orphan') else ''}".casefold()]
        self.fill_table(self.recovery_table, [[x["name"] + (" · deleted" if x.get("orphan") else ""), x["label"], capacity(x["size_mib"]), when(x["created_at"])] for x in items],
                        [[x["name"], x["label"]] for x in items])
        self.recovery_empty.setText("No matching recovery points." if query else
                                    "No recovery points yet. Select a workspace and choose Save snapshot.")
        self.recovery_empty.setVisible(not items)
        storage = self.data.get("storage")
        if storage:
            self.storage_summary.setText("Allocated storage · " + " · ".join(
                f"{title}: {byte_size(storage.get(key, 0))}" for title, key in
                [("Images", "images_bytes"), ("Recovery points", "snapshots_bytes"),
                 ("Exports", "exports_bytes"), ("Logs", "logs_bytes")]) +
                ". Shared blocks can make actual reclaimed space smaller.")
        else:
            self.storage_summary.setText(f"{len(self.data['snapshots'])} recovery points. Select a point to preview deletion and its allocated storage.")
        interrupted = self.interrupted_operations()
        if interrupted:
            first = interrupted[0]
            more = f" ({len(interrupted) - 1} more after this.)" if len(interrupted) > 1 else ""
            if first.get("invalid"):
                text = (f"Damaged operation record {first['id'][:12]}…. "
                        "Discard it to set the record aside. Staged files remain available for inspection.")
            elif first.get("recoverable"):
                text = (f"Interrupted {first['action']} publication for {first['name']} ({first['stage']}). "
                        "Recover it to finalize the staged image and metadata, or discard it.")
            else:
                reclaim = first.get("reclaimable_bytes", 0)
                text = (f"Interrupted {first['action']} for {first['name']} stopped at {first['stage']}. "
                        + (f"Discarding it frees {byte_size(reclaim)}." if reclaim else "Discard it to clear the record."))
            self.interrupted_label.setText(text + more)
            self.recover_operation_button.setVisible(bool(first.get("recoverable")))
            self.interrupted_frame.show()
        else:
            self.interrupted_frame.hide()
        exports = self.data.get("exports", [])
        if exports:
            current_file = self.exports_combo.currentData()
            self.exports_combo.blockSignals(True)
            self.exports_combo.clear()
            for exp in exports:
                self.exports_combo.addItem(f"{exp['filename']} ({byte_size(exp['size_bytes'])})", exp)
            if current_file:
                for i in range(self.exports_combo.count()):
                    if (self.exports_combo.itemData(i) or {}).get("filename") == current_file.get("filename"):
                        self.exports_combo.setCurrentIndex(i)
                        break
            self.exports_combo.blockSignals(False)
            self.exports_frame.show()
        else:
            self.exports_combo.clear()
            self.exports_frame.hide()
        archives = self.data.get("archives", [])
        if archives:
            summary = ", ".join(f"{a['suite']} ({byte_size(a['size_bytes'])})" for a in archives)
            self.archive_status.setText(f"Offline cache ready: {summary}")
        else:
            self.archive_status.setText("Offline cache: none synced yet")
        self.update_controls()

    def render_activity(self):
        query = self.activity_search.text().casefold().strip()
        mode = self.activity_filter.currentIndex()
        items = [item for item in self.data["activity"] if query in
                 f"{item['name']} {item['action'].replace('-', ' ')} {item['time']}".casefold()
                 and (mode == 0 or (item["status"] != "ok" if mode == 1 else item["status"] == "ok"))]
        self.fill_table(self.activity_table, [[when(x["time"]), x["action"].replace("-", " ").capitalize(), x["name"], "Completed" if x["status"] == "ok" else "Failed"] for x in items],
                        [[x["time"], x["action"], x["name"], x["log"]] for x in items])
        self.activity_empty.setText("No matching operations." if query or mode else "Your first operation will appear here.")
        self.activity_empty.setVisible(not items)

    def fill_table(self, table, rows, keys):
        if table.property("rendered_rows") == [rows, keys]:
            return
        table.setProperty("rendered_rows", [rows, keys])
        current = table.item(table.currentRow(), 0)
        selected = current.data(Qt.ItemDataRole.UserRole) if current else None
        table.setUpdatesEnabled(False)
        blocked = table.blockSignals(True)
        table.clearSelection()
        table.setCurrentItem(None)
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                if value == "Failed":
                    item.setForeground(QColor("#efa79d"))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, keys[r])
                table.setItem(r, c, item)
        if selected in keys:
            table.selectRow(keys.index(selected))
        table.blockSignals(blocked)
        table.setUpdatesEnabled(True)

    def build_detail_card(self):
        card = QFrame()
        card.setObjectName("detail")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        top, row = box(horizontal=True)
        top.setStyleSheet("background: transparent;")
        row.addWidget(label("WORKSPACE", "eyebrow"), 1)
        self.favorite_button = button("☆", self.favorite, "quiet")
        row.addWidget(self.favorite_button)
        layout.addWidget(top)
        self.detail_title = label("", "detailTitle", True)
        layout.addWidget(self.detail_title)
        self.detail_lifecycle = label("", "muted")
        layout.addWidget(self.detail_lifecycle)
        self.terminal_button = button("›_  Open terminal", self.open_terminal, "primary")
        self.terminal_button.setToolTip("Open in your native terminal · Ctrl Return")
        layout.addWidget(self.terminal_button)
        self.offline = QCheckBox("Isolate network for this session")
        self.offline.setToolTip("Only loopback is available; internet and host networking are disconnected.")
        self.offline.toggled.connect(lambda checked: self.save_session_choice("isolated", checked))
        layout.addWidget(self.offline)
        self.restricted = QCheckBox("Restrict devices for this session")
        self.restricted.setToolTip("The session gets a minimal /dev without host device nodes. Fewer tools will work; this is still not a security sandbox.")
        self.restricted.toggled.connect(lambda checked: self.save_session_choice("restricted", checked))
        layout.addWidget(self.restricted)
        layout.addWidget(label("SHARED FOLDER · /shared", "eyebrow"))
        self.share_path = QLineEdit()
        self.share_path.setObjectName("sharedFolderPath")
        self.share_path.setReadOnly(True)
        self.share_path.setPlaceholderText("No host folder shared")
        share_actions, share_row = box(horizontal=True, spacing=8)
        self.share_choose = button("Choose…", self.choose_shared_folder)
        self.share_choose.setToolTip("Choose a host folder to share at /shared.")
        self.share_clear = button("Clear", self.clear_shared_folder)
        share_row.addWidget(self.share_path, 1)
        share_row.addWidget(self.share_choose)
        layout.addWidget(share_actions)
        self.share_read_only = QCheckBox("Read-only shared folder")
        self.share_read_only.toggled.connect(lambda checked: self.set_shared_folder_read_only(self.selected, checked) if self.selected else None)
        access, access_row = box(horizontal=True, spacing=8)
        access_row.addWidget(self.share_read_only, 1)
        access_row.addWidget(self.share_clear)
        layout.addWidget(access)
        layout.addWidget(label("Shared files stay on the host, even after a disposable session. New files belong to root.", "muted", True))
        self.trial_button = button("Try a disposable session", lambda: self.open_terminal(disposable=True))
        layout.addWidget(self.trial_button)
        layout.addSpacing(2)
        metadata = QWidget()
        metadata.setStyleSheet("background: transparent;")
        grid = QGridLayout(metadata)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(16)
        self.meta_labels = {}
        for title, r, c, span in [("DISTRIBUTION", 0, 0, 1), ("PROFILE", 0, 1, 1), ("STORAGE", 1, 0, 2), ("LAST SESSION", 2, 0, 1), ("RECOVERY", 2, 1, 1)]:
            field, field_layout = box(spacing=4)
            field.setStyleSheet("background: transparent;")
            field_layout.addWidget(label(title, "eyebrow"))
            val_label = label("", None, True)
            self.meta_labels[title] = val_label
            field_layout.addWidget(val_label)
            grid.addWidget(field, r, c, 1, span)
        layout.addWidget(metadata)
        notes_header, notes_row = box(horizontal=True, spacing=8)
        notes_row.addWidget(label("PERSONAL NOTES", "eyebrow"), 1)
        self.notes_button = button("Edit notes…", self.edit_notes, "quiet")
        notes_row.addWidget(self.notes_button)
        layout.addWidget(notes_header)
        self.notes_preview = label("", "muted", True)
        layout.addWidget(self.notes_preview)
        layout.addStretch()
        actions, row = box(horizontal=True, spacing=8)
        self.save_snapshot_button = button("Save snapshot", self.snapshot)
        row.addWidget(self.save_snapshot_button, 1)
        self.more_button = button("•••")
        self.more_button.setToolTip("Clone, resize, export and maintenance")
        self.more_button.setAccessibleName("More workspace actions")
        menu = QMenu(self.more_button)
        self.mutating_actions = []
        self.mode_action = None
        for title, callback in [("Clone workspace…", self.clone), ("Expand capacity…", self.resize_image),
                                ("Switch to disposable", self.change_mode),
                                ("Export filesystem archive…", self.export), ("View latest log", self.workspace_log),
                                ("View recovery points", self.workspace_recovery),
                                ("Copy image path", lambda: QApplication.clipboard().setText((self.selected_data() or {}).get("path", ""))),
                                ("Check and repair…", self.repair), ("Delete workspace…", self.delete)]:
            action = menu.addAction(title)
            action.triggered.connect(callback)
            if callback == self.change_mode:
                self.mode_action = action
            if title not in ("View latest log", "Copy image path", "View recovery points"):
                self.mutating_actions.append(action)
        self.more_button.setMenu(menu)
        row.addWidget(self.more_button)
        layout.addWidget(actions)
        self.detail_card = card
        self.details.setWidget(card)

    def render_library(self):
        if self.store_locked:
            return
        query = self.search.text().casefold().strip()
        mode = self.filter.currentIndex()
        images = self.data["images"]
        if query:
            images = [x for x in images if query in f"{x.get('name', '')} {x.get('profile', '')} {x.get('suite', '')} {self.workspace_notes(x['name'])}".casefold()]
        else:
            images = list(images)
        if mode == 1:
            images = [x for x in images if x["lifecycle"] == "persistent"]
        elif mode == 2:
            images = [x for x in images if x["lifecycle"] == "ephemeral"]
        elif mode == 3:
            images = [x for x in images if x["name"] in self.favorites]
        elif mode == 4:
            images = [x for x in images if x["status"] == "busy"]
        images.sort(key=lambda x: (x["name"].casefold(), x["name"]))
        order = self.sort.currentData()
        if order == "favorites":
            images.sort(key=lambda x: x["name"] not in self.favorites)
        elif order in ("recent", "created"):
            field = "last_used_at" if order == "recent" else "created_at"
            images.sort(key=lambda x: x.get(field, "") if x.get(field) not in ("never", "unknown") else "", reverse=True)
        elif order in ("capacity", "allocated"):
            field = "size_mib" if order == "capacity" else "allocated_bytes"
            images.sort(key=lambda x: x.get(field, 0), reverse=True)
        self.library_count.setText(f"{len(images)} of {len(self.data['images'])} workspaces")
        selected = self.selected
        self.workspaces.setUpdatesEnabled(False)
        self.workspaces.blockSignals(True)
        while self.workspaces.count() > len(images):
            row = self.workspaces.count() - 1
            old_item = self.workspaces.item(row)
            old_widget = self.workspaces.itemWidget(old_item)
            self.workspaces.takeItem(row)
            if old_widget:
                old_widget.deleteLater()
        selected_item = None
        for index, data in enumerate(images):
            fav = data["name"] in self.favorites
            if index < self.workspaces.count():
                item = self.workspaces.item(index)
                item.setData(Qt.ItemDataRole.UserRole, data)
                row_widget = self.workspaces.itemWidget(item)
                if isinstance(row_widget, WorkspaceRow):
                    row_widget.update_row(data, fav)
                else:
                    self.workspaces.setItemWidget(item, WorkspaceRow(data, fav))
            else:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, data)
                item.setSizeHint(QSize(0, 93))
                self.workspaces.addItem(item)
                self.workspaces.setItemWidget(item, WorkspaceRow(data, fav))
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"{data['name']}, Debian {data.get('suite', 'unknown')}, {data['status']}, {data['lifecycle']}",
            )
            if data["name"] == selected:
                selected_item = item
        self.workspaces.blockSignals(False)
        self.workspaces.setUpdatesEnabled(True)
        self.library_stack.setCurrentIndex(0 if images else 1)
        if images:
            target = selected_item or self.workspaces.item(0)
            if self.workspaces.currentItem() is target:
                self.select_workspace(target)
            else:
                self.workspaces.setCurrentItem(target)
        else:
            self.selected = None
            filtered = bool(self.data["images"])
            self.empty_title.setText("No matching workspaces." if filtered else "Make room for your next idea.")
            self.empty_text.setText("Try a different search or filter." if filtered else "Start with a clean Debian image or a ready-to-build profile.\nKeep your changes, or start fresh every time.")
            self.empty_create.setVisible(not filtered)

    def select_workspace(self, item, previous=None):
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        same_selection = self.selected == data["name"]
        scroll_position = self.details.verticalScrollBar().value() if same_selection else 0
        self.selected = data["name"]
        fav = self.selected in self.favorites
        isolated = self.session_choice(self.selected, "isolated")
        restricted = self.session_choice(self.selected, "restricted")
        notes = self.workspace_notes(self.selected)
        share, read_only = self.shared_folder_choice(self.selected)
        can_configure = not self.demo and not self.busy and not self.authenticating and data["status"] != "busy"
        state = (
            self.selected, data.get("status"), data.get("lifecycle"), data.get("suite"),
            data.get("profile"), data.get("size_mib"), data.get("allocated_bytes"),
            data.get("last_used_at"), data.get("snapshots"), data.get("path"),
            fav, can_configure, isolated, restricted, share, read_only, notes, self.demo,
        )
        if state != self._detail_state:
            self._detail_state = state
            self.favorite_button.setText("★" if fav else "☆")
            fav_label = "Remove from favorites" if fav else "Add to favorites"
            self.favorite_button.setToolTip(fav_label)
            self.favorite_button.setAccessibleName(fav_label)
            self.detail_title.setText(self.selected)
            lifecycle = data["lifecycle"]
            self.detail_lifecycle.setText("A fresh start, every session." if lifecycle == "ephemeral" else "Your changes stay with you.")
            self.terminal_button.setEnabled(can_configure)
            self.offline.blockSignals(True)
            self.offline.setChecked(isolated)
            self.offline.blockSignals(False)
            self.restricted.blockSignals(True)
            self.restricted.setChecked(restricted)
            self.restricted.blockSignals(False)
            self.share_path.setText(share)
            self.offline.setEnabled(can_configure)
            self.restricted.setEnabled(can_configure)
            self.notes_button.setEnabled(not self.demo)
            self.notes_preview.setText(notes[:240] + ("…" if len(notes) > 240 else "") if notes else "Add project details or commands to remember.")
            self.share_path.setToolTip(share or "Choose a host folder to make it available at /shared.")
            self.share_choose.setEnabled(can_configure)
            self.share_clear.setEnabled(can_configure and bool(share))
            self.share_read_only.blockSignals(True)
            self.share_read_only.setChecked(read_only)
            self.share_read_only.blockSignals(False)
            self.share_read_only.setEnabled(can_configure and bool(share))
            self.trial_button.setEnabled(can_configure)
            profile = data.get("profile", "minimal")
            self.meta_labels["DISTRIBUTION"].setText(f"Debian {data.get('suite', 'unknown')}")
            self.meta_labels["PROFILE"].setText(PROFILES.get(profile, (profile,))[0])
            self.meta_labels["STORAGE"].setText(f"{capacity(data['size_mib'])} capacity · {byte_size(data['allocated_bytes'])} allocated")
            self.meta_labels["LAST SESSION"].setText(when(data.get("last_used_at")))
            self.meta_labels["RECOVERY"].setText(f"{data['snapshots']} snapshots")
            self.save_snapshot_button.setEnabled(can_configure)
            self.mode_action.setText("Switch to " + ("persistent" if lifecycle == "ephemeral" else "disposable"))
            for action in self.mutating_actions:
                action.setEnabled(can_configure)
        if not same_selection or self.details.verticalScrollBar().value() != scroll_position:
            self.details.verticalScrollBar().setValue(scroll_position)

    def selected_data(self):
        return next((x for x in self.data["images"] if x["name"] == self.selected), None)

    def library_preferences_changed(self):
        if not self.demo:
            self.settings.setValue(self.session_settings_prefix + "/filter", self.filter.currentText())
            self.settings.setValue(self.session_settings_prefix + "/sort", self.sort.currentData())
        self.render_library()

    def workspace_notes(self, target):
        if target not in self.notes_cache:
            self.notes_cache[target] = "" if self.demo else self.settings.value(self.session_settings_prefix + "/" + target + "/notes", "", type=str)[:4000]
        return self.notes_cache[target]

    def save_notes(self, target, text):
        if self.demo:
            return
        name(target)
        if len(text) > 4000:
            raise ValueError("Notes must be at most 4,000 characters.")
        self.notes_cache[target] = text
        self.settings.setValue(self.session_settings_prefix + "/" + target + "/notes", text)
        self.render_library()

    def edit_notes(self):
        target = self.selected
        if not target or self.demo:
            return
        dialog = NotesDialog(target, self.workspace_notes(target), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.save_notes(target, dialog.value())

    def session_choice(self, target, option):
        choices = self.network_choices if option == "isolated" else self.restricted_choices
        if target not in choices:
            choices[target] = False if self.demo else self.settings.value(self.session_settings_prefix + "/" + target + "/" + option, False, type=bool)
        return choices[target]

    def save_session_choice(self, option, checked):
        if not self.selected:
            return
        choices = self.network_choices if option == "isolated" else self.restricted_choices
        choices[self.selected] = checked
        if not self.demo:
            self.settings.setValue(self.session_settings_prefix + "/" + self.selected + "/" + option, checked)

    def shared_folder_choice(self, target):
        if target not in self.shared_folder_choices:
            key = self.session_settings_prefix + "/" + target
            path = "" if self.demo else self.settings.value(key + "/folder", "", type=str)
            read_only = False if self.demo else self.settings.value(key + "/read_only", False, type=bool)
            self.shared_folder_choices[target] = (path, bool(path) and read_only)
        return self.shared_folder_choices[target]

    def save_shared_folder(self, target, path, read_only=False):
        read_only = bool(path) and read_only
        self.shared_folder_choices[target] = (path, read_only)
        if not self.demo:
            key = self.session_settings_prefix + "/" + target
            if path:
                self.settings.setValue(key + "/folder", path)
                self.settings.setValue(key + "/read_only", read_only)
            else:
                self.settings.remove(key + "/folder")
                self.settings.remove(key + "/read_only")

    def choose_shared_folder(self):
        target = self.selected
        if not target or self.demo or self.busy:
            return
        previous, read_only = self.shared_folder_choice(target)
        path = QFileDialog.getExistingDirectory(self, "Share a host folder at /shared", previous or str(Path.home()))
        if path:
            try:
                path = shared_folder(path)
            except ValueError as error:
                self.show_error(str(error))
                return
            self.save_shared_folder(target, path, read_only)
            self.select_workspace(self.workspaces.currentItem())

    def clear_shared_folder(self):
        if self.selected and not self.demo and not self.busy:
            self.save_shared_folder(self.selected, "")
            self.select_workspace(self.workspaces.currentItem())

    def set_shared_folder_read_only(self, target, checked):
        path, _ = self.shared_folder_choice(target)
        self.save_shared_folder(target, path, checked)

    def favorite(self):
        if self.selected in self.favorites:
            self.favorites.remove(self.selected)
        elif self.selected:
            self.favorites.add(self.selected)
        if not self.demo:
            self.settings.setValue(self.session_settings_prefix + "/favorites", sorted(self.favorites))
        self.render_library()

    def focus_search(self):
        search = {0: self.search, 1: self.recovery_search, 2: self.activity_search}.get(self.current_view)
        if search is None:
            self.navigate(0)
            search = self.search
        search.setFocus()
        search.selectAll()

    def workspace_recovery(self):
        if self.selected:
            self.recovery_search.setText(self.selected)
            self.navigate(1)

    def create(self):
        if self.busy or self.authenticating:
            return
        dialog = CreateDialog({x["name"] for x in self.data["images"]}, self)
        if dialog.exec():
            self.perform("create", dialog.parameters())

    def import_archive(self, initial_archive="", managed_export=None):
        if self.busy or self.authenticating:
            return
        dialog = ImportDialog({x["name"] for x in self.data["images"]}, initial_archive if isinstance(initial_archive, str) else "", self,
                              managed_export=managed_export)
        if dialog.exec():
            self.perform("import", dialog.parameters())

    def snapshot(self):
        target = self.selected
        if not target:
            return
        existing_labels = {s["label"] for s in self.data["snapshots"] if s["name"] == target}
        dialog = ValueDialog(
            "Save a recovery point",
            "Capture this workspace before your next experiment. The workspace must be offline.",
            "Snapshot label",
            datetime.now().strftime("checkpoint-%Y%m%d-%H%M"),
            parent=self,
            forbidden=existing_labels,
            collision_message="That snapshot label already exists for this workspace.",
        )
        if dialog.exec():
            self.perform("snapshot", {"name": target, "target": dialog.value()})

    def clone(self):
        target = self.selected
        if not target:
            return
        existing_names = {x["name"] for x in self.data["images"]}
        dialog = ValueDialog(
            "Branch into a new workspace",
            "An independent copy of the filesystem and profile. The original stays intact.",
            "New workspace name",
            target[:58] + "-copy",
            parent=self,
            forbidden=existing_names,
            collision_message="That workspace name is already in use.",
        )
        if dialog.exec():
            self.perform("clone", {"name": target, "target": dialog.value()})

    def resize_image(self):
        data = self.selected_data()
        if not data or data["size_mib"] >= 1048576:
            return
        dialog = ValueDialog("Give it more room", f"Current capacity: {capacity(data['size_mib'])}. Images can expand up to 1 TiB.", "New capacity", minimum=min(data["size_mib"] + 1024, 1048576), parent=self)
        if dialog.exec():
            self.perform("resize", {"name": data["name"], "size_mib": dialog.value()})

    def change_mode(self):
        data = self.selected_data()
        if data:
            self.perform("mode", {"name": self.selected, "lifecycle": "persistent" if data["lifecycle"] == "ephemeral" else "ephemeral"})

    def confirm(self, title, text):
        return QMessageBox.question(self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes

    def delete(self):
        if not self.selected:
            return
        target = self.selected
        dialog = ValueDialog("Delete workspace", f"Permanently remove {target}'s image. Saved snapshots and exports remain available. Type the workspace name to continue.", "Confirm workspace name", parent=self)
        if dialog.exec():
            if dialog.value() != target:
                self.show_error("The name did not match. Nothing was deleted.")
                return
            self.perform("delete", {"name": target, "confirmed": True})

    def repair(self):
        target = self.selected
        if target and self.confirm("Repair filesystem", "Forced ext4 repair can change filesystem data. Save a snapshot first if you need a recovery point. Continue?"):
            self.perform("repair", {"name": target, "confirmed": True})

    def export(self):
        target = self.selected
        if not target:
            return
        destination, _ = QFileDialog.getSaveFileName(self, "Export filesystem", str(Path.home() / (target + ".tar.gz")), "Compressed archive (*.tar.gz)")
        if destination:
            # Import accepts only these suffixes, so keep the export importable.
            if not destination.endswith((".tar.gz", ".tgz")):
                destination += ".tar.gz"
            if Path(destination).exists():
                self.show_error("Choose a new filename. Exports never overwrite an existing file.")
                return
            self.perform("export", {"name": target, "destination": destination})

    def chosen_snapshot(self):
        current = self.recovery_table.item(self.recovery_table.currentRow(), 0)
        key = current.data(Qt.ItemDataRole.UserRole) if current else None
        return next((item for item in self.data["snapshots"] if [item["name"], item["label"]] == key), None)

    def restore(self):
        item = self.chosen_snapshot()
        if item and self.confirm("Restore workspace", f"Restore {item['name']} from {item['label']}?\n\nThe current image, if present, will be saved as a before-restore recovery point."):
            self.perform("restore", {"name": item["name"], "target": item["label"], "confirmed": True})

    def drop_snapshot(self):
        item = self.chosen_snapshot()
        if not item:
            return
        allocated = (f"Allocated file size: {byte_size(item['allocated_bytes'])}. Actual reclaimed space can be smaller when blocks are shared.\n\n"
                     if "allocated_bytes" in item else "")
        if self.confirm("Delete recovery point", f"Permanently delete {item['label']} for {item['name']}?\n\n" + allocated +
                        "Only this recovery point is removed. The current workspace and other recovery points remain. This cannot be undone."):
            self.perform("drop-snapshot", {"name": item["name"], "target": item["label"], "confirmed": True})

    def interrupted_operations(self):
        return [op for op in self.data.get("operations", []) if op.get("recoverable") or op.get("discardable")]

    def discard_interrupted_operation(self, op_id=None):
        interrupted = self.interrupted_operations()
        target = next((op for op in interrupted if op["id"] == op_id), interrupted[0] if interrupted else None)
        if not target:
            return
        if target.get("invalid"):
            description = (f"Set aside damaged operation record {target['id'][:12]}…?\n\n"
                           "Its staged files remain available for inspection because the damaged record "
                           "cannot safely identify them for removal.")
        else:
            description = (f"Abandon the interrupted {target['action']} for {target['name']} (operation {target['id'][:12]}…) "
                           "and permanently remove its staged files?\n\n"
                           "If it already replaced workspace metadata, Cube Chrootables will ask you to recover it instead.")
        if self.confirm("Discard interrupted operation", description):
            self.perform("discard-operation", {"id": target["id"], "confirmed": True})

    def recover_interrupted_operation(self, op_id=None):
        recoverable = [op for op in self.data.get("operations", []) if op.get("recoverable")]
        target = next((op for op in recoverable if op["id"] == op_id), recoverable[0] if recoverable else None)
        if not target:
            return
        if self.confirm("Recover interrupted publication",
                        f"Finish the interrupted {target['action']} publication for {target['name']} (operation {target['id'][:12]}…)?"):
            self.perform("recover-operation", {"id": target["id"], "confirmed": True})

    def import_selected_export(self):
        exp = self.exports_combo.currentData()
        if exp and isinstance(exp, dict) and exp.get("filename"):
            self.import_archive(exp.get("path", exp["filename"]), managed_export=exp["filename"])

    def delete_selected_export(self):
        exp = self.exports_combo.currentData()
        if not exp or not isinstance(exp, dict) or not exp.get("filename"):
            return
        if self.confirm("Delete export archive",
                        f"Permanently delete export archive {exp['filename']} ({byte_size(exp.get('size_bytes', 0))})?"):
            self.perform("delete-export", {"filename": exp["filename"], "confirmed": True})

    def prune_logs(self):
        if self.confirm("Prune operation logs",
                        "Remove older completed operation logs while retaining the 20 most recent logs and any active or interrupted operations?"):
            self.perform("prune-logs", {"keep": 20, "confirmed": True})

    def sync_archive(self):
        suite = self.archive_suite.currentData() or "trixie"
        if self.confirm("Sync offline archive",
                        f"Download and cache the Debian {suite} base system and profile packages for offline workspace creation?"):
            self.perform("archive-sync", {"suite": suite})

    def cancel_operation(self):
        if not self.busy or self.demo or not self.client.is_ready:
            return
        self.cancel_task_button.setEnabled(False)
        def done(ok, result):
            if ok:
                self.append_output("\nCancelling. Staged files will be removed.\n")
                self.set_task_phase("Cancelling")
            else:
                self.cancel_task_button.setEnabled(self.busy)
                self.show_error(str(result))
        self.client.request("cancel", {}, done)

    def perform(self, action, params):
        if self.demo:
            self.show_error("This is a read-only tour. Launch Cube Chrootables without --demo to manage real workspaces.")
            return
        if self.busy or self.authenticating:
            return
        if not self.client.writable:
            self.pending_action = (action, dict(params))
            self.unlock()
            return
        self.run_operation(action, params)

    def run_operation(self, action, params):
        self.busy = True
        self.last_task = action.replace("-", " ").capitalize() + (" · " + params["name"] if params.get("name") else "")
        self.task_started = time.monotonic()
        self.task_phase_text = "Starting"
        self.update_task_elapsed()
        self.task_title.setText(self.last_task)
        self.task_output.clear()
        self.task_panel.show()
        self.task_reopen.show()
        self.task_progress.show()
        self.cancel_task_button.setEnabled(True)
        self.cancel_task_button.show()
        self.update_controls()
        self.render_library()
        def done(ok, result):
            self.busy = False
            self.cancel_task_button.hide()
            cancelled = not ok and getattr(result, "code", None) == "cancelled"
            self.set_task_phase("Completed" if ok else "Cancelled" if cancelled else "Failed")
            self.update_task_elapsed()
            self.task_started = None
            self.task_progress.hide()
            self.task_title.setText(("✓  " if ok else "!  ") + self.last_task +
                                    (" completed" if ok else " cancelled" if cancelled else " failed"))
            if not ok:
                self.append_output("\n" + str(result) + "\n")
                self.task_panel.show()
            self.render_library()
            if ok and action in ("create", "import"):
                self.selected = params["name"]
                self.navigate(0)
            self.update_controls()
            self.refresh()
            if action == "setup" or not self.health_ready:
                self.check_health()
        self.client.request(action, params, done)

    def update_controls(self):
        available = not self.busy and not self.authenticating
        self.new_button.setEnabled(available)
        self.import_button.setEnabled(available and not self.demo)
        self.empty_create.setEnabled(available)
        self.unlock_button.setEnabled(not self.busy and not self.demo)
        self.setup_button.setEnabled(available and not self.demo and self.missing_dependencies)
        self.sync_archive_button.setEnabled(available and not self.demo)
        self.prune_logs_button.setEnabled(available and not self.demo)
        has_recoverable = any(op.get("recoverable") for op in self.data.get("operations", []))
        self.recover_operation_button.setEnabled(has_recoverable and available and not self.demo)
        self.discard_operation_button.setEnabled(bool(self.interrupted_operations()) and available and not self.demo)
        has_exports = bool(self.data.get("exports"))
        self.reimport_export_button.setEnabled(has_exports and available and not self.demo)
        self.delete_export_button.setEnabled(has_exports and available and not self.demo)
        has_snapshot = self.chosen_snapshot() is not None
        self.restore_button.setEnabled(has_snapshot and available and not self.demo)
        self.drop_snapshot_button.setEnabled(has_snapshot and available and not self.demo)

    def append_output(self, text):
        if not text:
            return
        scrollbar = self.task_output.verticalScrollBar()
        position = scrollbar.value()
        old_cursor = self.task_output.textCursor()
        old_anchor, old_position = old_cursor.anchor(), old_cursor.position()
        document = self.task_output.document()
        old_length = document.characterCount() - 1
        # Bound the temporary insertion too; astral characters take two units.
        text = text[-MAX_CONSOLE_CHARACTERS:]
        inserted_units = len(text.encode('utf-16-le', errors='replace')) // 2
        cursor = self.task_output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        removed = max(0, old_length + inserted_units - (document.characterCount() - 1))
        excess = document.characterCount() - 1 - MAX_CONSOLE_CHARACTERS
        if excess > 0:
            # Avoid retaining half a surrogate pair at the trimming boundary.
            if 0xDC00 <= ord(document.characterAt(excess)) <= 0xDFFF:
                excess += 1
            trim = QTextCursor(document)
            trim.setPosition(0)
            trim.setPosition(excess, QTextCursor.MoveMode.KeepAnchor)
            trim.removeSelectedText()
            removed += excess
        if self.follow_output.isChecked():
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.task_output.setTextCursor(cursor)
            self.task_output.ensureCursorVisible()
            scrollbar.setValue(scrollbar.maximum())
        else:
            end = document.characterCount() - 1
            old_cursor.setPosition(max(0, min(end, old_anchor - removed)))
            old_cursor.setPosition(max(0, min(end, old_position - removed)), QTextCursor.MoveMode.KeepAnchor)
            self.task_output.setTextCursor(old_cursor)
            scrollbar.setValue(position)

    def set_task_phase(self, text):
        if self.task_started is not None:
            self.task_phase_text = text
            self.update_task_elapsed()

    def update_task_elapsed(self):
        if self.task_started is not None:
            seconds = max(0, int(time.monotonic() - self.task_started))
            minutes, seconds = divmod(seconds, 60)
            self.task_phase.setText(f"{self.task_phase_text} · {minutes:02d}:{seconds:02d} elapsed")

    def change_text_scale(self):
        scale = self.text_scale.currentData() or 100
        self.sidebar.setMinimumWidth(round(178 * scale / 100))
        self.sidebar_scroll.setFixedWidth(round(178 * scale / 100) + 18)
        self.setStyleSheet(scaled_style(scale))
        self.update_compact_layout()
        if not self.demo:
            self.settings.setValue("appearance/text_scale", scale)

    def update_compact_layout(self):
        scale = self.text_scale.currentData() or 100
        compact = self.width() - self.sidebar_scroll.width() < round(760 * scale / 100)
        orientation = Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.workspaces.setMinimumHeight(180 if compact else 0)
            self.details.setMinimumHeight(220 if compact else 0)
            self.splitter.setSizes([260, 420] if compact else [620, 350])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "splitter"):
            self.update_compact_layout()

    def open_terminal(self, checked=False, disposable=False):
        data = self.selected_data()
        if not data or self.demo or self.busy or self.authenticating or data["status"] == "busy":
            return
        target = data["name"]
        try:
            share, read_only = self.shared_folder_choice(target)
            keyword = dict(disposable=disposable, isolated=self.offline.isChecked(),
                           restricted_dev=self.restricted.isChecked(),
                           share=share or None, read_only=read_only)
            command = session_command(target, self.store, **keyword)
        except ValueError as error:
            self.show_error(str(error))
            return

        def open_external():
            external = terminal_command(target, self.store, **keyword)
            started, _ = QProcess.startDetached(external[0], external[1:])
            if not started:
                raise RuntimeError("Could not start the terminal.")

        try:
            window = TerminalWindow(command, f"Cube Chrootables · {target}", external=open_external)
        except OSError as error:
            self.show_error("Could not start the terminal: " + str(error))
            return
        self.terminals.append(window)
        window.finished.connect(lambda: self.terminals.remove(window))
        window.show()
        self.status.setText("Embedded terminal opened. Administrator authentication happens in the terminal.")
        QTimer.singleShot(1500, self.refresh)

    def check_health(self):
        if self.demo or not self.client.is_ready:
            return
        def done(ok, data):
            if ok:
                self.health_ready = data["ready"]
                checks = data.get("checks")
                self.missing_dependencies = (any(check["status"] == "error" and
                    (check["id"].startswith("command:") or check["id"] == "archive-keys")
                    for check in checks) if checks is not None else not self.health_ready)
                if "errors" in data and "warnings" in data:
                    self.health_summary.setText(f"{data['errors']} blocking issues · {data['warnings']} warnings · Mount permissions are checked when a session starts.")
                else:
                    self.health_summary.setText("Host checks passed." if self.health_ready else "Review the host checks below.")
                self.health_output.setPlainText(data["output"])
                if not self.health_ready:
                    self.show_notice("Host health found blocking issues. Review the checks and suggested fixes before creating a workspace.")
                elif self.client.writable:
                    self.notice.hide()
                self.update_controls()
            else:
                self.health_output.setPlainText(str(data))
                self.health_summary.setText("Host checks could not be completed. Run checks to try again.")
        self.client.request("doctor", {}, done)

    def setup(self):
        self.perform("setup", {})

    def workspace_log(self):
        if self.selected:
            self.show_log(self.selected)

    def activity_log(self):
        current = self.activity_table.item(self.activity_table.currentRow(), 0)
        key = current.data(Qt.ItemDataRole.UserRole) if current else None
        entry = next((item for item in self.data["activity"] if
                      [item["time"], item["action"], item["name"], item["log"]] == key), None)
        if entry:
            self.show_log(entry["name"], entry["log"])

    def show_log(self, target, selected_log=None):
        if self.demo:
            return
        def done(ok, data):
            if not ok:
                self.show_error(str(data))
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Operation log · " + target)
            dialog.resize(850, 560)
            layout = QVBoxLayout(dialog)
            output = QPlainTextEdit()
            output.setReadOnly(True)
            output.setPlainText(data)
            layout.addWidget(output)
            layout.addWidget(button("Copy log", lambda: QApplication.clipboard().setText(data)))
            dialog.exec()
        self.client.request("logs", {"name": target, "log": selected_log}, done)

    def show_notice(self, message):
        self.notice_text.setText(message)
        self.notice.show()

    def show_error(self, message):
        QMessageBox.warning(self, "Cube Chrootables", message)

    def load_demo(self):
        self.data = json.loads((ROOT / "assets" / "demo.json").read_text())
        self.access_label.setText("○  Sample data · read-only")
        self.unlock_button.setEnabled(False)
        self.health_output.setPlainText("This is the desktop tour. Launch without --demo to inspect your host and manage real workspaces.")
        self.show_notice("READ-ONLY TOUR · Sample workspaces. No images are created or modified.")
        self.notice_action.hide()
        self.status.setText("Sample data · launch without --demo to use your own workspaces")
        self.selected = "python-playground"
        self.render()

    def _poll_timer(self):
        if not self.isMinimized():
            self.refresh()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and (event.oldState() & Qt.WindowState.WindowMinimized) and not self.isMinimized():
            self.refresh()

    def closeEvent(self, event):
        if self.busy:
            self.show_error("An operation is still running. Wait for it to finish before closing Cube Chrootables.")
            event.ignore()
            return
        self.timer.stop()
        self.task_timer.stop()
        if not self.demo:
            self.settings.setValue("window/geometry", self.saveGeometry())
            self.settings.setValue("window/splitter", self.splitter.sizes())
            self.settings.sync()
        self.client.stop()
        event.accept()


def main(*, desktop_id="cube-chrootables", application_name="Cube Chrootables"):
    parser = argparse.ArgumentParser(description=application_name + " desktop workbench")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--demo", action="store_true", help="Explore a read-only tour with clearly labeled sample data")
    parser.add_argument("--screenshot", type=Path, help="Save a window capture and exit (for UI checks)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    try:
        args.store = store_path(args.store)
    except ValueError as error:
        parser.error(str(error))
    if os.geteuid() == 0 and os.environ.get("QT_QPA_PLATFORM") not in ("offscreen", "minimal"):
        print("Launch cube-chrootables as your normal desktop user. Unlock store opens a terminal for sudo authentication.", file=sys.stderr)
        return 1
    if not any(os.environ.get(key) for key in ("WAYLAND_DISPLAY", "DISPLAY")) and os.environ.get("QT_QPA_PLATFORM") not in ("offscreen", "minimal"):
        print("Cube Chrootables needs a graphical desktop session. Launch cube-chrootables from your desktop terminal.", file=sys.stderr)
        return 1
    app = QApplication(sys.argv[:1])
    app.setApplicationName(application_name)
    app.setDesktopFileName(desktop_id)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = Window(args.store, args.demo)
    window.setWindowTitle(application_name)
    # A helper may need a short EOF/termination grace period after the last
    # window closes. Keep Qt running for that asynchronous cleanup, without
    # leaving a hidden GUI alive for the detached administrator task itself.
    app.setQuitOnLastWindowClosed(False)
    quitting = False
    exit_code = 0
    def last_window_closed():
        nonlocal quitting
        quitting = True
        if not window.client.retiring:
            app.quit()
    app.lastWindowClosed.connect(last_window_closed)
    window.client.stopped.connect(lambda: app.quit() if quitting else None)
    window.show()
    if args.screenshot:
        def capture():
            nonlocal exit_code
            try:
                save_screenshot(window, args.screenshot)
            except (OSError, RuntimeError, ValueError) as error:
                exit_code = 1
                print(f"Screenshot failed: {error}", file=sys.stderr)
            finally:
                window.close()
        QTimer.singleShot(1800, capture)
    return app.exec() or exit_code


if __name__ == "__main__":
    raise SystemExit(main())
