# Cube Browser

Cube Browser **0.10.1** is a native Wayland interactive-web preview for Whitecube.
It has its own HTML/CSS engine, Pango/Cairo software rendering, mbedTLS and a
bounded QuickJS interpreter behind the replaceable `cube-js` interface. It uses
no Chromium, Firefox, WebKit, Electron, GTK, X11, XWayland or D-Bus.

This release builds on the [eight-phase sprint](SPRINT.md). It supports a documented
subset of the web; complex applications can still fail. The full design in
[spec.md](spec.md), including the complete M0 milestone, remains a longer-term target.
See [compatibility](COMPATIBILITY.md), [validation](VALIDATION.md) and
[release/rollback notes](RELEASE.md).

## Install a standalone Debian package

The built archive is `dist/cube-browser_0.10.1_amd64.deb`:

```sh
sudo apt install ./dist/cube-browser_0.10.1_amd64.deb
cube-browser --check
cube-browser
```

Launch **Cube Browser** from your desktop menu; its **New Private Window** action
starts an isolated window. This build targets Ubuntu 26.04 amd64. Its actual
shared-library version requirements are recorded in the package. A native Wayland
session and Linux Landlock ABI 3 or newer are required for windows; headless text
and PNG exports work without a compositor. The package includes its workers and
private X-free Cairo and does not need this checkout or Whitecube installed.
It registers HTTP/HTTPS/HTML handlers without changing your default browser.

Build and verify another package after bootstrapping the dependencies below:

```sh
python3 packaging/build-deb.py
python3 tests/test_package.py dist/cube-browser_0.10.1_amd64.deb
```

See [packaging details](packaging/README.md) for dependencies, repeatable builds,
installation paths and removal. Building and testing do not install system files.

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
use the adjacent Whitecube build described in `scripts/e2e.py`, or an extracted
Weston installation via `--weston-dir PATH`. Packaging also needs `dpkg-dev`
and `desktop-file-utils`.

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
  reload/stop, keyboard navigation, scrolling, zoom, find, anchors, local history
  and persistent bookmarks.
- Bounded HTML recovery, selectors, cascade, CSS variables, responsive media,
  block/inline/flex, equal-column grids, tables, positioned blocks and clipping.
- External CSS and PNG/JPEG/static SVG images, loaded through isolated workers.
  Image URLs and local image files also open directly; bounded `data:` images
  render inline, subject to image settings and Content Security Policy.
- GET/POST forms, editable masked passwords, cookie sessions, origin storage,
  conditional HTTP cache, private windows and data clearing.
- Classic/deferred scripts, DOM text/tree/attribute changes, events and cancelled
  defaults, bounded HTML fragment insertion, promises, timers, text/JSON fetch
  and localStorage/sessionStorage.
- Categorized native settings with editable preferences, persistent per-site script
  choices, console/network diagnostics
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
| Ctrl+H | Local history (private windows show only the private history notice) |
| Ctrl+D / Ctrl+Shift+B | Add/remove current bookmark / open bookmarks |
| Ctrl+Shift+P | New private window |
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

Downloads save completed files with mode 0600 to the folder selected in settings,
an absolute `XDG_DOWNLOAD_DIR` environment setting, or `~/Downloads`. They never overwrite, execute or open a
file automatically. One transfer can run at a time, up to 64 MiB. Data clearing
retains downloaded files.

## Configuration and profiles

`$XDG_CONFIG_HOME/cube-browser/config` (default `~/.config/cube-browser/config`):

```ini
homepage=cube:home
search_engine=https://duckduckgo.com/html/?q=%s
zoom=1.0
javascript=true
images=true
cookies=true
site_storage=true
cache=true
save_history=true
clear_on_exit=false
https_only=false
download_directory=
```

