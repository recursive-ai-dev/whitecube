#!/usr/bin/env python3
"""Install (or --uninstall) the Cube apps for the current user: launchers in
~/.local/bin, .desktop entries, and cube icons in the hicolor theme."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cube_theme import cube_icon  # noqa: E402

HERE = Path(__file__).resolve().parent
HOME = Path.home()
BIN = HOME / ".local/bin"
APPS = HOME / ".local/share/applications"
ICONS = HOME / ".local/share/icons/hicolor/scalable/apps"
SCRIPTS = HERE            # where the launchers point; the system layout points at /usr/share/whitecube/cube-apps
EXEC_DIR = None           # where Exec= lines point; defaults to BIN


def use_system_layout(destdir):
    """Write a system-wide layout (/usr/bin, /usr/share/...) under DESTDIR instead of ~/.local."""
    global BIN, APPS, ICONS, SCRIPTS, EXEC_DIR
    root = Path(destdir)
    BIN, APPS = root / "usr/bin", root / "usr/share/applications"
    ICONS = root / "usr/share/icons/hicolor/scalable/apps"
    SCRIPTS, EXEC_DIR = Path("/usr/share/whitecube/cube-apps"), Path("/usr/bin")

STROKE = 'fill="none" stroke="#111" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"'
APPS_DEF = [
    # command, script, app id, name, comment, category, icon glyph
    ("cube-lock", "cube_lock.py", "dev.woez.CubeLock", "Cube PDF Lock",
     "Password-protect PDFs with AES-256", "Office",
     f'<rect x="53" y="73" width="22" height="17" fill="#111"/><path d="M58 73v-6a6 6 0 0 1 12 0v6" {STROKE}/>'),
    ("cube-pdf", "cube_pdf.py", "dev.woez.CubePdf", "Cube PDF Tools",
     "Merge, split and unlock PDFs", "Office",
     '<path d="M54 64h14l8 8v22H54Z" fill="#111"/><path d="M68 64v8h8" fill="none" stroke="#f2f2f2" stroke-width="2.5"/>'),
    ("cube-hash", "cube_hash.py", "dev.woez.CubeHash", "Cube Hash",
     "Checksums for files, with paste-to-verify", "Utility",
     f'<path d="M59 66l-3 24M71 66l-3 24M52 75h24M51 84h24" {STROKE}/>'),
    ("cube-rename", "cube_rename.py", "dev.woez.CubeRename", "Cube Rename",
     "Batch rename files with a live preview and undo", "Utility",
     f'<path d="M52 78h24M68 70l8 8-8 8" {STROKE}/>'),
]


def install():
    for d in (BIN, APPS, ICONS):
        d.mkdir(parents=True, exist_ok=True)
    for cmd, script, app_id, name, comment, cat, glyph in APPS_DEF:
        launcher = BIN / cmd
        launcher.write_text(f'#!/bin/sh\nexec python3 "{SCRIPTS / script}" "$@"\n')
        launcher.chmod(0o755)
        (ICONS / f"{cmd}.svg").write_text(cube_icon(glyph))
        (APPS / f"{app_id}.desktop").write_text(
            "[Desktop Entry]\nType=Application\n"
            f"Name={name}\nComment={comment}\nExec={(EXEC_DIR or BIN) / cmd} %F\nIcon={cmd}\n"
            f"Terminal=false\nCategories={cat};\nStartupWMClass={app_id}\n"
            + ("MimeType=application/pdf;\n" if cmd in ("cube-lock", "cube-pdf") else ""))
        print("installed", name)
    old = APPS / "pdf-lock.desktop"
    if old.exists():
        old.unlink()
        print("removed the earlier pdf-lock entry")


def uninstall():
    for cmd, _s, app_id, name, *_ in APPS_DEF:
        for f in (BIN / cmd, ICONS / f"{cmd}.svg", APPS / f"{app_id}.desktop"):
            if f.exists():
                f.unlink()
        print("removed", name)


if __name__ == "__main__":
    if "--destdir" in sys.argv:
        use_system_layout(sys.argv[sys.argv.index("--destdir") + 1])
    uninstall() if "--uninstall" in sys.argv else install()
