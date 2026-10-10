# Whitecube — Onboarding

**What this is:** Whitecube is a from-scratch, Wayland-native desktop environment written almost
entirely in C and Python, running on a heavily rebranded MX Linux (Debian 13) install. It is not a
theme on top of sway or Hyprland — it is its own wlroots compositor, its own top bar, its own
settings app, its own Wi-Fi stack, its own lock screen protocol and its own wallpaper engine.

This document is the map. It explains the OS underneath, every piece of the desktop, where the live
code actually runs from, and how to build, test and install it. Read `REVIEW.md` before you touch
anything for a release.

---

## 1. The operating system

| Property | Value |
|---|---|
| Hostname | `mx` |
| Marketing identity | **WhiteCube** (`/etc/mx-version` = `WhiteCube`, `/etc/lsb-release` fully rewritten) |
| Actual distribution | **MX Linux**, i.e. Debian 13 "trixie" (`/etc/os-release`, `13.7`) |
| Kernel | `6.12.107+deb13-amd64` (Debian stock, `SMP PREEMPT_DYNAMIC`) |
| Init | systemd (PID 1) — but **no system D-Bus**: `/run/dbus` does not exist |
| Packages | ~1667 installed via apt |
| CPU / GPU | Intel i7-12650H (10 cores) · Intel UHD (Alder Lake-P) + NVIDIA RTX 4060 Max-Q (unused) |
| RAM / swap | 15 GiB · 11 GiB zram |
| Storage | 931 GB NVMe, **both `/` and `/home` are LUKS-encrypted** (separate partitions) |
| Desktop user | `woez` |

Two facts about the OS shape everything else:

1. **There is no X anywhere.** No Xorg, no XWayland, no `DISPLAY` variable, ever. `MAINTENANCE.md`
   states the rule: *new GUI software must be Wayland-native; check `ldd` for libX11/libxcb before
   adopting it.* There are currently zero exceptions on record. `BUILD.sh` has a `REQUIRE_XFREE=1`
   mode that makes X11/XCB linkage a build failure, and there is a custom X-free Cairo 1.18.4 built
   into `/usr/local` so that even distro tools (`foot`, `fuzzel`, `swaylock`, `fnott`) resolve a
   cairo with no libX11/libxcb at all.
2. **There is no display manager and no session manager.** No GDM/LightDM, no `elogind` session
   services for the desktop, no `systemd --user`. The seat is granted by **seatd**; the compositor
   talks to DRM and input directly.

### Boot to desktop, end to end

```
GRUB (whitecube theme, boot/)
  └─ Plymouth (whitecube.script / whitecube.plymouth)
      └─ systemd
          ├─ seatd.service                 # DRM + input access for the session
          ├─ whitecube-network.service     # cube-net: Wi-Fi station + DNS + dhcpcd
          ├─ ufw.service, chrony, zramswap
          └─ getty@tty1 (override: --autologin woez)
               └─ ~/.bash_profile
                    └─ exec ~/.local/bin/whitecube-session
                         └─ /home/woez/.local/bin/whitecube  -s "<startup clients>"
                              ├─ cube-ca        # living wallpaper (cellular automata / cosmos)
                              ├─ cube-status    # top bar, app menu, quick settings
                              └─ cube-check --first-boot
```

`whitecube-session` is a POSIX `sh` script. It creates a private `XDG_RUNTIME_DIR` (on this machine
it falls back to `/tmp/xdg-runtime-1000` because `/run/user/1000` does not exist — nothing creates
it without logind), unsets `DISPLAY` and `DBUS_SESSION_BUS_ADDRESS`, exports
`MOZ_ENABLE_WAYLAND=1 QT_QPA_PLATFORM=wayland SDL_VIDEODRIVER=wayland`, optionally launches
PipeWire as session children (`audio=pipewire` in the config), and has a **crash guard**: three
failed starts inside 60 seconds drops you to a plain shell instead of letting getty respawn forever.

If the session dies, getty respawns the autologin and you get a fresh session. There is no
"recovery mode" other than the crash guard itself.

### What the rebrand actually changed

