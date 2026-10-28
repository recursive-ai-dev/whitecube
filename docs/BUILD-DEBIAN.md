# Building the Debian package

Whitecube targets Debian 13 (Trixie), amd64. Build in that distribution so the
package's shared-library dependencies match the target system. The desktop uses
wlroots 0.18 and bundles private X-free builds of wlroots and Cairo. Desktop Chat
includes its own Electron runtime; system GTK and Python dependencies remain
Debian packages.

## Build from a different Linux host

From the repository root, run:

```sh
bash tools/build-debian.sh
```

Use an amd64 Linux host with `apt-get`, `dpkg`, `dpkg-deb`, Perl, Python 3,
`flock`, `curl`, `tar`, `sha256sum` and `bwrap` (Bubblewrap). Bubblewrap must be able to create an unprivileged user
namespace and mount its private root. The runner does not require `chroot(2)` or
root access. Host apt sources must provide `mmdebstrap` and
`debian-archive-keyring`; the runner downloads and extracts those tools locally.
It downloads an authenticated Debian root filesystem and development packages,
configures them in a namespace, and binds this checkout at its existing absolute
path. It installs no host packages, changes no host services, and keeps the builder under
`build/debian-builder/`.

The first run downloads the Debian dependencies and may take several minutes.
It then runs `npm --prefix apps/desktop-chat ci` inside Debian when the Electron
executable is absent, builds the native dependencies locally, runs `BUILD.sh` and
creates the Debian package under `packaging/out/`. Desktop Chat requires Node.js
22.12 or newer and npm for source builds. The runner stages official Node.js
22.23.4 locally, verifies its pinned SHA256, and makes it available read-only at
`/opt/whitecube-node` inside the builder. `PATH` and `NODE` select that runtime;
the host's Node.js installation is not used.
`BUILD.sh` includes the native application tests; optional hardware training
tests still require the explicit flags described in the main README.

The prepared root filesystem is cached for subsequent builds; a ready cache skips
the root filesystem setup. Node.js downloads are cached in `node-downloads/`
under the selected builder directory, with the extracted runtime in `node/`.
Dependency archives and npm files also remain cached,
while native preparation rebuilds generated state for the selected toolchain.
Repeated builds can still download updated source or runtime inputs.
`WHITECUBE_BUILDER_DIR` selects another builder cache directory without moving
checkout outputs. Native source trees, generated objects and application `.deps`
directories remain in the checkout, so run one build per checkout at a time. Preserve earlier package artifacts until the new build passes
validation.

Useful commands:

```sh
# Prepare the Debian builder without compiling the project.
bash tools/build-debian.sh --prepare-only

# Open a shell in that builder, with the checkout available.
bash tools/build-debian.sh --shell

# Run a particular check inside Debian; arguments after -- execute directly.
bash tools/build-debian.sh -- python3 -I tests/test-session.py

# Run shell syntax after explicitly loading prepared native dependencies.
bash tools/build-debian.sh -- bash -euc 'source build/native-deps/environment.sh; python3 -I tests/test-doctor.py'
```

`--prepare-only` creates the Debian environment and local Node.js runtime, then
stops before npm dependency installation, native preparation or project compilation. `--shell` and command mode also skip those
steps. Once native preparation has completed, source
`build/native-deps/environment.sh` in a shell command that compiles native code;
the runner clears host library and Python search-path overrides on entry. The
checkout is writable and changes made inside the builder remain visible outside.

Bootstrap and configuration logs are `extract.log` and `configure.log` in the
selected builder cache directory. A package build writes its build/test log to `packaging/out.build.log`. Failed builds print the
relevant log path and leave the source checkout available for correction.

## Build directly on Debian 13

Install the system development dependencies first. The complete list is in
`tools/build-debian.sh`; source retrieval also requires an authenticated
`deb-src` entry for Debian 13 and refreshed apt metadata. Then run:

