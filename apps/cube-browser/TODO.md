# Cube Browser improvement plan

Work in this order, from failures that interrupt browsing to release polish.
Each item requires a complete implementation, relevant regression evidence, and
an update here before it is marked complete. Existing interpreter, sandbox,
origin, image-decoding and document quotas remain enforced.

Release scope: Cube Browser and its renderer/network workers, plus the requested
Whitecube floating-window desktop changes, on the supported Linux host
architecture. Preserve unrelated work elsewhere in the repository.

1. [x] **Finish failed and timed-out resource requests.** Ensure every accepted
   image/script request reaches one terminal state and a single acknowledgement;
   timeouts must reject waiting work, dispatch image errors, release workers and
   leave later dynamic batches usable. Test deadlines and rejected resources.
2. [x] **Cancel abandoned fetches.** Implement bounded AbortController/AbortSignal
   support for GET fetch, including pre-aborted signals, one-shot reasons, native
   worker cancellation, late-response rejection and quota recovery.
3. [x] **Make image state and events consistent.** Track source generations,
   suppress stale/duplicate events, and settle invalid, empty, cached, blocked and
   data-image sources correctly. Verify complete/natural dimensions and removal.
4. [x] **Report only genuinely unhandled promise rejections.** Defer rejection
   reporting to the job checkpoint, remove handled entries, retain useful stacks,
   and bound retained promises and diagnostic output.
5. [x] **Display static WebP images.** Add a verified private decoder dependency,
   MIME/signature handling and direct/inline/dynamic image support while retaining
   byte, dimension, pixel, animation and malformed-input limits.
6. [x] **Expose the engine's computed styles.** Implement a bounded read-only
   getComputedStyle interface for properties the layout engine actually supports;
   resolve inheritance, CSS variables and zoom consistently without claiming a
   complete CSSOM or silently fabricating unsupported pseudo-elements.
7. [x] **Reserve image space before downloads finish.** Honor declared image
   dimensions/aspect ratios in placeholder layout, preserve alt text, and prevent
   gallery positions from jumping when image data arrives or fails.
8. [x] **Make DOM replacement and cloning safe.** Validate replacements before
   mutation, make replacing a node with itself a no-op, and add bounded cloneNode
   semantics with independent attributes/children and inert cloned resources.
9. [x] **Correct event lifecycle and registration contracts.** Prevent listener
   quota corruption after failed registrations, reset dispatch state correctly,
   reject invalid callbacks predictably, and support legacy event initialization
   used by browser libraries without losing cancellation or propagation rules.
10. [x] **Tighten visibility-observer mathematics and delivery.** Use finite
    geometry, bounded sorted thresholds, stable observation snapshots and queued
    records; define disconnect/reobserve and takeRecords behavior precisely and
    verify boundary crossings, zero-area boxes and percentage root margins.
11. [x] **Keep media-query polling sustainable.** Avoid retaining every unused
    matchMedia result forever, bound active listeners rather than lifetime query
    calls, and add accurate desktop pointer/hover, color-scheme and resolution
    features with zoom/preference regression coverage.
12. [x] **Keep location metadata current.** Derive location fields and origin
    from the native current URL on reads, including same-document fragments,
    default ports, file paths and queries, without enabling scripted navigation.
13. [x] **Give images the correct DOM interface.** Separate HTMLImageElement from
    other elements, preserve instanceof/prototype identity, and reflect src,
    currentSrc, width/height and natural dimensions with consistent CSS pixels.
14. [x] **Embed JavaScript without C-string corruption.** Handle C trigraphs,
    Unicode, control bytes and line boundaries deterministically; verify generated
    source round-trips through a real C compiler and preserves JavaScript syntax.
15. [x] **Build complete, reproducible distribution artifacts.** Produce and
    relocate-test the standalone Debian package and Linux binary archive with
    all three executables, private libraries, licenses, build metadata and
    SHA-256 checksums; reject incomplete or mismatched outputs.
16. [ ] **Complete the release gate and publish.** Update version, compatibility
    and release notes; run regression, sanitizer, corpus and native Wayland gates;
    verify archive payloads; commit only this app's changes, integrate any remote
    changes without losing work, push main and publish a versioned GitHub release
    containing the validated binaries and checksums.
17. [x] **User-requested desktop prerequisite: floating stacking windows.** Make
    Whitecube keep ordinary windows visible together, cascade new floating
    placements, raise the focused window/family, honor client move/resize requests
    with input-serial validation, and retain fullscreen, workspaces and lock
    isolation. Validate placement/stacking and pointer resize bounds before the
    final rebuild, main push and release. This added request takes priority over
    the remaining browser work until the desktop behavior is implemented.

## Evidence

Implementation details, commands and results will be recorded here as each item
is completed. A checked item describes verified behavior, not an intention or a
claim of full web-platform compatibility.

