# Whitecube Autonomous Sprint Log
<!-- Format: ## [YYYY-MM-DD] Component Name | Status: DONE | Commit: <hash> -->


## [2026-11-15] src/whitecube.c & src/tab-panel.h | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - `src/tab-panel.h`: added `wc_panel_has_scrollbar()`, `wc_panel_on_scrollbar()`, `wc_panel_scroll_at()`, and `wc_panel_on_close_for_count()` for scrollbar-aware click-to-scroll and close-button hit testing.
  - `src/whitecube.c`: wired panel scrollbar clicks (`wc_more_press()`), quick digit selection `1..9`/`KP_1..KP_9` (`wc_panel_key()`), multi-output `wc_panel_close()` (including session lock & workspace switches), half-screen snap shortcuts (`Alt+Ctrl+Shift+Arrows`), center (`Alt+0`), maximize toggle (`Alt+M`), secondary-output `tab_tree` disabling in `wc_retile()`, and CLI flags (`-v|--version`, `-h|--help`, `-c|--check-config`, exit 2 on invalid option).
  - Hardening: added NULL/OOM allocation guards in `server_new_keyboard`, `server_new_output`, `server_new_toplevel`, `server_new_layer`, `wc_idle_inhibited`, whitespace check in `spawn`, and parent-tree ownership check (`node->parent == s->windows`) in `cursor_button`.
  - Docs/Build: updated `data/keys.txt` and `meson.build` (`-DWHITECUBE_VERSION`, CLI tests).
- **Tests & Verification**:
  - Extended `tests/compositor-test.c` (`test_tab_overflow`, `test_window_snap_center_maximize`, `test_idle_inhibit_and_config_syntax`, `test_panel_digit_shortcuts_and_lock`) and `tests/hotplug-test.c` (panel scrollbar click, lock closes panel, secondary output `tab_tree` toggle).
  - Full `./BUILD.sh` inside `bwrap` passed (all core, bus, portal, app, packaging, and `wc-suite` tests green).

## [2026-11-15] src/cube-status.c & src/cube-ca.c | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - `src/cube-field.h`: added `pause [on|off]`, `step [1..1000]`, `pulsar` (2D center stamp), `seeds` (sorted preset listing), and `delete [name]` IPC control commands in `apply_control_msg()`, extracted shared `seed_dir()`/`seed_delete()` helpers, and updated control help text.
  - `src/cube-ca.c`: added `-h|--help` and `-v|--version` (`WHITECUBE_VERSION`), finite/range validation (`[0, 6]`) on `--preview-zoom`, `signalfd` (`SIGTERM`/`SIGINT`/`SIGHUP`) handling in both supervisor and single-instance loops for clean child shutdown and control-socket unlinking, and Wayland shm/pool/buffer/surface/layer OOM & bounds guards.
  - `src/cube-status.c`: added `-h|--help` and `-v|--version`, `Ctrl+W`/`Ctrl+BackSpace` word-erase (`erase_last_word`) and `Delete` query clear with scroll reset in launcher/search input, thread-safe `localtime_r()`, multi-battery sysfs aggregation (`battery_from_dir`/`battery`) filtering out `Device`-scoped HID batteries and non-`Battery` supplies, `signalfd` shutdown cleanup, and shm/cairo/layer NULL & overflow guards.
  - Build/Tests: updated `meson.build` (`-DWHITECUBE_VERSION` for `cube-ca`/`cube-status` plus CLI help/version tests), `field_self_test()`, `status_selftest()` (temp sysfs multi-battery/HID test), and `tests/status-jobs-test.c` (`Ctrl+W`/`Delete`/scroll-reset checks).
- **Tests & Verification**:
  - Full `./BUILD.sh` and `packaging/check-tree.py packaging/out/stage --sources src` passed inside `bwrap` with zero warnings or failures.

