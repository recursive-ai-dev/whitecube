# Cube Browser 0.9 development sprint

The 0.9 release extends the native Wayland static browser into an interactive-web
preview. The original `spec.md` remains the longer-term design. Every phase keeps
the X-free dependency boundary, private test profiles, bounded inputs and worker
separation. Release status follows evidence, rather than version-number changes.

## Phases and release gates

1. **Foundation — complete.** Preserve the validated 0.4 source in Git and an
   archive, consolidate version identifiers, split engine responsibilities and
   record a ten-document rendering/performance corpus. Gate: all 13 original
   suites pass after refactoring; reproducible corpus output exists.
2. **HTML and CSS — complete.** Improve omitted-tag recovery, selectors, flex
   sizing/wrapping, relative/absolute positioning, clipped overflow, custom
   properties and an equal-track grid subset. Gate: desktop/narrow geometry,
   cascade, malformed-input and interaction regression tests pass.
3. **Isolation and policy — complete.** Restrict renderer filesystem access and
   system calls; centralize origins and resource policy, enforce the supported
   CSP directives in the trusted broker, and exercise crash recovery. Gate:
   adversarial workers cannot obtain forbidden files, network or actions.
4. **Forms and sessions — complete.** POST/password controls, bounded host/domain
   cookies with public-suffix and SameSite rules, cache validation, origin storage,
   private mode and clearing. Gate: local login/logout and redirect flows work,
   cross-site credentials are withheld, private state is not persisted.
5. **JavaScript runtime — complete.** Pinned QuickJS behind `cube-js`, memory/stack/
   execution/job budgets and diagnostic output. Gate: language cases and runaway
   execution tests pass without blocking UI or exposing host facilities.
6. **DOM and async APIs — complete.** Element/Text identity, selectors, attributes,
   text/child mutation, input values, events with default cancellation, timers,
   promises, DOMContentLoaded, classic inline/external/defer scripts and brokered
   fetch. Origin storage is exposed through localStorage/sessionStorage. Gate:
   interactive fixtures update pixels, submit forms, fetch JSON and cancel work
   on navigation. Unsupported modules, iframes and APIs fail explicitly.
7. **Everyday workflows — complete.** Native browser settings and per-site script
   controls; downloads with progress/cancellation; console/network diagnostics;
   keyboard, scaling and recovery polish. Gate: native UI workflows and failure
   paths pass, including private mode.
8. **Release — complete.** Regression fixes, sanitizer/fuzz runs, compatibility
   results, performance comparison, installation/rollback and release docs.
   Gate: complete suite, corpus and release checks pass before stamping 0.9.0.

## Compatibility scope

The sprint implements bounded, documented subsets. The DOM checklist above is
the phase 6 contract. HTTP/2/3, audio/video, WebGL, IndexedDB, browser extensions,
full layout/SVG conformance and a handwritten replacement JavaScript VM remain
future work. Remote site smoke checks supplement deterministic fixtures; they
are not proof of general web compatibility.

## Validation record

- Baseline: 0.4.0, 13 of 13 Meson suites pass on 2026-10-09, no skips.
- Source checkpoint: initial Git commit; `build/before-0.9-sprint.tar.gz`.
- Phase evidence and any unresolved release blockers are recorded here as work
  lands. `VALIDATION.md` will describe the final tested release.

- Phase 1: 14/14 suites pass (13 original plus version synchronization). Ten
  documents render at 1120px and 640px; baseline median 441.3ms including process
  startup. Report and PNGs: `build/sprint-0.9/baseline/`. Engine modules now separate
  tree/resources, style, layout/paint and interaction. `VERSION` owns release IDs.

- Phase 2: 15/15 suites pass. New semantic geometry/pixel cases cover variables,
  selectors, row/column flex, wrapping, shrink limits, grids, relative/absolute
  positioning, clipped hit targets and omitted-tag recovery. The ten-document
  corpus was rerendered to `build/sprint-0.9/phase2/`. Supported grammar and
  deliberate bounds are in `COMPATIBILITY.md`.

- Phase 3: all 17 suites pass, including Landlock/seccomp adversarial access tests,
  origin/CSP unit cases, HTTP CSP/redirect pixel checks and native renderer-crash
  recovery. Workers drop capabilities, scrub inherited environment and enforce
  filesystem/syscall allowlists. Kernel requirement and CSP bounds are documented.

- Phase 4: 18/18 suites pass. Native keyboard tests exercise POST login, a cookie
  through redirects/reload, logout and a 304 cache hit. Worker tests verify masked
  password pixels and sealed POST bodies. Session tests cover PSL exceptions,
  cross-site withholding, quotas, persistent/local versus private/session state,
  expiry, clearing and cache validation. HTTP redirects now return to the UI for
  a fresh policy/credential decision at each hop.

- Phase 5: 19/19 suites pass; the runtime suite also passes after bounded source
  copying and callback validation. QuickJS core is pinned and statically embedded
  behind `cube-js`, without its OS library. Language, promises, errors, dynamic
  code restrictions, heap exhaustion and runaway execution run under the actual
  renderer sandbox. Page integration is phase 6.

- Phase 6: 20/20 suites pass. New web fixtures cover DOM identity/mutation, event
  propagation/cancellation, script/defer ordering, promises/timers, JSON fetch,
  CORS/CSP refusal, failed scripts and runaway recovery. Native tests cover
  keyboard click handlers, cancelled POST, local/session storage across reload,
  origin separation and fetch cancellation on navigation. CSP opaque-origin
  cases conservatively disable unsupported active capabilities.

- Phase 7: 21/21 suites pass. Native tests exercise settings, persistent and private
  script permissions, console/network panels, a 4 MiB download, cancellation,
  private data clearing and live display scaling. Downloads use private files,
  refuse symlink destinations and never overwrite existing files. Worker startup
  now uses a neutral working directory and permits the read-only directory query
  required by Fontconfig, removing its misleading allocation diagnostics.

- Phase 8: version 0.9.0 passes all 23 regression suites and 13 ASan/UBSan component
  suites, including instrumented QuickJS and 1,000 deterministic mutation cases.
  The 10-document/20-render corpus passes at both widths; median export time is
  158.13ms versus 125.64ms for a fresh 0.4 rebuild (25.9% slower). Native artifacts
  cover settings, diagnostics and download progress/completion. Example Domain
  and Debian exports complete; Debian retains seven unavailable resources and
  partial layout. Installer rollback is tested in scratch profiles. The existing
  launcher reports 0.9.0. Final bounds and evidence are recorded in
  `COMPATIBILITY.md`, `VALIDATION.md` and `RELEASE.md`.
