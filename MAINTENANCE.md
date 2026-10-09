# whitecube maintenance notes

## X-free cairo (/usr/local wins over distro cairo)
Built from https://cairographics.org/releases/cairo-1.18.4.tar.xz with:
  meson setup build-nx --prefix=/usr/local -Dxlib=disabled -Dxcb=disabled \
      -Dxlib-xcb=disabled -Dtests=disabled && ninja -C build-nx install
Result: /usr/local/lib/x86_64-linux-gnu/libcairo.so.2 with ZERO libX11/libxcb
links. ldconfig prefers /usr/local, so fuzzel/swaybg/swaylock/fnott and our
own binaries all resolve X-free cairo automatically (verify: ldd <bin> | grep -ci x11).

The above records the prepared target. On the 2026-10-08 validation host,
/usr/local's custom Cairo is absent: both the installed compositor and the
rebuilt binaries resolve distro Cairo with X11/XCB dependencies. Feature tests
pass here; the X-free runtime requirement still needs the documented target
library. No system libraries were changed during this work.

apt upgrades of distro libcairo2 are harmless (they land in /lib, still lose
to /usr/local). Rebuild this if: cairo SONAME bumps (libcairo.so.3), or you
want a newer cairo. Consumers linked with -Wl,-rpath,/usr/local/lib/...
(cube-status) or plain ldconfig order (distro binaries) pick it up.

## X-free wlroots 0.18.2 (/usr/local, no XWayland)
  git clone --branch 0.18.2 https://gitlab.freedesktop.org/wlroots/wlroots.git
  meson setup build --prefix=/usr/local -Dxwayland=disabled -Dexamples=false \
      -Dxcb-errors=disabled && ninja -C build install
Verify: ldd /usr/local/lib/.../libwlroots-0.18.so | grep -i xcb -> empty.

## Rebuilding our binaries
whitecube: cd ~/whitecube && ninja -C build install   (needs wlroots-0.18 pc)
cube-ca / cube-settings / cube-status: ~/whitecube/BUILD.sh
cube-chat: plain python, just copy to /usr/local/bin/cube-chat.

The staged upgrade has `BUILD.sh`: run `./BUILD.sh /mnt/nvme_root` from the live
environment, or `./BUILD.sh` on the installed system. It builds only into `build/`
and runs actual compositor workspace helpers, native desktop-state checks, and
the CA selftest. It also checks command/numeric configuration reloads, Caps Lock
shortcuts, mouse placement bounds/lifecycle and the four native theme palettes.
`meson test -C build` runs the compositor and palette checks with the normal
Meson build; Cairo and Pango are declared dependencies of the compositor.
It needs no new packages. `desktop-state.h` owns the bounded,
private command-free native layout format and reads shared accessibility JSON.
Native workspace records retain app ID/title and output-relative geometry;
mapped clients restore state without a launcher or saved command evaluation.
The UI polls accessibility preferences once per second; CA checks within two
seconds and halts automatic steps/reseeding when reduced motion is enabled.
The installed `whitecube-terminal` wrapper adjusts existing foot configuration
font size for new terminals and retains literal argv and ANSI game colors.

Alt+left-drag moves windows; Alt+right-drag resizes them. Fullscreen/maximized
windows retain their display size. Placements are saved on mouse release so
motion does not force repeated disk syncs. Window close, workspace switches,
desktop visibility changes and display/config changes cancel the grab.
cube-status derives its bar and menu colors from `bg`/`tabbar`, observes shared
strong contrast, and repaints open panels when geometry or colors change.

The terminal default is the red, transparent style in `foot.ini` (alpha 0.5).
User Foot and Scripts configurations use this palette; their prior versions
are in `build/reliability-before-nqicdw7o/user-{foot,scripts-foot}.ini`.
New terminals pick up the change. `foot -C -c PATH` validates a profile without
opening a window.