## [2026-11-15] src/cube-settings.c & libexec/cube-settings-helper | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - `libexec/cube-settings-helper`: filtered out `scope=Device` peripheral batteries in `power()`, clamped reported backlight percentage to `[0, 100]`, updated `brightness()` to pick the first sysfs backlight with `max_brightness > 0` and clamp written brightness to `[1, max]`, unified `system()` to read `HOSTNAME`/`LOCALTIME`/`TIMEZONE` constants, and added `-h|--help` and `-v|--version` CLI flags.
  - `src/cube-settings.c`: added `-v|--version` (`WHITECUBE_VERSION`), bounds-safe `-h|--help` handling via `settings_usage()`, UTF-8-aware `Ctrl+W` (`erase_last_word()`) and `Delete` clear shortcuts in text inputs (`key_press()`/`handle_key()`), multi-battery label disambiguation and `[0, 100]` percent clamping in `page_power()`, `signalfd` (`SIGTERM`/`SIGINT`/`SIGHUP`) handling with full `cleanup_settings()` teardown (passwords, jobs, tables, font cache, Wayland/xdg/xkb/output-manager objects), and OOM/status guards in `outputs_apply()`, `get_buffer()`, `redraw()`, and `render_png()`.
  - Build/Tests: updated `meson.build` (`-DWHITECUBE_VERSION` for `cube-settings`/`settings-test`, `settings-cli-help`, `settings-cli-version`), extended `tests/settings-test.c` (`Ctrl+W`, `Delete`, CLI exit checks), and added `HelperPowerAndSystem` tests in `tests/test-settings-transactions.py`.
- **Tests & Verification**:
  - Full `./BUILD.sh` and `packaging/check-tree.py packaging/out/stage --sources src` passed inside `bwrap` with zero warnings or failures.

## [2026-11-15] src/cube-audio/ | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - `src/cube-audio/cube-audio.c`: added `-h|--help`, `-v|--version` (`WHITECUBE_VERSION`), `--json` output across `cards`/`status`/`volume`/`mute`/`info`, `-c|--control CTL` selector, hardware-free `info [--json] FILE.wav` metadata subcommand, 64-bit IEEE float WAV decoding (`wav_frame()`/`wav_open()`), resampler EOF tail flushing (`conv_flush()`), container-aware TLV dB parsing (`tlv_parse_db()` with `SNDRV_CTL_TLVT_CONTAINER` recursion), multi-channel percentage averaging in `volume_get()`, and strict bounds/OOM/overflow guards (`MAX_ELEMS`, channel cap `<= 128`, period/buffer checks, fd cleanup on error paths).
  - Build & Packaging Sync: added PIE/stack-protector/`_FORTIFY_SOURCE`/RELRO flags in `src/cube-audio/build.sh`; integrated `cube-audio` and `test-cube-audio` into `meson.build`, `BUILD.sh`, `packaging/install-tree.sh`, `packaging/check-tree.py`, `packaging/check-libraries.py`, `packaging/verify-deb.py`, `packaging/build-deb.sh`, `packaging/arch/PKGBUILD`, and `tests/test-packaging.py`.
- **Tests & Verification**:
  - Extended `src/cube-audio/test-cube-audio.c` (self-contained non-WAV fixture, 64-bit float WAV decoding, duplicate `fmt ` rejection, `parse_target()`, `tlv_parse_db()` container/overflow cases, `conv_flush()` same-rate and resampled tail flushing).
  - Verified ASan/UBSan standalone test suite, `./BUILD.sh`, `packaging/check-tree.py` (233 paths ok), and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] bin/ utilities (`whitecube-session`, `whitecube-doctor`, `cube-preferences`, `cube-privacy`, `whitecube-terminal`, `whitecube-desktop`, `whitecube-suspend`, `whitecube-next-wallpaper`) | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - `bin/whitecube-doctor`: added `cube-audio` across `install_map()`, `check_xfree()`, `check_package()`, and `check_shadowing()`; added new read-only `--check audio` check (`check_audio()`) inspecting `/dev/snd/controlC*` permissions and configured `alsa`/`pipewire` backend availability; added `--version`; updated `EXTERNAL_REFERENCES` in `packaging/check-tree.py`.
  - `bin/whitecube-session`: added `-v|--version`, `GDK_BACKEND=wayland` default in `session_environment()`, `RuntimeError` hardening in `quiet_gpu_devices()`, non-destructive `active_session_pid()` live-lock telemetry in `--check` JSON output, and owned `bus` socket cleanup when `whitecube-bus` exits.
  - `bin/cube-privacy`: integrated native `cube-audio mute --input` JSON fallback into `microphone()` and `set_value("microphone_muted", ...)` for systems without `wpctl`/`amixer`; added `--help` and `--version`.
  - `bin/cube-preferences`: added `-v|--version`, `--json` output for `list`/`check`/`--dry-run`, `--dry-run` validation (skipping `dbus-run-session`/writes), negative integer argument safety via `_parse_cli_flags()`, and directory rejection before file reads.
  - `bin/whitecube-suspend`: added `-v|--version`, `--lock-only` (lock without suspending), and bounded `--timeout` (`1..120` s).
  - `bin/whitecube-desktop`: added non-interactive CLI flags (`--show`, `--json`, `--set-ui-scale`, `--set-terminal-scale`, `--contrast`, `--reduced-motion`, `--list-backups`, `--backup`, `--version`) while preserving interactive TTY menu mode.
  - `bin/whitecube-terminal`: added `#`/`;` inline comment stripping and sequential `[main]` `font=` override handling plus missing `foot` install hint.
  - `bin/whitecube-next-wallpaper`: hardened POSIX iteration across `.jpg/.jpeg/.png/.webp` (whitespace-safe), eliminated hardcoded `/tmp/swaybg.log` & `/tmp` state files in favor of private `$XDG_RUNTIME_DIR`/`$XDG_STATE_HOME/whitecube`, validated state indices atomically, added `--next`/`--prev`/`--current`/`--list`/`--help`, and verified `swaybg` before `pkill`.
