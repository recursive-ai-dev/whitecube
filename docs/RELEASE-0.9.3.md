# Whitecube Desktop 0.9.3

Whitecube 0.9.3 hardens the native compositor, settings UI, status bar, wallpaper
supervisor, audio layer, and desktop utilities across 16 reliability, safety,
and edge-case improvements. The supported binary release is Debian 13 (Trixie),
amd64.

## Native desktop and audio hardening

- **Audio resampling and WAV parsing (`cube-audio`)**: High-ratio upsampling in
  `conv_push` is now bounded to `CONV_MAX_FRAMES = 128` (exceeding the maximum
  physical $384\text{ kHz} / 4\text{ kHz} = 96\times$ ratio) with matching
  playback buffer capacities and complete resource cleanup on allocation failure.
  WAV chunk skipping uses 64-bit offsets verified against regular file size via
  `fstat`/`ftello` and `fseeko` so `0xFFFFFFFF` chunk lengths cannot wrap around
  or seek past EOF.
- **Non-blocking settings helpers (`cube-settings`)**: Background helper jobs
  run in isolated process groups (`setsid()`) with a clean signal mask, capped
  `4 MiB` output buffers, safe `snprintf` stdin accumulation, non-blocking
  `waitpid(..., WNOHANG)` reaping, and deadline-based `SIGKILL` timeouts so a
  hung helper that closes stdout early never freezes the Wayland UI loop.
- **HiDPI / rotated initial window geometry (`whitecube-compositor`)**: Initial
  toplevel commits now query `wlr_output_effective_resolution` rather than raw
  mode dimensions so newly opened windows on scaled or $90^\circ / 270^\circ$
  rotated outputs receive logical workspace bounds immediately.
- **Wallpaper child lifecycle and config parsing (`cube-ca`)**: The multi-output
  supervisor terminates per-output wallpaper children on monitor hot-unplug and
  reaps remaining children on exit. Overlong lines in `cosmos_config` are drained
  to newline so long comments cannot forge configuration keys, and `apply_control`
  validates `rotate` direction arguments.
- **State, launcher, and utility hardening**: `desktop-state.h` rejects truncated
  state lines and requires valid JSON token delimiters after numbers and `true`;
  `cube-status` rejects trailing `%` and strips deprecated Desktop Entry field
  codes; `bar-layout.h` and `tab-panel.h` guard against non-positive scales,
  negative gaps, and signed integer overflow; `cube-settings-helper`,
  `whitecube-doctor`, and `whitecube-terminal` harden sysfs/procfs parsing,
  quoted/`env`-prefixed `Exec=` tokenization, and `-e` command option scanning.

## Installation

Download `whitecube-desktop_0.9.3_amd64.deb` and its `.sha256` file from this release,
then run these commands in the download directory:

```sh
sha256sum --check whitecube-desktop_0.9.3_amd64.deb.sha256
sudo apt install ./whitecube-desktop_0.9.3_amd64.deb
```

Save your work, then log out and back in to load the updated compositor and
panel. Installation does not restart the active desktop.

## Verification

Regression tests cover extreme $96\times$ upsampling and `0xFFFFFFFF` WAV chunk
rejection in `test-cube-audio`, non-blocking reaping of hung helpers in
`settings-test`, logical initial geometry on scaled/rotated HiDPI outputs in
`compositor-test`, overlong config line draining in `cube-ca --self-test`,
token boundary checks in `desktop-state-test`, deprecated field-code stripping
in `status-apps-test`, and CLI/doctor parsing in `test-doctor.py` and
`test-desktop-improvements.py`.

The release assets include the Debian package, checksums, build/archive manifest,
and verification reports. See [maintenance](MAINTENANCE.md) for the release
verification commands.
