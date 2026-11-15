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
