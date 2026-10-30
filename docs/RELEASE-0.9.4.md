# Whitecube Desktop 0.9.4

Whitecube 0.9.4 hardens output hotplug safety, Wayland event-loop responsiveness,
allocation error handling, audio metadata validation, and configuration parsing
across 12 prioritized engineering improvements. The supported binary release is
Debian 13 (Trixie), amd64.

## Native desktop, audio, and utility hardening

- **Compositor output hotplug and scene-graph lifecycle (`whitecube-compositor`)**:
  `output_destroy` now destroys `o->bg_tree` and `o->tab_tree` scene trees so
  repeated monitor hotplug cycles do not leak scene nodes, drops toplevel tab
  nodes (`t->tab_border`, `t->tab_cube`, `t->tab_label`) bound to a disconnected
  output, and triggers `wc_retile` so `wc_toplevel_ensure_tab` migrates window
  tabs to the remaining primary output. `layer_commit` resolves
  `s->layer_surface->output` (falling back to the primary output) instead of
  dereferencing a cached output pointer.
- **Race-free Wayland supervisor loop and reentrant terrain (`cube-ca`)**:
  `supervisor_main` now uses the canonical `wl_display_prepare_read` /
  `wl_display_flush` / `wl_display_read_events` / `wl_display_cancel_read`
  protocol so queued Wayland events never stall behind timer wakeups. `cos_land`
  allocates its heightfield on the stack for reentrancy, and `cosmos_offscreen`,
  `preview`, and `preview_zoom` release all offscreen simulation buffers via
  `cosmos_offscreen_free` on early error exits.
- **Non-blocking helper reaping and OOM safety (`cube-settings`, `cube-status`)**:
  `job_read` in `cube-settings` removes the synchronous 2 ms `usleep(200)` spin
  loop when child pipes close before process exit, instead using a short event-loop
  poll timeout while EOF-drained jobs await `waitpid(..., WNOHANG)` reaping.
  `table_set` checks `strdup` and `realloc` before mutating table state, and
  `paint` in `cube-status` validates `calloc` and `wl_shm_pool_create_buffer`
  with full shared-memory cleanup on failure.
- **ALSA TLV 64-bit bounds and WAV validation (`cube-audio`)**: `elem_db`
  computes ALSA `SNDRV_CTL_TLVT_DB_SCALE` bounds via `db_scale_range` in 64-bit
  signed arithmetic (`int64_t`) with span and step validation to prevent signed
  32-bit overflow UB. `wav_open` rejects duplicate `fmt ` chunks, `EOF` on odd
  chunk padding, and zero-frame `data` chunks (`data_left < align`).
- **Cube Player and configuration consistency (`cube-player`, `cube-preferences`, `whitecube-session`)**:
  `cube-player` updates `p->speed` and `a->state.speed` deterministically on
  transport-bar speed clicks, frees initialized state on invalid `--theme` CLI
  arguments, and guards `media_title` against empty-host stream URLs.
  `whitecube-compositor`, `cube-status`, `cube-preferences`, and
  `whitecube-session` strip key/value whitespace, ignore indented `#` comments,
  drain overlong lines (`> 255` bytes), clamp `tab_h` to `[20, 80]`, and require
  exact 6-digit hex color strings.

## Installation

Download `whitecube-desktop_0.9.4_amd64.deb` and its `.sha256` file from this release,
then run these commands in the download directory:

```sh
sha256sum --check whitecube-desktop_0.9.4_amd64.deb.sha256
sudo apt install ./whitecube-desktop_0.9.4_amd64.deb
```

Save your work, then log out and back in to load the updated compositor and
panel. Installation does not restart the active desktop.

## Verification

Regression tests cover scene-tree cleanup and tab migration across output hotplug
cycles in `hotplug-test`, whitespace/comment config parsing and strict 6-digit hex
validation in `compositor-test` and `status-theme-test`, `tab_h` clamping in
`cube-status --selftest`, 64-bit ALSA dB range validation and empty-WAV rejection
in `test-cube-audio`, transport-bar speed state updates and empty-host URL titles
in `cube-player` (`test_state` and `test_ui`), and session/preference config
handling in `test-preferences.py`.

The release assets include the Debian package, checksums, build/archive manifest,
and verification reports. See [maintenance](MAINTENANCE.md) for the release
verification commands.
