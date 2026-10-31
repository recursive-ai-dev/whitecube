# Cube Chrootables engineering TODO

Audit: 2026-10-31. Scope: Whitecube's Qt workspace app, its privileged helper,
Cube Strap handoff, and independently installable distribution artifacts.
Ordered by observed impact, from stalled or unsafe privileged workflows to
release polish. Each checkbox requires working code and relevant verification.

- [x] **1. Reject nonregular export sources without blocking.** Open export
  sources without following links or waiting on FIFOs, verify the descriptor is
  a regular file, and leave the destination untouched on failure.
- [x] **2. Make Cube Strap builds independent of sudo timestamps.** Run the
  build through its already authenticated helper; a prior helper unlock must not
  be mistaken for permission to run a separate `sudo -n` command.
- [x] **3. Isolate manager standard input from the helper protocol.** Manager
  subprocesses receive an explicit null input rather than inheriting JSON input
  or a terminal; streamed results continue over the existing output channel.
- [x] **4. Keep deeply nested requests from terminating the privileged helper.**
  Reject parser recursion failures and continue serving subsequent valid requests.
- [x] **5. Bound desktop protocol memory and event-loop work.** Limit incomplete
  frames and queued bytes; process bounded batches so large replies cannot lock
  up the window or exhaust memory.
- [x] **6. Bound combining characters in terminal cells.** Prevent unbounded
  strings and repeated copying within one display cell while retaining normal
  combining accents and terminal cursor behavior.
- [x] **7. Keep one authorization attempt and preserve its action.** Repeated
  clicks must not replace a pending action or spawn competing sudo terminals;
  expose cancellation and a clear retry state.
- [x] **8. Cancel Cube Strap imports through their actual running helper.**
  Cancellation during import must reach the manager and keep staged cleanup and
  final status consistent with cancellation during bootstrap.
- [x] **9. Always finish Cube Strap UI state after history-write failures.**
  Disk-full or permission errors while recording history must be reported without
  leaving the build controls permanently busy.
- [x] **10. Tolerate concurrent inventory deletion.** If an image or snapshot
  disappears while scanning, omit the disappeared entry and preserve coherent
  counts rather than losing the entire library refresh.
- [x] **11. Retire desktop helpers asynchronously.** Lock, unlock and reconnect
  must not synchronously wait seconds on the GUI thread; stale helper signals
  cannot affect a newer connection.
- [x] **12. Reap exited terminal leaders independently of PTY EOF.** A descendant
  holding the PTY must not leave the session UI waiting forever after its leader
  exits; preserve pending output and avoid zombie children.
- [x] **13. Bound operation-console characters as well as lines.** One enormous
  unterminated log line must not bypass the retained-output limit or break output
  following and selection.
- [x] **14. Reject deeply nested desktop responses cleanly.** Fail affected
  callbacks once with a useful protocol error rather than raising into Qt's event
  loop, then permit reconnection.
- [x] **15. Canonicalize and validate store paths at desktop boundaries.** Reject
  aliases resolving to the host root and control characters consistently before
  creating a window, helper or authentication terminal.
- [x] **16. Report screenshot failures with a nonzero exit.** Directory creation
  and image-save errors must finish the automated run with a useful error rather
  than hanging or reporting success without an image.
- [x] **17. Package a coexisting standalone Studio edition.** Keep its command,
  private payload and desktop identity separate from Whitecube and standalone
  Chrootables, while intentionally using the same workspace store and preferences.
- [x] **18. Build every app distribution format from one normalized payload.**
  Produce rootless DEB, RPM, Arch package and x86_64 host-runtime AppImage artifacts
  with explicit distribution dependencies and checksum-pinned AppImage tooling.
- [x] **19. Verify real extracted artifacts and reproducibility.** Check payload
  modes, links and metadata, run version/capabilities/diagnostics/helper/demo
  checks, and compare repeat builds across output paths and umasks.
- [x] **20. Publish complete artifacts atomically with checksums.** Preserve
  conflicting outputs, flush files before publication, and generate per-artifact
  SHA256 files, aggregate checksums and a machine-readable release manifest.
- [x] **21. Keep release version, provenance and installation instructions coherent.**
  Distinguish the Cube application version from its upstream engine, document
  terminal authentication and Studio installation, and preserve license notices.
- [x] **22. Gate and publish the reviewed release.** Run the combined regression
  suite and artifact checks in CI, commit only the authorized application changes,
  reconcile `main` without overwriting others' work, and publish the verified
  binaries on the matching GitHub release.

## Verification record

All 22 items are implemented and delivered.

- **Source:** tag `cube-chrootables-v1.3.0`, commit
  `0ba0a94d06aba046147ff86c68617caa12afd826`.
- **Release:** [Cube Chrootables Studio 1.3.0](https://github.com/recursive-ai-dev/whitecube/releases/tag/cube-chrootables-v1.3.0).
  Includes DEB, RPM, Arch `any.pkg.tar.zst`, x86_64 host-runtime AppImage,
  four individual checksum files, `SHA256SUMS`, and `release-manifest.json`.
- **Application/engine:** 357 tests passed on both Qt 6.10.2 and Qt 6.11.2;
  demo and absent-store screenshots rendered without creating an image store.
  The protected-publication cancellation regression passed 20 consecutive runs
  with an explicit ordering barrier.
- **Cube Strap:** 26 checks and the isolated Wayland workflow smoke passed,
  including timestamp-independent build/import and cancellation cleanup.
- **Distribution:** seven packaging tests passed, including actual extraction,
  payload/ownership checks, documentation-excluded installation, checksum
  verification and byte-identical rebuilds across paths and umasks for all formats.
- **Native lifecycle:** Debian, Fedora and Arch installation, upgrade and removal
  passed; workspace data survived package removal.
- **Privileged engine:** real signed Debian bootstrap for minimal and Python
  profiles, persistent/disposable sessions, recovery, shared folders, restricted
  devices, cancellation and cleanup passed.
- **Release gates:** [main validation](https://github.com/recursive-ai-dev/whitecube/actions/runs/41176492733)
  and [complete tagged validation](https://github.com/recursive-ai-dev/whitecube/actions/runs/41176877396)
  completed successfully before publication.

The application is version 1.3.0; its copied upstream engine remains version
1.2.1. The Studio artifacts have independent command, payload and desktop names
and intentionally share the existing Cube store/preferences. No image format
conversion or unrelated browser/desktop source changes were included.
