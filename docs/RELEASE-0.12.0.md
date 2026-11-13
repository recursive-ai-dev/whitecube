# Whitecube Desktop 0.12.0

Release date: 2026-11-13

## Cube Strap native QEMU/OVMF VMs and `whitecube-vm`

Cube Strap (`cube-strap`) no longer requires an external `whitecube` package to
create, store, or boot Debian VM whitecubes:

- VM builds import raw sparse disk images cleanly into `~/.whitecube/images/<name>/`
  while preserving sparse zero blocks (`SEEK_DATA`/`SEEK_HOLE` + `fallocate --dig-holes`),
  writing `meta.json`, and provisioning per-VM writable UEFI NVRAM (`vars.fd`).
- VM boots launch natively through `qemu-system-x86_64` (`q35`, KVM when `/dev/kvm`
  is accessible, read-only `OVMF_CODE*.fd` + per-VM `vars.fd`, virtio storage/network,
  optional `127.0.0.1:2222 -> 22` SSH forwarding).
- Missing boot runtime dependencies report the real Debian packages
  (`qemu-system-x86`, `ovmf`) only at boot time instead of blocking VM image creation.
- Added `/usr/bin/whitecube-vm` (`list`, `info`, `check`, `import`, `run`, `remove`/`rm`)
  with exclusive `flock` runtime locking (`vm.lock`) to prevent double-boot or deletion
  of running VMs and non-TTY QEMU log capture under `~/.local/state/cube-strap/logs/`.

## Cube Editor, session, and tooling updates

- **Cube Editor (`0.6.0`)**: Added opt-in built-in GTK4/VTE authentication terminal
  (`cube_terminal.py`, `CUBE_EDITOR_TERMINAL=builtin`), explicit `CUBE_EDITOR_TERMINAL`
  selection without silent fallback, unsaved project review/discard guards, and updated
  GUI/terminal/CLI workflow test coverage.
- **Session & settings diagnostics**: Refined `whitecube-session` live D-Bus socket
  handling, `cube-settings-helper` optional tool error messages, and `check-installed-conflicts`
  / `check-tree` / packaging tests (`cube_terminal.py`, `whitecube-vm`).
- **Admin scripts**: Updated `tools/harden-sudo-logging.sh` and `tools/sudo-log-audit.sh`
  to detect `SUDO_USER` dynamically rather than hardcoding a single username.
