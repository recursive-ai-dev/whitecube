# Whitecube maintenance

The supported release target is Debian 13 (Trixie), amd64. The package version
is defined once in `packaging/VERSION`; Meson and Debian packaging read it.
Start with [README](../README.md) and [the Debian build guide](BUILD-DEBIAN.md).
Historical machine-specific instructions are retained in
[the October maintenance archive](history/MAINTENANCE-2026-10.md).

## Build and tests

```sh
bash tools/build-debian.sh
```

This prepares a checkout-local Debian builder, compiles the native programs and
apps, runs `BUILD.sh`, and produces the binary `.deb`, checksum and JSON manifest.
It does not install host packages or restart the active desktop. Keep one build
per checkout; build caches contain writable generated native state.

Meson defines core native programs, hardening and C tests. `BUILD.sh` stages
compatibility paths such as `build/whitecube` and runs the app/Python/Node tests.
Direct Meson installs name the compositor `whitecube-compositor`, matching the
Debian package. Use `SANITIZE=1 ./BUILD.sh` for a separate ASan/UBSan build and
`REQUIRE_XFREE=1 ./BUILD.sh` to reject X11/XCB linkage in the core desktop.

For an individual command in the prepared builder:

```sh
bash tools/build-debian.sh -- bash -euc 'source build/native-deps/environment.sh; python3 -I tests/test-settings-transactions.py'
```

The builder recipe and Electron manifests are hashed for cache validation.
`bash tools/build-debian.sh --refresh` explicitly refreshes build inputs.
Do not export private Cairo through `LD_LIBRARY_PATH`; GTK needs its distribution
Cairo. Source `build/native-deps/environment.sh` only in the current build shell.

## Release verification

After building, run the complete verification outside the builder:

```sh
python3 -I tools/verify-release.py \
  --deb packaging/out/whitecube-desktop_0.9.1_amd64.deb \
  --rootfs build/debian-builder/rootfs \
  --previous-deb packaging/out/whitecube-desktop_0.8.1_amd64.deb \
  --output build/release-0.9.1
```

The output directory must be absent or empty. Add `--list` to inspect commands.
The gate checks the archive, strict headless session locking and live settings,
then runs package lifecycle checks and the installed Wayland runtime. With
`--previous-deb`, the runtime smoke uses the minimal dependency root produced by
the lifecycle test. It retains per-step logs, JSON reports and window captures.
The runtime bootstrap downloads Debian packages; do not treat an unavailable
network as a passing check.

The lifecycle test executes real apt/dpkg installation, maintainer scripts,
standalone-package coexistence, conffile-preserving upgrade, removal and purge.
It operates in a disposable user/PID/network namespace with service starts
blocked. The graphical smoke exercises installed paths, bundled libraries,
settings import/export, Browser, Player and Chat. It also verifies Trainer's
Parquet inspection and exact corpus round-trip with no PyArrow in the training
interpreter. See
[installed runtime scope](INSTALLED-SMOKE.md).

These checks do not replace physical GPU, display, input, audio and resume tests
on target hardware. The rootless graphical smoke uses `--no-sandbox` for Chat
inside its isolated namespace; normal packaged launchers retain the sandbox.
A booted system is still needed to verify systemd/login integration and Chromium's
real setuid sandbox.

## Release artifacts

Set `packaging/VERSION`, update README and add versioned release notes before the
final build. Commit the exact source first so package provenance identifies a
clean source revision. Verify the resulting archive, then publish the commit/tag,
`.deb`, `.deb.sha256`, `.deb.json` and verification reports together.
Never replace an already published version with different binaries.

```sh
cd packaging/out
sha256sum --check whitecube-desktop_0.9.1_amd64.deb.sha256
```

Keep the previous archive for upgrade testing and rollback. Installing never
restarts the compositor; users log out and back in to load native changes.

## Runtime contracts

- Wayland only: never export `DISPLAY`. Core libraries remain X-free; GTK,
  Player and Electron use their documented distribution compatibility libraries.
- No permanent session D-Bus is required. Preference writes use short-lived
  isolated dconf writers and retain per-user configuration.
- Privileged settings run through the validated root helper using literal argv.
  Hostname, timezone and console keyboard files are staged before replacement,
  flushed, permission-preserving and rolled back on reported apply failures.
  Atomic replacement of each file does not make a group of files crash-atomic.
- Desktop suspend waits for `swaylock --ready-fd` before requesting sleep.
  Missing/failed lockers must never result in an unprotected suspend request.
- Network probes observe; only association/lease/router faults trigger recovery.
  `cube-net` never invokes wpa_supplicant, logs no SSIDs or credentials, uses
  monotonic timers and publishes flat status JSON under 16 KiB.
- Package installation and removal never edit an existing account's dotfiles or
  start/stop live services. The sudoers examples require explicit administrator
  installation; the desktop package does not activate them automatically.

## Diagnostics and recovery

`whitecube-session --check` inspects startup prerequisites;
`whitecube-session --safe-mode` starts a minimal recovery desktop.
`whitecube-doctor --json --strict` produces scriptable findings; select groups
with repeated `--check`. Start with [diagnostics](DIAGNOSTICS.md) and
[session recovery](SESSION-RECOVERY.md).
