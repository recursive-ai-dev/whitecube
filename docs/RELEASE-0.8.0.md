# Whitecube Desktop 0.8.0

This release upgrades startup recovery, installed-system diagnostics, portable
settings and playlist handling, and strengthens Debian release verification.

## Startup and recovery

- Validate runtime directories for absolute paths, account ownership, mode 0700
  and symlink substitution; create a private fallback when logind is absent.
- Prevent duplicate launches from replacing an active session's logs.
- Add startup JSON inspection, compositor-only safe mode and crash-guard reset.
- Count recent early failures while excluding deliberate termination and long
  sessions; retain the previous log and forward termination to the compositor.
- Keep log and crash records private and reject unsafe links and file types.
- Honor XDG_CONFIG_HOME when selecting session audio and validating keyboards.

## Diagnostics, preferences and media

- Diagnose system package binaries, settings schemas, missing shared libraries,
  private Chat runtime, and active Wayland socket ownership/permissions.
- Add selectable diagnostic groups and strict exit mode while retaining the
  existing JSON report shape.
- Export explicit preference overrides to typed, versioned JSON; check profiles
  without writing, and merge validated imports in one dconf changeset.
- Save named profile files atomically with mode 0600. Reject duplicate keys,
  invalid types, out-of-range values and unsupported profile versions.
- Expand local PLS and M3U playlists with numeric track ordering, BOM/CRLF and
  relative-path handling. Preserve HLS manifests and limit recursive work.
- Import VLC-style XSPF playlists with XML entities, relative locations and
  alternative media locations; reject external entities and bound track/nesting counts.
- Keep the session crash guard armed across immediate login-manager retries and
  report failed library inspections instead of treating them as healthy.
- Preview Cube Editor build plans from a headless terminal even when no graphical
  authentication terminal is available; actual image operations retain authentication.

## Debian releases

- Release version 0.8.0; preserve all shipped /etc defaults with conffiles.
- Add runtime dependencies for the settings writer and derive ABI version
  requirements from the actual shipped ELF binaries and private runtimes.
- Declare the Help pager, process inspection and system-control commands as
  runtime dependencies rather than relying on a preconfigured desktop host.
- Bundle wlroots' display-info ABI privately with sibling library lookup, so
  newer distributions can install without an obsolete system library package.
- Keep apt dependencies to direct shipped-library requirements and their ABI
  minima; allow the target distribution to resolve its own transitive libraries.
- Verify archive ownership, modes, symlinks, conffiles and complete checksums.
- Harden the eight native executables with PIE, stack protection, fortified
  functions and full RELRO. Independently verify amd64 ELF headers, PIE, RELRO
  and non-executable stacks in the final archive.
- Test dpkg unpacking and its package database in a disposable installation root.
- Emit a portable SHA256 file and JSON manifest alongside the verified archive.
- Normalize archive timestamps using SOURCE_DATE_EPOCH and correct fresh-build
  ordering so package tests see the complete native app staging tree.
- Add an [installed runtime smoke test](INSTALLED-SMOKE.md) for the delivered
  archive, using installed absolute paths in a private, read-only Debian root.

The build never installs Whitecube on the host. Installation starts no desktop,
network service or firewall change. Select Whitecube at login after installing
the specific package printed by the build. Optional hardware training and live
physical display tests remain separate from automated headless validation.
