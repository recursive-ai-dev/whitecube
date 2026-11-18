# Whitecube 0.14.1

## whitecube-bus 0.2.1 — Service Activation

- Added optional D-Bus service activation via `StartServiceByName`. Disabled by default.
- New options:
  - `--allow-activation PATTERN` — permit activation for pattern (repeatable; `foo.*` prefix wildcards)
  - `--allow-activation-file FILE` — read activation patterns from file (one per line)
  - `--activation-dir DIR` — read standard `.service` files from directory
- Default config: `/etc/whitecube/bus-activation.conf` (packaged)
- `ListActivatableNames` now returns names from `--activation-dir` filtered by the activation allowlist
- Hard-deny list (screen-capture names) still applies to activation even if allowlisted
- Activation allowlist is independent from the `RequestName` allowlist

## Verification

- Bus suite passes, including `--seccomp=kill`
- Portal suite passes 106/106 against `whitecube-bus`
- All existing tests pass; service activation tested with standard `.service` files and allowlist patterns