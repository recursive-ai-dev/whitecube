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
