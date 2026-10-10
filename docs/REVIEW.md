# Whitecube — Pre-Release Review

**32 improvements that must be made before release.** Every item below was verified against the
tree on 2026-10-09; evidence is file:line so it can be checked, not argued with.

Severity: **BLOCKER** = cannot ship · **HIGH** = ship will generate support load or data loss ·
**MED** = should ship fixed, acceptable with a release note.

Read `ONBOARDING.md` first for the system map.

---

## Tier A — Release blockers

### 1. Install the hardened sudoers rule; the shipped one hands out passwordless wildcards
`sudoers-whitecube-settings:3-7` grants `%sudo ALL=(root) NOPASSWD` for `net-connect *`,
`net-forget *`, `brightness *`, `power-action *` plus `services`, `firewall`, `net-scan`, `net-add`,
`net-reconnect`. Any process running as any sudo-group member can silently add a rogue Wi-Fi
profile, delete your networks, or power the machine off — with no password, from a background job or
an SSH session. `sudoers-whitecube-settings.hardened:1` is literally headed
`"# STAGED, NOT INSTALLED."` and only lands when a human remembers `--with-sudoers`
(`whitecube-install:158-164`); nothing ever migrates an already-installed loose rule.
**Fix:** make the hardened variant the default install, add an upgrade path that detects and
replaces the loose rule, and remove the `*` wildcards (`brightness 1-100`, a fixed power-action
list).

### 2. `whitecube-doctor` rates that same rule as `info` and still exits 0
`whitecube-doctor:172` emits `finding('info', 'passwordless sudo', ...)` for the single
highest-severity issue in this review, and `:233` only fails on `level == 'FAIL'`. The health tool
that is supposed to catch this prints a shrug and returns success.
**Fix:** emit `WARN` (and make `--strict` fail on WARN) whenever a `%group` — rather than a single
user — matches a NOPASSWD Cmnd_Alias containing wildcards.

### 3. The root helper `exec_module`s `/usr/local/bin/cube-net` with no integrity check
`cube-settings-helper:24` `CUBE_NET = "/usr/local/bin/cube-net"`, executed as root by
`cube_net_module()` (`:105-110`) for `net-scan`, `net-add` and `net-profile` — all NOPASSWD.
There is no `st_uid`/`st_mode`/hash check anywhere in the file (grep for `st_uid|st_mode|sha` →
zero hits). `/usr/local/bin` is not root-only on this machine; whoever can write that path gets
arbitrary root code behind a NOPASSWD sudoers entry.
**Fix:** refuse to load unless the file is root-owned, not group/world-writable, and matches a
pinned sha256 recorded at install time (the install manifest already computes hashes — read them).

### 4. No privileged action leaves any audit trail
`cube-settings-helper` has no `syslog`/`logger`/journal call (grep → zero). `fail()` writes to
stderr (`:66-68`), and `cube-status` throws stderr away (`cube-status.c:275-279` dups it to
`/dev/null`). So a hostname change, firewall disable, group grant, network change or poweroff
produces **no record at all**, and failures produce no record either.
**Fix:** one `log_line()` in the helper that appends timestamp + argv + exit status to
`/var/log/whitecube/priv.log` (or `logger -t cube-settings-helper`) before returning; redirect
helper stderr in the bar into the same place instead of `/dev/null`.

### 5. Granting `sudo` to a user is one toggle-click with no confirmation
`cube-settings.c:1802` fires `action("group", ..., "add", user, "sudo")` straight from a switch in
the Users page. The helper only refuses *removing your own sudo* (`cube-settings-helper:507-508`).
Compare Power-off (`:1581`) and Firewall-off (`:1734`), which both go through `confirm(...)`.
This is the largest privilege amplification in the app and it has the lowest friction.
**Fix:** require `confirm()` plus a password for any group add, and a typed-username confirmation
for `sudo`/`admin` specifically.