- **Tests & Verification**:
  - Extended `tests/test-doctor.py` (47 tests), `tests/test-session.py` (23 tests), `tests/test-privacy.py` (31 tests), `tests/test-preferences.py` (14 tests), `tests/test-suspend.py` (8 tests), and `tests/test-desktop-improvements.py` (12 tests).
  - Verified full `./BUILD.sh`, `packaging/install-tree.sh`, and `packaging/check-tree.py` (235 paths ok) inside `bwrap`.

## [2026-11-15] net/cube-net, cube-net-dns, whitecube-net-switch (`net-off` / `net-on`) | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - `net/cube-net`: added configurable same-SSID signal-threshold BSS roaming with multi-sample hysteresis (`roam_threshold_dbm`, `roam_margin_dbm`, `roam_cooldown_s`, `roam_samples`) validated in `device_config()` and wired through `station_options()` into `Station` without disrupting healthy links; sorted `scan` results descending by `signal_dbm`; added pre-passphrase SSID UTF-8 length/NUL/duplicate checks in `add_profile()`; added `--version` and structured `selftest --json`.
  - `net/whitecube-net-switch` (`net-off` / `net-on`): added `--version`, `--json` output for `--status`, `--dry-run`, and lock state mutations; extracted live chain drop packet/byte counters from the existing `nft -a -j` snapshot in `Switch.status_data()`; hardened `configuration()` with `O_NOFOLLOW`, `S_ISREG`, and a 4 KiB read cap.
  - `net/cube-net-dns`: replaced predictable `.tmp` writes with `tempfile.mkstemp` atomic creation + automatic parent directory creation and cleanup on error; accepted comma/whitespace-separated DNS servers; added CLI `--check`/`--status`, `--json`, `--resolv`, and `--version` via `inspect_resolv()` while preserving `dhcpcd` hook `main()` signature.
  - `bin/whitecube-doctor`: added `check_dns_lease()` (verifying active `/run/whitecube-net/resolv.conf` resolvers during completed IPv4 association) and `check_lockdown()` (detecting active persistent `net-off` marker) under `--check network`.
- **Tests & Verification**:
  - Extended `net/test_cube_wifi.py` (roaming config validation, hysteresis roaming simulation, signal-sorted scan, `selftest --json`, `--version`), `net/test_cube_net.py` (`HookTests` comma servers, missing parent dir, `--check --json`, `--version`), `net/test_net_switch.py` (symlink/oversized config rejection, `--version`, `--dry-run --json`, `--status --json` counters), and `tests/test-doctor.py` (`test_dns_lease_and_lockdown_checks`).
  - Verified targeted suites (`test_net_switch.py` 33 ok, `test_cube_net.py` 130 ok, `test_cube_wifi.py`, `test-doctor.py` 48 ok), full `./BUILD.sh`, `packaging/install-tree.sh` (634 files), `packaging/check-tree.py` (235 path references ok), and `tests/test-packaging.py` (61 tests ok).

