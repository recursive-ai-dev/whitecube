# Cube Browser validation

## 0.11.0 / Whitecube 0.10.0 release gate

The Debian 13 amd64 release build passes all 24 non-Wayland suites, including
real resource timeout/recovery, cancellation, event/quota, observer records,
image-interface, computed-style, WebP and C-compiler round-trip checks. The final
inline-image percentage-width path also passes the native DOM regression.

All 13 browser component suites pass ASan/UBSan with the QuickJS core rebuilt
with instrumentation in the Debian builder. The three affected compositor,
workspace and hotplug suites also pass ASan/UBSan. Leak detection is disabled
explicitly; these results make no leak-check claim.

Whitecube passes all 13 native core suites. Native GUI checks cover trainer
pages and benchmark capture, editor pages, Qt workspace management, Cube Strap,
application IDs/restored placements and the four file tools. The latest Qt run
passes 232 tests with one explicit optional QtTest skip; all 61 packaging tests
and 52 command-suite tests pass. Release validation fixed cache creation in the
Qt entry points and made screenshot capture wait for a drawable frame.

The corpus gate renders 10 deterministic documents at two widths: 20 PNGs with
text assertions and profile nonpersistence checks. Median render time in this
run is 514.63 ms, a local smoke measurement rather than a portable benchmark.
The native Wayland workflow passes against the rebuilt floating compositor:
300 submitted buffers and 299 releases in the recorded run. It exercises actual
End-key lazy loading, rendered pixels, navigation, zoom, forms, cookies/cache,
bookmarks, settings persistence, downloads, anchors and renderer crash recovery.

The optimized standalone Debian package passes four extracted-archive checks,
including relocation into a path with spaces, private-library resolution,
license/metadata checks, sandboxed worker discovery and headless text/PNG output.
The Linux archive passes SHA-256 payload verification, relocated JavaScript and
image export checks. Two conversions of the same Debian package are byte-for-byte
identical. Final archives are built from the recorded release source commit;
clean-build provenance is checked against immutable Git objects. Source changes
during building or packaging reject the candidate archive.

Evidence is retained in `build/floating-browser-release.log`,
`build/floating-final-browser-sanitizers.log`,
`build/floating-latest-desktop-checks.log` and `build/release-0.10.0/`.
The published verification report records the final desktop archive, installation
lifecycle and installed-runtime checks. Reproduce that gate with:

```sh
python3 -I tools/verify-release.py --deb packaging/out/whitecube-desktop_0.10.0_amd64.deb \
  --previous-deb packaging/out/whitecube-desktop_0.9.6_amd64.deb \
  --output build/whitecube-0.10.0-verification
```

These are isolated fixtures, not a test of the unidentified public gallery.
Physical displays, hardware acceleration, complete web-platform compatibility
and modules remain outside these results. See COMPATIBILITY.md for limits.

## Earlier development checks

## 2026-10-31 runtime and lazy-image checks

The local build passes 22 non-Wayland Meson suites, including 22 HTTP web-API
cases. New coverage checks Navigator/location metadata, readable inert JSON and
template blocks, legacy JavaScript MIME aliases, separate module/quota messages,
source stacks, scroll/zoom geometry, media-query changes, observer limits,
animation callbacks, dynamic image downloads and cached-image load events.
Image requests retain CSP, settings, local-file restrictions and the shared
32-resource quota. Adversarial renderer tests reject late scripts/stylesheets,
out-of-order resource indices and invalid request flags.

The native Wayland workflow passes with the existing extracted Weston 14.0.2.
Its lazy-gallery fixture verifies that an offscreen image is not fetched until a
real End-key scroll, then checks the request, load event and painted pixels.
Screenshot: `build/lazy-image-e2e/wayland-lazy-image.png`. The accessibility-scale
check now waits for the actual title-bar boundary rather than an arbitrary
pending repaint. The adjacent Whitecube compositor cannot start on this host
because its `libdisplay-info.so.2` dependency is unavailable, so the supported
Weston path supplies native presentation coverage.

All 13 component suites also pass under ASan/UBSan with the QuickJS core rebuilt
for instrumentation. This run uses `CUBE_SANITIZER_LEAKS=0` and makes no leak-check
claim. These are deterministic local fixtures; the reported public gallery was
not identified by URL or tested directly. Modules, complete browser APIs and
general heavy-site compatibility remain outside these results.