### 6. `/etc/hostname` and `/etc/hosts` are written non-atomically, in two steps
`cube-settings-helper:435` `Path("/etc/hostname").write_text(...)` then `:439`
`Path("/etc/hosts").write_text(...)` — truncate-in-place, no temp file, no `os.replace`. A crash or
power loss between them boots a machine with mismatched identity. The same repo does this correctly
elsewhere (`whitecube-install:142-146` stages + `os.replace`; `cube-settings.c:440-445` uses
`mkstemp` + rename).
**Fix:** write both to temp files, fsync, then rename both, and only then run `hostname(1)`.

### 7. `/home/woez/...` is compiled into shipped binaries
`whitecube.c:901-902` — `Alt+Y`/`Alt+U` `spawn("/home/woez/Projects/desktop-chat/desktop-chat.sh
--toggle")`. `cube-status.c:52` `#define DESKTOP_CHAT_SH "/home/woez/Projects/desktop-chat/
desktop-chat.sh"`, executed through `/bin/sh` by the Chat button (`:1356-1360`). `config.example:15`
and the live `~/.config/whitecube/config` both contain
`scripts_cmd=... -c /home/woez/.config/whitecube/scripts-foot.ini ...`. On any other machine the
Chat button and the Chat keybinding are silently dead (stderr → `/dev/null`).
**Fix:** resolve from config (`chat_app_id` already exists) with a PATH lookup and an `access()`
check before spawn; fall back to a visible "Chat is not installed" message rather than silence.

### 8. A fresh install cannot bring networking up — three things no script creates
* `net/cube-net:2403` requires `/etc/whitecube/network/device.json` and `protected()` (`:429-440`)
  raises if it is absent — but `net/apply.sh:23-29` installs five files, none of them `device.json`,
  and `net/README.txt:47` calls the options file "all optional". Tests hide this because fixtures
  always write it (`net/test_cube_wifi.py:1980`).
* `/etc/resolv.conf` must be a symlink to `/run/whitecube-net/resolv.conf`
  (`net/cube-net-dns:4-5`). No script in the tree creates it (grep for `symlink|ln -s` → nothing).
* Nothing anywhere runs `systemctl enable whitecube-network`. The `[Install]` section
  (`net/whitecube-network.service:42-43`) is inert until someone does.

All three exist on *this* machine because they were done by hand. **Fix:** `whitecube-install
--system` must create `device.json` (or make it genuinely optional), create the resolv.conf symlink
with verification, and enable the unit — then verify with a `doctor` check.

### 9. The components every shipped default points at are not in the repo and are installed by no script
Referenced by `org.whitecube.desktop.gschema.xml:48,58,68,83`, `whitecube.c:179` and
`config.example:10-17`, but absent from this tree and from `INSTALL-USER.py:42-76`:
`whitecube-terminal`, `whitecube-web`, `whitecube-desktop`, `cube-scripts`, `cube-check`,
`cube-lab`, `whitecube-next-wallpaper` (spawned at `whitecube.c:893`), and `keys.txt` →
`/usr/share/whitecube/keys.txt` (the target of the default `help_cmd`, which `compositor-test.c:56`
asserts as a *string* without checking the file exists). They live only under
`/usr/local/share/whitecube-workspace/`, installed out-of-band. A clean machine gets a desktop
whose terminal, browser, Scripts panel and help key all fail.
**Fix:** move these into the repo, add them to `INSTALL-USER.py` and the meson install list, and
have `whitecube-doctor` verify every `*_cmd` target resolves.

### 10. There is no license, copyright notice or SPDX identifier anywhere
`git ls-files` contains no `LICENSE*`, `COPYING*`, `NOTICE*`, `AUTHORS*`, `CHANGELOG*` or root
`README*`; greps for `license|copyright|SPDX` across `meson.build`, `BUILD.sh`, `whitecube.c`,
`cube-status.c`, `net/cube-net`, `whitecube-install`, `INSTALL-USER.py`, `MAINTENANCE.md` return
nothing. (The only `COPYING` files belong to vendored Cairo inside the gitignored `build-cairo/`.)
The project installs binaries into `/usr/local/bin`, a sudoers fragment into `/etc/sudoers.d`,
systemd drop-ins and network config — and links wlroots, Cairo, Pango, GLib and libinput.
**Fix:** add a `LICENSE` (and an SPDX header per source file), a `THIRD-PARTY.md` listing what is
linked and under what terms, and a root `README.md`. Nothing else in this list matters until this
exists.