## [2026-11-15] boot/ (GRUB / Plymouth / tty1 black cube theme & recovery polish) | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - `boot/gen-boot-theme.py`: replaced bare positional CLI with `argparse` while keeping `OUT_DIR [WIDTH HEIGHT]`, added `--seed`, `--scale` (1..4 HiDPI), `--verify` (pure-stdlib PNG IHDR dimension verification and `whitecube.script` `Image(...)` cross-check), `--json`, and `--version`, sharper 9-slice frame hairlines (`frame_tile`), dimension bounds (`320..7680` x `240..4320`), and clean Pillow dependency error reporting (`_require_pillow()`).
  - `boot/install-boot.sh`: added `--status`, `--json`, `-n`/`--dry-run`, `--purge`, `--resolution WxH`, `--scale N`, `--timeout SEC`, `--no-issue`, `-V`/`--version`, `-h`/`--help`, `WC_BOOT_ROOT` sandboxed prefix support, preflight checks before touching backups, safe atomic `awk` `ENVIRON` variable updates in `set_var()`, and `/etc/issue` backup/absent state restoration.
  - `boot/whitecube.script`: clamped progress to `[0, 1]`, added centered `dialog.prompt` text and width-clamped passphrase bullets (`dialog.max_bullets`) so long passphrases stay inside `entry.png`, centered status messages in `display_message_callback()`, and hid password dialogs in `quit_callback()`.
- **Tests & Verification**:
  - Added `tests/test-boot.py` (5 offline tests covering PNG asset/script consistency, HiDPI rendering & verification, bounds & missing-Pillow diagnostics, `sh -n`, `--dry-run`, and end-to-end sandboxed install + `--status --json` + `--revert --purge`) and wired it into `BUILD.sh`.
  - Verified full `./BUILD.sh`, `packaging/install-tree.sh` (634 files), `packaging/check-tree.py` (235 path references ok), and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] net/cube-net Hardware Generalization (Multi-Vendor PCIe/USB Wi-Fi) | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - Removed the single-dongle hardware lock (`USB 2001:3329` / `MAC 88:76:b9:f2:9d:29` / `rtw88_8822cu`) from `net/cube-net`: `DEFAULT_DEVICE` now defaults to `{"mode": "auto"}` to auto-detect any Linux `cfg80211`/`nl80211` wireless interface (PCIe, USB, SDIO) while excluding loopback and wired Ethernet interfaces (`is_wireless()`, sysfs `wireless`/`phy80211`/`DEVTYPE=wlan`).
  - Generalized `device_config()` and `matches()` to validate and accept optional user-configured adapter pins (`interface`, `usb_id`, `mac`, `driver`, `bus`) without restricting users to one device model or MAC.
  - Added `cube-net devices` (`list_devices()`) to inspect detected wireless and wired adapters, and `sudo cube-net select <iface> [--auto|--by-interface]` (`select_device()`) to manage optional `/etc/whitecube/network/device.json` pinning while preserving country/power-save/roaming tuning.
  - Updated `diagnostic()` (`missing-device`, `driver-problem`, `identity-mismatch`, `identity-ambiguous` with `pci_wireless_devices()` + `usb_devices()` hardware presence checks), `audit()` (verifying `cfg80211` and detected/configured driver modules rather than only `rtw88_8822cu`), `status()`, `selftest()`, `run_service()`, `takeover()`, `net/whitecube-network.service` description, and `src/cube-status.c` (`waiting-for-device` alongside `waiting-for-dongle`).