```sh
meson compile -C build
meson test -C build --no-suite integration --print-errorlogs
python3 scripts/e2e.py --browser build/cube-browser --weston-dir .deps/weston --artifacts build/lazy-image-e2e
CUBE_SANITIZER_LEAKS=0 scripts/check-sanitizers.sh
```

## 0.10.1 image and configuration checks

The prepared Debian 13 builder runs all 23 browser suites, including real
Wayland presentation against Whitecube. Added coverage checks direct remote and
local PNG/JPEG/SVG, inline data images, CSP allow/block behavior, malformed image
refusal, settings editing and validation, image request blocking and repaint,
custom download folders, restart persistence, reload/reset, disabled history,
clear-on-exit preserving bookmarks and private settings nonpersistence. Component
image-engine and policy checks also passed under ASan/UBSan.

This records deterministic local checks, not full web compatibility. No 0.10.1
standalone archive or real-hardware browser compatibility results are claimed.

## Previous release: 0.10.0

Validated locally on Ubuntu 26.04.1 amd64, Linux 7.0.0-38, 2026-10-27.
The UI, renderer and network worker are built against system desktop libraries,
with the pinned private X-free Cairo and static SQLite, JPEG, QuickJS and Mbed TLS.

## Current release checks

Timed JavaScript/DOM and native UI suites are scheduled without competing Meson
tests. The package builder also runs its release gate sequentially. Production
JavaScript execution limits remain unchanged.

- `./BUILD.sh`: 22 core/worker suites passed, zero failures. The default native
  integration entry skips when the adjacent Whitecube compositor is absent;
  the complete native test passed separately against extracted Weston 14 below.
- `python3 scripts/corpus.py --output build/release-0.10/corpus`: all 10 fixture
  documents rendered at 1120 and 640 pixels; 20 PNGs, median 492.01 ms on this
  host. Text assertions and private profile nonpersistence passed. This is a
  smoke measurement rather than a cross-machine performance benchmark.
- `python3 scripts/e2e.py --weston-dir .deps/weston --artifacts
  build/release-0.10/wayland`: real native Wayland presentation passed, with 253
  shared-memory commits and 252 compositor releases in the recorded run. The
  local proxy supplies isolated keyboard events; Weston performs actual window
  configure, presentation and buffer release. Counts vary with scheduling.
- The native test covers navigation, history traversal, zoom/find, GET/POST forms,
  cookies/cache, script events and storage, cancellation, settings/diagnostics,
  downloads, anchors, responsive SVG/tables, preference changes, renderer crash
  recovery and keyboard close. New workflows verify bookmarks persist across
  windows, Ctrl+Shift+B opens the library, private reads succeed and private
  add/remove attempts leave the database's bytes and mtime unchanged.
- `python3 tests/test_package.py dist/cube-browser_0.10.0_amd64.deb`: all three
  archive tests passed, including extraction into a directory containing spaces,
  relocation, desktop validation, payload checksums, private Cairo resolution,
  no checkout runtime paths, worker diagnostics, local HTML/text and PNG exports.
- `apt-get --simulate install ./dist/cube-browser_0.10.0_amd64.deb`: accepted on
  this host, with Cube Browser as the only new package. No system installation
  was performed. The generated dependency set includes `libc6 (>= 2.43)`; this
  artifact does not target older Debian/Ubuntu library stacks.

Regression coverage adds UTF-8/URL validation, escaped bounded bookmark lists,
owner-only profiles, symlink refusal, bookmark capacity/upserts, private mutation
refusal, read-only missing config behavior, corrected defaults, CLI diagnostics,
relative paths and ambiguous argument refusal. Private history exports never read
or change an existing saved-history database. DOM regressions cover inert markup
insertion, detached node lifetimes, selector-list ordering and quoted commas,
text/attribute escaping, depth/node/control bounds, atomic quota failures,
serialization limits, and complete DOM textContent including hidden descendants.

All browser/network fixtures use loopback servers and temporary HOME/XDG profiles.
The tests do not connect to the live desktop, modify the user's profile, change
the default browser or require a system package installation. Screenshots and the
corpus report are in `build/release-0.10/`. The link closure check confirms no
X11/XCB/SDL dependency in the application and its workers.