## Active per-user Desktop Chat integration (2026-10-08)
The live session previously used older `/usr/local/bin` binaries without the
Desktop Chat menu or Alt+Y / Alt+U bindings. The rebuilt `whitecube` and
`cube-status`, and the updated `whitecube-session`, are now installed in
`~/.local/bin`. `~/.bash_profile` starts that session at the next tty1 login;
the session selects its adjacent compositor and puts its own directory first
in PATH so it starts the corresponding top bar.

Only the live top bar was refreshed; the running compositor and application
windows were kept. The central Chat menu is active now. Compiled compositor
changes, including Alt+Y (toggle Chat) and Alt+U (Chat Settings), take effect
after logging out and back in. System binaries in `/usr/local/bin` are retained.

`~/.local/bin/desktop-chat` links to
`~/Projects/desktop-chat/desktop-chat.sh`. The launcher resolves its project
directory, prefers project-local Electron and falls back to the installed
Electron command without invoking npm. Its application entry is installed in
`~/.local/share/applications/desktop-chat.desktop`. First-launch settings and
toggle requests wait for renderer load; a hide request cancels a pending show.

Before-change files are in `build/desktop-chat-before-moos_sei`.
`~/.local/state/whitecube/desktop-chat-install.json` records installed hashes.
Private live activation logs are `desktop-chat-{app,bar}.log` in that state
directory. `build/activate-desktop-chat.py --verify`, run on the host, checks
the live compositor, per-user top bar and Desktop Chat renderer processes.

## Rules of the desktop
- No X server / XWayland binaries on the system (verify: ls /usr/bin | grep -Ei '^(X|Xorg|Xwayland)$').
- No DISPLAY ever exported; WAYLAND_DISPLAY only. MOZ_ENABLE_WAYLAND=1 for firefox.
- New GUI software must be Wayland-native; check ldd for libX11/libxcb before adopting.
- Exceptions on record: none currently. (firefox /opt binary: 0 X deps in ldd.)

## Network controller (net/)
cube-net, cube-net-dns, dhcpcd.conf and whitecube-network.service live in net/ (they used to exist
only as installed files). Tests: python3 -I net/test_cube_net.py and net/test_cube_wifi.py.
Install/rollback: net/apply.sh (dry run by default; it never restarts services). Policy, status
keys, journal lines and the first-boot check are in net/README.txt; net/legacy/ holds the replaced
revisions 1 and 2. Since revision 3 cube-net has its OWN WPA2-PSK station (nl80211 + EAPOL, pure
Python): it must not call wpa_supplicant or wpa_cli, and wpasupplicant may be uninstalled.
`python3 -I net/cube-net selftest` is the no-root check of the kernel side.
Rules: probes observe, only association/lease/router faults may reassociate, nothing disconnects,
use time.monotonic() for timers, keep status.json flat and under 16 KiB (cube-status scans it with
strstr), and never log SSIDs or credentials.

## Desktop privacy upgrade (2026-10-08)

`Settings > Privacy` (also Apps > Privacy and Quick settings > Privacy) adds
15 preferences in four sections plus an overview. All newly added preferences
retain existing behavior until selected, except that a successful screen lock
clears the compositor clipboard by default. No browser or GTK preference is
written merely by opening the panel.

- Desktop: hide window titles, hide network names in the bar/quick settings,
  remember native layouts, include titles in layouts, permit clipboard sharing,
  clear the clipboard when locking, and an inactivity lock timeout.
- Activity: GTK 3/4 recent-file tracking and confirmed cleanup of recent-file
  records, standard thumbnail caches, and the regular Wayland clipboard when
  the necessary tool and protocol are available.
- Devices: verified default microphone capture mute using PipeWire, falling
  back to ALSA Capture. This does not grant/revoke app permissions or mute every
  input device; unsupported hardware is explicitly shown as unavailable.