- **Tests & Verification**:
  - Updated and extended `net/test_cube_net.py` (133 tests) and `net/test_cube_wifi.py` (165 tests) covering auto-detected PCIe Intel `iwlwifi`, arbitrary USB Wi-Fi dongles (`0e8d:7961` / `mt7921u`), wired Ethernet exclusion, multi-radio ambiguity + `select_device` pinning/revert, invalid config rejection, and `audit()` on generic kernels.
  - Verified full `./BUILD.sh`, `packaging/install-tree.sh` (634 files), `packaging/check-tree.py` (236 path references ok), and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] workspace/ (`whitecube-web` Suite) | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - Added `WorkflowStore.delete(ident)` (`workspace/jobs.py`) and wired `POST /api/workflows/delete` & `DELETE /api/workflows`, plus `POST /api/backups/delete` & `DELETE /api/backups` with `FileNotFoundError -> 404` mapping on backup endpoints (`workspace/server.py`).
  - Added `--version` (`0.12.0`) to `workspace/server.py` and `workspace/health.py`, `check --strict` CLI validation exiting non-zero on manifest errors, port `[1, 65535]` validation, empty/`.` guards in `safe_file()`, and `network-devices` action routing.
  - Replaced single-dongle (`2001:3329` / `Dongle`) copy and fallback metadata across `workspace/server.py`, `workspace/health.py`, `workspace/catalog.json`, `workspace/bin/network-status.sh`, `workspace/web/index.html`, and `workspace/web/app.js` with hardware-generic Wi-Fi adapter diagnostics (`wifi-dependencies`, `wifi-adapter`, `wifi-connection`, bus/driver/MAC/IPv4/route fields, `Adapters` action, hyphen/underscore normalization, and workflow/backup Delete UI controls).
  - Fixed `python3 -I` isolated-mode import failures across `workspace/server.py`, `workspace/health.py`, `workspace/jobs.py`, and `workspace/desktop_state.py` via `sys.path` guards, and closed a dangling-symlink bypass in `workspace/desktop_state.py` (`_read()`).
- **Tests & Verification**:
  - Created `tests/test-workspace.py` (8 comprehensive unit and live loopback HTTP tests covering `--version`, `check --strict`, `safe_file` traversal/symlink/dotfile confinement, public catalog redaction, `DesktopStore` bounds/dangling-symlink rejection, `WorkflowStore` DAG ordering & deletion, `RecoveryConsole` credential exclusion & backup/restore/verify/delete, generic Wi-Fi health checks, frontend syntax, HTTP token/Host/Origin/Sec-Fetch-Site isolation, separate app origin rewriting, and delete APIs) and wired it into `BUILD.sh`.
  - Verified `tests/test-workspace.py` (8 tests ok), full `./BUILD.sh`, `packaging/install-tree.sh`, `packaging/check-tree.py`, and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] apps/cube-browser | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - Added `--check --json` diagnostic output (`health_check(..., json)` in `apps/cube-browser/src/main.c`) reporting browser version, `cube-renderd`/`cube-netd` availability, CA bundle status, Landlock ABI, Wayland socket presence, and overall readiness without creating profile/config directories, plus strict argument rejection when `--json` is used without `--check`.
  - Added `--timeout SECONDS` (`0.1`–`3600.0`, default `45.0`) for `--render` and `--dump` in `apps/cube-browser/src/main.c` with bounds/syntax validation (`--timeout` requires `--render`/`--dump`).
  - Added `Ctrl+K` / `F6` address-bar focus shortcuts (also dismissing open panels), `Alt+Home` home navigation, and the built-in `cube:shortcuts` internal reference page (`apps/cube-browser/src/home.h`, `src/url.c`, `src/storage.c`, `src/main.c`).
  - Wired `check_browser(env)` and the `'browser'` check group into `bin/whitecube-doctor`, checking staged workers or invoking `cube-browser --check --json`.
- **Tests & Verification**:
  - Extended `apps/cube-browser/tests/test_app.py` (`test_check_json_timeout_and_shortcuts_page`), `apps/cube-browser/tests/test_core.c`, `apps/cube-browser/tests/test_storage.c`, and `tests/test-doctor.py` (`test_browser_check_reports_staged_dev_and_json_health`).
  - Verified targeted Meson tests (`cli-and-installer`, `ipc-and-address`, `storage`), `tests/test-doctor.py` (49 tests ok), full `./BUILD.sh`, `packaging/install-tree.sh`, `packaging/check-tree.py`, and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] apps/cube-player | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - Added `--check` and `--json` runtime readiness probe in `apps/cube-player/src/main.c` (`wayland_available()`, GStreamer plugin/sink/decoder readiness, version, and overall status) executing before `state_init()` so diagnostics never create XDG config/state files, while rejecting standalone `--json` with exit code 2.
  - Added CLI playback flags `--volume 0..100`, `--speed 0.25..3.0`, `--shuffle`, and `--repeat off|all|one` with strict range/value validation (`exit 2`), plus keyboard shortcuts (`+`/`=` and `-` for +/-5% volume, `Page_Up`/`Page_Down` for +/-60s seek, `C` to clear A-B loop), updated `help_panel()`, and extended `report()` with `repeat`, `shuffle`, `sub_delay`, `has_loop_a`, and `has_loop_b`.
  - Hardened M3U/PLS playlist loading in `apps/cube-player/src/state.c` (`media_playlist_read()` opening via `O_RDONLY | O_CLOEXEC | O_NONBLOCK` + `fstat()` `S_ISREG` guard before `fdopen()` so FIFOs/non-regular files reject immediately without blocking; `playlist_item()` allowing only `http`/`https`/`file` URIs or existing regular local files) and switched `apps/cube-player/tests/test_state.c` fixture writes to `g_file_set_contents_full(..., G_FILE_SET_CONTENTS_NONE, 0600, NULL)`.