## Current sanitizer results

`CUBE_SANITIZER_LEAKS=0 scripts/check-sanitizers.sh` passed all 13 component
suites with AddressSanitizer and UndefinedBehaviorSanitizer, including the
instrumented QuickJS core. Covered: storage, IPC/address, web policy, forms,
media, sessions, downloads, images, JS runtime, engine, layout, DOM lifecycle and
mutation smoke. Log: `build/release-0.10/sanitizers.log`.

Leak scanning is **not validated on this host**: the default leak-enabled run
aborted because LeakSanitizer could not attach to its own process (errno 1).
The address/undefined-behavior run explicitly disabled leak scanning. The script
retains leak scanning by default on hosts that permit it; no system tracing or
security policy was changed. Font-related suites already disable leak scanning
because of retained Pango/Fontconfig caches. UI/Wayland and production network
workers are exercised by the normal suites; sanitizer components omit worker
address-space limits to accommodate ASan shadow memory.

The older measurements below are historical 0.9.0 evidence and should not be
read as fresh 0.10.0 results.

---

# Cube Browser 0.9.0 validation

Validated on Debian 13, Linux 6.12.107, 2026-10-09. The local launcher and built
browser both report `cube-browser 0.9.0 (interactive-web preview)`.

## Deterministic release gate

`./BUILD.sh` passes **23 of 23 Meson suites**, with no failures, skips or timeouts.
The release build has no compiler warnings. Logs: `build/release-0.9.log` and
`build/meson-logs/testlog.txt`.

| Area | Evidence |
| --- | --- |
| HTML/CSS/layout | Omitted tags, selectors/cascade, variables/cycles, flex sizing/wrapping, equal-track grids, positioning/clipping, responsive preferences, table geometry and interactions |
| Images | PNG/JPEG/static SVG parsing, malformed and oversized input, finite geometry, arc extremes, XML/external-reference refusal and painting budgets |
| Profiles | Atomic owner-only settings, history expiry/escaping/clear, symlink refusal, origin separation, private/session persistence rules, encoded quotas and site script choices |
| Isolation/policy | Real Landlock/seccomp workers refuse unauthorized files, writes, execution and renderer sockets; origin/CSP intersections, redirects, mixed content and opaque-origin storage/cookie restrictions |
| HTTP/forms | Framing/chunks/limits, TLS certificate/protocol failures, redirects, GET/POST encoding, password masking, cookies/login/logout, conditional cache and updated CORS authorization after 304 |
| JavaScript/DOM | Language cases, promises, memory/stack/time/job bounds, dynamic-code policy, DOM identity/lifetime, depth/cycles, mutations, events, cancelled defaults, script order and timers |
| Async APIs | Real HTTP fetch/JSON, Origin headers, CORS/CSP refusal, redirect restrictions, embedded NUL/malformed UTF-8 handling, response limits, navigation cancellation and storage reload/origin isolation |
| UI/IPC | Sealed frames/bodies, bounded CBOR, malformed messages, faulty-renderer rejection, pending-user-action authorization, native keyboard/focus, settings/diagnostics/downloads, private clearing and renderer recovery |
| Delivery | Version synchronization, complete X-free link closure, CLI exports, scratch-profile installation and rollback, refusal to overwrite later user edits |
| Mutation smoke | 1,000 deterministic HTML/CSS/layout/SVG/URL/policy/HTTP-request/CBOR mutation cases, seed `09c0be42` |

## Sanitizers

`scripts/check-sanitizers.sh` passes **13 component suites** with AddressSanitizer
and UndefinedBehaviorSanitizer, including the recompiled QuickJS core. Covered
components: engine, layout, image/SVG, storage, IPC/address, policy/HTTP records,
forms, media, sessions/broker, downloads, JavaScript runtime, DOM lifecycle and
mutation smoke. `build/release-sanitizers.log` records the run; the subsequent
304/CORS regression also passed in `build/release-sanitizer-session.log`.