Open Menu, press Ctrl+comma, enter `cube:settings`, or launch with `--settings`.
Left/Right selects General, Content, Privacy, Downloads or Advanced; Up/Down
selects a preference. Enter edits a text field or toggles a switch. Text fields
support Ctrl+A/C/X/V, cursor movement, Enter to validate/save and Escape to cancel.
Zoom accepts percentages from 50 to 300 in the panel and ratios from 0.5 to 3.0
in the config file. Invalid values leave the current settings in place.

`%s` in the search URL is replaced with percent-encoded input and is never
evaluated as a shell command. Home/search URLs refuse embedded credentials.
The download folder must be an absolute path; empty uses the default directory.
Advanced provides reload from disk and restore defaults. Settings and SQLite
files are owner-only. Missing keys retain their defaults, including existing
four-setting profiles.

Turning off images prevents external image requests and hides inline/direct
images. Cookies off blocks both sending and accepting cookies; third-party
cookies are always blocked. Site storage off hides saved origin data and refuses
new writes without deleting it. Cache off discards the in-memory HTTP cache and
bypasses future caching. History off stops new saved visits. Clear-on-exit clears
saved history, cookies, site storage and script choices when that normal window
closes; bookmarks and downloaded files remain. Clearing now returns home so the
current site cannot immediately recreate its data. HTTPS-only blocks HTTP pages,
resources, redirects, fetch and downloads, including an active HTTP download when
the mode is enabled. Per-site script choices override the default.

Private settings changes apply only to the current window and are never saved.
Clear-on-exit in a private window never clears the normal profile.

History has 90-day retention in `$XDG_DATA_HOME/cube-browser/history.sqlite3`.
Cookies, local storage and site script choices use `sites.sqlite3` in that folder.
Bookmarks use a separate `bookmarks.sqlite3`, with at most 1,000 saved URLs.
Ctrl+D or the address-bar star adds/removes a page; `cube:bookmarks` lists saved
pages. Private windows may read bookmarks and never change them. Clearing browsing
data preserves bookmarks.

Session cookies, sessionStorage and the HTTP cache belong to the current window.
Private windows use independent in-memory site state. They never read or write
saved history, and they never save settings. The private start page explains what remains
on disk. They do not provide network anonymity. Clear browsing data resets site
state, site choices and the window's back/forward trail; normal clearing also
clears persistent history.

Accessibility is reloaded from `$XDG_STATE_HOME/whitecube/accessibility.json`.
Interface scaling supports 0.5–3.0 subject to window width. Fractional Wayland
output-scale protocols and input-method composition remain unsupported.

## Exports and limits

```sh
build/cube-browser --check
build/cube-browser --render cube:home build/home.png 1120x800
build/cube-browser --render https://example.com build/example.png
build/cube-browser --console --dump https://example.com
```

Relative HTML/image paths and `file:///` URLs are supported on the command line. `--`
ends option parsing for desktop URL handlers. `--help`, `--version` and `--check`
do not create profile files.

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

Full HTML/CSS/SVG conformance, modules, dynamic scripts, iframes, Shadow
DOM, file uploads, streaming fetch, service workers, IndexedDB, audio/video, WebGL,
extensions, HTTP/2/3 and IPv6 remain outside this release. HTML fragments support
`innerHTML`, `outerHTML` reads and `insertAdjacentHTML`; scripts, stylesheets and
resource URLs introduced by fragments stay inert. Detailed grammar, quotas and failure behavior are in [COMPATIBILITY.md](COMPATIBILITY.md).

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
# If host policy prevents LeakSanitizer thread attachment:
# CUBE_SANITIZER_LEAKS=0 scripts/check-sanitizers.sh
python3 scripts/corpus.py --output build/release-0.10/corpus
python3 scripts/e2e.py --artifacts build/release-0.10/wayland
```

Tests use local deterministic servers, temporary HOME/XDG profiles and an isolated
headless compositor. They do not connect to or restart the live desktop. See
[VALIDATION.md](VALIDATION.md) for measured results and sanitizer coverage.