- **Tests & Verification**:
  - Extended `apps/cube-player/tests/test_state.c` (unsafe URI scheme filtering & FIFO playlist rejection), `apps/cube-player/tests/test_ui.c` (`C` A-B loop reset & `+`/`-` volume keys), and `apps/cube-player/tests/test_install.py` (`--check --json`, standalone `--json` exit 2, non-creation of XDG dirs on `--check`, invalid `--volume`/`--speed`/`--repeat` exit 2).
  - Verified full end-to-end `./BUILD.sh`, `packaging/install-tree.sh`, `packaging/check-tree.py`, and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] apps/cube-editor | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - Added GTK-free `--version` (`0.6.0`) and `--cli` dispatch to `engine.py` in `bin/cube-editor` (matching `packaging/cube-editor-studio`).
  - Added headless CLI subcommands `presets`, `recent`, `qemu-command`, and `doctor --strict` in `apps/cube-editor/engine.py`, plus shared `RECIPES` tuple reused by `apps/cube-editor/app.py` (`VERSION` preserved verbatim for `build-deb.sh` AST checks) and updated `apps/cube-editor/packaging/cube-editor.1`.
  - Added `read_bounded_json()` (`O_RDONLY | O_CLOEXEC | O_NONBLOCK`, `fstat()` `S_ISREG`, and 1 MiB `MAX_JSON_BYTES` cap) in `apps/cube-editor/engine.py` and wired it into `import_recipe()`, `Project._read()`, `recent_projects()`, and `app.py`'s `load_preferences()`; hardened `Engine.qemu_command()` to reject `bool`/non-`int` memory values and commas in ISO paths.
  - Wired read-only `check_editor(env)` and the `'editor'` check group into `bin/whitecube-doctor`.
- **Tests & Verification**:
  - Extended `apps/cube-editor/tests/test_cli_workflows.py` (testing `presets`, `recent`, `doctor --strict`, `qemu-command` validation, FIFO/oversize JSON rejection in `import_recipe()` & `Project._read()`, and `bin/cube-editor --version`/`--cli`) and `tests/test-doctor.py` (`test_editor_check_reports_staged_and_doctor_health`).
  - Verified `apps/cube-editor/tests` (`DesktopWorkflowTests` skipped without GTK display), `tests/test-cube-editor-ui.py`, `tests/test-doctor.py` (50 tests ok), and full end-to-end `./BUILD.sh`, `packaging/install-tree.sh`, `packaging/check-tree.py`, and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] apps/cube-train | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - Added GTK-free `--version` and `--cli` dispatch to `bin/cube-train` without requiring Wayland/GTK libraries, plus `--json` support on `--list-runs` and `--status` and the new `--inspect-run RUN_ID [--json]` command in `apps/cube-train/cli.py` (with updated `--help` and `apps/cube-train/README.md`).
  - Hardened `apps/cube-train/engine.py` (`read_json()`, `tail_text()` via `_open_regular_readonly()`) and `apps/cube-train/pipeline/train_folder.py` (`_read_regular_json()`) with `O_RDONLY | O_CLOEXEC | O_NONBLOCK` and `stat.S_ISREG` guards to reject FIFOs/non-regular files without blocking.
  - Added pre-Torch `--resume` validation in `apps/cube-train/pipeline/train_folder.py` so missing/corrupt `run.json` or invalid dimensions fail cleanly via `parser.exit(1, ...)` instead of unhandled tracebacks, and guarded zero-duration division in `apps/cube-train/pipeline/benchmark.py`.
  - Wired read-only `check_trainer(env)` and the `'trainer'` check group into `bin/whitecube-doctor`.
