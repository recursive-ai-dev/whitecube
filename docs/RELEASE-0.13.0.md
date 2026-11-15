# Whitecube Desktop 0.13.0

Release date: 2026-11-15

This release consolidates a full-repository hardening, CLI automation, hardware compatibility, and packaging verification pass across the compositor, native daemons, networking stack, workspace, desktop applications, and release tooling.

## Hardware-agnostic Wi-Fi auto-detection & networking

- **`cube-net` general Wi-Fi hardware auto-detection**: Removed single-dongle hardware assumptions in favor of automatic discovery across any Linux `nl80211`/wireless interface (PCIe, USB, SDIO, platform, or virtual), with optional pinning by `interface`, `usb_id`, `mac`, `driver`, or `bus`.
- **Roaming, lockdown diagnostics, and DNS hooks**: Added same-SSID BSSID roaming support in `cube-net`, structured JSON counters in `whitecube-net-switch`, and hook state inspection in `cube-net-dns`.
- **Generic workspace Wi-Fi UI**: Updated `workspace` and `cube-settings` views to reflect generic multi-adapter Wi-Fi hardware cleanly without vendor-specific wording or broken assumptions.

## Compositor, bar, settings, and audio

- **Window management & status panel**: Added snap, center, and maximize keybindings plus panel scrollbar and digit layout refinements in `whitecube-compositor`, multi-battery aggregation, presets, pulsar controls, and menu shortcuts in `cube-status` / `cube-ca`, and keyboard shortcuts plus multi-battery/backlight filtering in `cube-settings`.
- **Pure-Wayland audio (`cube-audio`)**: Added CLI JSON/info/control flags, 64-bit float WAV support, container TLV parsing, and packaging sync.
- **Session & boot**: Added HiDPI scaling, theme verification, and safe GRUB/Plymouth installer hardening in `boot/`, plus expanded diagnostics (`whitecube-doctor`), session lifecycle hardening, and `whitecube-migrate` improvements across `bin/` and `tools/`.

## Headless CLI, JSON reporting, and I/O hardening across apps

- Added `--cli`, `--check`, `--json`, and `--version` entrypoints and non-blocking regular-file guards (`O_RDONLY | O_NOFOLLOW | O_NONBLOCK`, `S_ISREG`, bounded reads, atomic temporary writes) across:
  - `workspace` (`whitecube-web` / `whitecube-desktop`)
  - `apps/cube-browser`, `apps/cube-player`, `apps/cube-editor`, `apps/cube-train`
  - `apps/cube-strap` (`whitecube-vm`), `apps/chrootables`, `apps/cube-prompts`, `apps/desktop-chat`, `apps/wc-suite` (`--group`), and `apps/cube-apps` (`cube-lock`, `cube-pdf`, `cube-hash`, `cube-rename`)
- Wired read-only subsystem checks for browser, editor, trainer, VM, chrootables, prompts, chat, suite, and path shadowing into `whitecube-doctor`.

## Packaging & migration verification

- **`packaging/check-tree.py` & `verify-deb.py`**: Added `--conffiles` bidirectional stage verification, `--json` reporting, expanded deliverable checks, and ELF `DT_RPATH` / `DT_RUNPATH` hygiene verification.
- **`tools/whitecube-migrate` & developer tools**: Added `--json` / `--version` CLI support, coverage for newer app binaries, `.desktop` files, and `wc-suite` wrappers/completions, plus non-blocking regular-file and sandbox-path validation in rollback.