Leak checks remain enabled for nine suites. Engine, layout, DOM and mutation runs
disable leak detection because Pango/Fontconfig retain global caches; address and
undefined-behavior checks stay enabled. Other private/system dependencies are not
rebuilt with sanitizers. UI event handling, the live HTTP/TLS worker and native
Wayland processes are covered by normal integration tests, not ASan execution.

Component tests omit worker resource limits because ASan needs a large shadow
address space. Only the sanitizer build of the JS *test harness* omits the syscall
filter; production workers are unchanged, and the normal runtime and adversarial
worker suites exercise the actual restrictions. These are bounded smoke runs,
not a security audit, coverage-guided fuzzing campaign, WPT or Test262 conformance.

## Native Wayland evidence

```sh
python3 scripts/e2e.py --artifacts build/e2e-0.9
```

The artifact run passed with **258 shared-memory commits and 257 compositor
releases**. Counts vary with scheduling. It uses a temporary HOME/XDG profile,
an isolated headless Whitecube compositor and pixman. The test relays only the
browser's private Wayland connection and injects standard `wl_keyboard` events;
the real compositor handles configure/ack, presentation and buffer release.
It neither connects to nor restarts the live desktop.

The run covers HTTP navigation, reload, back/forward, find/zoom, GET and POST
forms, cookies/logout/cache, scripted events and cancelled submission, storage,
fetch cancellation, script settings, console/network panels, a verified 4 MiB
saved download, cancellation without a file, private clearing, live scaling,
responsive CSS/SVG/tables, anchors, renderer crash recovery and close.

Fourteen actual submitted frames are retained in `build/e2e-0.9/`, including
settings, console, network, download progress/completion, home, forms and responsive
views. The release review inspected the settings, network, completed-download,
home and narrow-grid frames. This validates these fixtures, not arbitrary sites.

## Corpus and performance

```sh
python3 scripts/corpus.py --output build/sprint-0.9/release
```

All **10 documents / 20 renders** pass visible-text, PNG and private-profile checks
at widths 1120 and 640, height 800. Cases cover articles, documentation, flex, grid,
forms, login, responsive CSS, malformed HTML, scripts and tables. Hashes, PNG sizes
and individual timings are in `build/sprint-0.9/release/report.json`.

| Build | Median render/export time | Observed range |
| --- | ---: | ---: |
| Preserved 0.4 foundation, freshly rebuilt (`4e571c1`) | 125.64 ms | 107.16–182.69 ms |
| 0.9.0 release | 158.13 ms | 135.63–194.33 ms |

Each timing includes process startup, loopback HTTP, worker creation and PNG
export. This single corpus sweep measures a **32.49 ms / 25.9% slowdown** relative
to the freshly rebuilt baseline. It is not a statistically controlled engine
benchmark or a claim of improved performance. The original phase-1 run measured
441.3 ms under different host load and is retained for history; the fresh same-host
comparison is the relevant one. The baseline source/build is isolated under
`build/sprint-0.9/baseline-src/`; its report is in `baseline-repeat/`.

## Live-network observations

`python3 scripts/smoke-sites.py --output build/smoke-0.9` fetched and exported
`https://example.com` and `https://www.debian.org` through the real certificate-
verified TLS worker. All four dump/render commands exited 0 without stderr
messages; total command times ranged from 2.149 to 6.996 seconds.

The Debian screenshot shows its logo, editable search control and partial layout,
with **seven unavailable images/styles** in the status bar. It remains a partial
compatibility result. Exit status 0 means the document/export completed, not that
every resource or page feature worked. These mutable public sites supplement the
local deterministic tests. Reports, text and PNGs are in `build/smoke-0.9/`.

## Installation and remaining limits

The existing user launcher resolves to this project's build and now opens 0.9.0;
no live desktop configuration rewrite or restart was required. Installation and
rollback were exercised only in temporary profiles. Source checkpoints, local
release artifacts and rollback instructions are described in [RELEASE.md](RELEASE.md).

The implemented browser is an interactive preview. Complete CSS/DOM/SVG, modules,
iframes, file uploads, service workers, IndexedDB, media, WebGL, HTTP/2/3, IPv6,
IME, advanced output scaling and the specification's namespace sandbox remain
outside this release. [COMPATIBILITY.md](COMPATIBILITY.md) documents the exact
subset and quotas; no independent security audit or general compatibility claim
is implied by these tests.
