# Cube Chrootables

Version 1.3.0 is Whitecube's application release. Its bundled image engine is
Chrootables 1.2.1, with the local changes listed in `UPSTREAM.md`.

Whitecube's Qt workbench for Debian images, copied from the standalone
Chrootables desktop. Run `cube-chrootables` as your normal desktop user.
`cube-chrootables --demo` opens a read-only tour with sample workspaces.
The standalone `chrootables-desktop` remains a separate application.

## Distribution packages

The independent **Cube Chrootables Studio** edition installs alongside Whitecube
and standalone Chrootables. It uses the same workspace store and Cube preferences;
its command, application-menu identity and installed code are separate.

| Edition | Desktop command | Engine command |
| --- | --- | --- |
| Whitecube Desktop | `cube-chrootables` | `whitecube-chrootable-manager` |
| Cube Chrootables Studio | `cube-chrootables-studio` | `cube-chrootables-manager` |
| Standalone Chrootables | `chrootables-desktop` | `chrootable-manager` |

Studio distribution artifacts are a Debian/Ubuntu `.deb`, a Fedora `.rpm`, an
Arch Linux `.pkg.tar.zst`, and an x86_64 host-runtime `.AppImage`. Native packages
declare their distribution dependencies. The AppImage requires host
`/usr/bin/python3` 3.10+, PySide6 6.8+, Qt's native Wayland plugin, `sudo`, an
installed terminal, and the engine's host tools. It extracts before starting
administrator operations so the root helper can read its application files.

Build all formats without root from the Whitecube checkout:

```bash
/usr/bin/python3 apps/chrootables/packaging/fetch-build-tools.py --output /tmp/cube-chrootables-tools
export APPIMAGETOOL=/tmp/cube-chrootables-tools/appimagetool-1.9.1-x86_64.AppImage
export APPIMAGE_RUNTIME_FILE=/tmp/cube-chrootables-tools/runtime-x86_64
/usr/bin/python3 apps/chrootables/packaging/build-packages.py --all
```

Installable artifacts, SHA256 checksums and the release manifest are written to
`apps/chrootables/dist/`. Verify `SHA256SUMS` before installation. See
`packaging/README.md` for format-specific installation, runtime requirements and
repeatable-build checks. Removing any package leaves workspace images intact.

## Workspace management

Create profiled workspaces, open embedded or external terminals, use disposable
sessions, share host folders, save and restore recovery points, clone, expand,
repair, export and import images, and inspect host health and operation logs.
The same `/var/lib/chrootable-manager` store is used by both desktops and Cube
Strap. Existing images are not copied or converted.

**Unlock store** opens Whitecube Terminal and runs `sudo`. Type your password
in the terminal; the graphical application never handles it. Administrator
access lasts until you lock the store or close the app. The helper accepts
validated requests through a private local connection and finishes an active
filesystem operation if the window disconnects. No graphical authentication
agent is required. Cancelling authentication returns to the read-only window.
Interactive shells authenticate separately through `sudo` in their terminal.

The desktop needs system Python 3.10+, PySide6 6.8+ and Qt's Wayland plugin.
Debian/Ubuntu packages are `python3-pyside6.qtwidgets` and `qt6-wayland`;
Arch packages are `pyside6` and `qt6-wayland`. Launchers select `/usr/bin/python3`
so an unrelated virtual environment cannot hide the distro bindings.

The `chrootables/` package contains the window, backend, client, terminal and
helper. `chrootable-manager` is the copied engine, installed privately under
`/usr/libexec/whitecube/` and available as `whitecube-chrootable-manager`.
`MANAGER.md` documents the upstream engine and desktop. The top-level
`backend.py` and `broker.py` remain only for Cube Strap compatibility.

Cube Chrootables saves its own settings under
`~/.config/whitecube/cube-chrootables.conf`. The standalone desktop and previous
GTK application's settings are independent. Image data remains in the shared
store. Source attribution and local changes are recorded in `UPSTREAM.md`.

Run `/usr/bin/python3 tests/test-chrootables.py` from the Whitecube repository
for compatibility, launcher, helper and Qt checks. The copied upstream suites
under `apps/chrootables/tests` cover operations, protocol validation, the
desktop, shared folders and terminal behavior without changing real images.