- Browser: startup overrides for Firefox telemetry, HTTPS-only browsing,
  strict tracking protection, search-provider suggestions, speculative
  connections and automatic session restoration. Only detected default profiles
  are changed. Existing unrelated user.js content is retained. Restart Firefox
  to apply; unmanaged means no Whitecube override, not the browser's live state.

The user-only `cube-privacy` helper is independent of the root settings helper
and sudoers policy. It refuses root, uses private atomic writes and bounded
fd-relative cleanup without following symlinks, reports unsupported controls,
verifies microphone changes and rolls back multi-file preference failures when
no intervening edit prevents a safe rollback. Its TSV contract is documented in
the script and covered by `test-privacy.py` using temporary account directories.
GTK preferences apply to newly started GTK apps. Neither recent-file cleanup nor
thumbnail cleanup deletes original files. Browser restoration settings do not
erase session-recovery files.

The compositor now supports `ext-session-lock-v1`. Alt+L and the existing lock
buttons can use swaylock. It hides all ordinary scene layers, blocks desktop
shortcuts and routes input only to lock surfaces. It acknowledges locking after
shielded frames are presented on every enabled output. A disconnected locker
leaves the session covered; a replacement lock client can recover it. The idle
lock timer is monotonic, off by default, and retries failed launches at most once
every 30 seconds. The lock protects the graphical session, not separate console
login policies. The previous compositor had no session-lock protocol despite
exposing lock actions; logout/login is required to get this implementation.

Settings now use the desktop's light/dark palette and strong-contrast preference.
Keyboard focus reaches off-screen controls and scrolls both page and sidebar;
modal focus stays inside confirmation/password dialogs. Config updates retain
comments, long unrelated lines and final lines without a newline, write mode0600
via a unique temporary file, and remove duplicate replaced keys. Administrator
passwords are erased immediately after being passed to sudo instead of being
cached for the panel's lifetime. The session wrapper uses umask077 for new logs.

The bar has keyboard navigation in Quick settings, Chat and Power, visible app
selection, scrollable Quick settings at larger scales, and a responsive Chat
button that avoids overlapping network/clock controls. `bar-layout.h` shares the
bar geometry with the compositor. Network names are omitted from periodic logs.
The native bar and compositor find the per-user Settings installation.

Build and validate:

    ./BUILD.sh
    meson setup build/meson-privacy
    meson test -C build/meson-privacy --print-errorlogs
    python3 session-lock-test.py --compositor build/whitecube --require-headless

The last command starts only an isolated two-output headless compositor using a
temporary home/runtime; it requires permission to bind a local Unix socket.
It does not touch a running desktop. Native UI preview commands:

    build/cube-settings --render privacy build/privacy.png
    build/cube-settings --section desktop --render privacy build/privacy-desktop.png 1100x1500
    build/cube-settings --section browser --render privacy build/privacy-browser.png 1100x1500
    build/cube-status --selftest
    build/cube-status --render build

`python3 INSTALL-USER.py` prints a plan; `--apply` atomically installs tested
binaries/helper/session to ~/.local/bin, adds a Settings app entry, and changes
only settings_cmd in the desktop config. Originals and a manifest are stored
under build/privacy-install-before-*, with installed hashes also recorded in
~/.local/state/whitecube/privacy-install.json. A failed install rolls back
files already replaced. It never stops the compositor; log out and back in to
activate the complete upgrade. New Settings windows can use the installed binary
immediately. Source backup: build/privacy-before-20261008-090356.tar.gz.

## Health check, cleanup, hooks
`whitecube-doctor` (read-only, no root; `--json` available) reports firewall output policy, the Wi-Fi
controller, competing network managers, wpa_supplicant leftovers, installed-vs-source drift and
over-broad passwordless sudo, each with the fix command. Tests: python3 -I test-doctor.py.
`tools/clean.sh [--apply]` removes __pycache__ and build/. `tools/install-hooks.sh` enables the
pre-commit hook that runs the pure-Python suites (git config core.hooksPath tools/hooks).
