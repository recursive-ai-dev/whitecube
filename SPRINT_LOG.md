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
