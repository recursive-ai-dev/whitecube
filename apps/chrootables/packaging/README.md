# Cube Chrootables Studio packages

Studio runs the same app as Whitecube's `cube-chrootables`, with its own
`cube-chrootables-studio` command and menu entry. Its private files live under
`/usr/lib/cube-chrootables-studio`; its CLI is `cube-chrootables-manager`.
It coexists with Whitecube and standalone Chrootables. Both Cube editions
intentionally share workspace data and preferences. Removal keeps image data.

Build every format without root:

```bash
/usr/bin/python3 apps/chrootables/packaging/fetch-build-tools.py --output "$HOME/.cache/cube-chrootables/build-tools"
export APPIMAGETOOL="$HOME/.cache/cube-chrootables/build-tools/appimagetool-1.9.1-x86_64.AppImage"
export APPIMAGE_RUNTIME_FILE="$HOME/.cache/cube-chrootables/build-tools/runtime-x86_64"
/usr/bin/python3 apps/chrootables/packaging/build-packages.py --all
```

The builder requires Python 3.12+, `dpkg-deb`, `rpmbuild`, `rpm`, `rpm2cpio`,
`cpio`, `zstd`, `mksquashfs`, `unsquashfs`, distribution PySide6, and the pinned
AppImage tools. It never installs or downloads tools implicitly. `--format`
selects `deb`, `rpm`, `arch`, or `appimage`. `--output` selects another directory;
`--revision` sets the package revision. Identical builds are accepted; changing
an existing artifact or checksum requires `--force`. All candidate outputs are
checked before publication. Builds produce individual checksums, `SHA256SUMS`
and `release-manifest.json`, with source/payload provenance.

Install the appropriate native artifact:

```bash
sudo apt install ./cube-chrootables-studio_*_all.deb
sudo dnf install ./cube-chrootables-studio-*.noarch.rpm
sudo pacman -U ./cube-chrootables-studio-*-any.pkg.tar.zst
```

Native packages declare Python 3.10+, PySide6 6.8+, Qt Wayland, Foot, sudo and
workspace engine dependencies. Debian 13/Ubuntu 26.04 and current Fedora/Arch
provide these runtimes. DEB, RPM and Arch payloads are architecture independent;
workspace guest support remains x86_64. Native installation lifecycle testing
on Fedora and Arch must be performed on those distributions; extraction checks
are also run by the release builder's verification command.

The x86_64 AppImage uses host `/usr/bin/python3`, PySide6 6.8+ and a terminal.
It does **not** bundle Python, Qt, a Linux distribution or engine prerequisites.
Run it with `--demo` for a read-only tour. It extracts before authentication so
root can read the helper outside a user's FUSE mount. Building another runtime
architecture requires separately pinned tooling and architecture validation.

```bash
sha256sum --check SHA256SUMS
chmod +x Cube-Chrootables-Studio-*-x86_64.AppImage
./Cube-Chrootables-Studio-*-x86_64.AppImage --demo
/usr/bin/python3 apps/chrootables/packaging/check-installed.py --prefix /path/to/extracted/usr
```

The application version and copied upstream engine version are tracked
separately. `SOURCE_DATE_EPOCH` controls reproducible timestamps, defaulting to
2026-10-31 00:00 UTC. A release must rebuild after its source commit so the
included provenance names the published revision.
