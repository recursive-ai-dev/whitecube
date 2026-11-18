# Whitecube 0.14.2

## whitecube-bus 0.2.2 — Service Activation Fixed with Seccomp

- Fixed service activation with `--seccomp=kill` mode
- Added required syscalls to seccomp filter: `fork`, `clone`, `execve`, `wait4`
- Added `SIGCHLD` signal handling to reap child processes
- Updated systemd service unit to enable activation by default:
  - `--allow-activation-file /etc/whitecube/bus-activation.conf`
  - `--activation-dir /usr/share/dbus-1/services`

## Default Allowlist Updates

- Added `org.freedesktop.systemd1` to activation allowlist (enables transient scope creation for apps like Firefox)
- Added `org.mozilla.firefox.*` to RequestName allowlist

## Verification

- Bus suite passes, including `--seccomp=kill` with service activation
- Portal suite passes 106/106 against `whitecube-bus`
- Service activation tested with standard `.service` files and allowlist patterns