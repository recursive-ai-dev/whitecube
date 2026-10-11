# cube-browser — ground-up spec (no Chromium / no Firefox)

Target: Wayland-native browser for whitecube (wlroots 0.18, no X/XWayland,
no dbus/logind, seatd, X-free cairo 1.18.4 in `/usr/local`).
Processes use files+signals+`cube-settings-helper` style fork/exec, never dbus.
Follows `~/whitecube/MAINTENANCE.md` rules: `ldd | grep -ci x11` must be 0,
`DISPLAY` never exported, `WAYLAND_DISPLAY` only.

Firefox 157.0~mozillabinaries (`/opt/firefox`, 335 MB) was removed
2026-10-08 via `chroot apt remove firefox` (dry-run showed only that package).
`gjs` stays. `whitecube-web` (python workspace server) is unaffected.

## 0. Architecture (4 processes, like RenderingNG simplified)

- `cube-browser` (UI): omnibox, tabs, history, downloads, settings. One process.
- `cube-renderd` per site (site isolation): parse+style+layout+paint, JS.
- `cube-netd`: DNS (reads `/run/whitecube-net/resolv.conf`), sockets, TLS, HTTP, cookies, cache.
- `cube-rasterd`: software raster (cairo/pango, `wl_shm`) first; GPU (Vulkan/wayland-egl) later.
- IPC: `SOCK_SEQPACKET` in `$XDG_RUNTIME_DIR/cube-browser/`, length-prefixed CBOR, no dbus.
  Capability model: renderer holds no net/storage fds, only shm + IPC.
- Threads per renderer: main (parse/style/layout/JS/DOM), compositor (scroll/animate/layerize),
  workers (raster/decode), media threads. Main produces immutable artifacts;
  compositor consumes them (same split as RenderingNG 12 stages).

## 1. Wayland shell

- `xdg-shell stable` for all windows/tabs/popups (`xdg_wm_base` v6, `get_toplevel/get_popup`,
  initial commit without buffer, ack configure). No `wl_shell`, no `xdg_shell_unstable_v6`.
- `CSD` always (whitecube draws no SSD). `xdg-decoration` negotiated, fallback CSD.
- `layer-shell` only for browser-owned panels if needed (not for web content).
- Protocols from `wayland-protocols`: `xdg-shell`, `viewporter`, `fractional-scale`,
  `xdg-output`, `text-input-v3`, `relative-pointer`, `pointer-constraints`,
  `primary-selection`, `xdg-activation`, `idle-inhibit`, `cursor-shape`.
- Build check: `pkg-config wayland-client>=1.18 xkbcommon>=0.5 wayland-protocols`,
  `wayland-scanner`, `stable/xdg-shell/xdg-shell.xml`. Link-closure test:
  no `SDL/X11/xcb/GLX` in `NEEDED` (same as whitecube rule).
- Input: `wl_seat` keyboard/pointer, `xkbcommon`, click-to-focus per whitecube.
  No global hotkeys (same limit as desktop-chat).

## 2. Network stack (`cube-netd`)

- DNS: read `/run/whitecube-net/resolv.conf` (written by `cube-net-dns`), no
  `systemd-resolved` dependency. Respect UFW allows (53 out must stay).
- Sockets: `AF_INET` (IPv6 off per `ipv6.disable=1` initially, add v6 later),
  `SO_BINDTODEVICE` optional per-profile, Happy Eyeballs when v6 returns.
- TLS 1.3 only (1.2 fallback flag): use `Rustls` (no X deps) or `mbedTLS`;
  system CA via `/usr/share/ca-certificates`, HSTS preload, OCSP stapling,
  ALPN `h2,h3,http/1.1`. No custom root store UI in M0.
- HTTP: `1.1` (keep-alive, chunked) M0, `h2` M1, `h3/QUIC` M2. Preload scanner:
  fetch CSS/JS/fonts while HTML parses. Cache: memory + `$XDG_CACHE_HOME`
  HTTP cache with `ETag/Last-Modified`, range requests for media.
- Cookies: `RFC6265bis` jar per-site, `SameSite=Lax` default, third-party block
  flag. `Referrer-Policy: no-referrer` default (matches `cube-net` probe note).
- Security: HSTS, `Trusted Types` subset, CORB/ORB, Opaque Response Blocking
  for no-cors. Proxy: env + per-profile PAC-lite (no WPAD in M0).

## 3. HTML (`cube-html`)

