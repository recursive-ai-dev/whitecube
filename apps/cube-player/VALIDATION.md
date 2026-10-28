# Cube Player 1.0.0 validation

Release checks use generated media, isolated settings, and a private Whitecube
compositor. They do not alter the running desktop or the user's media files.

Release run on 2026-10-09: all four Meson suites passed. AddressSanitizer and
UndefinedBehaviorSanitizer passed the component and native playback checks with
the Fontconfig cache suppression below. A separate three-second check in the
running desktop session played the generated video through the normal OpenGL
backend, with volume set to zero, no playback errors, and a clean exit. Its
artifacts are `build/live-session.png`, `build/live-session.ini`, and
`build/live-session.log`.

## Automated coverage

- Settings round trip, safe bounds, private file modes, recent-history limits,
  resume/reset behavior, and history clearing.
- Literal filenames containing spaces, quotes, shell punctuation, and Unicode;
  local file URIs, HTTP(S) URLs, invalid locations, and unsupported URI schemes.
- 48 combinations of panel, viewport size, and interface scaling, from 640 × 400
  to 1920 × 1080, including assertions that controls stay inside the viewport.
- Live Whitecube palette, strong contrast, and interface scale integration.
- M3U/PLS relative paths, numeric PLS ordering, XSPF URI escaping and XML bases,
  XML entities/CDATA, malformed XML and external entities, track/depth limits,
  and recursive-playlist bounds.
- Per-user installation, desktop entry validation, launcher execution, backups,
  rollback, and refusal to overwrite user changes during rollback.
- Real native Wayland window creation, configure acknowledgement, frame
  presentation, and stable app ID.
- OpenGL video pixels, correct vertical orientation, software fallback,
  pause, seek, volume, speed, mute, fullscreen, and keyboard focus.
- Embedded subtitle selection and visible subtitle rendering; audio tracks and
  chapters are populated from actual media metadata.
- Resume, speed, and volume persistence across process restart.
- Audio-only playback, automatic queue advancement, native path entry, direct
  HTTP streaming, invalid-media error handling, multi-file URI drops, and pointer
  control of pause and volume.

The integration artifacts include PNGs, playback reports, and logs under
`build/validation/`. The test compositor may print its existing session-lock
listener assertion during termination; Cube Player itself must exit cleanly.

## Sanitizers

```sh
meson setup build-sanitize -Db_sanitize=address,undefined -Db_lundef=false
meson compile -C build-sanitize
LSAN_OPTIONS=suppressions="$PWD/tests/lsan.supp" \
  meson test -C build-sanitize --no-suite integration --print-errorlogs
CUBE_PLAYER_TEST_BINARY="$PWD/build-sanitize/cube-player" \
  CUBE_PLAYER_TEST_ARTIFACTS="$PWD/build-sanitize/validation" \
  LSAN_OPTIONS=suppressions="$PWD/tests/lsan.supp" \
  python3 tests/test_wayland.py
```

The only leak suppression is Fontconfig's process-wide font cache. Application
allocations are checked. The isolated playback tests disable hardware decoding
and use null audio; they exercise the OpenGL renderer with Mesa's headless
software GL implementation, as well as the mpv software render API.

## Practical limits

The player supports the codecs and protocols available in its mpv build. URLs
must point to media; website extraction and DRM playback are not implemented.
OpenGL uses bilinear scaling for reliable operation on the installed Mesa stack.
Hardware decoding is requested in normal use, with mpv's software decode fallback;
availability depends on the system drivers. The explicit software renderer costs
more CPU, especially at high resolutions.

Window surfaces are bounded to 4096 × 4096 pixels. Fractional-output scaling and
input-method composition protocols are not implemented. File dialogs list at
most 4,096 matching entries; queues hold at most 2,048 items. Files with invalid
UTF-8 paths are not supported. This custom UI supports keyboard navigation and
Whitecube contrast/scaling, but does not expose an AT-SPI accessibility tree.