`rebrand-system.sh` rewrote `/etc/lsb-release` and `/etc/mx-version` to say `WhiteCube` and sed'd
every MX-branded Plymouth theme and two `.desktop` files. The MX userspace (`mx-welcome`,
`mx-packageinstaller`, `mx-snapshot`, ~30 `.desktop` entries in `~/.local/share/applications`) is
still fully installed and still expects to be on MX. See `REVIEW.md` §31 for why that script is
unsafe to re-run.

---

## 2. The architecture

### The rule of the desktop

Everything is a **tab**. There is no tiling and no stacking area. Each of the **4 workspaces**
shows at most one normal window plus an optional always-on-top "Scripts" terminal; every other
window in that workspace lives as a labelled tab in the top bar. Windows keep running when hidden
or moved, including fullscreen games.

Every keybinding requires **Alt**. A lone **Super** press toggles the app menu (via
`pkill -USR1 ... cube-status` — see `whitecube.c:64`).

### Source → binary map

| Source (in this repo) | Binary | Role |
|---|---|---|
| `whitecube.c` (1819 L) | `whitecube` | The compositor. wlroots 0.18, `wlr_scene`, 6 layer trees, 4 workspaces, tab bar, drag/resize, ext-session-lock-v1, wlr-output-management-v1, libinput. |
| `cube-status.c` (2024 L) | `cube-status` | Top bar via wlr-layer-shell + cairo: app menu (10 categories, type-to-search), clock, quick settings (volume/brightness/Wi-Fi/lock/power), Chat dropdown, workspace cubes. |
| `cube-settings.c` (2355 L) | `cube-settings` | 14-page settings app (xdg-shell + cairo): Network, Display, Appearance, Keyboard, Sound, Power, Privacy, Firewall, Services, Users, Updates, System, Mouse & touchpad, Advanced. |
| `cube-ca.c` + `cube-field.h` + `cube-cosmos.h` | `cube-ca` | Living wallpaper. Six 2D rules, a real 18×18×18 toroidal 3D volume, and a 7-phase "cosmos zoom" (cells → geometry → land → planet → stars → galaxy → universe). Private UNIX datagram control socket. |
| `cube-settings-helper` (Python, root) | `/usr/local/libexec/cube-settings-helper` | The only privileged entry point, reached via sudoers. |
| `cube-privacy` (Python) | `cube-privacy` | User-level privacy backend (GTK recent files, thumbnails, clipboard, Firefox `user.js`, mic mute). Refuses root. |
| `cube-preferences` + `native-preferences.h` + `org.whitecube.desktop.gschema.xml` | `cube-preferences` | GSettings/dconf layer over 37 typed preferences. |
| `net/cube-net` (2573 L Python) | `cube-net` | **Its own WPA2-PSK station**: nl80211 + EAPOL + AES/CCMP/CMAC implemented from scratch. No `wpa_supplicant`. Also DHCP (dhcpcd), DNS, status publishing. |
| `net/cube-net-dns` | `cube-net-dns` | Writes `/run/whitecube-net/resolv.conf`; `/etc/resolv.conf` symlinks to it. |
| `cube-chat` (Python) | `cube-chat` | Chat panel running in a `foot` terminal; Alt+T / Alt+Y / Alt+U. Talks to a local or remote model endpoint. |
| `desktop-state.h` | (header) | Bounded, private, command-free native layout format (`~/.local/state/whitecube/native-layout.txt`). |
| `whitecube-session` | session sh | Described above. |
| `whitecube-install`, `INSTALL-USER.py`, `whitecube-doctor`, `BUILD.sh`, `tools/`, `boot/` | ops tooling | Install front door, per-user installer, read-only health report, build+test driver, cleanup/hooks, GRUB+Plymouth theme. |

### Things referenced everywhere that are **not in this repo**

These live only on the installed system, under `/usr/local/share/whitecube-workspace/`
(`bin/`, `health.py`, `server.py`, `jobs.py`, `recovery.py`, `session_daemon.py`, `catalog.json`):

`whitecube-terminal`, `whitecube-web`, `whitecube-desktop`, `cube-scripts`, `cube-check`,
`cube-lab`, and the Alt+W "project shelf" web server. They are installed by hand, not by any script
in this tree. This is `REVIEW.md` §8.

### Sibling projects (outside `~/whitecube`)

* `~/cube-browser` — the Alt+W browser shell (meson, has its own `README.md`, `RELEASE.md`,
  `COMPATIBILITY.md` and `COPYING`).
