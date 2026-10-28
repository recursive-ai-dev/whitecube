# Standalone Debian package

From the Cube Browser directory, bootstrap the pinned dependencies as described
in the main README, then run:

```sh
python3 packaging/build-deb.py
python3 tests/test_package.py dist/cube-browser_0.10.0_amd64.deb
```

The builder uses Debian system development tools and libraries, the pinned
private X-free Cairo, and static SQLite, JPEG, QuickJS and Mbed TLS. It builds
in `build-deb/`, runs the unit and worker tests, and writes an architecture-specific
archive and SHA-256 file to `dist/`. Tests run sequentially to avoid competing
with the browser's short JavaScript execution budgets. It requires `dpkg-dev` and
`desktop-file-utils` in addition to the documented build dependencies. The native
compositor integration suite is run separately with `scripts/e2e.py`.
`--skip-tests` is available when the same source has already passed the suite.
`--output`, `--build-dir` and `--jobs` override the default build locations and
parallelism. No system files or running desktop session are changed by building.

Install the built archive with:

```sh
sudo apt install ./dist/cube-browser_0.10.0_amd64.deb
```

Start **Cube Browser** from the application menu or run `cube-browser`. The menu
also offers **New Private Window**. HTTP, HTTPS and HTML associations are
registered as choices; installation does not change the default browser. A native
Wayland session and Linux Landlock ABI 3 (Linux 6.2 or newer) are required.
Headless `--dump` and `--render` also work without a compositor. The archive's
shared-library dependencies are generated from its actual ELF symbol usage;
packages built on a newer distribution can require newer libraries than older
Debian/Ubuntu releases provide. Build on the oldest target distribution for
broader compatibility. This build targets the host architecture, not cross builds.

The launcher and all three executables live under `/usr/bin` and
`/usr/lib/cube-browser`; private Cairo is in `/usr/lib/cube-browser/lib`. Their
relative library path also allows testing an extracted archive. The package has
no dependency on a source checkout or Whitecube installation. Documentation and
all bundled dependency licenses are installed under `/usr/share/doc/cube-browser`.
Replace the private Cairo with an ABI-compatible shared build if desired; its
corresponding unmodified upstream source is identified in `copyright`.

Whitecube Desktop 0.8.1 and later use `whitecube-browser` for their bundled
browser command, desktop entry and icon, allowing both packages to coexist.
Whitecube Desktop 0.8.0 and earlier share these public paths and cannot be
coinstalled. This standalone archive declares neither `Conflicts` nor `Replaces`;
dpkg rejects owned-file collisions instead of removing or overwriting a desktop.

Existing user profiles are retained. Remove the app using `sudo apt remove
cube-browser`; this retains settings, history and downloads. User data is in the
standard XDG locations documented in the main README and can be removed manually
when desired. The package does not run scripts as root during install or removal.

For repeatable archives, set `SOURCE_DATE_EPOCH` to the desired release timestamp.
The default is the most recent Git commit timestamp affecting this application.
The builder fixes timestamps and ownership, strips debug information, normalizes
source paths in compiler output and emits sorted checksums. To verify a release,
run it twice with the same source, toolchain, dependencies and timestamp, then
compare SHA-256 files.
