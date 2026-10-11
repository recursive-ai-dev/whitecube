# Cube Browser

Cube Browser **0.9.0** is a native Wayland interactive-web preview for Whitecube.
It has its own HTML/CSS engine, Pango/Cairo software rendering, mbedTLS and a
bounded QuickJS interpreter behind the replaceable `cube-js` interface. It uses
no Chromium, Firefox, WebKit, Electron, GTK, X11, XWayland or D-Bus.

This release completes the [eight-phase sprint](SPRINT.md). It supports a documented
subset of the web; complex applications can still fail. The full design in
[spec.md](spec.md), including the complete M0 milestone, remains a longer-term target.
See [compatibility](COMPATIBILITY.md), [validation](VALIDATION.md) and
[release/rollback notes](RELEASE.md).

## Build and run

```sh
# Verified private dependencies; no system package installation.
scripts/bootstrap-cairo.sh        # X-free Cairo and SQLite
scripts/bootstrap-mbedtls.sh      # mbedTLS 3.6.7 LTS
scripts/bootstrap-jpeg.sh         # pinned Debian x86-64 libjpeg-turbo package
scripts/bootstrap-quickjs.sh      # QuickJS 2026-06-04 core, without its OS library
./BUILD.sh                       # configure, compile and run the full test suite
./cube-browser.sh                # inside a native Wayland session
./cube-browser.sh https://example.com
./cube-browser.sh /absolute/path/to/page.html
./cube-browser.sh --private
```

Host tools: C11 compiler, make, Meson, Ninja, pkg-config, Python 3, curl, tar,
dpkg-deb, wayland-scanner, and development files for wayland-client/cursor,
wayland-protocols, xkbcommon, Pango, GLib, pixman, FreeType, Fontconfig, libpng,
Expat and zlib. Workers require Linux Landlock ABI 3 or later and seccomp filters;
they fail closed if restrictions cannot be installed. Native integration tests
also require the adjacent Whitecube build described in `scripts/e2e.py`.

Bootstraps write only to `.deps/`. Private Cairo is selected by the executables'
runtime search path. The build checks the complete dependency closure for missing
libraries and forbidden X/SDL dependencies. The JPEG bootstrap targets Debian
Linux x86-64; other architectures need matching local development files.

```sh
scripts/install-user.py           # installation plan
scripts/install-user.py --apply   # launcher, menu entry, Whitecube Alt+W
scripts/install-user.py --restore /absolute/path/to/manifest.json
scripts/install-user.py --restore /absolute/path/to/manifest.json --apply
```

Installation backs up replaced files under
`$XDG_STATE_HOME/cube-browser/install-backups/` (default `~/.local/state/`).
Rollback refuses files edited since installation. Neither command restarts the
desktop, changes MIME defaults, or replaces `whitecube-web`. Keep this project
in place: its launcher and private library paths point here. `meson install` is
also intended for this machine, rather than as a portable binary distribution.

## What works

- Native windows, address/search bar, clipboard, desktop tabs, back/forward,
  reload/stop, keyboard navigation, scrolling, zoom, find, anchors and local history.
- Bounded HTML recovery, selectors, cascade, CSS variables, responsive media,
  block/inline/flex, equal-column grids, tables, positioned blocks and clipping.
- External CSS and PNG/JPEG/static SVG images, loaded through isolated workers.
- GET/POST forms, editable masked passwords, cookie sessions, origin storage,
  conditional HTTP cache, private windows and data clearing.
- Classic/deferred scripts, DOM text/tree/attribute changes, events and cancelled
  defaults, promises, timers, text/JSON fetch and localStorage/sessionStorage.
- Native settings, persistent per-site script choices, console/network diagnostics
  and downloads with progress and cancellation.
- Whitecube scaling, strong contrast and reduced-motion preferences; headless PNG
  and text export using the same browser workers.

HTTPS uses certificate-verified TLS 1.3; HTTP uses HTTP/1.1, IPv4 and bounded
redirects. Credential-bearing URLs and HTTPS downgrades are refused. The broker
checks origins, cookie credentials and the supported CSP directives at each hop.
Fetch implements a small CORS subset and refuses redirects that change origin.

## Controls