---

## Tier B — Correctness

### 11. Uncommitted work and untracked load-bearing files
`git status --short` on a clean checkout target shows:
```
 M BUILD.sh INSTALL-USER.py MAINTENANCE.md compositor-test.c cube-settings.c cube-status.c
   meson.build settings-test.c test-install.py whitecube-install whitecube.c
?? SETTINGS-IMPROVEMENTS.txt cube-preferences native-preferences.h
?? org.whitecube.desktop.gschema.xml schemas/ settings-integration-test.py test-preferences.py
```
Four of the seven untracked paths are required to build (`org.whitecube.desktop.gschema.xml` is
copied by `BUILD.sh:46`; `schemas/meson.build` is pulled in by `meson.build:9`; `cube-preferences`
is read by `INSTALL-USER.py:48`). There are 82 tracked files, no tags and no remote. **Anyone
cloning this repo cannot build it.** **Fix:** commit everything, tag `v0.2.0`, add a remote, and
make `tools/hooks/pre-commit` fail on untracked files.

### 12. Tabs are parented to the first output's scene tree and are orphaned when it disappears
`t_map` (`whitecube.c:1397-1404`) creates `tab_border/tab_cube/tab_label` under
`server_first_output()->tab_tree` exactly once. `output_destroy` (`:1210-1221`) destroys only
`lock_bg` and frees the output — it never re-parents or destroys `tab_tree`, `bg_tree`, `tab_bg` or
`workspace_cube[4]`. Unplug the primary monitor and the tabs are left children of a freed tree at
the dead output's coordinates: leaked scene nodes plus invisible tabs. `hotplug-test.c:99-108`
asserts only output/layout counts, so this is untested. **Fix:** move `t_map`'s tab nodes to the
surviving output's `tab_tree` in `output_destroy`, and add a regression test that maps a window,
removes the output, and asserts the tab node's parent.

### 13. Five unchecked allocations and three unchecked constructors kill the compositor on OOM
`calloc` results are dereferenced without NULL checks at `whitecube.c:995` (`server_new_keyboard`),
`:1367` (`server_new_output`), `:1477` (`server_new_toplevel`), `:1503`
(`server_new_popup` — `calloc(...); p->x = x;`), `:1686` (`server_new_layer`). The same for
`xkb_context_new` (`:763-768`, NULL ctx handed to `xkb_keymap_new_from_names`),
`wlr_xcursor_manager_create(NULL, 24)` (`:1774`) and `wl_display_create()` (`:1730`). Contrast with
the correctly checked `server_new_pointer` (`:1033`) and `lock_new_surface` (`:1607`). A single
memory spike from an app takes down the whole desktop. **Fix:** check every one; on failure log to
`session.log` and degrade (skip the client, refuse the map) instead of crashing.

### 14. Session lock can hang forever waiting for a frame that never commits
The lock implementation is otherwise strong — input routed only to lock surfaces (`:1541-1548`),
all keybindings blocked while locked (`:962-969`), a crashed locker fails closed (`:1633-1642`),
`locked` acked only after every enabled output presents a lock frame (`:1560-1566`). But if
`wlr_scene_output_commit` keeps failing, `:1188` resets `lock_frame_submitted` and returns — and
`locked` is **never sent**, so the locker waits indefinitely and the user has a frozen screen.
**Fix:** an ack deadline (e.g. 5 s) that either forces a plain black commit or deliberately fails
the lock with a user-visible error, never a silent hang.