- WHATWG tokenizer + tree construction, incremental (14 KB first-packet render),
  scripting/paused parsing (`async/defer` respected), foster parenting, ADR.
- DOM: `Element/Text/Comment/DocumentType`, namespaces (html/svg/math),
  `id/class/dataset`, mutation observers (subset M1).
- Preload scanner thread: high-priority CSS/JS/font/image URLs.
- Parser-abort limits: nesting cap, entity expansion cap (billion-laughs guard).

## 4. CSS (`cube-css`)

- Parser: stylesheet/rules/selectors/declarations, error-recovery (drop bad decl).
- Cascade: origin (UA/author), specificity, `!important`, inheritance, CSS vars,
  `calc()/clamp()`, media queries (width/height/prefers-reduced-motion — wire to
  `accessibility.json`), container queries M2.
- Layout triggers: `flex/grid` M0-subset (flex row/col, grid auto-flow), `block/inline`
  full, `position static/relative/absolute/fixed/sticky`, floats, `box-sizing`,
  `overflow`, `z-index`/stacking contexts. `contain:layout`, `content-visibility:auto`
  + `contain-intrinsic-size` for perf.

## 5. Style recalc → Layout → Pre-paint → Paint → Composite

Mirrors RenderingNG, software-first:
1. `Style`: DOM+CSSOM → `ComputedStyle`, `LayoutObject` tree.
2. `Layout`: constraint algorithm → immutable `Fragment Tree` (positions/sizes,
   no transforms). BFC/IFC/flex/grid contexts, containing-block rules,
   intrinsic (`min/max-content`) vs extrinsic sizing, fragmentation (printing M2).
   Dirty-bit + parent-constraint cache; `contain:layout` prunes subtrees.
3. `Pre-paint`: walk `LayoutObject` (DOM order) → 4 property trees
   (transform/clip/effect/scroll) + paint invalidations. Replaces legacy layer tree.
4. `Paint`: `LayoutObject` → display items (`draw rect/text/border/shadow`),
   `PaintController` reuses unchanged subsequences, stacking-context atomicity.
5. `Commit`: copy trees+display list main→compositor (sync handoff).
6. `Layerize`: merge paint chunks by default, split on compositor-animated
   (`transform/opacity/scroll`) + overlap/memory heuristics.
7. `Raster/decode`: cairo/pango → `wl_shm` tiles (viewport tiled, 256–512 px);
   image decode (see §7) + paint worklets on workers. CPU fallback always works
   if GPU fails (same rule as RenderingNG compositor workers).
8. `Activate/Aggregate/Draw`: compositor frames (quads+render passes) per renderer,
   one aggregator in `cube-rasterd`, VSync-aligned `BeginFrame` scheduler,
   `transform/opacity/scroll` skip Style/Layout/Paint (why they are cheap).

## 6. JavaScript (`cube-js`) — ground-up, no V8/SpiderMonkey/JSC

- M0: hand-written ES2023-subset bytecode VM in C11 (`cube-js.c`):
  lexer→parser→AST→bytecode→register interpreter, mark-sweep GC,
  `Array/Object/String/Number/BigInt/RegExp(JSON-subset)/Promise timers`.
  No JIT in M0 (interpreter + inline caches). Fuzzed against Test262 subset.
- Bootstrap option (allowed, still not Chromium/Firefox): `QuickJS` (MIT,
  Fabrice Bellard) behind `cube-js` ABI, then replace piecemeal. Never link
  V8/JSC/SpiderMonkey.
- Event loop: tasks/microtasks, `setTimeout/fetch/XHR` async, `OffscreenCanvas`
  workers. DOM bindings via WebIDL-subset codegen (`cube-bindgen.py`).
- Web APIs M0: `console/timers/fetch/XHR/DOM/CSSOM/Events/storage/canvas2d-path`.
  M1: `TypedArrays/WASM-interp (no JIT)/WebWorkers/IntersectionObserver`.
  M2: `WebGL-via-Vulkan-lite/MediaSource/IndexedDB`.

## 7. Images / media

- Images: `PNG/JPEG/GIF/SVG-static` M0 (use `libpng/libjpeg-turbo`, `nanosvg`),
  `WebP/AVIF/JXL` M1, `SVG-SMIL` never. Decode on workers, EXIF-orient, ICC-lite.
- Video/audio: demux (`mp4/webm`) + decode (`dav1d/aom-lite`, `openh264` optional)
  on media threads, A/V sync via `CLOCK_MONOTONIC`, output via PipeWire
  (session children like `whitecube-session:audio=pipewire`, no system service).