- **Tests & Verification**:
  - Extended `apps/cube-train/tests/test_comparison_cli.py` (`--list-runs --json`, `--status --json`, `--inspect-run [--json]`, and `bin/cube-train --version` / `--cli`), `apps/cube-train/tests/test_engine_improvements.py` (FIFO non-blocking rejection in `read_json`/`tail_text` and clean error exit on corrupt `--resume` `run.json`), and `tests/test-doctor.py` (`test_trainer_check_reports_staged_and_doctor_health`), and wired `test_comparison_cli.py` and `test_diagnostics.py` into `BUILD.sh`.
  - Verified full end-to-end `./BUILD.sh`, `packaging/install-tree.sh`, `packaging/check-tree.py`, and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] apps/cube-strap | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - Added GTK-free `--version` (`Cube Strap 1.0.0`) and `--cli` delegation to `whitecube-vm` in `bin/cube-strap`, plus `-v`/`--version`, `set` (`update_vm` for default RAM/vCPU updates under `vm.lock`), and `--json` output on `import` and `remove`/`rm` in `bin/whitecube-vm` (and updated `apps/cube-strap/README.md`).
  - Hardened `apps/cube-strap/strap.py` with bounded `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` + `S_ISREG` checks (`_read_regular_json()` and `_copy_sparse()`), private `0o600` atomic JSON writes (`_write_private_json()` on `vm_meta`, history, and draft state), and strict `_validate_vm_resources()` bounds/bool checks across `import_vm()`, `clone_vm()`, `update_vm()`, and `vm_run_argv()`.
  - Wired read-only `check_vm(env)` and the `'vm'` check group into `bin/whitecube-doctor`.
- **Tests & Verification**:
  - Extended `apps/cube-strap/tests/test_whitecube_vm.py` (`--version`, `set --memory/--cpus --json`, lock/bounds rejections, `--json` on `import`/`remove`, and `bin/cube-strap --version` / `--cli check --json`), `apps/cube-strap/tests/test_strap.py` (`Hardening` tests for `0o600` metadata, FIFO/non-regular file rejection, 1 MiB JSON cap, and resource validation), and `tests/test-doctor.py` (`test_vm_check_reports_staged_and_qemu_readiness`).
  - Verified `apps/cube-strap/tests` (32 tests ok), `tests/test-cube-strap-ui.py`, `tests/test-doctor.py` (52 tests ok), and full end-to-end `./BUILD.sh`, `packaging/install-tree.sh`, `packaging/check-tree.py`, and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.

## [2026-11-15] apps/chrootables | Status: DONE | Commit: (atomic)
- **Extensions & Enhancements**:
  - Added Qt/Wayland-free `--version` (`1.3.0` from `chrootables/__init__.py`) and `--cli [ARGS...]` pass-through to the bundled `chrootable-manager` in `bin/cube-chrootables` (plus updated `apps/chrootables/README.md`).
  - Hardened `apps/chrootables/backend.py` (`load_settings()` opening with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK`, `S_ISREG`, <256 KiB cap, and strict `type() is int` bool rejection; `save_settings()` via atomic `tempfile.mkstemp()` `0o600` + `os.fsync` + `os.replace`; `import_args()` validating regular non-symlink files via `O_NOFOLLOW | O_NONBLOCK` + `S_ISREG`) and switched `apps/chrootables/chrootables/app.py`'s `load_demo()` to bounded `read_regular()`.
  - Wired read-only `check_chrootables(env)` and the `'chrootables'` check group into `bin/whitecube-doctor`.
- **Tests & Verification**:
  - Extended `tests/test-chrootables.py` (`bin/cube-chrootables --cli --version` and `--cli --capabilities --json`, `0o600` `save_settings()`, FIFO/bool rejection in `load_settings()` and `import_args()`) and `tests/test-doctor.py` (`test_chrootables_check_reports_staged_and_manager_readiness`).
  - Verified `tests/test-chrootables.py` (all unit/backend/helper/Qt screenshot checks), `tests/test-doctor.py` (53 tests ok), and full end-to-end `./BUILD.sh`, `packaging/install-tree.sh`, `packaging/check-tree.py`, and `tests/test-packaging.py` (61 tests ok) inside `bwrap`.