* `~/cube-player` — media player (meson, has `COPYING`, `tests/`).
* `~/Projects/desktop-chat` — the Electron chat app behind the Chat menu; `~/.local/bin/desktop-chat`
  symlinks to its `desktop-chat.sh`.
* `~/whitecube-sec` — a private local RAG index (Ollama + chunk store + `index.json`) used for
  offline docs/questions. Not part of the desktop.

### Where the running code actually is

**This is the single most common source of confusion.** There are three generations on disk:

* `~/whitecube/` — source + `build/` output (current).
* `~/.local/bin/` — **what is actually running**: `whitecube`, `cube-status`, `cube-settings`,
  `cube-privacy`, `whitecube-session`. Installed by `python3 INSTALL-USER.py --apply`.
* `/usr/local/bin/` — the *system* copy, **stale** (dated Oct 7), and littered with
  `*.bak-20261007`, `*.bak-20261008`, `*.round2` leftovers. `whitecube-session` puts
  `~/.local/bin` first in `PATH`, so the per-user copies win.

`~/.bash_profile` runs `$HOME/.local/bin/whitecube-session`. Change compositor behaviour →
rebuild → re-install to `~/.local/bin` → **log out and log back in**. A live refresh only ever
affects the bar, not the compositor.

---

## 3. Configuration model

There are **two layers plus a schema**, and they are not merged (see `REVIEW.md` §14):

```
dconf override  >  ~/.config/whitecube/config  >  /etc/whitecube/config  >  built-in / device default
```

* **Legacy flat file** (`key=value`, `#` comments only in column 1). Read by five independent
  parsers: `whitecube.c`, `cube-cosmos.h`, `cube-status.c`, `cube-settings.c` and
  `whitecube-session` (which greps for `audio=` with an exact suffix match).
* **GSettings schema** `org.whitecube.desktop` — 37 typed keys (colors, pointer, keyboard, clock,
  display, privacy). Written through `cube-preferences`, which spins a **fresh short-lived
  `dbus-run-session`** per save because there is no session bus, then verifies by read-back.
  The schema is installed to `<datadir>/whitecube/schemas`, *not* the standard GSettings dir, so
  `gsettings` and `dconf-editor` cannot see it.
* **`accessibility.json`** (`~/.config/whitecube/`) — shared with the browser; UI scale, terminal
  scale, strong contrast, reduced motion. The compositor polls it once per second.

Current live user config (`~/.config/whitecube/config`) is the **light** palette
(`bg=fbfbfb`), privacy-maxed (`hide_titles=1`, `hide_network=1`, `clipboard=0`,
`lock_minutes=5`), settings/browser pointed at the per-user binaries.

### State locations (all private, mode 0600/0700)

```
~/.local/state/whitecube/session.log          # session output
~/.local/state/whitecube/failed-starts        # crash-guard timestamps
~/.local/state/whitecube/native-layout.txt    # window placements (atomic writes)
~/.local/state/whitecube-ca/                  # saved automaton worlds/seeds
~/.local/state/whitecube/privacy-install.json # installed hashes
/tmp/xdg-runtime-1000/                        # XDG_RUNTIME_DIR (no logind)
/run/whitecube-net/status.json                # Wi-Fi status read by the bar
/run/whitecube-net/resolv.conf                # the system resolver file
```

---

## 4. Key bindings

Full text is in `keys.txt` (installed at `/usr/share/whitecube/keys.txt`; Alt+H opens it).

| Key | Action |
|---|---|
| Alt+F1..F4 / Shift+F1..F4 | Switch workspace / move window to workspace |
| Alt+Enter | Terminal (foot) |
| Alt+B | Toggle this workspace's Scripts terminal |
| Alt+F | Fullscreen / restore |
| Alt+V | Show desktop / restore |
| Alt+C | Living-field (wallpaper) command panel |
| Alt+W | Open the browser desktop / project shelf |
| Alt+S | Settings |
| Alt+T / Alt+Y / Alt+U | Chat panel / toggle Chat / Chat Settings |
| Alt+D | Launcher (fuzzel) |
| Alt+L | Lock screen (swaylock via ext-session-lock) |
| Alt+A | Accessibility scale, workspace names, backup/restore |
| Alt+N / Alt+Q / Alt+H | Next wallpaper / close window / help |
| Alt+1..9, Alt+Tab | Jump to tab / cycle tabs |
| Alt+Shift+Arrows / Ctrl+Arrows | Move / resize focused window |
| Alt+left-drag / right-drag | Move / resize window under pointer |
| Alt+Shift+Esc | Quit desktop · F12 scene dump |