- Text: `pango+cairo` (X-free `/usr/local` build), `fontconfig`, `harfbuzz`,
  `line-height/baselines/wrapping`, `content-visibility` skips offscreen.

## 8. Security / sandbox

- Site isolation: one `cube-renderd` per eTLD+1, cross-origin iframes remote.
- OS sandbox: `user+mount+net` namespaces, `seccomp-bpf` allowlist
  (`read/write/mmap/futex/poll` + Wayland/shm), `caps drop`, `no new privs`,
  `rlimits`, read-only `/usr`, private `$XDG_RUNTIME_DIR/cube-browser/`.
  GPU/media brokered; renderer never opens net/storage directly.
- Web: Same-Origin Policy, CORP/COOP/COEP, CSP Level 3 subset, `sandbox` attr,
  download quarantine (`XDG_DOWNLOAD_DIR`, executable bit never set),
  permission prompts (camera/mic/geolocation default deny, persisted per-site).

## 9. Storage

- Per-site partitions in `~/.local/share/cube-browser/`: cookies, `localStorage`
  (5 MB quota), `sessionStorage`, `IndexedDB` (sqlite backend M1), Cache API.
- Clear-on-exit flag, per-site wipe UI. Backups exclude credentials (same rule
  as whitecube `Alt+A` backups: 32 MiB total, 2 MiB/file).

## 10. Accessibility / whitecube integration

- Poll `~/.local/state/whitecube/accessibility.json` 1 Hz (same as `cube-status`):
  `ui_scale` → `devicePixelRatio`, `reduced_motion` → disable CSS/Web animations
  + CA-style auto-advance, `strong_contrast` → forced-colors palette.
- `Alt+W` (`g_browser_cmd`) opens `cube-browser`; `chat_app_id` untouched.
  Tabs are `xdg-toplevel` with whitecube tab bar (no custom tab strip in M0).
  `cube-status` Wi-Fi label reused for offline banner (`NET_STATUS` stale>20 s).
- Config: `~/.config/cube-browser/config` (`0600`, same style as
  `~/.config/whitecube/config` + `cube-chat/config.json`): `homepage`,
  `search_engine`, `js_enabled`, `thirdparty_cookies`, `autoplay`, `zoom`.

## 11. UI shell M0 (no extensions)

Omnibox (search+URL, IDNA, punycode warn), tabs (whitecube-provided),
history (sqlite, 90 d), downloads shelf, find-in-page, zoom, Reader Mode
(readability subset), Print-to-PDF via cairo, DevTools-lite (console/network/
elements). Extensions: explicitly non-goal M0–M1.

## 12. Build / test (mirrors `BUILD.sh` policy)

- `meson setup build --prefix=/usr/local && ninja -C build install`.
  Deps (all X-free, `ldd` checked): `wlroots-0.18`, `wayland-client/cursor`,
  `xkbcommon`, `cairo/pango/harfbuzz/fontconfig`, `libpng/libjpeg-turbo`,
  `sqlite3`, `Rustls/mbedTLS`, `libuv` (net), `dav1d` (M1).
  Toolchain wrappers like `build/toolchain/{as,ld}` when cross-building to
  `/mnt/nvme_root` from live USB.
- Tests: `workspace-test.py`-style layout tests, `desktop-state-test`-style
  state tests, `cube-ca --self-test` pattern for parser (`cube-html --self-test`),
  WPT subset (html/css/selectors), Test262 subset (js), fuzz corpora for
  HTML/CSS/JS/image decoders, `--render` PNG goldens like `cube-status --render`.

## 13. Milestones

- M0 (6–8 wks): `cube-netd` HTTP/1.1+TLS + `cube-html/css` block/inline/flex-lite
  + software raster + `cube-js` interpreter + 10-site smoke (example.com, debian.org).
- M1: h2, grid, workers, media images, sandbox seccomp, history/downloads.
- M2: h3, video/audio PipeWire, IndexedDB, printing, container queries.
- M3: Vulkan raster, WebGL-lite, perf (60 fps scroll on Intel iGPU), WPT pass-rate gate.

## 14. Non-goals / risks

No XWayland, no NPAPI/PPAPI, no Chrome/Firefox extension compat, no DRM
Widevine (PipeWire clear content only), no sync service M0. Biggest risks:
JS compat and CSS grid/flex edge cases — mitigated by QuickJS bootstrap +
incremental WPT burn-down.