```sh
npm --prefix apps/desktop-chat ci
export ELECTRON_DIST="$PWD/apps/desktop-chat/node_modules/electron/dist"
bash tools/prepare-native-deps.sh
source build/native-deps/environment.sh
bash packaging/build-deb.sh
```

The default isolated build sets `ELECTRON_DIST` to that same npm runtime path.
An explicit distribution can also be supplied to the isolated builder:

```sh
ELECTRON_DIST=/absolute/path/to/electron-dist bash tools/build-debian.sh
```

The runner validates its executable and makes the supplied directory available
read-only inside the builder at the same path. For direct builds, `ELECTRON_DIST` can name another complete Electron `dist/`
directory, or stage it at `build/desktop-chat-runtime`. It must include the
executable, sandbox helper, version, licenses, resources and locale files. Use
`npm ci` to refresh an existing npm runtime when the lockfile changes; the default
runner reuses an already present executable and skips npm installation when an
explicit distribution is supplied. Installed packages include Electron
and require neither Node.js nor npm.

`tools/prepare-native-deps.sh` does not install system packages. It verifies the
browser's pinned Cairo, SQLite, JPEG, QuickJS and Mbed TLS downloads, rebuilds
generated objects and Meson state for the selected distribution, extracts
mpv development files from the configured signed apt repository, and obtains
wlroots 0.18 through authenticated Debian source metadata. Its wlroots build
omits Xwayland and the X11 backend; Pixman and the headless backend remain
available for tests. It checks the resulting Cairo and wlroots library closures
for missing libraries and X11/XCB linkage before continuing.

The generated `build/native-deps/environment.sh` sets only `PKG_CONFIG_PATH` for
this checkout's compiler metadata. Source it in the same shell as `BUILD.sh` or
the package command. It changes no login configuration. Set `JOBS` to choose the
dependency compilation concurrency.

`BUILD.sh` embeds the local library directories in its core binaries and clears
inherited library overrides before running GTK tests. Meson applications also
carry their own build search paths. The package replaces source paths with
`/usr/lib/whitecube` and gives private libraries a sibling search path. GTK apps
use their distribution Cairo. Avoid exporting the private Cairo directory in a
global `LD_LIBRARY_PATH`: its X-free build omits symbols required by GTK's X11
backend. If an older generated environment was already sourced in your shell,
run `unset LD_LIBRARY_PATH` before continuing.

## Validation scope

The default package build runs the core C and Python tests, desktop application
unit and rendering checks, Chat's Node.js suite, and the native browser/player
Meson suites with their `integration` suite excluded. The finished archive is
checked for ownership, permissions, conffiles, symlinks, checksums and ABI
requirements, then unpacked with dpkg into a disposable installation root.
This unpack test does not configure the desktop or start its session.

To run Cube Player's optional real Wayland playback checks after a successful
build:

```sh
bash tools/build-debian.sh -- bash -euc 'source build/native-deps/environment.sh; meson test -C build/cube-player --suite integration --print-errorlogs'
```

Training pipeline checks require `TRAINING_PYTHON` to name a Python environment
with the training dependencies visible inside the builder. `TRAINING_SMOKE=1`
adds hardware worker checks. Physical display, input and audio behavior still
need a desktop session on the target machine. See the individual app validation
guides for their additional checks.

The output directory retains earlier packages. A successful release also produces
`<package>.deb.sha256` and `<package>.deb.json`, with the latter recording the
verified archive hash, dependency list and file counts. The checksum file uses
just the artifact filename, so check it from `packaging/out/` with
`sha256sum --check <package>.deb.sha256`. `SOURCE_DATE_EPOCH` controls normalized
archive timestamps; by default it comes from the last source commit.

## Install the artifact

Use the exact package filename printed by the successful build:

```sh
sudo apt install ./packaging/out/whitecube-desktop_<version>_amd64.deb
```

Apt resolves the declared Debian dependencies. Log out and select **Whitecube**
in the login manager to load the new compositor and panel. See
[SESSION-RECOVERY.md](SESSION-RECOVERY.md) for startup checks, logs and safe mode.
