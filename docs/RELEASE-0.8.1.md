# Whitecube Desktop 0.8.1

Fixes installation alongside the standalone Chrootables and Cube Browser packages.
Version 0.8.0 could pass dependency resolution and still fail at dpkg unpacking
because those packages already owned four public command/launcher/icon paths.

- The bundled manager now lives at `/usr/libexec/whitecube/chrootable-manager`;
  its public alias is `whitecube-chrootable-manager`. The integrated window and
  Cube Strap use that private helper. Copied shell commands select the same helper.
- The bundled browser's command, desktop entry and icon are named `whitecube-browser`.
  Compositor defaults and `wc-browser` select it; the latter retains a standalone
  browser fallback when used without Whitecube. Existing user preferences remain intact.
- Standalone package files are neither overwritten nor replaced. No package
  removal or forced overwrite is required.
- Archive verification unpacks into a disposable dpkg root containing fixture
  standalone packages and checks their file contents and ownership afterward.
- A read-only installed-file conflict checker supplements apt dependency simulations:
  `python3 -I packaging/check-installed-conflicts.py PATH.deb`.
  The normal isolated builder also runs this checker against the real host.

Retry the corrected archive with:

```sh
sudo apt install ./packaging/out/whitecube-desktop_0.8.1_amd64.deb
```

Apt can finish configuring dependencies left unpacked by the failed attempt.
The `_apt` notice about downloading as root is separate: a private home directory
can prevent its restricted account from reading the local archive. It does not
cause the dpkg ownership error.