### 15. `spawn()` reports success even when the exec fails, and SIGCHLD is ignored
`whitecube.c:724-746` — the child writes the readiness byte *before* `execl("/bin/sh", ...)`, so
the parent always gets a valid pid; `signal(SIGCHLD, SIG_IGN)` at `:1710` auto-reaps any diagnostic.
Every keybinding that launches a program fails silently, including the screen locker (`:847`,
`:894` — `swaylock -f -c 111827`). If swaylock is missing the screen simply never locks, and the
30 s retry throttle just re-fires forever. **Fix:** write the readiness byte *after* exec succeeds
(pipe close = exec success), and `access()`-check external binaries once at session start, reporting
absence in the bar and in `whitecube-doctor`.

### 16. Five config parsers with five different semantics
`whitecube.c:151-163`, `cube-cosmos.h:431-439`, `cube-status.c:1090`, `cube-settings.c:391` and
`whitecube-session:49` (`sed -n 's/^audio=//p' ... | tail -n 1`) each read the same two files
independently. The session's is an exact suffix match, so `audio=pipewire # comment` silently does
not enable PipeWire. The compositor's parser only honours `#` in column 1 (`:156`), never trims
around `=` (so `terminal = foo` yields key `"terminal "`), and truncates values into fixed buffers
`g_terminal[128]`/`g_chat_cmd[256]` (`:74-76,124-132`) with no warning. Typos are invisible.
**Fix:** one shared reader (a small header for C, one function for the shell path) with trimming,
comment stripping, size checks and a "unknown key at line N" warning written to `session.log`.

### 17. The dconf layer and the legacy file disagree about what the defaults *are*
The schema declares `pointer-speed` `min=-100` and `tap-to-click` `false`
(`org.whitecube.desktop.gschema.xml:160-179`); the compositor uses `-1`/`-101` sentinels to mean
"inherit device default" (`whitecube.c:84-85`). A `config_integer` clamp (`:145`) turns an explicit
`pointer_speed=-101` into `-100` — a real speed, not "inherit". `report_preferences` (`:203-241`)
then exports the sentinels into diagnostics. The same drift exists for colours: schema
`bg='06080d'` vs the settings fallback `cfg_get("bg", "fbfbfb")` (`cube-settings.c:767`) vs
`config.example:5`. When the schema is not found (see §19) the bar paints white and the compositor
paints near-black. **Fix:** make the schema the single source of defaults, generate the legacy
defaults from it, and use a documented sentinel that cannot collide with the declared range.

### 18. Multi-monitor is half-implemented and silently wrong
The plumbing exists (wlr-output-management `:1289-1343`, per-output settings `:1236-1268`, per-output
lock shields) but every windowing decision resolves against `server_first_output()`:
`wc_retile` (`:445-514`), `wc_start_drag` (`:530-553`), `cursor_button` (`:1130-1147`). `t->output`
is assigned once at map (`:1399`) and never updated. A second monitor gets wallpaper and a lock
shield and no windows, no tabs, no workspace cubes. `server_first_output` can even return a
*disabled* output (`:369`) which `t_map` then uses for geometry. `output_manager_apply_or_test`
(`:1314-1320`) commits heads sequentially, so a mid-way failure leaves a partially applied layout.
**Fix (release-honesty minimum):** either implement per-output tab trees and window-to-output
assignment, or explicitly detect >1 output at startup, document single-output support, and place
secondary outputs behind a clear "extended wallpaper only" mode. `VALIDATION.txt:44` already
concedes this; the UI does not.

### 19. Tabs past the end of the bar are silently unreachable
`wc_retile` (`whitecube.c:470-473`) computes `tab_visible = ... && next_end < bar.tabs_end` — tabs
beyond the bar's right edge are disabled with no scrollbar, no overflow marker and no count. The
window keeps running but cannot be reached from the bar at all. `tab_at` (`:1059-1069`) re-implements
the same index arithmetic independently, so the two can drift. **Fix:** an overflow chevron with a
count that opens the existing app menu filtered to that workspace, and one shared layout function
used by both the painter and the hit-test.

