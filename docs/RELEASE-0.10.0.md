# Whitecube Desktop 0.10.0

Whitecube now opens ordinary applications as floating stacking windows. New
windows cascade inside the work area; clicking raises a window and its dialogs.
Client move/resize requests validate the current pointer grab, and Alt+left-drag
and Alt+right-drag provide compositor move/resize controls. Maximize/fullscreen
restoration, workspace isolation and session locking remain supported. See
[Floating windows](FLOATING-WINDOWS.md) for the complete controls.

The desktop includes Cube Browser 0.11.0. Its improvements cover runtime
metadata/diagnostics, scroll-triggered lazy images, static WebP, computed styles,
stable image layout, safer DOM operations, abortable requests/listeners, queued
visibility observations, sustainable media polling and current URL metadata.
Browser limits and validation evidence are in the app's COMPATIBILITY.md,
VALIDATION.md and TODO.md.

The release provides the desktop Debian package and standalone browser Debian
and relocatable Linux archives for amd64. Each archive includes the required
binaries, private dependencies and upstream notices. Verify the SHA-256 checksums
before installation. Build tools operate inside the disposable Debian builder;
no build or verification step installs the desktop on the host.

Install the chosen desktop package with:

```sh
sudo apt install ./whitecube-desktop_0.10.0_amd64.deb
```

Log out and start a new Whitecube session to use the rebuilt compositor. Existing
profiles and normalized placements are retained. To roll back, install the
previous desktop package and start a new session. Browser and standalone
Chrootables commands remain namespaced so their independent packages can coexist.

Validation covers 13 native core suites, the browser's 24 regression suites,
browser/compositor ASan and UBSan checks, real Wayland GUI workflows and archive
relocation. The release verification report records final package installation,
upgrade/removal and installed-runtime checks. The reported gallery was not
identified, so no claim is made that every heavy JavaScript site is supported.