| Keys | Action |
| --- | --- |
| Ctrl+L | Select address; enter a URL or search |
| Ctrl+T / Ctrl+W | New desktop tab / close window |
| Alt+Left / Alt+Right | Back / forward |
| Ctrl+R or F5 | Reload |
| Ctrl+H | Local history |
| Ctrl+F, then Enter; F3 | Find and advance |
| Ctrl+plus / Ctrl+minus / Ctrl+0 | Zoom in / out / reset |
| Ctrl+comma | Settings; arrows and Enter choose an action |
| F12 or Ctrl+Shift+J | Console/network diagnostics; Tab switches list |
| Ctrl+S / Ctrl+J | Download current HTTP(S) URL / show download status |
| Tab / Shift+Tab | Next / previous link or control |
| Enter | Activate link/button or submit a text field |
| Ctrl+A / Ctrl+C / Ctrl+X / Ctrl+V | Select all / copy / cut / paste in a field |
| Up/Down, PageUp/PageDown, Space, Home/End | Scroll outside editable controls |
| Escape | Cancel active download, close panel or leave address/find mode |

Password fields refuse copy/cut and require POST submission. POST bodies never
appear in the address or history. Reload/back use GET without replaying a POST.
Fields survive relayout; ordinary navigation creates a fresh document.

Downloads save completed files with mode 0600 to an absolute `XDG_DOWNLOAD_DIR`
environment setting, or `~/Downloads`. They never overwrite, execute or open a
file automatically. One transfer can run at a time, up to 64 MiB. Data clearing
retains downloaded files.

## Configuration and profiles

`$XDG_CONFIG_HOME/cube-browser/config` (default `~/.config/cube-browser/config`):

```ini
homepage=cube:home
search_engine=https://duckduckgo.com/html/?q=%s
zoom=1.0
javascript=true
```

The native settings panel edits these choices. `%s` in the search URL is replaced
with percent-encoded input and is never evaluated as a shell command. Per-site
script choices override the default. Settings and SQLite files are owner-only.

History has 90-day retention in `$XDG_DATA_HOME/cube-browser/history.sqlite3`.
Cookies, local storage and site script choices use `sites.sqlite3` in that folder.
Session cookies, sessionStorage and the HTTP cache belong to the current window.
Private windows use independent in-memory site state and do not write history or
settings. They do not provide network anonymity. Clear browsing data resets site
state, site choices and the window's back/forward trail; normal clearing also
clears persistent history.

Accessibility is reloaded from `$XDG_STATE_HOME/whitecube/accessibility.json`.
Interface scaling supports 0.5–3.0 subject to window width. Fractional Wayland
output-scale protocols and input-method composition remain unsupported.

## Exports and limits

```sh
build/cube-browser --render cube:home build/home.png 1120x800
build/cube-browser --render https://example.com build/example.png
build/cube-browser --console --dump https://example.com
```

Exports use a private profile, wait for initial scripts/resources and allow up
to one second of async settling. `--console` prints bounded page output to stderr.
Failures return nonzero; a PNG may still contain an error page. Export dimensions
range from 640×400 through 4096×4096.

Documents and individual page resources are limited to 2 MiB. Pages have at most
32 resource URLs, three concurrent resource workers, a 20-second resource budget,
and 16 MiB of encoded resources. JS uses a 32 MiB heap, 100 ms turns, bounded jobs
and a two-second cumulative execution budget. Local files cannot fetch external
resources or use origin storage. Remote pages cannot navigate to local files or
browser pages. Symlink local documents are refused.

Full HTML/CSS/SVG conformance, modules, dynamic scripts, innerHTML, iframes, Shadow
DOM, file uploads, streaming fetch, service workers, IndexedDB, audio/video, WebGL,
extensions, HTTP/2/3 and IPv6 remain outside this release. Detailed grammar,
quotas and failure behavior are in [COMPATIBILITY.md](COMPATIBILITY.md).

## Process boundaries and verification

The UI owns Wayland, profiles, permissions and network authorization. A fresh
network worker handles each request; a renderer owns each document/runtime.
A short-lived raster child paints sealed frame buffers. Private SOCK_SEQPACKET
IPC carries bounded CBOR messages and sealed memory descriptors. The UI validates
frame metadata and requires pending user input for navigation, submission and
clipboard replies.

Workers drop capabilities, scrub inherited environment/descriptors, and apply
Landlock filesystem and seccomp syscall allowlists. Renderer reads are limited
to system fonts/configuration and library directories; network reads are limited
to hosts/resolvers and the compiled CA bundle. Workers cannot write ordinary files
or execute programs; renderers cannot open sockets or executable mappings.
The spec's namespaces/site-process reuse remain future work. This preview has
not undergone an independent security audit.

```sh
./BUILD.sh
scripts/check-sanitizers.sh
python3 scripts/corpus.py --output build/sprint-0.9/release
python3 scripts/e2e.py --artifacts build/e2e-0.9
```

Tests use local deterministic servers, temporary HOME/XDG profiles and an isolated
headless compositor. They do not connect to or restart the live desktop. See
[VALIDATION.md](VALIDATION.md) for measured results and sanitizer coverage.