### 20. Bar geometry and the workspace count are duplicated across 5+ files with no single source
`whitecube.c:59-62` (`BAR_APPS_W 104 / BAR_STATUS_W 300 / BAR_CHAT_W 140 / BAR_POWER_W 44`) is
commented "mirrored in cube-status.c"; `bar-layout.h:9-11` re-encodes the same numbers scaled;
`cubes_left()` (`whitecube.c:89`) adds a magic `112`. `WORKSPACES 4` appears at `whitecube.c:65`,
`desktop-state.h:22,90,95,100,145` and `workspace-test.py:29`, and the hit-test uses a literal
`unit * 4` (`whitecube.c:1136`) rather than the macro. **Any** rebrand or workspace-count change is
a multi-file lockstep edit that will silently desync the compositor and the bar.
**Fix:** one `whitecube-layout.h` (the shared-header pattern already exists — follow it), with
`static const` values and a compile-time assert in the bar.

---

## Tier C — Install, ops and testing

### 21. Two build systems produce the same binaries from the same directory with different flags
`BUILD.sh:13,48` uses `-std=c11 -O2 -g -Wall -Wextra` into `build/`; `meson.build:1,13-33` builds
the same four names into `build/` with `warning_level=2` and the default buildtype. `MAINTENANCE.md`
gives conflicting advice (`ninja -C build install` for the compositor, `BUILD.sh` for everything
else). Which artifact is authoritative is undefined, and meson's auto-generated `build/.gitignore`
(`*`) means a `BUILD.sh` binary can silently shadow a meson one. **Fix:** make Meson authoritative
and turn `BUILD.sh` into a thin wrapper (`meson setup` + `meson test`), keeping the sysroot and
sanitizer modes as Meson native options.

### 22. No version in any binary, no tags, no changelog, no remote
`0.2.0` exists only in `meson.build:1` and `INSTALL-USER.py:86`; grep finds no version string
compiled into `whitecube.c`, `cube-settings.c` or `cube-status.c`, so `whitecube-doctor` cannot
report what is installed and drift detection compares bytes, not versions. Git has no tags and no
remote. Builds are not reproducible (`-g` embeds absolute paths; no `-ffile-prefix-map`, no
`SOURCE_DATE_EPOCH`, no `__DATE__` usage). **Fix:** a generated `version.h` with
`whitecube --version`, `-ffile-prefix-map`, a `CHANGELOG.md`, an annotated `v0.2.0` tag and a remote.

### 23. Three scripts mutate the system in ways that cannot be cleanly undone
* `rebrand-system.sh:4-13` backs up `/etc/lsb-release` and `/etc/mx-version` **unconditionally**, so
  a second run overwrites the originals with the already-rebranded files and makes revert impossible.
  No root check, no dry run, no `--revert`, no tests, referenced nowhere.
