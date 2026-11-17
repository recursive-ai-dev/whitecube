# Whitecube 0.14.0

## Privacy

- `whitecube-bus` refuses to register any name that could capture or remote-control the screen, even when
  an `--allow` pattern matches: portal backends (`org.freedesktop.impl.portal.*`), ScreenCast, Screenshot,
  RemoteDesktop, InputCapture, and the GNOME Shell, Mutter, KWin, Spectacle and krfb capture services.
- `BecomeMonitor` is refused unless the bus is started with the new `--allow-monitor` flag (testing only).
  A monitor receives every message on the bus.
- The default allowlist now permits `org.freedesktop.portal.Desktop` instead of `org.freedesktop.portal.*`
  and no longer permits `org.freedesktop.impl.portal.*`. `/etc/whitecube/bus-allow.conf` still extends it.
- `src/bus/tests/verify.sh` covers the hard-deny names and the monitor refusal.

## Reliability

- `whitecube-session` now treats a bus socket as live only if something is listening on it. A crashed bus
  leaves its socket file behind; previously the next session skipped starting `whitecube-bus`, so the portal
  never started and clients got "connection refused". `tests/test-session.py` covers the stale socket.

## Documentation

- README: origin, privacy design, included apps, and a GIF of the cellular-automaton wallpaper.

## Verification

- Bus suite passes, including `--seccomp=kill`; portal suite passes 106/106 against `whitecube-bus`.
- Booted a Debian 13 live image in QEMU, installed the 0.13.0 package with the new bus and portal binaries,
  and confirmed: the session runs on `whitecube-bus`, `Settings.Read` works, Screenshot and ScreenCast answer
  `NotAllowed`, registering `org.freedesktop.impl.portal.*` is denied and `BecomeMonitor` is denied.
- Firefox and Electron were not tested against the narrower allowlist.
