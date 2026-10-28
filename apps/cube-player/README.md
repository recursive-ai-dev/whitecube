# Cube Player

Cube Player 1.0.0 is a native Wayland media player for Whitecube. It combines a
custom Cairo/Pango interface with mpv's playback engine and an EGL/OpenGL video
renderer. A software renderer is available when EGL is unavailable.

## Open the app

Launch **Cube Player** from the application menu, or run:

```sh
~/.local/bin/cube-player
~/.local/bin/cube-player '/path/to/a film.mkv'
~/.local/bin/cube-player ~/Music
~/.local/bin/cube-player https://example.com/direct-media.mp4
```

The Open button and **Ctrl+O** open the themed file browser. **Ctrl+L** accepts a
file path, folder, or direct HTTP(S) media URL. Drop files into the window to add
them to the queue. Files begin playing immediately when the player is idle.

The queue supports selecting/removing items, clearing, shuffle, repeat queue,
repeat one, and automatic advance. Opening a folder adds its supported files in
name order; it does not recursively scan subfolders. Local UTF-8 M3U, PLS and XSPF playlists are
expanded in queue order, including BOM-prefixed M3U files and paths relative to
the playlist. PLS files follow their numeric `FileN` order. XSPF locations support
URI escaping, XML entities, CDATA and `xml:base`; the first supported location in
each track is used. HLS manifests stay
intact for streaming playback. Imports skip cyclic references, limit nesting and
total work, and reject malformed or oversized playlists (2 MiB, 2,048 items). The Recent tab reopens previously played media.

The sound/subtitle panel selects audio and subtitle tracks, loads external
subtitles, adjusts subtitle delay, and provides chapter navigation. Embedded
subtitles are selectable rather than forced on. The fullscreen controls and
pointer hide after three seconds of inactivity and return on input.

## Whitecube appearance

The default appearance follows `~/.config/whitecube/config` (`bg` and `tabbar`),
using the same derived palette as Whitecube's native settings app. Changes are
picked up while Cube Player runs. Preferences also provide fixed light and dark
appearances. The shared interface scale and strong contrast preferences come
from `~/.local/state/whitecube/accessibility.json`.

All app controls, file browsing, preferences, track menus, error states, queue,
and fullscreen overlays use the custom interface. No GTK or Electron UI is used.
At narrow window widths the queue is hidden to give the video and transport
controls enough room. Interface scaling is bounded to fit a 640 × 400 layout.

## Keys

| Key | Action |
| --- | --- |
| Space / K | Play or pause |
| Left / Right | Seek 5 seconds; Shift seeks 30 seconds |
| Up / Down | Adjust volume |
| M | Mute |
| N / P | Next / previous; previous restarts after 3 seconds |
| F / F11 | Fullscreen |
| Escape | Close panel or leave fullscreen |
| [ / ] | Decrease / increase speed |
| S / R | Shuffle / cycle repeat mode |
| L | Toggle queue sidebar |
| J / V | Audio, subtitles, and chapters |
| Ctrl+O / Ctrl+Shift+O | Open media / add to queue |
| Ctrl+L | Enter a path or media URL |
| Ctrl+, | Preferences |
| F1 / ? | Keyboard help |
| Tab / Shift+Tab, Enter | Focus and activate controls |
| Ctrl+Q | Quit |

In the file browser, arrows select rows, Enter opens the selection, and Backspace
goes to the parent folder. Click to select, double-click to open. Ctrl+L focuses
the path field. Text fields support Unicode caret navigation, Home/End, deletion,
select all, copy, cut, and paste. Mouse wheel scrolls lists or adjusts volume over
the main player; the timeline and volume slider support dragging.

## Settings and history

- `~/.config/cube-player/settings.ini`: appearance, volume, playback speed,
  sidebar, shuffle, repeat, and resume preference.
- `~/.local/state/cube-player/history.ini`: the latest 40 media paths and their
  playback positions. Positions close to the end reset to the beginning.
- **Preferences → Clear history** removes recent media and stored positions,
  including the current item's saved position. That item stays forgotten until
  another item is opened.

These locations honor the standard XDG environment variables. Settings/history
are written atomically with owner-only permissions. Playback positions are
saved periodically and on normal exit. Each launch creates a new window and
queue; the recent list and per-file positions persist. The player uses its own
mpv configuration and does not load the stock player's scripts or settings.

## Build and install

```sh
./BUILD.sh
./scripts/install-user.py             # inspect the installation paths
./scripts/install-user.py --apply     # install the launcher, icon, and menu entry
```

The build requires a C compiler, Meson, Ninja, pkg-config, Wayland client/cursor/
EGL development files, wayland-protocols and wayland-scanner, EGL/GLES, xkbcommon,
GLib/GIO, and Cairo/Pango. Tests also use Python 3, Pillow, FFmpeg, and the adjacent
Whitecube compositor (`../whitecube/build/whitecube`). Set
`CUBE_PLAYER_TEST_COMPOSITOR` to use a different Whitecube build.

`scripts/bootstrap.sh` downloads the matching Debian amd64 libmpv 0.40.0 packages
through APT and extracts their library and headers into `.deps/`. It does not
install system packages. The existing mpv runtime dependencies must be present.
This build uses the local library via an embedded runtime search path.

**Keep this project directory in place:** the user launcher points at its built
executable. The installer creates a backup manifest under
`~/.local/state/cube-player/install-backups/`. To reverse an installation:

```sh
./scripts/install-user.py --restore /absolute/path/to/manifest.json --apply
```

Restore checks that installed files have not been edited since installation.
Installation registers supported media types in the Open With menu; it does not
change the default media player or replace mpv.

## Validation and diagnostics

`./BUILD.sh` compiles and runs state, UI, desktop-installation, and real Wayland
playback tests. The integration test creates a private headless Whitecube with
isolated XDG directories and generated media. A protocol relay supplies keyboard,
pointer, and URI-drop events to that client. The compositor presents actual
buffers, and the test checks video pixels and persisted playback results.

```sh
meson test -C build --print-errorlogs
build/cube-player --preview build/preview.png
build/cube-player --theme dark
build/cube-player --software
build/cube-player --capture build/window.png --report build/playback.ini --quit-after 5 video.mkv
```

`--silent-test` substitutes null audio and disables hardware decoding for isolated
diagnostics. `CUBE_PLAYER_DEBUG=1` enables mpv debug logging. Validation screenshots
and reports are in `build/validation/`. See [VALIDATION.md](VALIDATION.md) for
the release checks, sanitizer command, and runtime limits.

mpv is an independent upstream project. Its render/client APIs and distribution
notices are included in `.deps/usr/include/mpv/` and
`.deps/usr/share/doc/libmpv2/copyright`. Cube Player's Wayland plumbing and palette
were adapted from this workspace's Cube Browser and Whitecube native apps.
The Debian mpv binary is distributed under GPL-3-or-later; a copy of GPL version 3
is provided in `COPYING`, and the package copyright file records its component
licenses.