* `whitecube-install --with-cairo` (`:126-132`) runs `cp -a "$stage/." /usr/local/` + `ldconfig` and
  prints only an undo *hint*, contradicting its own header ("Every step that changes files makes a
  backup first"). `tools/build-xfree-cairo.sh:41` says outright "backs up nothing". Overwriting a
  live system Cairo is the least reversible action in the installer.
* `boot/install-boot.sh:43-56` `sed -i`s `/etc/default/grub` (including forcing `GRUB_TIMEOUT 5`),
  `printf ... > /etc/issue` clobbering the distro banner, and `plymouth-set-default-theme -R`
  (initramfs rebuild). `--revert` restores three files but never removes
  `/boot/grub/themes/whitecube`, the `.pf2` fonts or the Plymouth theme. It also requires Pillow,
  which is declared nowhere, and with `set -eu` a missing DejaVu font aborts *after* the Plymouth
  files land but *before* the GRUB edits — a half-applied theme.

**Fix:** dry-run by default, `--revert` that is tested, idempotent backups keyed on the *original*
content hash, and root-only execution checks.

### 24. The installed system is dirty and `whitecube-doctor` cannot see it
`/usr/local/bin` contains `cube-net.bak-20261007`, `cube-status.bak-20261007`,
`cube-status.bak-20261008`, `cube-status.round2`, `whitecube.bak-20261007`,
`whitecube.bak-20261008`, `whitecube.round2`, `cube-settings-helper.bak-20261007` and a
`__pycache__/` directory — all root-owned, all shipped next to the live binaries, all of which are
themselves stale (Oct 7) versus the per-user copies that actually run. `whitecube-doctor`'s drift map
(`:129-139`) covers 5 system files + 6 binaries and misses `cube-preferences`, the desktop entry,
the schemas and the `/usr/local/bin/whitecube` + `cube-ca` binaries that `ninja -C build install`
puts there. It also never checks that the network unit is *enabled*, that `/etc/resolv.conf` is the
right symlink, that `device.json` exists, or that `ufw` is actually filtering (its own README records
`ENABLED=no` and an empty nftables ruleset as the norm, and `check_firewall` `:61-78` only inspects
`DEFAULT_OUTPUT_POLICY` and `ENABLED`).
**Fix:** delete the `.bak`/`.round2`/`__pycache__` debris, extend the drift map, and add the four
missing checks.

### 25. No CI, and three test suites are manual-only
There is no `.github/`, no `*.yml`, no `Makefile`, no `debian/`. Automation exists only as a local
opt-in git hook (`tools/install-hooks.sh` → `core.hooksPath`), which runs 8 Python suites but skips
`test-preferences.py` (`tools/hooks/pre-commit:7`), all C tests, and every Meson test.
`session-lock-test.py`, `settings-integration-test.py` (which exercises all 37 settings against a
live helper and headless Wayland) and `test-live-test.py` (the Wi-Fi dead-man's-switch) appear in no
runner at all — `settings-integration-test.py` is referenced only from `MAINTENANCE.md`. Meanwhile
`boot/`, `rebrand-system.sh`, `fetch-wallpapers.py` and `gen-boot-theme.py` have zero tests.
**Fix:** one `./check` entry point that everything (hook, CI, release) calls, containing every suite
that can run without root or hardware; CI on push; and a release checklist that additionally runs the
three manual suites.

### 26. Rollback is asymmetric and unverified
`whitecube-install --rollback-user` (`:94-104`) copies `record['before']` over the target with **no
checksum verification and no "changed since install" guard** — unlike `net/apply.sh:111-117`, which
explicitly refuses post-install edits. The `sha256` field written by `INSTALL-USER.py:92` is written
and never read by any shipped tool. `--rollback-system` (`:165-168`) only shells out to
`net/apply.sh --rollback`, whose file list (`:23-29`) contains none of the extras that
`backup_extra()` (`:108-116`) put in `/var/backups/whitecube/extras-STAMP` — so a hardened sudoers or
network drop-in can only be restored by hand from an undocumented directory name. User rollback
archives also live *inside* `build/` (`INSTALL-USER.py:85`), which any `rm -rf build` destroys.
**Fix:** verify hashes on restore, refuse to clobber post-install edits, make `--rollback-system`
restore extras, and move rollback archives out of `build/`.

---

## Tier D — Feature and UX gaps a release will be judged on

### 27. Settings is missing the controls people look for on day one
`cube-settings.c:728-731` defines exactly 14 pages. Absent, and absent from the helper's command
table (`cube-settings-helper:531-544`): **night light / colour temperature, notifications &
do-not-disturb, screen blank / DPMS / screensaver timeout, VPN and proxy, user account creation /
avatars / password expiry (Users offers only group toggles plus a `passwd` terminal at `:1808`),
multi-keyboard-layout picker (`:1349-1352` shows one `xkb_layout` string), cursor size, and printer
setup** (CUPS exists only as a Services on/off toggle). **Fix:** at minimum ship night light, screen
blank/DPMS and notifications+DND before release; document the rest as out of scope in the release
notes rather than leaving the gap silent.

### 28. Accessibility stops at scale and contrast; HiDPI renders soft
`cube-settings.c:478` exposes only `{ui_scale, terminal_scale, strong, reduced_motion}`. There is no
AT-SPI, no accessible name on any widget (grep for `atk|AtkObject|accessible_name` → zero), no
screen-reader announcement of errors, and no large-cursor option. Worse, **neither `cube-settings`
nor `cube-status` ever calls `wl_surface_set_buffer_scale` or fractional-scale-v1** (grep → zero):
the bar and the settings window render one buffer pixel per logical pixel and are composited up, so
text is blurry on a scale-2 panel, and a 160% "interface scale" merely oversamples on a scale-1 one
(`cube-status.c:1166-1169`, `cube-settings.c:1999-2008`). Keyboard navigation and a
machine-checked >4.5 contrast ratio (`status-theme-test.c:16-27`) do exist and are good.
**Fix:** set the buffer scale from the output's scale for both clients, add a large-cursor option,
and add accessible names to the focus/move handlers so a screen reader has something to announce.

### 29. Every error is a 5-second toast, and privileged stderr is discarded
`cube-settings.c:749-755` sets `ui.toast_until = now_s() + 5`, and `job_finish` (`:316`) surfaces a
failure as one line before `refresh_page(true)` reloads the tables. No error list, no notification,
no log. In the bar, `job_start` (`cube-status.c:275-279`) dups helper stderr to `/dev/null`, so a
missing or mis-installed sudoers rule (which would print "a password is required") is invisible —
the bar just shows the last known value forever. Exit status is never collected: `job_parse`
(`:1129-1156`) resets `vol_ok`/`bright_ok`/`nprofiles` and repopulates data, and grep for
`waitpid|WEXITSTATUS` returns nothing. A failed `net-connect` sets `wifi_label` to `"Connecting…"`
(`:1481`) and never resolves it. **Fix:** a persistent notification list in Settings, a status
indicator in the bar for "last action failed", helper stderr appended to a log file, and exit-status
propagation on every job.

### 30. The admin password is re-prompted for every single privileged action
`cube-settings.c:236-243` re-opens the password modal whenever `p->priv == P_PASS && !g_have_password`,
and `job_launch` (`:201-205`) wipes the credential with `explicit_bzero` after each spawn and clears
`g_have_password`. Changing firewall → service → group → hostname prompts four times in a row, even
though sudo's own timestamp would authorise all four. Meanwhile the top bar calls
`sudo -n power-action ...` with no prompt at all. The security gain is marginal (sudo already
caches) and the UX cost is a password field on nearly every row of four pages.
**Fix:** replace the per-action prompt with one `sudo -v` credential ticket obtained once per
Settings session, kept in memory, wiped on window close, with a visible countdown.

### 31. The network stack's limits are real and are not surfaced to the user
`net/README.txt:20-22` is admirably honest — WPA2-PSK/CCMP/IPv4 only, no WPA3/SAE, no 802.1X
enterprise, no WPS, no AP mode, no IPv6 (`dhcpcd -4` at `:2468`, `read_resolvers()` accepts only
`IPv4Address` at `:228-240`, `tcp_probe` is `AF_INET` at `:269`). But the *user-facing* behaviour on
those networks is poor:

* A wrong passphrase produces no distinct diagnosis — `diagnostic()` (`:166-168`) only ever reports
  `association-failed`; the "check the saved passphrase" hint goes to the journal (`:1826`, `:1846`).
* The retry ladder `SCAN_DELAYS = (1, 3, 5, 10)` (`:1493`) repeats **forever**, and because "bans
  never strand the only AP" (`:1729`) a single AP is retried every 10 s indefinitely — enough to
  trigger AP-side rate-limiting.
* Hidden-SSID discovery probes only the **first three** saved networks
  (`:1701  self.driver.trigger_scan(list(self.profiles)[:3])`).
* `profiles.json` is written `0o644` (`:574`) and `status.json` publishes the SSID into a `0755`
  runtime dir (`:694-696`, `:2315`) — which sits oddly beside the "hide network names" privacy
  feature.
* `networks.json.backup-<time_ns>` files (`:566-572`) accumulate without bound, and the legacy
  `wpa.conf` is read but never removed (`:589` says to delete it; nothing does — it is still on disk
  here with three unbounded backups beside it).

**Fix:** a `bad-passphrase` status the bar can display, a bounded retry budget with a "needs
attention" state, directed rescans on failure for all saved networks, mode `0600` for the profile
store, and pruning of backups and the legacy file.

### 32. No i18n, no icons, no entries, no docs — and dead weight in the tree
Four separate packaging gaps that will be read as "unfinished":

* **Localisation:** zero `gettext`/`setlocale`/`bindtextdomain` calls and no `.po`/`.pot` files.
  Because `setlocale(LC_TIME, "")` is never called, `strftime(date, ..., "%A %-d %B",
  ...)` (`cube-status.c:873`) and the clock (`:1056`) are hardwired to English regardless of the
  user's locale. All UI strings are compile-time English literals (`cube-settings.c:730-731`).
* **Launchers and icons:** exactly one `.desktop` file, generated at install time
  (`INSTALL-USER.py:72-76`) and borrowing `Icon=preferences-system` from the host theme.
  `cube-privacy`, `whitecube-doctor`, `whitecube-install` and the desktop itself have no entry.
  No icon asset ships with the project (glob `**/*.png`/`**/*.svg` in source → none), and the bar can
  only load PNG icons — SVG-only hicolor icons fall back to a drawn cube
  (`cube-status.c:418`, `:426-443`). Fonts are hardcoded to `"DejaVu Sans"`/`"DejaVu Sans Mono"`
  with no fallback chain (`whitecube.c:691`, `cube-settings.c:787`, `cube-status.c:462`).
* **Documentation:** no man pages, no root `README`, no `CHANGELOG`. `net/README.txt:200` claims
  "100 tests" and `:82` "128 more checks" against 118/162 actual; `:231` admits the *installed*
  copy of the network README on the target still describes `resolvectl`/systemd-resolved and a
  three-attempt budget — a wrong document ships. `MAINTENANCE.md:193` says `tools/clean.sh` "removes
  __pycache__ and build/" but `clean.sh:10-14` only removes a whitelist and never removes `build/`.
* **Dead weight:** `net/legacy/` ships six retired revisions (kept deliberately, for the replay
  test — fine, but half of the only network document describes them); `DIFF.txt` is a
  revision-2 changelog; ~20 review PNGs, 8 logs, three patch files, three whole backup source trees
  and three Meson build trees sit in `build/`; three `whitecube-backup-*.tar.gz` and a
  `whitecube.bak-*` directory sit in `$HOME`.

**Fix:** `setlocale` + gettext scaffolding, ship a 16-px and 32-px icon set and one `.desktop` per
component, add a font fallback list, write the root `README.md`/`CHANGELOG.md`/man pages, correct
the test counts in `net/README.txt`, and move build debris out of `build/` into a dated archive
directory outside the repo.

---

## Suggested order of attack

| Wave | Items | Rationale |
|---|---|---|
| 1 | 10, 11, 1, 2, 3 | Nothing else matters if it is unlicensed, unbuildable and root-exploitable. |
| 2 | 4, 5, 6, 7, 8, 9 | Privilege hygiene and a first boot that actually works on a clean disk. |
| 3 | 12, 13, 14, 15, 16, 17, 20 | Correctness of the running desktop. |
| 4 | 21, 22, 25, 26 | One build system, one test entry point, verifiable rollback, a real version. |
| 5 | 23, 24, 18, 19, 31 | Safe system mutation, honest multi-monitor and network limits. |
| 6 | 27, 28, 29, 30, 32 | The user-visible polish that decides whether a release feels finished. |