- **1:** Resource delivery has an independent acknowledgement deadline. Every
  dispatched body/failure is marked before delivery, preventing a rejected image
  from receiving a second error. Expired batches notify the renderer rather than
  abandoning scripts. `meson test -C build resource-deadlines web-apis` passes,
  including the real 20-second deadline, subsequent image-batch recovery and
  single-error assertions for malformed images.
- **2:** AbortController/AbortSignal support stable abort reasons, pre-aborted
  fetch refusal, bounded timeout signals and cancellation independent of public
  abort-event propagation. A dedicated IPC message removes queued requests or
  terminates their network workers; the renderer frees the matching pending ID
  and ignores late replies. Three HTTP cancellation tests pass, including 64
  cancellations followed by a successful fetch and active timeout recovery.
- **17:** Ordinary windows remain visible together and new placements cascade.
  Focus raises only the selected family while preserving window-list order.
  Validated client pointer grabs now drive move/resize; opposing/invalid edges,
  stale serials and locked-session requests are refused. Corner movement honors
  size limits and saturates before integer conversion, and interrupted grabs
  release pressed-button bookkeeping. The Debian-builder compositor, workspace
  and hotplug suites pass. The final release gate will also exercise native
  presentation. Documentation is in `docs/FLOATING-WINDOWS.md`.
- **3:** Source assignments carry native generations; pending image notifications
  are delivered from stable snapshots in batches of at most 64. Reassignments
  invalidate stale events and removal cancels them. Empty, invalid, quota-denied
  and failed data sources are complete with zero natural dimensions and settle
  as errors. Cached/data loads use the same asynchronous path. Web-API and DOM
  suites pass, including rapid A/B/A changes, source removal and cached reuse.
- **4:** QuickJS rejection notifications retain at most 128 promise/reason pairs
  until the microtask checkpoint. Installing a handler removes its pair; only
  remaining rejections are formatted and reported. Diagnostic conversion stays
  inside the execution budget, conversion failures do not poison the context,
  and retained values are freed on shutdown. Runtime, web-API and DOM suites
  pass, including handled pre-aborted fetches with no false console errors.

- **5:** The pinned libwebp 1.6.0 decoder handles static remote, data and direct
  images. Strict RIFF length checks, animation refusal and existing allocation
  limits precede decoding. Image, subresource and web-API suites pass, including
  exact premultiplied alpha, every truncation, oversized headers and animations.
  Decoder notices and patent grant are included in distribution packaging.

- **6–8:** Live computed-style reads use the engine cascade and CSS pixels;
  pseudo-elements and mutation are rejected. Image placeholders retain declared
  dimensions and positive aspect ratios with clipped alt text. Replacement is
  validated before mutation; native clones have independent attributes/children
  and no copied listeners or resource grants. Layout, DOM and HTTP tests pass.
- **9–13:** Event redispatch/legacy initialization, abortable listener cleanup and
  repeated quota failure/recovery pass. Observer records drain exactly once,
  with edge-inclusive and zero-area arithmetic tested at native checkpoints.
  Media polling accepts 1000 unused queries and bounds 128 active subscriptions;
  desktop features and zoom resolution pass. Location reads track native fragment
  changes/default ports and encoded file paths. Images have independent prototype
  identity and CSS-pixel dimensions. All 24 non-Wayland Meson suites pass.
- **14:** Fixed-width octal C escapes preserve exact UTF-8 bytes and cannot form
  trigraphs or consume adjacent digits. A real C11 compiler with trigraph handling
  enabled round-trips all nine trigraph spellings, Unicode and control bytes;
  empty source is valid and NUL/invalid UTF-8 inputs are refused.

- **15:** Optimized Debian amd64 and relocatable Linux archives include all three
  executables, private Cairo, decoder notices and build metadata. Four Debian
  extraction tests and the Linux archive workflow pass, including worker sandbox
  checks, JavaScript, PNG export, paths with spaces and SHA-256 payload manifests.
  Repeated conversion produces identical tar archives. Source snapshots reject
  mutations during building/packaging; clean builds record an immutable Git
  revision. A real temporary Git repository verifies clean/dirty source metadata,
  normalized flags and exact source hashes.
- **17, final validation:** Initial configures let clients choose a preferred
  size; first-map geometry is bounded and saved in normalized coordinates.
  Oversized preferences, HiDPI bounds, restoration and eight-edge resizing pass
  native tests. All 13 core suites and three compositor sanitizer suites pass.
  Trainer, file-tool and launcher checks render on the new compositor; the browser
  Wayland workflow passes with 300 commits/299 buffer releases in the recorded
  run. Qt bytecode writes and an empty-snapshot capture race found during the
  wider gate are fixed and covered by the packaging and GUI checks.
- **16:** Code, regression, sanitizer, corpus and native-presentation gates are
  complete. Final clean-source archives, installed-runtime verification and
  publication remain pending; this checkbox is updated only after publication.
