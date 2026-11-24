# Whitecube 0.14.3

Release date: 2026-11-24

## Summary

- Reverted the `0.14.0`–`0.14.2` `whitecube-bus` and D-Bus/portal updates (hard-deny list, `BecomeMonitor` refusal,
  narrowed portal allowlist, service activation, and `/etc/whitecube/bus-activation.conf`) back to the `0.13.0`
  implementation.
- Kept the `whitecube-session` stale bus socket recovery fix (`_is_unix_socket` connection probe and stale socket
  cleanup) and its regression test in `tests/test-session.py`.
- Updated `README.md` and `packaging/VERSION` for `0.14.3`.