---

## 5. Building, testing, installing

```bash
cd ~/whitecube
./BUILD.sh                  # builds into build/ and runs the whole battery
SANITIZE=1 ./BUILD.sh       # ASan+UBSan into build-asan/
REQUIRE_XFREE=1 ./BUILD.sh  # X11/XCB linkage becomes fatal
./BUILD.sh /mnt/nvme_root   # cross-build against an installed root from live media
meson setup build/meson && meson test -C build/meson --print-errorlogs
```

`BUILD.sh` never installs anything. It builds all four binaries, generates `workspace-test.c` from
the compositor source, and runs ~9 C test binaries plus 9 Python suites (~300 tests total, all fast,
none requiring root, network or a radio).

Install is deliberately split and **dry-run by default**:

```bash
whitecube-install                             # plan for everything
whitecube-install --apply                     # build if stale + per-user install (backed up, roll-backable)
sudo whitecube-install --system --apply \
     [--with-hardening] [--with-sudoers] [--with-cairo]
whitecube-install --rollback-user
sudo whitecube-install --rollback-system <backup dir>
```

Diagnostics: `whitecube-doctor` (read-only, no root, `--json`) reports firewall policy, the Wi-Fi
controller, competing network managers, `wpa_supplicant` leftovers, installed-vs-source drift and
over-broad passwordless sudo — each with its fix command.

Network subsystem: `net/apply.sh` (dry-run by default, never restarts services),
`python3 -I net/cube-net selftest` for a no-root kernel-side check.

---

## 6. Invariants you must not break

1. **No X.** Verify any new GUI tool with `ldd <bin> | grep -ci x11` before adopting it.
   Never export `DISPLAY`.
2. **`cube-net` must never call `wpa_supplicant` or `wpa_cli`.** It *is* the station.
3. **Never log SSIDs or credentials.** Enforced by tests
   (`test_the_journal_never_names_the_network`).
4. **`status.json` stays flat and under 16 KiB** — `cube-status` scans it with `strstr`.
5. **Use `time.monotonic()`** for all timers in the network controller.
6. **Privileged work goes through `cube-settings-helper` only**, and only via argv lists — never
   shell interpolation in a root context.
7. **Config/state writes are atomic** (unique temp file → `rename` → `fsync`) and mode 0600.
8. **The compositor is never restarted by an installer.** It prints the command; you log out.
9. **`tools/clean.sh` is dry-run by default and never deletes rollback archives.** Keep it that way.

---

## 7. Known sharp edges (read before debugging)

* `systemctl` **does not work** from inside the session — there is no `/run/dbus`. Use
  `sudo systemctl ...` only where a system bus exists at all; today most service control has to be
  done from a TTY. MX's own service tools are broken by this.
* `whitecube-doctor` and `whitecube-install` are **not installed** anywhere; run them from the repo.
* Editing the config file with identical content still resets your keyboard (keymaps are re-applied
  on mtime change).
* A monitor unplug leaves tabs parented to the dead output's scene tree.
* Alt+F12 dumps the whole scene tree to `session.log` in release builds.
* `keys.txt` is keyboard help, not credentials. Yes, it looks alarming in a security review.

---

## 8. Document index

| File | What it is |
|---|---|
| `MAINTENANCE.md` | The real operational manual: X-free builds, rebuild instructions, privacy upgrade, net rules, install/rollback. |
| `VALIDATION.txt` | Dated validation report from the 2026-10-07 staged upgrade. |
| `SETTINGS-IMPROVEMENTS.txt` | The 16 settings changes shipped in 0.2.0. |
| `DIFF.txt` | Revision-2 changelog for the network controller. |
| `net/README.txt` | Network policy, status keys, journal lines, first-boot check (contains superseded revision-2 text). |
| `keys.txt` | In-desktop keyboard help. |
| `REVIEW.md` | **32 things that must be fixed before release.** |

Version: `0.2.0` (declared in `meson.build` and `INSTALL-USER.py` only — it is compiled into no
binary). There is no license, no changelog, no README and no tag in the git repo.
