# Whitecube Desktop 0.9.0

Whitecube 0.9.0 improves desktop action feedback, protects explicit suspend
actions, keeps large Chat/Trainer libraries responsive, and adds a repeatable
binary-release verification workflow. The supported release is Debian 13
(Trixie), amd64.

## Desktop and settings

- Suspend in the native panel and Settings invokes `whitecube-suspend`, which
  waits for `swaylock --ready-fd` to confirm compositor locking before requesting
  suspend. A missing, failed or unresponsive locker prevents the suspend request.
  A failed suspend leaves the locker running.
- Panel helper jobs capture bounded diagnostics, track child completion and
  deadlines, and show visible failure notices. Rapid brightness/volume changes
  coalesce to the latest requested value; repeated reads do not saturate jobs.
- App launches use an exec handshake to report missing executables, inaccessible
  application directories and permission failures. An application's later exit
  is not treated as a failed launch.
- Settings and the launcher process Compose/dead keys using the selected locale.
  Launcher key repeat ends on release, focus loss, panel changes or keymap reload.
- Hostname, timezone and console-keyboard changes stage complete replacements,
  preserve file ownership/permissions, serialize Whitecube writers and roll back
  partial writes or failed apply commands. Rollback retains unrelated concurrent
  edits and reports when manual recovery is needed. Individual replacements are
  atomic; a group of files is not a crash-atomic filesystem transaction.

## Applications

- Chat searches and indexes conversation history in a worker with a bounded text
  cache, debounced queries and a paged sidebar. File changes/deletions are reflected
  in results, and an unreadable conversation does not hide the rest of the library.
- Trainer collects run/job metadata, progress, logs and detail views in a
  background snapshot. Refresh requests coalesce, unchanged metadata is cached,
  and stale selection results are rejected.
- Editor offers Save, Discard edits and Keep editing when closing or switching
  modified projects. Invalid fields and unwritable projects retain a discard path;
  discarding never writes the invalid draft to the saved project.

## Building and verification

- Meson owns the core native target, hardening and C-test definitions used by
  normal `BUILD.sh` builds. Direct installs use `whitecube-compositor`, matching
  the Debian package and avoiding the unrelated image tool's command name.
- Debian builder caches are keyed by their recipes; Electron caches follow the
  package manifests/lockfile and verify the pinned runtime version. `--refresh`
  explicitly refreshes build inputs. Fresh bootstrap also correctly separates
  essential-package apt patterns from the named package list.
- Packages embed build provenance: source commit/dirty state, input hashes,
  compiler/runtime versions and builder identity. The archive manifest carries
  that provenance alongside the existing checksums, hardening and ABI checks.
- `tools/verify-release.py` retains archive verification, strict headless lock
  tests, live-settings integration, installed-runtime reports and window captures.
  With `--previous-deb`, it runs a runtime-only Debian lifecycle test covering
  clean apt install, maintainer scripts, standalone package coexistence, upgrade,
  modified-conffile preservation, removal and purge before graphical smoke testing.
- GitHub checks run the fast build/release, session, privileged-write and Chat
  regression suites. Current onboarding and maintenance docs describe the packaged
  product; historical machine-specific notes and reviews are marked accordingly.

## Binary installation

Download `whitecube-desktop_0.9.0_amd64.deb` and its `.sha256` file from the release.
Run both commands in the directory containing those files:

```sh
sha256sum --check whitecube-desktop_0.9.0_amd64.deb.sha256
sudo apt install ./whitecube-desktop_0.9.0_amd64.deb
```

Log out and back in to load native changes. Existing user preferences remain in
place. The package coexists with standalone Browser/Chrootables packages, edits
no existing home directory and starts no services. Active sudoers policy still
requires explicit administrator setup; examples are shipped as documentation.

## Validation scope

The release assets include the compiled Debian package, checksum, archive/build
manifest and verification evidence. See [maintenance](MAINTENANCE.md) for the
exact full verification command and [installed smoke](INSTALLED-SMOKE.md) for
isolation and limitations.

Explicit desktop Suspend controls are covered by lock coordination. External
lid/system-initiated suspension remains a follow-up. Physical display/GPU/input,
audio, actual hardware resume, booted systemd/login-manager integration and
Chromium's real setuid sandbox require a target-machine check. The isolated
headless Chat smoke uses `--no-sandbox`; normal launchers retain the sandbox.
Multiple-monitor placement/panels and mixed-scale rendering remain a separate
milestone.
