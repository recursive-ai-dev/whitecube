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
- [ ] **22. Gate and publish the reviewed release.** Run the combined regression
  suite and artifact checks in CI, commit only the authorized application changes,
  reconcile `main` without overwriting others' work, and publish the verified
  binaries on the matching GitHub release.

## Verification record

Implementation and release results will be recorded here as the work completes.

- Desktop items 5–7 and 11–14: focused client (20), desktop (50), and terminal
  (76) tests passed, followed by the combined 357-test suite and real-launcher
  demo/empty-store screenshots. Python compilation and whitespace checks passed.
- Protocol limits allow 8 MiB inventory frames and 16 MiB of queued wire data.
  Decoding yields after 100 messages or 64 KiB per turn; one larger inventory
  frame may be decoded to make progress. JSON nesting is limited to 64 levels.
  Terminal cells retain 32 characters, and the operation console retains at most
  256 Ki UTF-16 units as well as its existing 4,000-block limit.

Local release verification: 357 combined application/engine tests, 26 Cube Strap
checks and seven packaging tests passed. The DEB, RPM, Arch and AppImage artifacts
passed extracted CLI, helper, checksum and desktop smoke checks, and each format
rebuilt byte-for-byte across output paths and umasks. Real desktop screenshots
were rendered without creating an image store. Native installation/upgrade/removal
and privileged signed-bootstrap integration are release CI gates. Publication is
pending those gates and the final source-commit provenance rebuild.
